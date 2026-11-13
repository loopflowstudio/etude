"""A recipe is called again with its saved state and without trimming un-replaced rows."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from etude.carriage import put_back, restore_set
from etude.experiment import checkpoint_file, observe
from etude.run.delivery import sync
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage
from etude.train.read import read
from etude.train.records import Step
from etude.train.report import training
from etude.train.run import Stopped, local_launch, track
from etude.working import WorkingFiles
from tests.run.storage import MeasuredStorage

CLOCKS = [Step("updates", "optimizer updates")]


def test_local_recipe_stops_resumes_and_repeats_without_another_checkpoint(tmp_path: Path) -> None:
    out = tmp_path / "run"
    before = dict(os.environ)
    with pytest.raises(Stopped), local_launch(out=out, experiment="recipe"):
        with track(clocks=CLOCKS, strict=True) as run:
            assert run.out == out
            (run.out / "state").write_text("3")
            run.log({"loss": 3}, updates=3)
            raise Stopped
    assert dict(os.environ) == before
    stopped = read(out, "recipe", "run")
    assert stopped.state is not None and stopped.state.state == "stopped"
    assert training([stopped]).runs[0].state == "running or unfinished"
    for _ in range(2):
        with local_launch(out=out, experiment="recipe"):
            with track(clocks=CLOCKS, strict=True) as run:
                assert (run.out / "state").read_text() == "3"
                model = run.out / "model.pt"
                model.write_bytes(b"weights")
                run.checkpoint(model, updates=4)
    data = read(out, "recipe", "run")
    assert len(data.checkpoints) == 1
    assert data.points("loss", "updates").x.tolist() == [3]
    assert data.state is not None and data.state.state == "finished"
    assert not list((out / "etude/run/checkpoints").glob("*.pt"))
    with local_launch(out=tmp_path / "other/run", experiment="recipe"):
        with track(clocks=CLOCKS) as other:
            assert other.latest_checkpoint() is None
    with track(experiment="recipe", clocks=CLOCKS, directory=tmp_path / "plain") as plain:
        with pytest.raises(EtudeError, match="run_directory_not_named"):
            _ = plain.out


def test_stopping_reserves_the_slowest_step_and_remembers_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    # Monotonic time is shared with writer threads; the fixture only advances it.
    monkeypatch.setattr("etude.train.run.time.monotonic", lambda: now[0])
    monkeypatch.setattr("etude.train.run.time.time", lambda: 1000 + now[0])
    monkeypatch.setenv("ETUDE_DEADLINE", datetime.fromtimestamp(1010, UTC).isoformat())
    stop = tmp_path / "stop"
    monkeypatch.setenv("ETUDE_STOP_FILE", str(stop))
    with local_launch(out=tmp_path / "run", experiment="recipe"):
        with track(clocks=CLOCKS) as run:
            assert not run.stopping()
            now[0] = 4
            assert not run.stopping()
            now[0] = 7
            assert run.stopping()  # three seconds left; slowest update took four
            monkeypatch.delenv("ETUDE_DEADLINE")
            assert run.stopping()
        with track(clocks=CLOCKS) as run:
            assert not run.stopping()
            stop.touch()
            assert run.stopping()
            stop.unlink()
            assert run.stopping()


@pytest.mark.parametrize("logged", ["none", "other", "loss"])
def test_first_row_replaces_history_and_an_empty_attempt_changes_nothing(
    tmp_path: Path, logged: str
) -> None:
    with track(experiment="e", run="r", clocks=CLOCKS, directory=tmp_path) as run:
        for update in range(6):
            run.log({"loss": update}, updates=update)
        model = tmp_path / "model"
        model.write_bytes(b"model at 1")
        run.checkpoint(model, updates=1)
    with track(experiment="e", run="r", clocks=CLOCKS, directory=tmp_path) as run:
        if logged != "none":
            run.log({logged: 100}, updates=4)
    data = read(tmp_path, "e", "r")
    expected = (
        list(range(6))
        if logged == "none"
        else [*range(4), 100]
        if logged == "loss"
        else list(range(4))
    )
    assert data.points("loss", "updates").y.tolist() == expected
    assert data.attempts[-1].resumed_at == ({} if logged == "none" else {"updates": 4})


def test_out_checkpoint_is_carried_once_and_relocated_without_a_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "track/r"
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(experiment="e", run="r", clocks=CLOCKS, directory=root.parent, strict=True) as run:
        (run.out / "nested").mkdir()
        model = run.out / "nested/model.pt"
        model.write_bytes(b"weights")
        ref = run.checkpoint(model, updates=1)
        (run.out / "state").write_bytes(b"step one")
    storage = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    assert sync(root.parent, storages=[storage]) == {}
    first = storage.uploads
    assert sync(root.parent, storages=[storage]) == {} and storage.uploads == first
    state = root / "out/state"
    stamp = state.stat()
    state.write_bytes(b"step two")
    os.utime(state, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.uploads == first + 1
    assert storage.storage.get("r/out/state") == b"step two"
    restored = tmp_path / "fresh/r"
    put_back(restored, restore_set(root))
    assert (restored / "out/state").read_bytes() == b"step two"
    monkeypatch.setenv("ETUDE_OUT", str(restored / "out"))
    with track(experiment="e", run="r", clocks=CLOCKS, directory=restored.parent) as resumed:
        latest = resumed.latest_checkpoint()
        assert latest is not None
        assert latest.artifact.uri == (restored / "out/nested/model.pt").as_uri()
        assert resumed.checkpoint(resumed.out / "nested/model.pt", updates=1).sha256 == ref.sha256
    assert len(read(restored.parent, "e", "r").checkpoints) == 1
    saved = observe(restored.parent, "e").runs[0].checkpoints[0]
    assert checkpoint_file(saved) == "out/nested/model.pt"
    assert str(saved.key) == "r/out/nested/model.pt"


def test_sync_carries_working_file_deletion_and_recreation_without_duplicate_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "track/r"
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(experiment="e", run="r", clocks=CLOCKS, directory=root.parent) as run:
        state = run.out / "state"
        state.write_bytes(b"saved")
    storage = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    assert sync(root.parent, storages=[storage]) == {}
    count = storage.uploads
    state.unlink()
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.uploads == count + 1
    assert WorkingFiles.model_validate_json(storage.get("r/out.json")).files == ()
    state.write_bytes(b"saved")
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.uploads == count + 2
    assert WorkingFiles.model_validate_json(storage.get("r/out.json")).files == ("out/state",)
    assert storage.get("r/out/state") == b"saved"


def test_recipe_retirement_notes_cannot_delete_tracking_or_retained_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "track/r"
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(experiment="e", run="r", clocks=CLOCKS, directory=root.parent) as run:
        model = run.out / "model.pt"
        model.write_bytes(b"weights")
        run.checkpoint(model, updates=1)
    storage = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    assert sync(root.parent, storages=[storage]) == {}
    (root / "out/recipe.removed").write_text("run.json\n")
    (root / "metrics/retired.removed").write_text("out/model.pt\n")
    model.unlink()
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.get("r/run.json") == (root / "run.json").read_bytes()
    assert storage.get("r/out/model.pt") == b"weights"
    assert WorkingFiles.model_validate_json(storage.get("r/out.json")).files == (
        "out/recipe.removed",
    )
