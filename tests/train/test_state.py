"""A recipe resumes only committed state and never enters another phase after a stop."""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from etude.carriage import put_back, restore_set
from etude.records import HOURS_SINCE_START, RUNNING_HOURS, Clock
from etude.stopping import Stopped
from etude.train.read import read
from etude.train.state import read_state
from tests.train.tracking import open_run


class Counts(BaseModel):
    games: int = 0


def test_custom_state_and_counts_resume_together_after_relay_without_torch(tmp_path: Path) -> None:
    # The child starts here, so even an optional import that catches ImportError is counted.
    attempts = tmp_path / "torch-imports"
    attempts.touch()
    (tmp_path / "torch.py").write_text(
        "from pathlib import Path\n"
        "with Path('torch-imports').open('a') as log:\n"
        "    log.write('torch\\n')\n"
        "raise ImportError('PyTorch unavailable')\n"
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "from tests.train.test_state import _round_trip_after_relay\n"
            "_round_trip_after_relay(Path.cwd())",
        ],
        cwd=tmp_path,
        check=True,
    )
    assert attempts.read_text().splitlines() == []


def _round_trip_after_relay(tmp_path: Path) -> None:
    weights = b""
    counts = Counts()

    def export(folder: Path) -> None:
        (folder / "model").mkdir()
        (folder / "model/weights").write_bytes(weights)

    def restore(folder: Path) -> None:
        nonlocal weights
        assert counts.games == 6
        weights = (folder / "model/weights").read_bytes()

    with open_run(directory=tmp_path / "first", experiment="e", run="r") as run:
        assert run.resume(export=export, restore=restore, extra=counts, name="learner") is counts
        assert run.resume(export=export, name="rival") is None
        for step in run.steps("updates", to=2, save_every=1):
            counts.games += 3
            weights = bytes([0, step, 255])
            run.save(name="learner")
            weights = b"rival"
            run.save(name="rival")
        saved = read_state(run.out / "state/learner")
        assert saved is not None and saved.record.files == {"model/weights": "model/weights"}

    put_back(tmp_path / "second/e/r", restore_set(tmp_path / "first/e/r"))
    counts, weights = Counts(), b""
    with open_run(directory=tmp_path / "second", experiment="e", run="r", resume=True) as run:
        assert run.resume(export=export, restore=restore, extra=counts, name="learner") is counts
        assert weights == bytes([0, 2, 255])
        path = run.resume(restore=restore, name="rival")
        assert path is not None and path.parent == run.out / "state/rival"
        assert weights == b"rival"
        assert list(run.steps("updates", to=4)) == [3, 4]


def test_per_step_export_and_legacy_unlisted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = 0

    def export(folder: Path) -> None:
        (folder / "value").write_text(str(value))

    def restore(folder: Path) -> None:
        nonlocal value
        value = int((folder / "value").read_text())

    with open_run(directory=tmp_path, experiment="e", run="r") as run:
        counts = run.resume(restore=restore, extra=Counts())
        for step in run.steps("updates", to=2, save_every=2):
            value += 5
            counts.games += 3
            if step == 1:
                run.save(export=export)
            else:
                run.save()
        saved = read_state(run.out / "state")
        assert saved is not None
        # Earlier callback saves kept the files but listed only named PyTorch objects.
        (saved.path / "state.json").write_text(
            saved.record.model_copy(update={"files": {}}).model_dump_json()
        )
    value = 0
    with open_run(directory=tmp_path, experiment="e", run="r", resume=True) as resumed:
        assert resumed.resume(restore=restore, extra=Counts()).games == 6
        assert value == 10
        stop = tmp_path / "stop"
        monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
        steps = resumed.steps("updates", to=4, save_every=1)
        assert next(steps) == 3
        with pytest.raises(ValueError, match="requires an export"):
            resumed.save()
        stop.touch()
        with pytest.raises(ValueError, match="requires an export"):
            next(steps)
        assert resumed.resume(restore=restore) == saved.path


@pytest.mark.parametrize(
    "name", ["state.json", ".cache/file", "weights.open", "weights.partial", "link"]
)
def test_unpublishable_export_preserves_the_committed_save(tmp_path: Path, name: str) -> None:
    def export(folder: Path) -> None:
        target = folder / name
        target.parent.mkdir(exist_ok=True)
        if name == "link":
            target.symlink_to(tmp_path / "missing")
        else:
            target.write_bytes(b"unpublishable")

    with open_run(directory=tmp_path) as run:
        run.resume()
        for step in run.steps("updates", to=2, save_every=1):
            if step == 1:
                run.save()
            else:
                with pytest.raises(ValueError, match="export"):
                    run.save(export=export)
        saved = read_state(run.out / "state")
        assert saved is not None and saved.record.at["updates"] == 1


def test_stop_saves_custom_state_only_after_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    stop.touch()
    value = 0

    def export(folder: Path) -> None:
        (folder / "value").write_text(str(value))

    with open_run(directory=tmp_path, experiment="e", run="r") as run:
        run.resume(export=export)
        with pytest.raises(Stopped):
            list(run.steps("updates", to=2))
        assert read_state(run.out / "state") is None
    stop.unlink()
    with open_run(directory=tmp_path, experiment="e", run="r", resume=True) as run:
        assert run.resume(export=export) is None
        with pytest.raises(Stopped):
            for value in run.steps("updates", to=2):
                stop.touch()
        saved = read_state(run.out / "state")
        assert saved is not None and (saved.path / "value").read_text() == "1"
        assert saved.record.at["updates"] == 1


def test_stop_in_a_new_clock_saves_files_and_counts_only_after_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    value = 10

    def export(folder: Path) -> None:
        (folder / "weights").write_text(str(value))

    def restore(folder: Path) -> None:
        nonlocal value
        value = int((folder / "weights").read_text())

    with open_run(directory=tmp_path, run="r") as run:
        run.resume(export=export, extra=Counts(games=3))
        for _ in run.steps("updates", to=1):
            run.save()

    with open_run(directory=tmp_path, run="r", resume=True) as run:
        original = run.resume(export=export, restore=restore)
        stop.touch()
        with pytest.raises(Stopped):
            list(run.steps("fits", to=2))
        assert original is not None and run.resume() == original

    stop.unlink()
    value = 0
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        counts = run.resume(export=export, restore=restore, extra=Counts())
        with pytest.raises(Stopped):
            for fit in run.steps("fits", to=2):
                value += fit
                counts.games += 3
                stop.touch()

    stop.unlink()
    value = 0
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        assert run.resume(restore=restore, extra=Counts()).games == 6
        assert value == 11
        assert run.at("updates") == 1 and list(run.steps("fits", to=2)) == [2]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("export", "export must be callable"),
        ("restore", "restore must be callable"),
        ("seed", "saved seed"),
        ("manifest", "files"),
        ("missing file", "missing or nonportable file"),
        ("extra", "games"),
    ],
)
def test_failed_registration_preserves_live_state(
    tmp_path: Path, failure: str, message: str
) -> None:
    def export(folder: Path) -> None:
        (folder / "weights").write_bytes(b"original")

    def restore(folder: Path) -> None:
        pytest.fail("restore ran before registration was validated")

    with open_run(directory=tmp_path) as run:
        counts = run.resume(export=export, extra=Counts(), name="learner")
        for step in run.steps("updates", to=2, save_every=1):
            counts.games += 3
            if step == 2:
                saved = read_state(run.out / "state/learner")
                assert saved is not None
                manifest = saved.path / "state.json"
                data = json.loads(manifest.read_bytes())
                if failure == "seed":
                    data["seed"] = run.seed + 1
                elif failure == "extra":
                    data["extra"]["games"] = "invalid"
                elif failure == "manifest":
                    del data["files"]
                elif failure == "missing file":
                    (saved.path / "weights").unlink()
                manifest.write_text(json.dumps(data))
                # Exercise runtime validation for an untyped recipe.
                invalid = cast(Callable[[Path], None], 1)
                with pytest.raises((TypeError, ValueError), match=message):
                    run.resume(
                        name="learner",
                        extra=Counts(),
                        restore=invalid if failure == "restore" else restore,
                        export=invalid if failure == "export" else None,
                    )
            run.save(name="learner")
        latest = read_state(run.out / "state/learner")
        assert latest is not None and latest.record.extra == {"games": 6}
        assert (latest.path / "weights").read_bytes() == b"original"


def test_raising_restore_leaves_first_registration_unavailable(tmp_path: Path) -> None:
    def restore(folder: Path) -> None:
        raise RuntimeError("unreadable weights")

    with open_run(directory=tmp_path, run="r") as run:
        run.resume()
        for _ in run.steps("updates", to=1):
            run.save()
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        with pytest.raises(RuntimeError, match="unreadable weights"):
            run.resume(restore=restore)
        with pytest.raises(ValueError, match="register state"):
            run.save()


@pytest.mark.parametrize("collision", ["model", "model.pt"])
def test_export_collision_preserves_registered_object(tmp_path: Path, collision: str) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    model = torch.nn.Linear(1, 1)

    def export(folder: Path) -> None:
        (folder / collision).write_bytes(b"custom")

    with open_run(directory=tmp_path) as run:
        run.resume(model=model)
        for step in run.steps("updates", to=2, save_every=1):
            if step == 1:
                run.save()
            else:
                with pytest.raises(ValueError, match="export"):
                    run.save(export=export)
        restored = torch.nn.Linear(1, 1)
        path = run.resume(model=restored)
        assert path is not None and torch.equal(model.weight, restored.weight)
        saved = read_state(run.out / "state")
        assert saved is not None and saved.record.at["updates"] == 1


def test_two_models_custom_files_and_extra_resume_from_one_save(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    model, rival = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.25)
    config = Counts(games=3)

    def export(folder: Path) -> None:
        (folder / "custom").write_bytes(b"custom weights")

    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        config=config,
        strict=True,
    ) as run:
        counts = run.resume(
            export=export, model=model, rival=rival, optimizer=optimizer, extra=Counts()
        )
        for value in run.steps("updates", to=2, save_every=1):
            counts.games += value
            run.log({"loss": value})
            run.save()
        expected = model.weight.detach().clone()
        expected_rival = rival.weight.detach().clone()
    model2, rival2 = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer2 = torch.optim.SGD(model2.parameters(), lr=0.01)
    counts = Counts()
    custom = b""

    def restore(folder: Path) -> None:
        nonlocal custom
        assert counts.games == 3
        assert torch.equal(model2.weight, expected)
        custom = (folder / "custom").read_bytes()

    with open_run(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        config=config,
        strict=True,
    ) as resumed:
        restored = resumed.resume(
            restore=restore,
            export=export,
            model=model2,
            rival=rival2,
            optimizer=optimizer2,
            extra=counts,
        )
        assert restored is counts and custom == b"custom weights"
        assert torch.equal(rival2.weight, expected_rival)
        assert optimizer2.param_groups[0]["lr"] == 0.25
        assert list(resumed.steps("updates", to=4, save_every=1)) == [3, 4]
        assert resumed.seed == 0
    assert read(tmp_path, "e", "r").points("loss", "updates").x.tolist() == [1, 2]
    assert json.loads((tmp_path / "e/r/run.json").read_text())["config"] == '{"games": 3}'


def test_callback_save_failure_leaves_the_previous_save_whole(tmp_path: Path) -> None:
    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as run:
        assert run.resume() is None
        for value in run.steps("updates", to=2, save_every=1):

            def export(folder: Path) -> None:
                (folder / "model").write_text(str(value))
                if value == 2:
                    raise ValueError("optimizer failed")
                (folder / "optimizer").write_text(str(value))

            if value == 2:
                with pytest.raises(ValueError, match="optimizer failed"):
                    run.save(export)
            else:
                run.save(export)
        saved = read_state(run.out / "state")
        assert saved is not None
        assert saved.record.at["updates"] == 1
        assert (saved.path / "model").read_text() == (saved.path / "optimizer").read_text() == "1"


def test_stop_saves_the_pass_before_raising_and_never_enters_the_next_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    entered_second = False
    with (
        pytest.raises(Stopped),
        open_run(
            experiment="e",
            run="r",
            directory=tmp_path,
            clocks=[
                Clock(name="updates", kind="step", meaning="updates"),
                Clock(name="fits", kind="step", meaning="sampler fits"),
            ],
            strict=True,
        ) as run,
    ):
        run.resume(name="policy")
        for value in run.steps("updates", to=3):
            stop.touch()

            def export(folder: Path) -> None:
                (folder / "policy").write_text(str(value))

            run.save(export, name="policy")
        entered_second = True
    assert not entered_second
    stop.unlink()
    with open_run(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[
            Clock(name="updates", kind="step", meaning="updates"),
            Clock(name="fits", kind="step", meaning="sampler fits"),
        ],
        strict=True,
    ) as resumed:
        path = resumed.resume(name="policy")
        assert path is not None and (path / "policy").read_text() == "1"
        assert list(resumed.steps("updates", to=3, save_every=1)) == [2, 3]
        assert list(resumed.steps("fits", to=2)) == [1, 2]


def test_running_hours_resume_from_saved_state_instead_of_a_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("etude.train.tracking.time.monotonic", lambda: now)
    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as run:
        run.resume()
        for _ in run.steps("updates", to=1, save_every=1):
            now += 3600
            run.save()
    with open_run(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as resumed:
        resumed.resume()
        assert resumed.at(RUNNING_HOURS) == pytest.approx(1)
        assert resumed.at("updates") == 1


def test_state_registration_refuses_objects_without_a_state_protocol(tmp_path: Path) -> None:
    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
    ) as run:
        with pytest.raises(TypeError, match="state_dict"):
            run.resume(model=object())
        with pytest.raises(ValueError, match="portable"):
            run.resume(name="../outside")


def test_both_hour_clocks_survive_a_relay_without_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now, wall_ms = 100.0, 1_000_000_000_000
    monkeypatch.setattr("etude.train.tracking.time.monotonic", lambda: now)
    monkeypatch.setattr("etude.train.tracking.time.time_ns", lambda: wall_ms * 1_000_000)
    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path / "first",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as run:
        run.resume()
        for _ in run.steps("updates", to=1, save_every=1):
            now += 3600
            wall_ms += 3_600_000
            run.save()
    put_back(tmp_path / "second/e/r", restore_set(tmp_path / "first/e/r"))
    now += 1800
    wall_ms += 1_800_000
    with open_run(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path / "second",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as resumed:
        resumed.resume()
        assert resumed.at(RUNNING_HOURS) == pytest.approx(1)
        assert resumed.at(HOURS_SINCE_START) == pytest.approx(1.5)
