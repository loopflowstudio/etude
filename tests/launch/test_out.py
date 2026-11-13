"""Bucket carriage preserves all recipe state, with counted transfers and strict restore."""

import json
import os
import shutil
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

from etude.carriage import put_back, restore_set
from etude.jobs import continue_from
from etude.launch.errors import LauncherError
from etude.launch.records import Input, Job
from etude.launch.records import Storage as Place
from etude.launch.schemas import Delivery, Inbox, Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys, Reply
from etude.launch.worker.courier import Courier
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.run.storage import DirectoryStorage
from etude.storage_timeout import StorageUnavailable
from etude.train.read import read
from etude.train.records import Step
from etude.train.run import track
from tests.launch.test_runpod_machines import BucketObjects, Observed, Storage, machine
from tests.launch.test_worker_attempts import local
from tests.run.storage import MeasuredStorage


class Writes(Storage):
    """Count stored objects for both signed PUTs and upload-only POSTs."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: Counter[str] = Counter()

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        before = dict(self.objects)
        reply = super().__call__(method, url, body, headers)
        self.writes.update(
            key for key, value in self.objects.items() if before.get(key) is not value
        )
        return reply


def test_courier_hashes_mutable_out_and_restores_every_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Observed()
    courier, _, root = machine(tmp_path / "first", storage)
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(
        experiment="e", run="run1", clocks=[Step("updates", "updates")], directory=root.parent
    ) as run:
        (run.out / "weights").mkdir()
        (run.out / "weights/model.json").write_bytes(b"weights")
        run.checkpoint(run.out / "weights/model.json", updates=2)
        (run.out / "state").write_bytes(b"save one")
        (run.out / "unfinished.partial").write_bytes(b"unfinished")
        (run.out / ".private").mkdir()
        (run.out / ".private/data").write_bytes(b"local only")
    courier.carry_out()
    sent = [key for method, key in storage.asked if method in ("PUT", "POST") and "/run1/" in key]
    assert sent.count("etude/run1/out/weights/model.json") == 1
    assert sent.index("etude/run1/out/weights/model.json") < sent.index(
        "etude/run1/checkpoints/updates-2.json"
    )
    assert not any("unfinished" in key or ".private" in key for key in sent)
    state = root / "out/state"
    stamp = state.stat()
    state.write_bytes(b"save two")
    os.utime(state, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    courier.carry_out()
    courier.carry_out()
    sent = [key for method, key in storage.asked if method in ("PUT", "POST") and "/out/" in key]
    assert sent.count("etude/run1/out/state") == 2
    assert sent.count("etude/run1/out/weights/model.json") == 1
    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
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
        run="run1",
        resumes=True,
    )
    before = len(storage.asked)
    links = journal._restore([work])
    assert len(storage.asked) == before + 1  # one listing; links make no requests
    fresh, _, restored = machine(tmp_path / "fresh", storage)
    before = len(storage.asked)
    fresh.restore(links, [work])
    reads = [key.partition("?")[0] for method, key in storage.asked[before:] if method == "GET"]
    assert reads == [
        "etude/run1/out.json",
        "etude/run1/checkpoints/updates-2.json",
        "etude/run1/out/state",
        "etude/run1/out/weights/model.json",
        "etude/run1/run.json",
    ]
    assert OutFiles.model_validate_json(storage.objects["etude/run1/out.json"]).files == (
        OutFile(key="out/state", sha256=sha256(b"save two").hexdigest(), size_bytes=8),
        OutFile(key="out/weights/model.json", sha256=sha256(b"weights").hexdigest(), size_bytes=7),
    )
    assert (restored / "out/state").read_bytes() == b"save two"
    transfers = [
        json.loads(line) for line in (fresh._home / "usage.jsonl").read_text().splitlines()
    ]
    purposes = {
        event["purpose"] for event in transfers if event["key"].endswith("/out/weights/model.json")
    }
    assert purposes == {"checkpoint"}
    checkpoint = read(restored.parent, "e", "run1").checkpoints[0]
    assert checkpoint.artifact.uri == (restored / "out/weights/model.json").as_uri()
    before = len(storage.asked)
    fresh.carry_out()
    assert not [
        key
        for method, key in storage.asked[before:]
        if method in ("PUT", "POST") and "/run1/out/" in key
    ]
    # Relaying from storage to an owned host carries the same complete directory.
    held = journal.held("run1")
    assert held["out/state"] == b"save two" and held["out/weights/model.json"] == b"weights"
    storage.objects["etude/run1/out/state"] = b"truncated"
    broken, _, broken_root = machine(tmp_path / "broken", storage)
    with pytest.raises(StorageUnavailable, match="changed length"):
        broken.restore(links, [work])
    assert not (broken_root / "out/state").exists()


def test_a_retry_on_the_same_machine_restores_out_only_when_its_folder_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reimaged host, or another behind the same address, resumes from storage, not zero."""
    storage = Observed()
    courier, _, root = machine(tmp_path / "box", storage)
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(
        experiment="e", run="run1", clocks=[Step("updates", "updates")], directory=root.parent
    ) as run:
        (run.out / "state").write_bytes(b"update 12")
    courier.carry_out()
    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
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
        run="run1",
        resumes=True,
        here=True,
    )
    links = journal._restore([work])
    (root / "out/state").write_bytes(b"update 13, not yet carried")
    before = len(storage.asked)
    courier.restore(links, [work])
    assert storage.asked[before:] == []
    assert (root / "out/state").read_bytes() == b"update 13, not yet carried"
    wiped, _, again = machine(tmp_path / "wiped", storage)
    shutil.rmtree(again)  # the same machine, its disk wiped
    wiped.restore(links, [work])
    assert (again / "out/state").read_bytes() == b"update 12"


@pytest.mark.parametrize("grants", [True, False])
def test_deleted_files_stay_absent_after_restart_recovery_and_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grants: bool
) -> None:
    storage = Writes()
    storage.grants = grants
    courier, _, root = machine(tmp_path / "first", storage)
    scheduler = local(tmp_path / "client")
    parent = scheduler.queue(Job(command="train", max_hours=1), "local")
    # The fixture's courier grant names run1; use its tracked run with this job.
    record = scheduler.record(parent).model_copy(update={"id": "run1"})
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(
        experiment="e", run="run1", clocks=[Step("updates", "updates")], directory=root.parent
    ) as run:
        final = run.out / "final.pt"
        final.write_bytes(b"immutable weights")
        run.checkpoint(final, updates=2)
        run.log({"loss": 0}, updates=2)
        (run.out / "state").write_bytes(b"saved")
    courier.carry_out()
    final.unlink()
    (root / "out/state").unlink()
    # Restart with no in-memory sent cache, after the recipe's ordinary unlink.
    bucket = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage)
    courier = Courier(
        courier._home,
        bucket.grant("etude/sessions/r/out/", 3600, "r"),
        "unused",
        storage,
        {"run1": bucket.grant("etude/run1/", 3600, "run1")},
        courier._home / "worker.log",
    )
    courier.carry_out()
    courier.carry_out()
    member = "etude/run1/" + MEMBERSHIP
    assert OutFiles.model_validate_json(storage.objects[member]).files == ()
    assert storage.objects["etude/run1/out/final.pt"] == b"immutable weights"
    assert storage.writes[member] == 2
    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"), bucket
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
        run="run1",
        resumes=True,
    )
    before = len(storage.asked)
    links = journal._restore([work])
    assert len(storage.asked) == before + 1
    fresh, _, restored = machine(tmp_path / "fresh", storage)
    (restored / "out").mkdir()
    (restored / "out/stale").write_bytes(b"old host state")
    before = len(storage.asked)
    fresh.restore(links, [work])
    reads = [key.partition("?")[0] for method, key in storage.asked[before:] if method == "GET"]
    assert reads == [member, "etude/run1/checkpoints/updates-2.json", "etude/run1/run.json"]
    assert list((restored / "out").iterdir()) == []
    before = len(storage.asked)
    held = journal.held("run1")
    reads = [key.partition("?")[0] for method, key in storage.asked[before:] if method == "GET"]
    assert len(reads) == 4  # one listing, the checkpoint record, declaration and membership
    relayed = tmp_path / "relay/run1"
    (relayed / "out").mkdir(parents=True)
    (relayed / "out/stale").write_bytes(b"old host state")
    put_back(relayed, held)
    assert not (relayed / "out/stale").exists()
    source = MeasuredStorage(DirectoryStorage(relayed.parent))
    with pytest.raises(LauncherError, match="latest checkpoint's out file was removed"):
        continue_from(record, "updates=4", source=source)
    assert source.listings == 2
    assert not any("/out/" in key for key in source.got)
    source = MeasuredStorage(BucketObjects(storage, "b"))
    with pytest.raises(LauncherError, match="latest checkpoint's out file was removed"):
        continue_from(record, "updates=4", source=source)
    assert source.got.count(member) == 1 and source.listings == 2
    assert not any("/out/" in key for key in source.got)
    source = MeasuredStorage(BucketObjects(storage, "b"))
    child = continue_from(record, "updates=4", source=source, ordinal=1)
    assert [given.key for given in child.job.inputs] == ["out/final.pt"]
    assert member not in source.got and source.listings == 2
    assert not any("/out/" in key for key in source.got)
    # Explicit checkpoint selection still retrieves the immutable object.
    assert journal.held("run1", "out/final.pt")["out/final.pt"] == b"immutable weights"
    # Recreating the same mutable bytes is visible again; a fresh host does not
    # mistake its retained storage object for membership.
    (root / "out/state").write_bytes(b"saved")
    courier.carry_out()
    assert OutFiles.model_validate_json(storage.objects[member]).files == (
        OutFile(key="out/state", sha256=sha256(b"saved").hexdigest(), size_bytes=5),
    )
    fresh.restore(journal._restore([work]), [work])
    assert (restored / "out/state").read_bytes() == b"saved"
    assert not (restored / "out/final.pt").exists()
    assert storage.writes["etude/run1/out/state"] == 2
    source = MeasuredStorage(BucketObjects(storage, "b"))
    child = continue_from(record, "updates=4", source=source)
    assert [given.out for given in child.job.inputs] == ["state"]
    assert source.got.count(member) == 1
    assert not any("/out/" in key for key in source.got)
    assert child.job.inputs[0].sha256 == sha256(b"saved").hexdigest()
    assert child.job.inputs[0].size_bytes == 5
    # Direct host relay uses the live directory, not its earlier carried membership.
    (root / "out/state").unlink()
    put_back(restored, restore_set(root))
    assert not (restored / "out/state").exists()


def test_continuation_initial_state_is_stored_before_its_first_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage()
    courier, attempt, root = machine(tmp_path, storage)
    body = b"initial optimizer state"
    digest = sha256(body).hexdigest()
    inputs = courier._home / "inputs"
    inputs.mkdir()
    (inputs / digest).write_bytes(body)
    work = Work(
        attempt="a1",
        command="true",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
        run="run1",
        inputs=(
            Input(
                name="initial",
                key="out/state",
                run="parent",
                out="state",
                sha256=digest,
                size_bytes=len(body),
            ),
        ),
    )
    bucket = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage)
    courier._inbox_url = bucket.get_url("inbox", 3600)
    access = bucket.grant("etude/run1/", 3600, "run1")
    storage.objects["inbox"] = (
        Inbox(work=(Delivery.model_validate({"work": work.model_dump_json(), "access": access}),))
        .model_dump_json(round_trip=True)
        .encode()
    )
    (courier._home / "work").mkdir()
    initialize = courier.initialize_out
    failed = False

    def initialize_out(given: Work) -> None:
        nonlocal failed
        initialize(given)
        if not failed:
            failed = True
            raise StorageUnavailable("initial upload reply lost")

    monkeypatch.setattr(courier, "initialize_out", initialize_out)
    with pytest.raises(StorageUnavailable, match="reply lost"):
        courier.bring_in()
    executable = courier._home / "work/a1.json"
    assert not executable.exists()
    # The unchanged inbox must be applied again after a failed delivery pass.
    courier.bring_in()
    assert Work.model_validate_json(executable.read_bytes()) == work
    assert not (attempt / "claim").exists()
    assert storage.objects["etude/run1/out/state"] == body
    assert (root / "out/state").read_bytes() == body
    courier.carry_out()
    sent = [key for method, key in storage.asked if method in ("PUT", "POST")]
    assert sent.count("etude/run1/out/state") == 1


@pytest.mark.parametrize("same_key", [False, True])
def test_failed_membership_delivery_keeps_the_previous_membership_until_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_key: bool
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    (root / "out").mkdir()
    (root / "out/old").write_bytes(b"old")
    courier._track("run1", done=False)
    (root / "out/old").unlink()
    key = "out/old" if same_key else "out/new"
    (root / key).write_bytes(b"new")
    folder = courier._runs["run1"]
    write = folder.write_bytes

    def fail_membership(key: str, body: bytes) -> None:
        if key == MEMBERSHIP:
            raise StorageUnavailable("membership delivery interrupted")
        write(key, body)

    monkeypatch.setattr(folder, "write_bytes", fail_membership)
    with pytest.raises(StorageUnavailable, match="interrupted"):
        courier._track("run1", done=False)
    assert storage.objects["etude/run1/" + key] == b"new"
    assert OutFiles.model_validate_json(storage.objects["etude/run1/out.json"]).files == (
        OutFile(key="out/old", sha256=sha256(b"old").hexdigest(), size_bytes=3),
    )
    # Recovery still uses current bytes and listing lengths. A machine lost
    # between data and membership delivery can restore the newer save.
    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
    )
    assert journal.held("run1")["out/old"] == (b"new" if same_key else b"old")
    monkeypatch.setattr(folder, "write_bytes", write)
    courier._track("run1", done=False)
    assert OutFiles.model_validate_json(storage.objects["etude/run1/out.json"]).files == (
        OutFile(key=key, sha256=sha256(b"new").hexdigest(), size_bytes=3),
    )
    assert storage.writes["etude/run1/" + key] == (2 if same_key else 1)
    assert storage.writes["etude/run1/out.json"] == 2


def test_courier_carries_a_changed_out_file_before_publishing_its_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Writes()
    courier, _, root = machine(tmp_path, storage)
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    with track(
        experiment="e", run="run1", clocks=[Step("updates", "updates")], directory=root.parent
    ) as run:
        model = run.out / "model.pt"
        model.write_bytes(b"draft")
        courier._track("run1", done=False)
        partial = model.with_suffix(".partial")
        partial.write_bytes(b"final weights")
        partial.replace(model)
        run.checkpoint(model, updates=2)
    courier.carry_out()
    courier.carry_out()
    assert storage.objects["etude/run1/out/model.pt"] == b"final weights"
    assert storage.writes["etude/run1/out/model.pt"] == 2
    assert OutFiles.model_validate_json(storage.objects["etude/run1/out.json"]).files == (
        OutFile(key="out/model.pt", sha256=sha256(b"final weights").hexdigest(), size_bytes=13),
    )
