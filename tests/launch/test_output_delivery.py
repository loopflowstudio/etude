"""Tiny gated objects prove delivery ordering, retry and owner-thread accounting."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from etude.deadline import limit_time, remaining
from etude.launch.aws import Keys
from etude.launch.records import Output
from etude.launch.records import Storage as Place
from etude.launch.schemas import AttemptRecord, Finished, Packet, Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Grant, PostForm
from etude.launch.storage.transport import HTTPReply, attribute, emit, observe
from etude.launch.worker import courier as delivery
from etude.launch.worker.courier import Courier
from etude.storage_timeout import StorageUnavailable
from tests.launch.fake import open_database
from tests.launch.test_runpod_machines import Storage


class _Files:
    def __init__(self, *, gated: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.failed = ""
        self.active = 0
        self.peak = 0
        self.allowances: list[float | None] = []
        self._lock = threading.Lock()
        self._gate = threading.Barrier(2, timeout=5) if gated else None
        self._gated: set[str] = set()

    def send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> HTTPReply:
        assert method == "POST" and body is not None
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body
        )
        fields: dict[str, bytes] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            assert isinstance(name, str) and isinstance(payload, bytes)
            fields[name] = payload
        key, payload = fields["key"].decode(), fields["file"]
        output = key.startswith("runs/study/attempts/a1/")
        gated = False
        with self._lock:
            if output:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.allowances.append(remaining())
                if self._gate is not None and key.rsplit("/", 1)[-1] in {"a", "b"}:
                    gated = key not in self._gated
                    self._gated.add(key)
        try:
            # Two single-byte transfers rendezvous; no throughput/load fixture.
            if gated:
                assert self._gate is not None
                self._gate.wait()
            if output and key.endswith(self.failed) and self.failed:
                emit("POST", url, supplied=len(body), completed=False)
                raise TimeoutError("controlled unavailable reply")
            with self._lock:
                if output:
                    assert key not in self.objects, "successful immutable output was resent"
                if key == "runs/study/attempts/a1.json":
                    record = AttemptRecord.model_validate_json(payload)
                    assert record.exited is not None
                    if record.delivered:
                        assert all(
                            f"runs/study/attempts/a1/{name}" in self.objects for name in "abcd"
                        )
                self.objects[key] = payload
            emit("POST", url, supplied=len(body), status=204, completed=True)
            return HTTPReply(status=204, body=b"", headers={})
        finally:
            if output:
                with self._lock:
                    self.active -= 1

    def delivered(self) -> bool:
        stored = self.objects.get("runs/study/attempts/a1.json")
        return stored is not None and AttemptRecord.model_validate_json(stored).delivered


def _courier(tmp_path: Path, files: _Files) -> tuple[Courier, Path]:
    home = tmp_path / "sessions/session"
    attempt = home / "attempts/a1"
    outputs = attempt / "outputs"
    outputs.mkdir(parents=True)
    for name in "abcd":
        (outputs / name).write_bytes(name.encode())
    (attempt / "run").write_text("study")
    (attempt / "claim").write_text(datetime.now(UTC).isoformat())
    finished = Finished(
        finished_at=datetime.now(UTC),
        outcome="succeeded",
        cause=None,
        clocks={},
        exit_code=0,
        outputs=tuple(
            Output(name=name, sha256=hashlib.sha256(name.encode()).hexdigest(), size_bytes=1)
            for name in "abcd"
        ),
    )
    (attempt / "finished.json").write_text(finished.model_dump_json())
    form = PostForm(url="https://fixture.invalid/upload", fields={})
    return Courier(
        home,
        Grant(prefix="out/", form=form),
        "unused",
        files.send,
        runs={"study": Grant(prefix="runs/study/", form=form)},
    ), outputs


def test_failed_output_keeps_completion_absent_and_retry_keeps_delivered_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(delivery, "_OUTPUT_UPLOADS", 2)
    files = _Files(gated=True)
    files.failed = "/b"
    courier, _ = _courier(tmp_path, files)
    with pytest.raises(StorageUnavailable):
        courier.carry_out()
    assert files.peak == 2
    assert not files.delivered()
    journal = tmp_path / "sessions/session/usage.jsonl"
    observations = [json.loads(line) for line in journal.read_text().splitlines()]
    assert any(not row["completed"] and row["supplied_bytes"] > 0 for row in observations)
    files.failed = ""
    courier.carry_out()
    for name in "abcd":
        assert files.objects[f"runs/study/attempts/a1/{name}"] == name.encode()
    assert files.delivered()
    assert files.peak <= 2


def test_transfer_retention_runs_on_the_invoking_database_thread_with_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    files = _Files()
    courier, outputs = _courier(tmp_path, files)
    with closing(open_database(tmp_path / "owner.sqlite")) as database:
        with (
            limit_time(60),
            attribute(session="session", run="study", attempt="a1"),
            observe(database.retain_transfer_json),
        ):
            courier._put_outputs(outputs, "study", "a1")
        events = database.transfers()
    assert len(events) == 4 and all(event.completed for event in events)
    assert all(
        (event.session, event.run, event.attempt) == ("session", "study", "a1") for event in events
    )
    assert all(left is not None and 0 < left <= 60 for left in files.allowances)
    assert sum(event.supplied_bytes for event in events) > 4


@pytest.mark.parametrize("interrupted", [False, True])
def test_delivery_retains_in_flight_outputs_when_the_caller_cannot_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(delivery, "_OUTPUT_UPLOADS", 2)
    files = _Files(gated=True)
    courier, outputs = _courier(tmp_path, files)
    observations: list[str] = []
    with monkeypatch.context() as fault:
        if interrupted:

            def interrupt(*args: object, **kwargs: object) -> None:
                raise KeyboardInterrupt()

            fault.setattr(delivery, "wait", interrupt)
            error: type[BaseException] = KeyboardInterrupt
        else:
            # Fail at submission, after traversal has admitted all four files.
            original_unsent = courier._unsent

            def unsent(path: Path, run: str, key: str) -> delivery._Upload | None:
                if path == outputs / "c":
                    raise FileNotFoundError(path)
                return original_unsent(path, run, key)

            fault.setattr(courier, "_unsent", unsent)
            error = FileNotFoundError
        with observe(observations.append), pytest.raises(error):
            courier._put_outputs(outputs, "study", "a1")
    assert len(observations) >= 2
    assert all(json.loads(row)["completed"] for row in observations)
    assert not files.delivered()
    courier.carry_out()
    assert all(f"runs/study/attempts/a1/{name}" in files.objects for name in "abcd")
    assert files.delivered()
    assert not outputs.exists()


def test_confirmed_final_delivery_cleans_session_while_failed_delivery_keeps_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    files = _Files()
    courier, outputs = _courier(tmp_path, files)
    home = courier._home
    (home / "sources").mkdir()
    (home / "sources/commit.tar").write_bytes(b"source-tar")
    (home / "inputs").mkdir()
    (home / "inputs/deadbeef").write_bytes(b"input-bytes")
    (home / "packet.json").write_text("{}")
    (home / "ended").write_text(datetime.now(UTC).isoformat())

    # Unconfirmed final delivery keeps outputs, sources, inputs, and attempts intact.
    files.failed = "/b"
    assert not delivery._deliver(
        courier,
        lambda _: None,
        0.01,
        lambda: 0.0,
        closed=True,
        ended=True,
    )
    assert outputs.is_dir()
    assert (home / "sources/commit.tar").is_file()
    assert (home / "inputs/deadbeef").is_file()
    assert (home / "attempts/a1").is_dir()

    # Confirmed final delivery removes outputs, sources, inputs, and attempts, keeping control files.
    files.failed = ""
    assert delivery._deliver(
        courier,
        lambda _: None,
        0.01,
        lambda: 60.0,
        closed=True,
        ended=True,
    )
    assert not outputs.exists()
    courier.clean_session()
    assert not (home / "sources").exists()
    assert not (home / "inputs").exists()
    assert not (home / "attempts").exists()
    assert (home / "packet.json").is_file()
    assert (home / "ended").is_file()


@pytest.mark.parametrize("fail_final", [False, True])
def test_serve_cleans_session_only_after_confirmed_final_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_final: bool
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    storage = Storage()
    place = Place(name="s", bucket="b", prefix="test", provider="s3", region="us-east-1")
    bucket = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage)
    journal = BucketJournal(place, bucket)
    work = Work(
        attempt="a1",
        run="exp/job@0",
        command='echo hi > "$ETUDE_OUTPUT_DIR/model.pt"',
        limit_seconds=60,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )
    packet = Packet(
        session="s-clean",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        work=(work,),
    )
    bootstrap = journal.prepare(packet)

    def transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> HTTPReply:
        if fail_final and method in ("PUT", "POST") and "/out/ended" in url:
            raise StorageUnavailable("final ended upload failed")
        if fail_final and method == "POST" and body and b"out/ended" in body:
            raise StorageUnavailable("final ended upload failed")
        return storage(method, url, body, headers)

    root = tmp_path / "sessions"
    worker_log = root / "s-clean" / "worker.log"
    delivery._serve(
        bootstrap.url,
        root,
        transport,
        lambda _: None,
        0.01,
        worker_log,
        "",
    )
    session_dir = root / "s-clean"
    if fail_final:
        assert (session_dir / "attempts").is_dir()
    else:
        assert not (session_dir / "attempts").exists()
        assert not (session_dir / "sources").exists()
        assert not (session_dir / "inputs").exists()
        assert (session_dir / "packet.json").is_file()
        assert (session_dir / "ended").is_file()
        assert (session_dir / "pid").is_file()
