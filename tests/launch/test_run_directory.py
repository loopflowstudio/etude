"""Bucket carriage preserves all recipe state, with counted transfers and strict restore."""

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from etude.launch.records import Input
from etude.launch.records import Storage as Place
from etude.launch.schemas import Delivery, Inbox, Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys
from etude.storage_timeout import StorageUnavailable
from etude.train.read import read
from etude.train.records import Step
from etude.train.run import track
from tests.launch.test_runpod_machines import Observed, Storage, machine


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
    fresh.restore(links)
    reads = [key.partition("?")[0] for method, key in storage.asked[before:] if method == "GET"]
    assert reads == [
        "etude/run1/checkpoints/updates-2.json",
        "etude/run1/out/state",
        "etude/run1/out/weights/model.json",
        "etude/run1/run.json",
    ]
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
        broken.restore(links)
    assert not (broken_root / "out/state").exists()


def test_retiring_a_working_file_forgets_its_delivery_fingerprint(tmp_path: Path) -> None:
    storage = Storage()
    courier, _, root = machine(tmp_path, storage)
    out = root / "out"
    out.mkdir()
    state = out / "state"
    state.write_bytes(b"saved")
    courier.carry_out()
    assert storage.objects["etude/run1/out/state"] == b"saved"
    state.unlink()
    note = root / "retired.removed"
    note.write_text("out/state\n")
    courier.carry_out()
    assert "etude/run1/out/state" not in storage.objects
    note.unlink()
    state.write_bytes(b"saved")
    courier.carry_out()
    assert storage.objects["etude/run1/out/state"] == b"saved"
    assert storage.asked.count(("PUT", "etude/run1/out/state")) == 2


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
