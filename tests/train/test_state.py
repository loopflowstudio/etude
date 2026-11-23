"""A recipe resumes only committed state and never enters another phase after a stop."""

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass
class Weights:
    value: int = 0

    def export(self, folder: Path) -> None:
        (folder / "weights").write_text(str(self.value))

    def restore(self, folder: Path) -> None:
        self.value = int((folder / "weights").read_text())


def test_custom_state_and_counts_resume_together_after_relay_without_torch(tmp_path: Path) -> None:
    # The child starts here, so even an optional import that catches ImportError is counted.
    attempts = tmp_path / "torch-imports"
    (tmp_path / "torch.py").write_text(
        "from pathlib import Path\n"
        "Path('torch-imports').touch()\n"
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
    assert not attempts.exists()


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
        assert run.resume(export=export, restore=restore, name="rival") is None
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
        path = run.resume(export=export, restore=restore, name="rival")
        assert path is not None and path.parent == run.out / "state/rival"
        assert weights == b"rival"
        assert list(run.steps("updates", to=4)) == [3, 4]


def test_callbacks_restore_legacy_unlisted_files(tmp_path: Path) -> None:
    weights = Weights(10)
    with open_run(directory=tmp_path, experiment="e", run="r") as run:
        counts = run.resume(export=weights.export, restore=weights.restore, extra=Counts())
        for _ in run.steps("updates", to=1):
            counts.games = 6
            run.save()
        saved = read_state(run.out / "state")
        assert saved is not None
        # Earlier callback saves kept the files but listed only named PyTorch objects.
        (saved.path / "state.json").write_text(
            saved.record.model_copy(update={"files": {}}).model_dump_json()
        )
    weights = Weights()
    with open_run(directory=tmp_path, experiment="e", run="r", resume=True) as resumed:
        counts = resumed.resume(export=weights.export, restore=weights.restore, extra=Counts())
        assert counts.games == 6 and weights.value == 10


def test_custom_files_with_counts_require_callbacks(tmp_path: Path) -> None:
    weights = Weights(10)
    with open_run(directory=tmp_path) as run:
        run.resume(extra=Counts())
        with pytest.raises(ValueError, match="register export and restore"):
            run.save(export=weights.export)
        counts = run.resume(export=weights.export, restore=weights.restore, extra=Counts())
        for update in run.steps("updates", to=1):
            counts.games = update
            run.save()
        fresh = Counts()
        with pytest.raises(ValueError, match="saved state includes custom files"):
            run.resume(extra=fresh)
        assert fresh.games == 0
        path = run.resume()
        assert path is not None and (path / "weights").read_text() == "10"


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


@pytest.mark.parametrize("prior_phase", [False, True], ids=["fresh", "new-clock"])
def test_stop_saves_files_and_counts_only_after_a_clock_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_phase: bool
) -> None:
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    weights = Weights()
    original = None
    if prior_phase:
        with open_run(directory=tmp_path, run="r") as run:
            counts = run.resume(export=weights.export, restore=weights.restore, extra=Counts())
            for update in run.steps("updates", to=1):
                weights.value = update
                counts.games += 3
                run.save()
            original = read_state(run.out / "state")
            assert original is not None

    stop.touch()
    with open_run(directory=tmp_path, run="r", resume=prior_phase) as run:
        run.resume(export=weights.export, restore=weights.restore, extra=Counts())
        with pytest.raises(Stopped):
            list(run.steps("fits", to=2))
        assert read_state(run.out / "state") == original

    stop.unlink()
    weights = Weights()
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        counts = run.resume(export=weights.export, restore=weights.restore, extra=Counts())
        with pytest.raises(Stopped):
            for fit in run.steps("fits", to=2):
                weights.value += fit
                counts.games += 3
                stop.touch()

    stop.unlink()
    weights = Weights()
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        counts = run.resume(export=weights.export, restore=weights.restore, extra=Counts())
        assert weights.value == (2 if prior_phase else 1)
        assert counts.games == weights.value * 3
        if prior_phase:
            assert run.at("updates") == 1
        assert list(run.steps("fits", to=2)) == [2]


@pytest.mark.parametrize(
    ("invalid_callback", "manifest_fields", "message"),
    [
        ("export", {}, "export must be callable"),
        ("missing_export", {}, "register export and restore together"),
        ("missing_restore", {}, "register export and restore together"),
        ("restore", {}, "restore must be callable"),
        (None, {"seed": -1}, "saved seed"),
        (None, {"files": None}, "files"),
        (None, {"files": {"weights": "missing"}}, "missing or nonportable file"),
        (None, {"extra": {"games": "invalid"}}, "games"),
    ],
)
def test_failed_registration_preserves_live_state(
    tmp_path: Path, invalid_callback: str | None, manifest_fields: dict[str, object], message: str
) -> None:
    weights = Weights(42)

    def restore(folder: Path) -> None:
        pytest.fail("restore ran before registration was validated")

    with open_run(directory=tmp_path) as run:
        counts = run.resume(
            export=weights.export, restore=weights.restore, extra=Counts(), name="learner"
        )
        steps = run.steps("updates", to=2, save_every=1)
        counts.games = next(steps)
        run.save(name="learner")
        folder = run.out / "state/learner"
        saved = read_state(folder)
        assert saved is not None
        if manifest_fields:
            manifest = saved.path / "state.json"
            manifest.write_text(json.dumps(saved.record.model_dump() | manifest_fields))

        # Untyped recipes can pass a non-callable in either callback slot.
        invalid = cast(Callable[[Path], None], 1)
        export_callback = invalid if invalid_callback == "export" else weights.export
        restore_callback = invalid if invalid_callback == "restore" else restore
        error = TypeError if invalid_callback in ("export", "restore") else ValueError
        with pytest.raises(error, match=message):
            run.resume(
                name="learner",
                extra=Counts(),
                restore=None if invalid_callback == "missing_restore" else restore_callback,
                export=None if invalid_callback == "missing_export" else export_callback,
            )

        counts.games = next(steps)
        weights.value += 1
        run.save(name="learner")
        latest = read_state(folder)
        assert latest is not None and latest.record.extra == {"games": 2}
        assert (latest.path / "weights").read_text() == "43"


def test_raising_restore_leaves_first_registration_unavailable(tmp_path: Path) -> None:
    def restore(folder: Path) -> None:
        raise RuntimeError("unreadable weights")

    with open_run(directory=tmp_path, run="r") as run:
        run.resume()
        for _ in run.steps("updates", to=1):
            run.save()
    with open_run(directory=tmp_path, run="r", resume=True) as run:
        with pytest.raises(RuntimeError, match="unreadable weights"):
            run.resume(export=Weights().export, restore=restore)
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


def test_custom_file_named_for_an_object_is_never_loaded_as_one(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    with open_run(directory=tmp_path) as run:
        run.resume()
        for _ in run.steps("updates", to=1):
            run.save(export=Weights().export)
        with pytest.raises(ValueError, match="saved state has no object 'weights'"):
            run.resume(weights=torch.nn.Linear(1, 1))


def test_partial_object_restore_with_counts(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    model = torch.nn.Linear(1, 1)
    with open_run(directory=tmp_path) as run:
        counts = run.resume(model=model, rival=torch.nn.Linear(1, 1), extra=Counts())
        for _ in run.steps("updates", to=1):
            counts.games = 7
            run.save()
        restored = torch.nn.Linear(1, 1)
        assert run.resume(model=restored, extra=Counts()).games == 7
        assert torch.equal(restored.weight, model.weight)


def test_two_models_custom_files_and_extra_resume_from_one_save(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    model, rival = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.25)
    config = Counts(games=3)
    counts = Counts()
    custom = b"custom weights"
    expected = model.weight.detach().clone()

    def export(folder: Path) -> None:
        (folder / "custom").write_bytes(custom)

    def restore(folder: Path) -> None:
        nonlocal custom
        assert counts.games == 3
        assert torch.equal(model.weight, expected)
        custom = (folder / "custom").read_bytes()

    with open_run(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        config=config,
        strict=True,
    ) as run:
        run.resume(
            export=export,
            restore=restore,
            model=model,
            rival=rival,
            optimizer=optimizer,
            extra=counts,
        )
        for value in run.steps("updates", to=2, save_every=1):
            counts.games += value
            run.log({"loss": value})
            run.save()
        expected_rival = rival.weight.detach().clone()
    model, rival = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    counts = Counts()
    custom = b""
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
            model=model,
            rival=rival,
            optimizer=optimizer,
            extra=counts,
        )
        assert restored is counts and custom == b"custom weights"
        assert torch.equal(rival.weight, expected_rival)
        assert optimizer.param_groups[0]["lr"] == 0.25
        assert list(resumed.steps("updates", to=4, save_every=1)) == [3, 4]
        assert resumed.seed == 0
    assert read(tmp_path, "e", "r").points("loss", "updates").x.tolist() == [1, 2]
    assert json.loads((tmp_path / "e/r/run.json").read_text())["config"] == '{"games": 3}'


@pytest.mark.parametrize("planned_stop", [False, True], ids=["save", "planned-stop"])
def test_failed_export_preserves_save_and_previous_exporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planned_stop: bool
) -> None:
    weights = Weights()
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))

    def broken(folder: Path) -> None:
        (folder / "weights").write_text("incomplete")
        raise ValueError("optimizer failed")

    with open_run(directory=tmp_path) as run:
        assert run.resume() is None
        steps = run.steps("updates", to=4, save_every=1)
        weights.value = next(steps)
        run.save(export=weights.export)
        weights.value = next(steps)
        with pytest.raises(ValueError, match="optimizer failed"):
            run.save(export=broken)
        saved = read_state(run.out / "state")
        assert saved is not None and saved.record.at["updates"] == 1
        assert (saved.path / "weights").read_text() == "1"

        if planned_stop:
            stop.touch()
            with pytest.raises(Stopped):
                next(steps)
        else:
            run.save()
        saved = read_state(run.out / "state")
        assert saved is not None and saved.record.at["updates"] == 2
        assert (saved.path / "weights").read_text() == "2"


def test_deferred_export_is_available_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    weights = Weights()
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    with open_run(directory=tmp_path) as run:
        run.resume()
        steps = run.steps("updates", to=3, save_every=3)
        weights.value = next(steps)
        run.save(export=weights.export)
        # An untyped recipe gets immediate feedback even between saves.
        with pytest.raises(TypeError, match="export must be callable"):
            run.save(export=cast(Callable[[Path], None], 1))
        assert read_state(run.out / "state") is None
        stop.touch()
        with pytest.raises(Stopped):
            next(steps)
        path = run.resume()
        assert path is not None and (path / "weights").read_text() == "1"


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
