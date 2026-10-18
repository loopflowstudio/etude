"""Durable diagnosis without consuming scientific identities or acquiring extra machines."""

from __future__ import annotations

import hashlib
import io
import os
import select
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from etude.launch import logging, source
from etude.launch.database import Database
from etude.launch.environment import install
from etude.launch.errors import LauncherError, LaunchError
from etude.launch.logging import CLOSED, LIMIT, bounded, portions
from etude.launch.logs import LogCursor, render, snapshot_stream
from etude.launch.providers.package import program_archive
from etude.launch.providers.runpod import pod
from etude.launch.records import Diagnostic, LogSnapshot, LogStream, Run
from etude.launch.records import Storage as StorageRecord
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import AttemptRecord, Bootstrap, Observation, Packet
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.disk import DirectoryJournal
from etude.launch.storage.files import write
from etude.launch.storage.s3 import Bucket, Keys, PostForm, Reply
from etude.launch.worker import courier as courier_module
from etude.launch.worker.courier import Courier
from etude.launch.worker.main import work
from tests.launch.counting import Counting, Counts, job_text
from tests.launch.fake import bench
from tests.launch.test_runpod_machines import Storage as MemoryStorage
from tests.launch.test_scheduler import job
from tests.launch.test_source import repository  # noqa: F401


def test_failed_preparation_survives_restart_and_does_not_consume_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    original = source.freeze

    def fail(run: Run) -> Run:
        raise OSError("source publication refused")

    monkeypatch.setattr(source, "freeze", fail)
    requested = Run(id="baseline-seed-7", job=job())
    with pytest.raises(LaunchError) as caught:
        b.scheduler.launch(requested, "l4")
    assert caught.value.runs == (requested.id,)
    assert b.store.runs() == () and b.service.creates == 0
    b.store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)
    snapshot = scheduler.logs(run="baseline-seed-7")
    assert "source publication refused" in render(snapshot)
    assert snapshot.launches[0].id == caught.value.launch
    assert snapshot.streams == ()
    monkeypatch.setattr(source, "freeze", original)
    assert scheduler.launch(requested, "l4") == "baseline-seed-7"
    assert len(store.runs()) == 1
    assert [launch.state for launch in scheduler.logs(run="baseline-seed-7").launches] == [
        "failed",
        "admitted",
    ]
    with pytest.raises(LauncherError, match="already admitted"):
        scheduler.launch(requested, "l4")
    assert b.service.creates == 1


def test_uncertain_creation_blocks_reusing_id_until_confirmed_cleanup(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.lose_reply = 1
    requested = Run(id="chosen", job=job())
    with pytest.raises(LaunchError):
        b.scheduler.launch(requested, "l4")
    for _ in range(2):
        with pytest.raises(LauncherError, match="reconciling existing request"):
            b.scheduler.launch(requested, "l4")
    assert b.service.creates == 1 and b.store.runs() == ()
    assert "reply never arrived" in render(b.scheduler.logs(run="chosen"))
    b.run(1)
    b.run(1)
    b.scheduler.launch(requested, "l4")
    assert b.service.creates == 2 and len(b.store.runs()) == 1


def test_partial_cohort_retains_shared_failure_without_admitting_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    create = b.service.create
    calls = 0

    def fail_second(packet: Packet) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            b.service.refuse = 1
        return create(packet)

    monkeypatch.setattr(b.service, "create", fail_second)
    with pytest.raises(LaunchError):
        b.scheduler.launch_cohort(
            "pair", [Run(id="one", job=job()), Run(id="two", job=job())], "l4", parallel=2
        )
    assert b.store.runs() == ()
    assert not b.store.cohort_exists("pair")
    first, second = b.scheduler.logs(run="one"), b.scheduler.logs(run="two")
    assert first.launches == second.launches
    assert "no capacity" in render(first)
    assert all(s.ended_at or s.refused_at for s in b.store.sessions())
    assert b.service.creates == 2


def test_all_attempts_retained_after_cleanup_and_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    run = b.scheduler.queue(job(), "l4")
    cursor = LogCursor()
    snapshots = [b.scheduler.logs(run=run)]
    followed = [cursor.render(snapshots[-1])]
    assert not snapshots[-1].complete and not snapshots[-1].streams
    b.scheduler.tick()
    snapshots.append(b.scheduler.logs(run=run))
    followed.append(cursor.render(snapshots[-1]))
    b.run(1)
    assert b.scheduler.read(run).phase == "running"
    snapshots.append(b.scheduler.logs(run=run))
    followed.append(cursor.render(snapshots[-1]))
    b.service.kill(b.scheduler.attempts(run)[0].session)
    b.run(1)
    snapshots.append(b.scheduler.logs(run=run))
    followed.append(cursor.render(snapshots[-1]))
    b.run(700)
    assert len(b.scheduler.attempts(run)) == 2
    before = b.scheduler.logs(run=run)
    snapshots.append(before)
    followed.append(cursor.render(before))
    assert before.complete
    assert len([s for s in before.streams if s.producer == "job"]) == 2
    b.store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)

    def unavailable(*args: object, **kwargs: object) -> LogStream:
        raise OSError("storage denied")

    monkeypatch.setattr(b.service, "logs", unavailable)
    after = scheduler.logs(run=run)
    snapshots.append(after)
    followed.append(cursor.render(after))
    assert job_text(after) == job_text(before)
    assert "machine deletion confirmed" in render(after)
    for previous, current in zip(snapshots, snapshots[1:]):
        assert {d.id for d in previous.diagnostics} <= {d.id for d in current.diagnostics}
        for old in previous.streams:
            new = next(stream for stream in current.streams if stream.id == old.id)
            assert new.text.startswith(old.text)
    for attempt in scheduler.attempts(run):
        assert "".join(followed).count(f"log of {attempt.id}") == 1
    assert followed[-1] == ""
    assert b.service.creates == 2


def test_missing_silent_and_unavailable_are_distinct(tmp_path: Path) -> None:
    home = tmp_path / "s" / "attempts" / "a"
    home.mkdir(parents=True)
    journal = DirectoryJournal(tmp_path)
    missing = journal.logs("s", "a")
    (home / "log").touch()
    silent = journal.logs("s", "a")
    (home / "finished.json").write_text("{}")
    final = journal.logs("s", "a")
    assert not missing.received and not missing.complete
    assert silent.received and not silent.complete and silent.text == ""
    assert final.received and final.complete and final.text == ""
    cursor = LogCursor()
    for stream, state in (
        (missing, "output not received"),
        (silent, "no output yet"),
        (final, "silent producer"),
    ):
        snapshot = LogSnapshot(observed_at=datetime.now(UTC), streams=(stream,))
        for text in (render(snapshot), cursor.render(snapshot)):
            assert state in text and "session=s" in text
            assert f"collected={stream.observed_at}" in text
        assert cursor.render(snapshot) == ""


def test_disk_completion_during_log_read_keeps_partial_output_refreshable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "s" / "attempts" / "a"
    home.mkdir(parents=True)
    log = home / "log"
    log.write_bytes(b"early output\n")
    read_bytes = Path.read_bytes

    def read(path: Path) -> bytes:
        body = read_bytes(path)
        if path == log:
            log.write_bytes(b"early output\nfinal output\n")
            (home / "finished.json").write_text("{}")
        return body

    monkeypatch.setattr(Path, "read_bytes", read)
    journal = DirectoryJournal(tmp_path)
    early = journal.logs("s", "a")
    assert early.text == "early output\n" and not early.complete
    final = journal.logs("s", "a")
    assert final.complete and final.text == "early output\nfinal output\n"


def test_bucket_completion_during_log_read_keeps_cached_output_refreshable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    run = b.scheduler.launch(job(), "l4")
    attempt = b.scheduler.attempts(run)[0]
    session = b.service.sessions[attempt.session]
    packet = session.packet.model_copy(update={"work": tuple(w for w, _ in session.work())})
    storage = MemoryStorage()
    log_key = f"p/{run}/attempts/{attempt.id}.log"
    record_key = f"p/{run}/attempts/{attempt.id}.json"
    storage.objects[f"p/sessions/{attempt.session}/packet.json"] = packet.model_dump_json().encode()
    storage.objects[log_key] = b"early output\n"

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        reply = storage(method, url, body, headers)
        if method == "GET" and urlsplit(url).path == f"/{log_key}":
            storage.objects[log_key] = b"early output\nfinal output\n"
            storage.objects[record_key] = AttemptRecord(finished={}).model_dump_json().encode()
        return reply

    journal = BucketJournal(
        StorageRecord(name="fixture", bucket="fixture", prefix="p", region="us-east-1"),
        Bucket("fixture", "us-east-1", Keys(access_key="fixture", secret_key="fixture"), send),
    )
    original = b.service.logs

    def read(session_id: str, attempt: str | None = None, *, producer: str = "job") -> LogStream:
        if producer == "job":
            return journal.logs(session_id, attempt, producer=producer)
        return original(session_id, attempt, producer=producer)

    monkeypatch.setattr(b.service, "logs", read)
    early = b.scheduler.logs(run=run)
    assert job_text(early) == "early output\n"
    assert not next(s for s in early.streams if s.producer == "job").complete
    final = b.scheduler.logs(run=run)
    assert job_text(final) == "early output\nfinal output\n"
    assert next(s for s in final.streams if s.producer == "job").complete
    assert len(storage.asked) == 7  # Four reads, then three with the immutable packet cached.
    storage.objects.clear()
    assert job_text(b.scheduler.logs(run=run)) == job_text(final)
    assert len(storage.asked) == 7  # Final evidence needs no further bucket reads.


@pytest.mark.parametrize("timeout", [False, True])
def test_installer_retains_sanitized_stderr_and_original_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: bool,
) -> None:
    log = tmp_path / "bootstrap.log"
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setenv("ETUDE_LOG_PATH", str(log))
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-sensitive-value")
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\necho 'download failed: fixture-sensitive-value https://host/?signature=secret' >&2\n"
        + ("sleep 30\n" if timeout else "exit 17\n")
    )
    uv.chmod(0o700)
    monkeypatch.setattr("shutil.which", lambda name: str(uv))
    monkeypatch.setenv(
        "ETUDE_STARTUP_DEADLINE", (datetime.now(UTC) + timedelta(seconds=600)).isoformat()
    )
    elapsed = 0.0
    original_write = logging.Console.write

    def collect(self: logging.Console, body: bytes, *, final: bool = False) -> None:
        nonlocal elapsed
        original_write(self, body, final=final)
        if timeout and b"download failed" in body:
            elapsed = 601

    monkeypatch.setattr("etude.launch.environment.time.monotonic", lambda: elapsed)
    monkeypatch.setattr(logging.Console, "write", collect)
    with pytest.raises(TimeoutError if timeout else subprocess.CalledProcessError):
        install(program_archive())
    text = log.read_text()
    assert "download failed" in text and "[redacted]" in text
    assert "fixture-sensitive-value" not in text and "signature=secret" not in text
    assert ("startup deadline reached" if timeout else "17") in text


def test_console_redacts_credentials_split_across_pipe_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-sensitive-value")
    path = tmp_path / "bootstrap.log"
    fragments = [
        b"entry failed fixture-sensitive-",
        b"value https://host/?sign",
        b"ature=private token=un",
        b"known Authorization: Bearer un",
        b"listed-credential\n",
    ]
    code = (
        "import sys\n"
        f"for fragment in {fragments!r}:\n"
        "    sys.stdout.buffer.write(fragment)\n"
        "    sys.stdout.buffer.flush()\n"
        "    sys.stdin.buffer.read(1)\n"
    )
    with (
        logging.log_to(path),
        subprocess.Popen(
            [sys.executable, "-S", "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
        ) as process,
    ):
        assert process.stdin is not None and process.stdout is not None
        console = logging.Console("worker console")
        for index, fragment in enumerate(fragments):
            body = process.stdout.read(len(fragment))
            assert body == fragment
            console.write(body)
            if index < len(fragments) - 1:
                assert not path.exists()
            process.stdin.write(b"x")
        console.write(b"", final=True)
        assert process.wait(timeout=5) == 0
    text = path.read_text()
    assert "entry failed [redacted] [redacted URL] token=[redacted]" in text
    assert "sensitive" not in text and "private" not in text and "unknown" not in text
    assert "unlisted-credential" not in text


@pytest.mark.parametrize("oversized", [False, True])
def test_courier_redacts_across_console_refreshes_before_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-sensitive-value")
    console = tmp_path / "session.log"
    retained = tmp_path / "worker.log"
    courier = Courier(tmp_path, PostForm(url="unused", fields={}, prefix=""), "unused")
    with logging.log_to(retained):
        console.write_bytes((b"x" * LIMIT if oversized else b"error ") + b"fixture-sensitive-")
        courier.carry_out()
        assert not retained.exists()
        with console.open("ab") as output:
            output.write(b"value\nlast line without newline")
        courier.carry_out()
        assert "last line" not in retained.read_text()
        courier.carry_out(final=True)
        courier.carry_out(final=True)
    text = retained.read_text()
    assert "fixture-sensitive" not in text and "value" not in text
    assert text.count("last line without newline") == 1
    assert ("oversized console line omitted" if oversized else "error [redacted]") in text


@pytest.mark.parametrize("cleanup", [None, "[]"])
def test_worker_leaves_cleanup_to_parent_only_when_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str | None
) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "fixture-pod")
    monkeypatch.delenv("ETUDE_WORKER_LOG_URL", raising=False)
    deleted = tmp_path / "deleted"

    class Reply(io.BytesIO):
        status = 200
        headers: dict[str, str] = {}

    def read(request: Request, *, timeout: float) -> Reply:
        return Reply(b"{}")  # malformed bootstrap fails before a receipt or user code

    def delete(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        deleted.touch()
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr("urllib.request.urlopen", read)
    monkeypatch.setattr(subprocess, "run", delete)
    with pytest.raises(ValueError, match="Bootstrap"):
        work(None, "https://fixture/bootstrap", tmp_path, cleanup)
    assert deleted.exists() is (cleanup is None)


def test_follow_positions_survive_truncation_without_repeating_text() -> None:
    initial = "opening\n" + "x" * (LIMIT + 100)
    text = bounded(initial)
    opening, tail, omitted = portions(text)
    assert len(text.encode()) <= LIMIT and omitted > 0
    cursor = LogCursor()
    first = snapshot_stream("s", "a", "job", text.encode(), False)
    assert first.observed_at is not None
    snapshot = LogSnapshot(observed_at=first.observed_at, streams=(first,))
    output = cursor.render(snapshot)
    assert "opening" in output and "retention truncated" in output
    assert cursor.render(snapshot) == ""
    second = snapshot_stream("s", "a", "job", bounded(text + "new output\n").encode(), False)
    output = cursor.render(snapshot.model_copy(update={"streams": (second,)}))
    assert "new output" in output and "opening" not in output
    assert "x" * 100 not in output
    assert len(opening) + len(tail) + omitted == len(initial)


def test_log_reads_and_phases_never_drive_execution(tmp_path: Path) -> None:
    b = bench(tmp_path)
    run = b.scheduler.launch(job(), "l4")
    counting = Counting(b.store, b.service)
    with counting.record():
        b.scheduler.logs(run=run)
        count = counting.snapshot()
    assert count == Counts(bucket=3, database=17)
    with counting.record():
        assert b.scheduler.diagnostics(run=run)
        assert b.scheduler.start_phases(run)
        count = counting.snapshot()
    assert (
        Counts(provider=count.provider, bucket=count.bucket, ssh=count.ssh, git=count.git)
        == Counts()
    )
    assert b.service.creates == 1


def test_machine_view_excludes_job_output(tmp_path: Path) -> None:
    b = bench(tmp_path)
    machine = b.scheduler.rent("l4", 2)
    first = b.scheduler.launch(Run(id="first", job=job()), "l4")
    b.run(601)
    second = b.scheduler.launch(Run(id="second", job=job()), "l4")
    assert "log of" not in render(b.scheduler.logs(machine=machine))
    assert b.scheduler.attempts(first)[0].id not in job_text(b.scheduler.logs(run=second))
    assert b.scheduler.attempts(second)[0].id not in job_text(b.scheduler.logs(run=first))


def test_closed_bootstrap_is_final_even_without_receipt() -> None:
    stream = snapshot_stream(
        "s",
        None,
        "bootstrap",
        f"install failed\n{datetime.now(UTC).isoformat()} bootstrap: {CLOSED}\n".encode(),
        False,
    )
    assert stream.complete and stream.received


def test_child_console_cannot_close_its_parents_stream(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.log"
    with logging.log_to(path):
        logging.Console("worker console").write(f"bootstrap: {CLOSED}\n".encode())
        assert not snapshot_stream("s", None, "bootstrap", path.read_bytes(), False).complete
        logging.write_log("bootstrap", CLOSED)
    assert snapshot_stream("s", None, "bootstrap", path.read_bytes(), False).complete


def test_cached_partial_output_and_refresh_error_are_both_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.launch(job(), "l4")
    before = b.scheduler.logs(run=identity)

    def unavailable(*args: object, **kwargs: object) -> LogStream:
        raise OSError("storage denied")

    monkeypatch.setattr(b.service, "logs", unavailable)
    after = b.scheduler.logs(run=identity)
    assert job_text(after) == job_text(before)
    assert all(stream.retrieval_error == "OSError: storage denied" for stream in after.streams)
    assert [stream.observed_at for stream in after.streams] == [
        stream.observed_at for stream in before.streams
    ]
    assert not after.complete


def test_queue_is_not_mistaken_for_complete_logs(tmp_path: Path) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.queue(job(), "l4")
    snapshot = b.scheduler.logs(run=identity)
    assert snapshot.streams == () and not snapshot.complete
    assert b.service.creates == 0


@pytest.mark.parametrize("closed", [False, True])
def test_diagnostics_use_positions_without_showing_another_attempts_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    closed: bool,
) -> None:
    b = bench(tmp_path)
    b.scheduler.rent("l4", 2)
    first = b.scheduler.launch(job(), "l4")
    b.run(601)
    second = b.scheduler.launch(job(), "l4")
    previous = b.scheduler.attempts(first)[0]
    error = Diagnostic(
        id="previous-error",
        observed_at=b.clock(),
        operation="source fetch",
        message="other job's source failure",
        attempt=previous.id,
    )
    body = (
        f"startup\n{b.clock().isoformat()} diagnostic: {error.model_dump_json()}\nready\n"
    ).encode()

    def read(session_id: str, attempt: str | None = None, *, producer: str = "job") -> LogStream:
        return snapshot_stream(
            session_id, attempt, producer, body if producer == "worker" else b"", closed
        )

    monkeypatch.setattr(b.service, "logs", read)
    b.clock.advance(1)
    snapshot = b.scheduler.logs(run=second)
    assert "other job" not in render(snapshot)
    assert all(error.id != d.id for d in snapshot.diagnostics)
    assert all(not stream.diagnostics for stream in snapshot.streams)
    b.store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)
    after = scheduler.logs(run=first)
    assert "other job" in render(after)
    (retained,) = (d for d in after.diagnostics if d.id == error.id)
    assert retained.produced_at == error.observed_at
    assert retained.observed_at == b.clock()
    assert "other job" not in render(scheduler.logs(run=second))


def test_bootstrap_publication_is_one_bounded_request_and_keeps_local_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bootstrap.log"
    path.write_text("install failed\n")
    received: list[bytes] = []
    monkeypatch.setenv("ETUDE_LOG_SECONDS", "2")
    allowances: list[float] = []

    def denied(request: Request, *, timeout: float) -> io.BytesIO:
        assert isinstance(request.data, bytes)
        received.append(request.data)
        allowances.append(timeout)
        raise OSError("storage unavailable")

    monkeypatch.setattr(logging, "urlopen", denied)
    logging.publish(path, "https://fixture/write-only", timeout=2)
    assert received == [b"install failed\n"]
    assert len(allowances) == 1 and 0 < allowances[0] <= 2
    assert path.read_text() == "install failed\n"


def test_client_chosen_id_freezes_source_before_admission(
    tmp_path: Path,
    repository: tuple[Path, Path],  # noqa: F811
) -> None:
    repo, _ = repository
    b = bench(tmp_path)
    identity = b.scheduler.queue(Run(id="my-scientific-id", job=job(repo=str(repo))), "l4")
    record = b.scheduler.record(identity)
    assert record.id == "my-scientific-id"
    assert record.source is not None and record.spec.revision == record.source.commit
    assert record.spec.revision != "HEAD"


def test_remote_preparation_failure_keeps_controller_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    launch = b.scheduler.prepare_launch((Run(id="remote-id", job=job()),), "l4")
    b.scheduler.fail_launch(launch.id, "source publication failed: denied")
    assert "source publication failed" in render(b.scheduler.logs(run="remote-id"))
    assert b.store.runs() == () and b.service.creates == 0
    assert b.scheduler.launch(Run(id="remote-id", job=job()), "l4") == "remote-id"


@pytest.mark.parametrize("failure", ["install", "spawn", "import", "entry"])
@pytest.mark.parametrize("storage_denied", [False, True])
def test_bootstrap_failure_remains_readable_after_deletion_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    storage_denied: bool,
) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.launch(Run(id="install-failure", job=job(max_attempts=1)), "l4")
    session = b.scheduler.session(b.scheduler.attempts(identity)[0].session)
    marker = tmp_path / "guardian-started"
    monkeypatch.setattr(pod, "GUARDIAN_MARKER", marker)
    for name, value in {
        "RUNPOD_POD_ID": "fixture-pod",
        "RUNPOD_API_KEY": "fixture-key",
        "ETUDE_SESSION": session.id,
        "ETUDE_PACKAGE_URL": "https://fixture/package",
        "ETUDE_PACKAGE_SHA256": hashlib.sha256(b"package").hexdigest(),
        "ETUDE_BOOTSTRAP_LOG_URL": "https://fixture/bootstrap.log",
        "ETUDE_BOOTSTRAP_URL": "https://fixture/bootstrap",
        "FIXTURE_TOKEN": "fixture-sensitive-value",
    }.items():
        monkeypatch.setenv(name, value)

    class Timer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            marker.write_text("guardian active")

        def poll(self) -> int | None:
            return None

    def download(url: str, *, timeout: float) -> io.BytesIO:
        return io.BytesIO(b"package")

    def install(code: bytes) -> Path:
        if failure == "install":
            raise subprocess.CalledProcessError(
                17, ["uv"], stderr=b"fixture dependency unavailable"
            )
        return tmp_path

    def delete(api: pod.GuardianAPI, machine: str) -> None:
        b.service.kill(session.id)

    uploaded: list[bytes] = []
    monkeypatch.setenv("ETUDE_LOG_SECONDS", "2")
    allowances: list[float] = []

    def upload(request: Request, *, timeout: float) -> io.BytesIO:
        assert isinstance(request.data, bytes)
        allowances.append(timeout)
        if storage_denied:
            raise OSError("log storage denied")
        uploaded.append(request.data)
        return io.BytesIO()

    def read(session_id: str, attempt: str | None = None, *, producer: str = "job") -> LogStream:
        return snapshot_stream(
            session_id,
            attempt,
            producer,
            uploaded[-1] if uploaded and producer == "bootstrap" else None,
            False,
        )

    popen = subprocess.Popen
    cursor = LogCursor()
    earlier = ""

    def start(command: list[str], **kwargs: object) -> Timer | subprocess.Popen[bytes]:
        nonlocal earlier
        if command[-1] == "guard":
            return Timer()
        log = marker.parent / "sessions" / session.id / "bootstrap.log"
        logging.publish(log, "https://fixture/bootstrap.log")
        snapshot = b.scheduler.logs(run=identity)
        assert not snapshot.streams[0].complete
        earlier = cursor.render(snapshot)
        if failure == "spawn":
            raise FileNotFoundError("installed worker unavailable fixture-sensitive-value")
        code = (
            "import missing_worker_dependency"
            if failure == "import"
            else "import sys; sys.stderr.write('worker entry failed fixture-sensitive-value'); "
            "sys.exit(23)"
        )
        return popen(
            [sys.executable, "-S", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )

    monkeypatch.setattr(b.service, "logs", read)
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(pod, "_start_ssh", lambda: None)
    monkeypatch.setattr(pod, "ensure_uv", lambda: None)
    monkeypatch.setattr(
        pod, "send", lambda method, url, body, headers: (200, download(url, timeout=60).read())
    )
    monkeypatch.setattr(pod, "install", install)
    monkeypatch.setattr(pod.GuardianAPI, "delete_self", delete)
    monkeypatch.setattr(logging, "urlopen", upload)
    assert pod.main(["pod", "boot"]) == (23 if failure == "entry" else 1)
    assert len(allowances) == (1 if failure == "install" else 2)
    assert all(0 < allowance <= 2 for allowance in allowances)
    b.run(1)
    assert b.store.machine(session.machine).deleted_at is not None
    b.store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)

    def unavailable(*args: object, **kwargs: object) -> LogStream:
        raise OSError("storage now unavailable")

    monkeypatch.setattr(b.service, "logs", unavailable)
    snapshot = scheduler.logs(run=identity)
    output = render(snapshot)
    expected = {
        "install": "fixture dependency unavailable",
        "spawn": "installed worker unavailable",
        "import": "ModuleNotFoundError: No module named 'missing_worker_dependency'",
        "entry": "worker entry failed",
    }[failure]
    local = (marker.parent / "sessions" / session.id / "bootstrap.log").read_text()
    assert expected in local and "fixture-sensitive-value" not in local
    assert (expected in output) is not storage_denied
    if failure in {"install", "import", "entry"}:
        status = {"install": 17, "import": 1, "entry": 23}[failure]
        assert f"exit status {status}" in local
    assert "fixture-sensitive-value" not in output
    assert "machine deletion confirmed" in output and "output not received" in output
    assert snapshot.streams[0].complete is not storage_denied
    fresh = cursor.render(snapshot)
    assert (expected in fresh) is not storage_denied
    if earlier and not storage_denied:
        assert "starting installed client" in earlier and "starting installed client" not in fresh
    assert (
        any(expected in (phase.evidence or "") for phase in scheduler.start_phases(identity))
        is not storage_denied
    )
    assert b.service.creates == 1


def test_cleanup_failure_retains_reason_and_never_claims_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.launch(job(), "l4")

    def unavailable(session: str) -> None:
        raise OSError("deletion endpoint unavailable")

    monkeypatch.setattr(b.service, "end", unavailable)
    b.run(601)
    snapshot = b.scheduler.logs(run=identity)
    assert "deletion endpoint unavailable" in render(snapshot)
    assert all(session.ended_at is None for session in b.store.sessions())
    assert b.service.creates == 1


def test_expired_log_read_uses_cache_without_starting_another_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.launch(job(), "l4")
    before = b.scheduler.logs(run=identity)
    clock = iter([0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr("etude.launch.deadline.time.monotonic", lambda: next(clock))
    counting = Counting(b.store, b.service)
    with counting.record():
        after = b.scheduler.logs(run=identity, timeout=1)
        count = counting.snapshot()
    assert job_text(before) == job_text(after)
    assert all(
        stream.retrieval_error and "time limit" in stream.retrieval_error
        for stream in after.streams
    )
    assert count.provider == 0 and count.bucket == 0


def test_failure_after_atomic_admission_does_not_relabel_it_as_failed_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)

    def broken(session: str) -> Observation:
        raise RuntimeError("unexpected delivery failure")

    monkeypatch.setattr(b.service, "observe", broken)
    with pytest.raises(LaunchError, match="run admitted; delivery unconfirmed"):
        b.scheduler.launch(Run(id="admitted", job=job()), "l4")
    assert b.store.run("admitted").id == "admitted"
    assert b.store.launches("admitted")[0].state == "admitted"
    with pytest.raises(LauncherError, match="already admitted"):
        b.scheduler.launch(Run(id="admitted", job=job()), "l4")
    assert b.service.creates == 1


def test_log_capabilities_cover_final_delivery_but_not_extra_rental_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 17, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> FrozenDateTime:
            return cls(2026, 10, 17, tzinfo=tz)

    monkeypatch.setattr("etude.launch.storage.bucket.datetime", FrozenDateTime)
    storage = MemoryStorage()
    bucket = Bucket(
        "fixture",
        "us-east-1",
        Keys(access_key="fixture", secret_key="fixture"),
        storage,
        clock=lambda: now,
    )
    journal = BucketJournal(
        StorageRecord(name="fixture", bucket="fixture", prefix="p", region="us-east-1"), bucket
    )
    packet = Packet(
        session="s",
        deadline=now + timedelta(seconds=180),
        cap_usd=Decimal(1),
        work=(),
        kept=False,
        autorun=True,
    )
    links = journal.prepare(packet, expires_at=now + timedelta(seconds=300))
    for url in (links.log_url, links.worker_log_url):
        assert parse_qs(url.split("?", 1)[1])["X-Amz-Expires"] == ["300"]
    assert len(storage.asked) == 3  # packet, inbox and bootstrap; signing makes no requests


@pytest.mark.parametrize("failure", ["receipt", "loop", "console", "none"])
def test_courier_owns_child_through_failure_cleanup_and_cached_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    b = bench(tmp_path)
    identity = b.scheduler.launch(Run(id="courier-failure", job=job(max_attempts=1)), "l4")
    session = b.scheduler.session(b.scheduler.attempts(identity)[0].session)
    root = tmp_path / "sessions"
    home = root / session.id
    log = home / "worker.log"
    packet = Packet(session=session.id, deadline=None, work=(), log_seconds=2)
    bootstrap = Bootstrap.model_validate(
        {
            "packet": packet.model_dump_json(),
            "form": PostForm(url="unused", fields={}, prefix=""),
            "inbox_url": "unused",
        }
    )
    monkeypatch.setenv("ETUDE_SESSION", session.id)
    monkeypatch.setenv("ETUDE_WORKER_LOG_URL", "https://fixture/worker.log")
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-sensitive-value")
    monkeypatch.delenv("ETUDE_DEADLINE", raising=False)
    uploaded: list[bytes] = []
    receipts: list[str] = []
    monkeypatch.setenv("ETUDE_LOG_SECONDS", "2")
    allowances: list[float] = []

    def upload(request: Request, *, timeout: float) -> io.BytesIO:
        assert isinstance(request.data, bytes)
        uploaded.append(request.data)
        allowances.append(timeout)
        return io.BytesIO()

    def read(url: str, send: object) -> bytes:
        return bootstrap.model_dump_json(round_trip=True).encode() if url == "bootstrap" else b"{}"

    def post(form: PostForm, key: str, body: bytes, send: object) -> None:
        receipts.append(key)

    def logs(session_id: str, attempt: str | None = None, *, producer: str = "job") -> LogStream:
        return snapshot_stream(
            session_id,
            attempt,
            producer,
            uploaded[-1] if uploaded and producer == "worker" else None,
            False,
        )

    popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []
    cursor = LogCursor()
    earlier: list[str] = []

    def start(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin: int,
        stdout: IO[bytes],
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        # Readiness precedes the failing receipt: the real child is alive and
        # able to emit its final unterminated line when its owner stops it.
        ready, announce = os.pipe()
        code = """
import os, signal, sys
from pathlib import Path

def finish(signum, frame):
    sys.stderr.write('late session evidence fixture-sensitive-value')
    if sys.argv[2] == 'console':
        Path('session.log').unlink()
    sys.exit(23)

signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGUSR1, finish)
os.write(int(sys.argv[1]), b'R')
os.close(int(sys.argv[1]))
signal.pause()
"""
        try:
            child = popen(
                [sys.executable, "-S", "-c", code, str(announce), failure],
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                pass_fds=(announce,),
            )
            children.append(child)
            assert select.select([ready], [], [], 5)[0], "child did not become ready"
            assert os.read(ready, 1) == b"R"
        finally:
            os.close(ready)
            os.close(announce)
        return child

    original = OSError("local receipt failed fixture-sensitive-value")

    def persist(path: Path, text: str) -> None:
        if path.name == "receipt":
            assert receipts == ["receipt"]  # durable start intent precedes child execution
            logging.publish(log, "https://fixture/worker.log")
            snapshot = b.scheduler.logs(run=identity)
            assert not next(s for s in snapshot.streams if s.producer == "worker").complete
            earlier.append(cursor.render(snapshot))
            if failure in {"receipt", "console"}:
                raise original
            if failure == "none":
                children[0].send_signal(signal.SIGUSR1)
        write(path, text)

    def bring_in(self: Courier) -> None:
        if failure == "loop":
            raise RuntimeError("courier loop failed fixture-sensitive-value")

    def delete() -> None:
        assert children[0].returncode == 23
        local = log.read_text()
        if failure == "console":
            assert CLOSED not in local and "collection incomplete" in local
        else:
            assert "late session evidence [redacted]" in local
            assert "exit status 23" in local and local.rstrip().endswith(CLOSED)
        b.service.kill(session.id)
        if failure == "receipt":
            raise OSError("cleanup reply unavailable")  # cannot replace the receipt error

    monkeypatch.setattr(logging, "urlopen", upload)
    monkeypatch.setattr(courier_module, "read", read)
    monkeypatch.setattr(courier_module, "post", post)
    monkeypatch.setattr(courier_module, "write", persist)
    monkeypatch.setattr(Courier, "bring_in", bring_in)
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(b.service, "logs", logs)
    try:
        if failure in {"receipt", "console"}:
            with pytest.raises(OSError) as caught:
                courier_module.run("bootstrap", delete, root, every=0.1)
            assert caught.value is original
        elif failure == "loop":
            with pytest.raises(RuntimeError, match="courier loop failed"):
                courier_module.run("bootstrap", delete, root, every=0.1)
        else:
            courier_module.run("bootstrap", delete, root, every=0.1)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    assert receipts == (["receipt"] if failure == "console" else ["receipt", "ended"])
    assert len(uploaded) == 2 and all(0 < allowance <= 2 for allowance in allowances)
    # Restart with the earlier open snapshot still cached, then refresh final output.
    b.store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)
    after = scheduler.logs(run=identity)
    worker = next(s for s in after.streams if s.producer == "worker")
    assert worker.complete is (failure != "console")
    output = render(after)
    assert "exit status 23" in output and "fixture-sensitive-value" not in output
    if failure in {"receipt", "console"}:
        assert "local receipt failed [redacted]" in output
    elif failure == "loop":
        assert "courier loop failed [redacted]" in output
    if failure == "console":
        assert "collection incomplete" in output
    else:
        assert "late session evidence [redacted]" in output
        fresh = cursor.render(after)
        assert "late session evidence" in fresh
        assert "client ready" in earlier[0] and "client ready" not in fresh
    scheduler.tick()
    assert store.machine(session.machine).deleted_at is not None
    store.close()
    store = Database(tmp_path / "launcher.db")
    scheduler = Scheduler(store, {"runpod": lambda pool, machine: b.service}, clock=b.clock)

    def unavailable(*args: object, **kwargs: object) -> LogStream:
        raise OSError("storage unavailable after cleanup")

    monkeypatch.setattr(b.service, "logs", unavailable)
    retained = scheduler.logs(run=identity)
    assert "exit status 23" in render(retained)
    assert next(s for s in retained.streams if s.producer == "worker").complete is (
        failure != "console"
    )
    assert b.service.creates == 1


def test_courier_leaves_collection_incomplete_when_child_exit_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = Packet(session="unconfirmed", deadline=None, work=())
    bootstrap = Bootstrap.model_validate(
        {
            "packet": packet.model_dump_json(),
            "form": PostForm(url="unused", fields={}, prefix=""),
            "inbox_url": "unused",
        }
    )
    monkeypatch.setenv("ETUDE_SESSION", packet.session)
    monkeypatch.setenv("ETUDE_DEADLINE", (datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    monotonic = time.monotonic
    elapsed = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + elapsed)
    monkeypatch.delenv("ETUDE_WORKER_LOG_URL", raising=False)
    waits: list[float] = []
    signals: list[str] = []
    receipts: list[str] = []
    cleaned: list[bool] = []

    class Unconfirmed:
        pid = 123

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            signals.append("terminate")

        def kill(self) -> None:
            signals.append("kill")

        def wait(self, timeout: float) -> int:
            waits.append(timeout)
            raise subprocess.TimeoutExpired(["fixture-sensitive-argv"], timeout)

    def start(*args: object, **kwargs: object) -> Unconfirmed:
        nonlocal elapsed
        elapsed = 2.0
        (tmp_path / packet.session / "session.log").write_bytes(
            b"last available line\npending credential fragment"
        )
        return Unconfirmed()

    def read(url: str, send: object) -> bytes:
        return bootstrap.model_dump_json(round_trip=True).encode()

    def post(form: PostForm, key: str, body: bytes, send: object) -> None:
        receipts.append(key)

    original = OSError("local receipt unavailable")

    def persist(path: Path, text: str) -> None:
        if path.name == "receipt":
            raise original
        write(path, text)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(courier_module, "read", read)
    monkeypatch.setattr(courier_module, "post", post)
    monkeypatch.setattr(courier_module, "write", persist)
    with pytest.raises(OSError) as caught:
        courier_module.run("bootstrap", lambda: cleaned.append(True), tmp_path)
    assert caught.value is original
    assert cleaned == [True] and receipts == ["receipt"]
    assert signals == ["terminate", "kill"] and waits == [0, 0]
    stream = snapshot_stream(
        packet.session,
        None,
        "worker",
        (tmp_path / packet.session / "worker.log").read_bytes(),
        False,
    )
    assert not stream.complete
    assert "collection incomplete" in stream.text and "exit status" not in stream.text
    assert "last available line" in stream.text and "pending credential fragment" not in stream.text
    assert "fixture-sensitive-argv" not in stream.text
    assert "local receipt unavailable" in (tmp_path / packet.session / "worker.log").read_text()
