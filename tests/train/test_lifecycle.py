"""A recipe is called again with its saved state and without trimming un-replaced rows."""

import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from etude.carriage import put_back, restore_set
from etude.experiment import observe
from etude.out import OutFile, OutFiles
from etude.storage import DirectoryStorage
from etude.tracking.delivery import sync
from etude.train.read import read
from etude.train.records import Step, Time
from etude.train.report import training
from etude.train.run import Stopped, local_launch, track
from tests.experiments import authored
from tests.run.storage import MeasuredStorage

CLOCKS = [Step("updates", "optimizer updates")]


@pytest.mark.parametrize("target", [None, "0", "6", "6.5"])
def test_recipe_uses_launch_target_for_its_clock_or_its_local_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str | None
) -> None:
    monkeypatch.delenv("ETUDE_UNTIL_CLOCK", raising=False)
    monkeypatch.delenv("ETUDE_UNTIL_VALUE", raising=False)
    if target is not None:
        monkeypatch.setenv("ETUDE_UNTIL_CLOCK", "seconds")
        monkeypatch.setenv("ETUDE_UNTIL_VALUE", target)
    with local_launch(out=tmp_path / "run", experiment="recipe"):
        with track(clocks=[*CLOCKS, Time("seconds", "active time")]) as run:
            assert run.until("seconds", default=2) == (2 if target is None else float(target))
            assert run.until("updates", default=3) == 3
            with pytest.raises(ValueError, match="undeclared clock"):
                run.until("typo", default=2)


@pytest.mark.parametrize(
    ("clock", "value"),
    [(None, "4"), ("updates", None), ("updates", "nan"), ("updates", "inf"), ("updates", "-1")],
)
def test_recipe_refuses_incomplete_or_invalid_launch_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: str | None, value: str | None
) -> None:
    for name, given in (("ETUDE_UNTIL_CLOCK", clock), ("ETUDE_UNTIL_VALUE", value)):
        if given is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, given)
    with pytest.raises(ValueError, match="launch target"):
        track(experiment="recipe", clocks=CLOCKS, directory=tmp_path)


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
    with track(resume=True, experiment="e", run="r", clocks=CLOCKS, directory=tmp_path) as run:
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
    assert authored(data.attempts[-1].resumed_at) == ({} if logged == "none" else {"updates": 4})


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
    assert storage.uploads == first + 2
    assert storage.storage.get("r/out/state") == b"step two"
    restored = tmp_path / "fresh/r"
    put_back(restored, restore_set(root))
    assert (restored / "out/state").read_bytes() == b"step two"
    monkeypatch.setenv("ETUDE_OUT", str(restored / "out"))
    with track(
        resume=True, experiment="e", run="r", clocks=CLOCKS, directory=restored.parent
    ) as resumed:
        latest = resumed.latest_checkpoint()
        assert latest is not None
        assert latest.artifact.uri == (restored / "out/nested/model.pt").as_uri()
        assert resumed.checkpoint(resumed.out / "nested/model.pt", updates=1).sha256 == ref.sha256
    assert len(read(restored.parent, "e", "r").checkpoints) == 1
    saved = observe(restored.parent, "e").runs[0].checkpoints[0]
    assert saved.file == "out/nested/model.pt"
    assert str(saved.key) == "r/out/nested/model.pt"


def test_sync_carries_out_file_deletion_and_recreation_without_duplicate_bytes(
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
    assert OutFiles.model_validate_json(storage.get("r/out.json")).files == ()
    state.write_bytes(b"saved")
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.uploads == count + 2
    assert OutFiles.model_validate_json(storage.get("r/out.json")).files == (
        OutFile(key="out/state", sha256=sha256(b"saved").hexdigest(), size_bytes=5),
    )
    assert storage.get("r/out/state") == b"saved"


def test_sync_membership_describes_the_save_uploaded_during_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "track/r"
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(experiment="e", run="r", clocks=CLOCKS, directory=root.parent) as run:
        state = run.out / "state"
        state.write_bytes(b"first")
    storage = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    put_file = storage.put_file

    def replace_during_upload(key: str, path: Path) -> None:
        if key == "r/out/state":
            partial = state.with_suffix(".partial")
            partial.write_bytes(b"second save")
            partial.replace(state)
        put_file(key, path)

    monkeypatch.setattr(storage, "put_file", replace_during_upload)
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.get("r/out/state") == b"first"
    assert OutFiles.model_validate_json(storage.get("r/out.json")).files == (
        OutFile(key="out/state", sha256=sha256(b"first").hexdigest(), size_bytes=5),
    )
    first = storage.uploads
    monkeypatch.setattr(storage, "put_file", put_file)
    assert sync(root.parent, storages=[storage]) == {}
    assert storage.uploads == first + 2  # the new save, then its membership
    assert storage.get("r/out/state") == b"second save"
    assert OutFiles.model_validate_json(storage.get("r/out.json")).files == (
        OutFile(key="out/state", sha256=sha256(b"second save").hexdigest(), size_bytes=11),
    )
    assert not list(root.glob(".out-*"))


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
    assert OutFiles.model_validate_json(storage.get("r/out.json")).files == (
        OutFile(key="out/recipe.removed", sha256=sha256(b"run.json\n").hexdigest(), size_bytes=9),
    )


def test_tracking_contexts_have_distinct_writers_under_one_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_ATTEMPT", "allocation-1")
    for value in (1, 2):
        with track(
            experiment="e",
            run="allocated",
            directory=tmp_path,
            clocks=[Step("updates", "updates")],
        ) as run:
            run.log({"loss": value}, updates=value)
    data = read(tmp_path, "e", "allocated")
    assert len({writer.writer for writer in data.attempts}) == 2
    assert [writer.attempt for writer in data.attempts] == ["allocation-1", "allocation-1"]
    assert data.points("loss", "updates").y.tolist() == [1, 2]
    with track(
        experiment="e",
        run="another",
        directory=tmp_path,
        clocks=[Step("updates", "updates")],
    ) as run:
        run.log({"loss": 3}, updates=1)
    other = read(tmp_path, "e", "another")
    assert other.attempts[0].attempt == "allocation-1"
    assert other.points("loss", "updates").y.tolist() == [3]
