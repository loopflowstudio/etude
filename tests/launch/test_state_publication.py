"""Whole-save delivery and recovery, with bounded files and counted bucket writes."""

import json
from collections import Counter
from pathlib import Path
from typing import IO

import pytest

from etude.carriage import Publication, publication, put_back, restore_set
from etude.launch.aws import Keys
from etude.launch.records import Storage as Place
from etude.launch.schemas import Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket
from etude.out import MEMBERSHIP
from etude.records import Clock, RunCounters, RunOutcome, RunRecord, RunWriter
from etude.run import RunFiles, StorageDestination, prune
from etude.storage import DirectoryStorage
from etude.storage_timeout import StorageUnavailable
from tests.launch.test_out import Writes
from tests.launch.test_runpod_machines import machine
from tests.run.storage import MeasuredStorage
from tests.train.tracking import open_run


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


@pytest.mark.parametrize("address", ["run1", "tiny", "tiny/policy@2"])
def test_fresh_host_restores_a_whole_save_at_its_run_address(tmp_path: Path, address: str) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path / "old", storage, run=address)
    saved = _save(root, 2)
    courier._track(address, done=False)
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
        run=address,
        resumes=True,
    )
    fresh, _, restored = machine(tmp_path / "fresh", storage, run=address)
    stale = restored / "out/stale"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"superseded local state")
    before = len(storage.asked)
    links = _journal(storage)._restore([work])
    assert Counter(method for method, _ in storage.asked[before:]) == {"GET": 1}
    before = len(storage.asked)
    fresh.restore(links, [work])
    assert Counter(method for method, _ in storage.asked[before:]) == {"GET": 5}
    assert not stale.exists()
    assert (restored / "out/state/current.json").read_bytes() == (
        saved.parent / "current.json"
    ).read_bytes()
    for member in saved.iterdir():
        assert (
            restored / "out/state" / saved.name / member.name
        ).read_bytes() == member.read_bytes()
    before = len(storage.asked)
    fresh._track(address, done=False)
    assert storage.asked[before:] == []


@pytest.mark.parametrize("name", [None, "policy"])
def test_only_the_selected_save_travels_and_its_pointer_is_last(
    tmp_path: Path, name: str | None
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    first = _save(root, 1, name=name)
    second = _save(root, 2, name=name)
    (second / "weights.tail").write_bytes(b"committed")
    (second / "save-7").mkdir()
    (second / "save-7/weights").write_bytes(b"framework checkpoint")
    unfinished = second.parent / "save-00000003"
    unfinished.mkdir()
    (unfinished / "model").write_bytes(b"partial")
    courier._track("run1", done=False)
    sent = [key for key in storage.writes if "/out/state/" in key]
    prefix = "etude/run1/" + second.relative_to(root).as_posix()
    pointer = "etude/run1/" + (second.parent / "current.json").relative_to(root).as_posix()
    members = {"model", "optimizer", "state.json", "weights.tail", "save-7/weights"}
    assert set(sent[:-1]) == {f"{prefix}/{member}" for member in members}
    assert sent[-1] == pointer
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
    assert "out/state/current.json" in publication(root, tail_seconds=0).keys
    assert not any("save-00000001" in key for key in storage.keys("run1/"))


@pytest.mark.parametrize("value", ["../escape", "save-00000001/../../escape", "arbitrary"])
def test_invalid_pointer_never_selects_or_deletes_files(tmp_path: Path, value: str) -> None:
    folder = _save(tmp_path, 1)
    (folder.parent / "current.json").write_text(json.dumps({"save": value}))
    with pytest.raises(ValueError, match="numbered save"):
        publication(tmp_path, tail_seconds=0).keys
    assert folder.exists()


def test_server_retires_remote_saves_under_an_upload_only_grant(tmp_path: Path) -> None:
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
    courier, _, root = machine(tmp_path, storage, run="tiny/run1")
    with open_run(
        experiment="tiny",
        run="run1",
        directory=root.parent.parent,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        strict=True,
    ) as run:

        def export(folder: Path) -> None:
            (folder / "policy").write_bytes(b"weights")
            (folder / "value").write_bytes(b"value weights")

        run.checkpoint(export, updates=1)
        (checkpoint,) = run.checkpoints()
    courier._track("tiny/run1", done=False)
    courier._track("tiny/run1", done=False)
    record = next(root.glob("checkpoints/*.json")).relative_to(root).as_posix()
    assert checkpoint.file is not None
    written = list(storage.writes)
    for member in checkpoint.artifact.members:
        key = "etude/tiny/run1/" + checkpoint.file + "/" + member
        assert storage.writes[key] == 1
        assert written.index(key) < written.index("etude/tiny/run1/" + record)
        relayed = _journal(storage).held("tiny/run1", checkpoint.file + "/" + member)
        target = tmp_path / "relay" / member.replace("/", "_")
        put_back(target, relayed)
        assert (target / checkpoint.file / member).read_bytes() == (
            checkpoint.path / member
        ).read_bytes()


def test_host_relay_keeps_the_group_outcome_that_preceded_its_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = DirectoryStorage(tmp_path)
    group = RunRecord(
        id="league/group", experiment="league", kind="evaluation", members=("league/a",)
    )
    storage.put("run.json", group.model_dump_json().encode())
    failed = RunOutcome(state="failed", counters=RunCounters()).model_dump_json().encode()
    storage.put("outcome.json", failed)
    block = "league/a/blocks/retry/block.json"

    def finish_after_listing(root: Path, *, tail_seconds: float) -> Publication:
        snapshot = publication(root, tail_seconds=tail_seconds)
        storage.put(block, b"sealed")
        storage.put(
            "outcome.json",
            RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
        )
        return snapshot

    monkeypatch.setattr("etude.carriage.publication", finish_after_listing)
    relayed = restore_set(tmp_path)
    assert block not in relayed
    assert relayed["outcome.json"] == failed


@pytest.mark.parametrize("delivered", [False, True], ids=["before-first-match", "both-tournaments"])
def test_group_end_follows_both_members_and_a_fresh_machine_restores_them(
    tmp_path: Path,
    delivered: bool,
) -> None:
    storage = Writes()
    address = "league/evals-test"
    courier, _, root = machine(tmp_path / "old", storage, run=address)
    members = ("league/blocks", "league/gauntlet")
    record = RunRecord(id=address, experiment="league", kind="evaluation", members=members)
    files = DirectoryStorage(root)
    files.put("run.json", record.model_dump_json().encode())
    files.put(
        "outcome.json",
        RunOutcome(state="failed", counters=RunCounters()).model_dump_json().encode(),
    )
    files.put(
        "metrics/abcdef123456.json",
        RunWriter(writer="abcdef123456", run=address, attempt="a1", host="test", started_ms=42)
        .model_dump_json()
        .encode(),
    )
    for member in members if delivered else ():
        files.put(
            member + "/run.json",
            RunRecord(id=member, experiment="league", kind="evaluation").model_dump_json().encode(),
        )
        files.put(member + "/tournament.json", b"tournament")
        files.put(member + "/blocks/one/scores.parquet", b"scores")
        files.put(member + "/blocks/one/block.json", b"sealed")
    courier._track(address, done=True)
    sent = list(storage.writes)
    assert sent[0].endswith("/" + address + "/run.json")
    assert sent[-1].endswith("/" + address + "/outcome.json")
    assert len(sent) == (11 if delivered else 3) and all(
        count == 1 for count in storage.writes.values()
    )

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
        run=address,
        resumes=True,
    )
    journal = _journal(storage)
    reads = len(storage.asked)
    links = journal._restore([work])
    assert Counter(method for method, _ in storage.asked[reads:]) == {"GET": 2}
    fresh, _, restored = machine(tmp_path / "fresh", storage, run=address)
    fresh.restore(links, [work])
    if delivered:
        for member in members:
            assert (restored / member / "blocks/one/scores.parquet").read_bytes() == b"scores"
    assert restore_set(restored) == restore_set(root) == journal.held(address)
    writes = storage.writes.copy()
    fresh._track(address, done=False)
    assert storage.writes == writes
