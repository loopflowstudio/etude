"""A recipe resumes only committed state and never enters another phase after a stop."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.carriage import put_back, restore_set
from etude.records import HOURS_SINCE_START, RUNNING_HOURS
from etude.stopping import Stopped
from etude.train.read import read
from etude.train.records import Step
from etude.train.state import read_state
from etude.train.tracking import track


class Counts(BaseModel):
    games: int = 0


def test_two_models_and_extra_resume_from_one_save(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")  # optional training dependency
    model, rival = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.25)
    config = Counts(games=3)
    with track(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
        config=config,
        strict=True,
    ) as run:
        counts = run.resume(model=model, rival=rival, optimizer=optimizer, extra=Counts())
        for value in run.steps("updates", to=2, save_every=1):
            counts.games += value
            run.log({"loss": value})
            run.save()
        expected = model.weight.detach().clone()
        expected_rival = rival.weight.detach().clone()
    model2, rival2 = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)
    optimizer2 = torch.optim.SGD(model2.parameters(), lr=0.01)
    with track(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
        config=config,
        strict=True,
    ) as resumed:
        restored = resumed.resume(model=model2, rival=rival2, optimizer=optimizer2, extra=Counts())
        assert restored.games == 3
        assert torch.equal(model2.weight, expected)
        assert torch.equal(rival2.weight, expected_rival)
        assert optimizer2.param_groups[0]["lr"] == 0.25
        assert list(resumed.steps("updates", to=4, save_every=1)) == [3, 4]
        assert resumed.seed == 0
    assert read(tmp_path, "e", "r").points("loss", "updates").x.tolist() == [1, 2]
    assert json.loads((tmp_path / "e/r/run.json").read_text())["config"] == '{"games": 3}'


def test_callback_save_failure_leaves_the_previous_save_whole(tmp_path: Path) -> None:
    with track(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
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
        track(
            experiment="e",
            run="r",
            directory=tmp_path,
            clocks=[Step("updates", "updates"), Step("fits", "sampler fits")],
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
    with track(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates"), Step("fits", "sampler fits")],
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
    with track(
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
        strict=True,
    ) as run:
        run.resume()
        for _ in run.steps("updates", to=1, save_every=1):
            now += 3600
            run.save()
    with track(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
        strict=True,
    ) as resumed:
        resumed.resume()
        assert resumed.at(RUNNING_HOURS) == pytest.approx(1)
        assert resumed.at("updates") == 1


def test_state_registration_refuses_objects_without_a_state_protocol(tmp_path: Path) -> None:
    with track(
        experiment="e", run="r", directory=tmp_path, clocks=[Step("updates", "updates")]
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
    with track(
        experiment="e",
        run="r",
        directory=tmp_path / "first",
        clocks=[Step("updates", "updates")],
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
    with track(
        resume=True,
        experiment="e",
        run="r",
        directory=tmp_path / "second",
        clocks=[Step("updates", "updates")],
        strict=True,
    ) as resumed:
        resumed.resume()
        assert resumed.at(RUNNING_HOURS) == pytest.approx(1)
        assert resumed.at(HOURS_SINCE_START) == pytest.approx(1.5)
