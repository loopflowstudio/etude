"""Tiny gated objects prove delivery ordering, retry and owner-thread accounting."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import closing
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from etude.launch.deadline import limit_time, remaining
from etude.launch.database import Database
from etude.launch.records import Output
from etude.launch.schemas import AttemptRecord, Progress
from etude.launch.storage.s3 import Grant, PostForm, Reply, StorageUnavailable
from etude.launch.storage.transport import attribute, emit, observe
from etude.launch.worker import courier as delivery
from etude.launch.worker.courier import Courier


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

    def send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
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
                    assert record.finished is not None
                    assert all(f"runs/study/attempts/a1/{name}" in self.objects for name in "abcd")
                self.objects[key] = payload
            emit("POST", url, supplied=len(body), status=204, completed=True)
            return Reply(status=204, body=b"", headers={})
        finally:
            if output:
                with self._lock:
                    self.active -= 1


def _courier(tmp_path: Path, files: _Files) -> tuple[Courier, Path]:
    home = tmp_path / "sessions/session"
    attempt = home / "attempts/a1"
    outputs = attempt / "outputs"
    outputs.mkdir(parents=True)
    for name in "abcd":
        (outputs / name).write_bytes(name.encode())
    (attempt / "run").write_text("study")
    (attempt / "claim").write_text(datetime.now(UTC).isoformat())
    finished = Progress(
        attempt="a1",
        started_at=datetime.now(UTC),
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
    (attempt / "finished.json").write_text(
        finished.model_dump_json(exclude={"attempt", "started_at"})
    )
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
    assert "runs/study/attempts/a1.json" not in files.objects
    journal = tmp_path / "sessions/session/usage.jsonl"
    observations = [json.loads(line) for line in journal.read_text().splitlines()]
    assert any(not row["completed"] and row["supplied_bytes"] > 0 for row in observations)
    files.failed = ""
    courier.carry_out()
    for name in "abcd":
        assert files.objects[f"runs/study/attempts/a1/{name}"] == name.encode()
    assert (
        AttemptRecord.model_validate_json(files.objects["runs/study/attempts/a1.json"]).finished
        is not None
    )
    assert files.peak <= 2


def test_transfer_retention_runs_on_the_invoking_database_thread_with_its_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "cache"))
    files = _Files()
    courier, outputs = _courier(tmp_path, files)
    with closing(Database(tmp_path / "owner.sqlite")) as database:
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
            original_pending = courier._pending

            def pending(path: Path, run: str, key: str) -> delivery._Upload | None:
                if path == outputs / "c":
                    raise FileNotFoundError(path)
                return original_pending(path, run, key)

            fault.setattr(courier, "_pending", pending)
            error = FileNotFoundError
        with observe(observations.append), pytest.raises(error):
            courier._put_outputs(outputs, "study", "a1")
    assert len(observations) >= 2
    assert all(json.loads(row)["completed"] for row in observations)
    assert "runs/study/attempts/a1.json" not in files.objects
    courier.carry_out()
    assert all(f"runs/study/attempts/a1/{name}" in files.objects for name in "abcd")
