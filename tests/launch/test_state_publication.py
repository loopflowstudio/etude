"""Whole-save delivery and recovery, with bounded files and counted bucket writes."""

import json
from collections import Counter
from pathlib import Path
from typing import IO

import pytest

from etude.carriage import listing, put_back, restore_set
from etude.launch.records import Storage as Place
from etude.launch.schemas import Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys
from etude.out import MEMBERSHIP
from etude.storage_timeout import StorageUnavailable
from etude.tracking.delivery import RunFiles, StorageDestination, prune
from etude.tracking.storage import DirectoryStorage
from etude.train.records import Step
from etude.train.run import track
from tests.launch.test_out import Writes
from tests.launch.test_runpod_machines import machine
from tests.run.storage import MeasuredStorage


def _save(root: Path, number: int, *, name: str | None = None) -> Path:
    state = root / "out/state"
    if name is not None:
        state /= name
    folder = state / f"save-{number:08d}"
    folder.mkdir(parents=True)
    (folder / "model").write_bytes(b"unchanged model")
    (folder / "optimizer").write_text(str(number))
    (folder / "state.json").write_text(json.dumps({"at": {"updates": number}}))
    partial = state / "current.json.partial"
    partial.write_text(json.dumps({"save": folder.name}))
    partial.replace(state / "current.json")
    return folder


def _journal(storage: Writes) -> BucketJournal:
    return BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
    )


@pytest.mark.parametrize("name", [None, "policy"])
def test_only_the_selected_save_travels_and_its_pointer_is_last(
    tmp_path: Path, name: str | None
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    first = _save(root, 1, name=name)
    second = _save(root, 2, name=name)
    unfinished = second.parent / "save-00000003"
    unfinished.mkdir()
    (unfinished / "model").write_bytes(b"partial")
    courier._track("run1", done=False)
    sent = [key for key in storage.writes if "/out/state/" in key]
    prefix = "etude/run1/" + second.relative_to(root).as_posix()
    pointer = "etude/run1/" + (second.parent / "current.json").relative_to(root).as_posix()
    assert set(sent) == {prefix + "/model", prefix + "/optimizer", prefix + "/state.json", pointer}
    assert sent.index(pointer) > max(
        sent.index(prefix + "/" + file) for file in ("model", "optimizer", "state.json")
    )
    assert first.exists()  # one delivery pass of grace after acknowledgement
    assert unfinished.exists()
    courier._track("run1", done=False)
    assert not first.exists()
    assert all(count == 1 for key, count in storage.writes.items() if key in sent)


@pytest.mark.parametrize("interruption", ["model", "pointer", "membership"])
def test_interrupted_delivery_recovers_one_whole_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path / "old", storage)
    _save(root, 1)
    courier._track("run1", done=False)
    second = _save(root, 2)
    folder = courier._runs["run1"]
    write = folder.write

    def interrupt(key: str, stream: IO[bytes]) -> None:
        if (
            (interruption == "model" and key.endswith("save-00000002/optimizer"))
            or (interruption == "pointer" and key.endswith("current.json"))
            or (interruption == "membership" and key == MEMBERSHIP)
        ):
            raise StorageUnavailable("delivery interrupted")
        write(key, stream)

    monkeypatch.setattr(folder, "write", interrupt)
    with pytest.raises(StorageUnavailable, match="interrupted"):
        courier._track("run1", done=False)
    held = _journal(storage).held("run1")
    pointer = json.loads(held["out/state/current.json"])["save"]
    expected = 2 if interruption == "membership" else 1
    assert pointer == f"save-{expected:08d}"
    assert held[f"out/state/{pointer}/optimizer"] == str(expected).encode()
    assert not any(f"save-{3 - expected:08d}" in key for key in held)
    work = Work(
        attempt="a2",
        command="true",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
        run="run1",
        resumes=True,
    )
    fresh, _, restored = machine(tmp_path / "fresh", storage)
    links = _journal(storage)._restore([work])
    fresh.restore(links, [work])
    assert (restored / f"out/state/{pointer}/optimizer").read_bytes() == str(expected).encode()
    assert not any(
        path.name == f"save-{3 - expected:08d}" for path in (restored / "out/state").iterdir()
    )
    assert second.exists()  # failed publication retains local recovery state
    monkeypatch.setattr(folder, "write", write)
    courier._track("run1", done=False)
    assert (
        json.loads(_journal(storage).held("run1")["out/state/current.json"])["save"] == second.name
    )
    assert storage.writes["etude/run1/out/state/save-00000002/model"] == 1


def test_a_pointer_changed_during_upload_waits_for_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    first = _save(root, 1)
    folder = courier._runs["run1"]
    write = folder.write
    changed = False

    def advance(key: str, stream: IO[bytes]) -> None:
        nonlocal changed
        write(key, stream)
        if not changed and key.endswith("/model"):
            changed = True
            _save(root, 2)

    monkeypatch.setattr(folder, "write", advance)
    courier._track("run1", done=False)
    held = _journal(storage).held("run1")
    assert json.loads(held["out/state/current.json"])["save"] == first.name
    assert held[f"out/state/{first.name}/optimizer"] == b"1"
    assert first.exists()
    courier._track("run1", done=False)
    assert first.exists()
    courier._track("run1", done=False)
    assert not first.exists()
    assert storage.writes["etude/run1/out/state/save-00000001/model"] == 1
    assert storage.writes["etude/run1/out/state/save-00000002/model"] == 1


def test_directory_delivery_and_host_relay_select_the_same_save(tmp_path: Path) -> None:
    root = tmp_path / "source/run1"
    first = _save(root, 1)
    storage = MeasuredStorage(DirectoryStorage(tmp_path / "dest"))
    destination = StorageDestination(storage)
    files = RunFiles(root)
    destination.ship(files, "run1")
    second = _save(root, 2)
    _save(root, 3)
    destination.ship(files, "run1")
    prune(files, [destination])
    destination.ship(files, "run1")
    prune(files, [destination])
    assert storage.uploads == 10  # three save files, pointer and membership per pass
    assert not first.exists() and not second.exists()
    assert not any("save-00000002" in key for key in storage.keys("run1/"))
    target = tmp_path / "relay"
    put_back(target, restore_set(root))
    assert (target / "out/state/save-00000003/optimizer").read_bytes() == b"3"
    assert "out/state/current.json" in listing(root, tail_seconds=0)
    assert not any("save-00000001" in key for key in storage.keys("run1/"))


@pytest.mark.parametrize("value", ["../escape", "save-00000001/../../escape", "arbitrary"])
def test_invalid_pointer_never_selects_or_deletes_files(tmp_path: Path, value: str) -> None:
    folder = _save(tmp_path, 1)
    (folder.parent / "current.json").write_text(json.dumps({"save": value}))
    with pytest.raises(ValueError, match="numbered save"):
        listing(tmp_path, tail_seconds=0)
    assert folder.exists()


def test_controller_retires_remote_saves_under_an_upload_only_grant(tmp_path: Path) -> None:
    storage = Writes()
    storage.grants = False
    courier, _, root = machine(tmp_path, storage)
    first = _save(root, 1)
    courier._track("run1", done=False)
    second = _save(root, 2)
    courier._track("run1", done=False)
    old = "etude/run1/out/state/" + first.name + "/model"
    current = "etude/run1/out/state/" + second.name + "/model"
    assert storage.objects[old] == storage.objects[current] == b"unchanged model"
    newer = "etude/run1/out/state/save-00000003/model"
    storage.objects[newer] = b"in flight"
    unrelated = "etude/run1/out/policies/keep"
    storage.objects[unrelated] = b"checkpoint"
    journal = _journal(storage)
    before = sum(storage.writes.values())
    asked = len(storage.asked)
    journal._clear_state("session", "run1")
    assert Counter(method for method, _ in storage.asked[asked:]) == {"GET": 3, "DELETE": 3}
    assert old not in storage.objects
    assert storage.objects[current] == b"unchanged model"
    assert storage.objects[newer] == b"in flight"
    assert storage.objects[unrelated] == b"checkpoint"
    assert sum(storage.writes.values()) == before
    asked = len(storage.asked)
    journal._clear_state("session", "run1")
    assert Counter(method for method, _ in storage.asked[asked:]) == {"GET": 1}
    assert sum(storage.writes.values()) == before
    assert json.loads(journal.held("run1")["out/state/current.json"])["save"] == second.name


def test_a_folder_checkpoint_is_delivered_once_and_relayed_member_by_member(tmp_path: Path) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    with track(
        experiment="tiny",
        run="run1",
        directory=root.parent,
        clocks=[Step("updates", "updates")],
        strict=True,
    ) as run:

        def export(folder: Path) -> None:
            (folder / "policy").write_bytes(b"weights")
            (folder / "value").write_bytes(b"value weights")

        run.checkpoint(export, updates=1)
        (checkpoint,) = run.checkpoints()
    courier._track("run1", done=False)
    courier._track("run1", done=False)
    record = next(root.glob("checkpoints/*.json")).relative_to(root).as_posix()
    assert checkpoint.file is not None
    written = list(storage.writes)
    for member in checkpoint.artifact.members:
        key = "etude/run1/" + checkpoint.file + "/" + member
        assert storage.writes[key] == 1
        assert written.index(key) < written.index("etude/run1/" + record)
        relayed = _journal(storage).held("run1", checkpoint.file + "/" + member)
        target = tmp_path / "relay" / member.replace("/", "_")
        put_back(target, relayed)
        assert (target / checkpoint.file / member).read_bytes() == (
            checkpoint.path / member
        ).read_bytes()
