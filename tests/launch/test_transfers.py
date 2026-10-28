"""Real local HTTP proves observed bytes, resumable reuse and offline accounting."""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import subprocess
import tarfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from moto import mock_aws

from etude import cache as engine
from etude.cache import Artifact, Cache, Content, Fetched
from etude.deadline import limit_time, remaining
from etude.launch import client
from etude.launch.database import Database
from etude.launch.logging import log_to
from etude.launch.providers.runpod import pod
from etude.launch.records import Job, Output, Storage
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import (
    WORKER_LOG,
    AttemptRecord,
    Delivery,
    Inbox,
    Progress,
    Result,
    WorkerLog,
    attempt_log,
    log_piece,
)
from etude.launch.storage import download as downloads
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.download import Download, download
from etude.launch.storage.files import RESULT
from etude.launch.storage.s3 import (
    Bucket,
    Grant,
    Keys,
    PostForm,
    Reply,
    StorageUnavailable,
    _send,
)
from etude.launch.storage.transport import attribute, observe
from etude.launch.usage import R2, S3_US_WEST_2, Access, Transfer, Usage, reconcile
from etude.launch.worker.courier import Courier
from etude.run.storage import S3Storage
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench
from tests.run.storage import CountedClient


class HTTP:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {"/payload": b"abcdefghijkl"}
        self.uploads: dict[str, bytes] = {}
        self.received = 0
        self.requests = 0
        self.posts = 0
        self.ranges: list[str] = []
        self.fault = ""
        self.etag = '"version-one"'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests += 1
                path = urlsplit(self.path).path
                body = owner.objects.get(path)
                status = 200 if body is not None else 404
                body = body or b""
                # Like S3's, an ETag changes with the object's bytes.
                etag = f'{owner.etag[:-1]}-{hashlib.md5(body).hexdigest()}"'
                requested = self.headers.get("Range")
                content_range = None
                if status == 200 and self.headers.get("If-None-Match") == etag:
                    status, body = 304, b""  # as S3 answers a current version
                if requested:
                    owner.ranges.append(requested)
                    start, end = map(int, requested.removeprefix("bytes=").split("-"))
                    content_range = f"bytes {start}-{end}/{len(body)}"
                    body = body[start : end + 1]
                    status = 206
                    if owner.fault == "ignored":
                        status = 200
                    elif owner.fault == "wrong-range":
                        content_range = "bytes 1-4/12"
                    elif owner.fault == "denied":
                        status = 403
                        body = b""
                    elif owner.fault == "timeout":
                        time.sleep(0.1)
                    elif owner.fault == "transient":
                        status = 503
                        body = b"busy"
                        owner.fault = ""
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", etag)
                self.send_header("x-amz-request-id", f"request-{owner.requests}")
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                if owner.fault == "interrupted" and requested == "bytes=4-7":
                    body = body[:2]
                    self.close_connection = True
                try:
                    owner.received += len(body)
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self) -> None:
                owner.posts += 1
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.uploads[self.path] = body
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
                )
                fields = {
                    part.get_param("name", header="content-disposition"): part.get_payload(
                        decode=True
                    )
                    for part in message.iter_parts()
                }
                key, payload = fields.get("key"), fields.get("file")
                if isinstance(key, bytes) and isinstance(payload, bytes):
                    owner.objects["/" + key.decode()] = payload
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def source(self, path: str = "/payload") -> Download:
        body = self.objects[path]
        return Download(
            url=self.url + path + "?X-Amz-Signature=fixture-private-query",
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )

    def send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        return _send(method, self.url + urlsplit(url).path, body, headers)


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HTTP]:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    fixture = HTTP()
    try:
        yield fixture
    finally:
        fixture.server.shutdown()
        fixture.server.server_close()
        fixture.thread.join()


def test_ranges_are_read_as_concurrent_parts_and_ahead_observed_on_the_calling_thread(
    http: HTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloads, "PART_BYTES", 4)
    observed: list[tuple[int, str]] = []
    cache = Cache(tmp_path / "cache", range_bytes=8)  # the second range is read ahead
    with observe(lambda raw: observed.append((threading.get_ident(), raw))):
        result = download(http.source(), tmp_path / "result", cache=cache)
    assert result.read_bytes() == b"abcdefghijkl"
    assert sorted(http.ranges) == ["bytes=0-3", "bytes=4-7", "bytes=8-11"]
    assert {thread for thread, _ in observed} == {threading.get_ident()}
    assert sum(Transfer.model_validate_json(raw).received_bytes for _, raw in observed) == 12


def test_cold_warm_concurrent_and_new_process_retrieval(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", range_bytes=4)
    events: list[Transfer] = []
    with observe(lambda raw: events.append(Transfer.model_validate_json(raw))):
        download(http.source(), tmp_path / "first", cache=cache)
        download(http.source(), tmp_path / "second", cache=cache)
    assert (sum(e.received_bytes for e in events), sum(e.reused_bytes for e in events)) == (12, 12)
    assert (http.requests, http.received) == (3, 12)
    script = """from pathlib import Path
import sys
from etude.cache import Cache
from etude.launch.storage import download as downloads
from etude.launch.storage.download import Download, download
download(Download.model_validate_json(sys.argv[2]), Path(sys.argv[3]), cache=Cache(Path(sys.argv[1])))
"""
    subprocess.run(
        [
            "uv",
            "run",
            "--offline",
            "python",
            "-c",
            script,
            str(cache.root),
            http.source().model_dump_json(),
            str(tmp_path / "third"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert (tmp_path / "third").read_bytes() == b"abcdefghijkl"
    assert (http.requests, http.received) == (3, 12)
    another = Cache(tmp_path / "concurrent", range_bytes=4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(
            pool.map(lambda n: download(http.source(), tmp_path / str(n), cache=another), [1, 2])
        )
    assert [p.read_bytes() for p in paths] == [b"abcdefghijkl"] * 2
    assert (http.requests, http.received) == (6, 24)
    assert (
        "fixture-private-query"
        not in Usage(events=tuple(events), observed_at=None).model_dump_json()
    )


def test_interrupted_ranges_count_partial_bytes_and_resume(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", range_bytes=4)
    events: list[Transfer] = []
    http.fault = "interrupted"
    with observe(lambda raw: events.append(Transfer.model_validate_json(raw))):
        with pytest.raises(StorageUnavailable):
            download(http.source(), tmp_path / "result", retries=0, cache=cache)
        assert not (tmp_path / "result").exists()
        assert sum(e.received_bytes for e in events) == 6
        assert not events[-1].completed
        http.fault = ""
        download(http.source(), tmp_path / "result", cache=cache)
    assert (tmp_path / "result").read_bytes() == b"abcdefghijkl"
    assert http.ranges == ["bytes=0-3", "bytes=4-7", "bytes=4-7", "bytes=8-11"]
    assert sum(e.received_bytes for e in events) == 14
    assert sum(e.reused_bytes for e in events) == 4
    assert Usage(events=tuple(events), observed_at=None).retransferred_bytes == 2
    assert (http.requests, http.received) == (4, 14)


@pytest.mark.parametrize("fault", ["ignored", "wrong-range", "denied", "timeout", "digest"])
def test_failures_preserve_existing_destination(http: HTTP, tmp_path: Path, fault: str) -> None:
    source = http.source()
    target = tmp_path / "result"
    target.write_bytes(b"previous verified result")
    http.fault = fault
    if fault == "digest":
        source = source.model_copy(update={"sha256": "0" * 64})
    with pytest.raises((StorageUnavailable, TimeoutError)):
        download(
            source, target, timeout=0.05, retries=0, cache=Cache(tmp_path / "cache", range_bytes=4)
        )
    assert target.read_bytes() == b"previous verified result"


def test_changed_identity_and_corrupt_retained_range_fail(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", range_bytes=4)
    http.fault = "interrupted"
    with pytest.raises(StorageUnavailable):
        download(http.source(), tmp_path / "result", retries=0, cache=cache)
    http.fault = ""
    http.etag = '"version-two"'
    with pytest.raises(StorageUnavailable, match="identity"):
        download(http.source(), tmp_path / "result", cache=cache)
    partial = next(cache.root.glob("*.partial"))
    with partial.open("r+b") as stream:
        stream.write(b"corrupt")
    with pytest.raises(StorageUnavailable, match="corrupt"):
        download(http.source(), tmp_path / "result", cache=cache)
    assert not (tmp_path / "result").exists()


def test_bounded_cache_and_oversized_download(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", max_bytes=12, range_bytes=4)
    download(http.source(), tmp_path / "a", cache=cache)
    http.objects["/other"] = b"mnopqrstuvwx"
    download(http.source("/other"), tmp_path / "b", cache=cache)
    payloads = [
        p for p in cache.root.iterdir() if p.is_file() and p.suffix not in {".lock", ".json"}
    ]
    assert sum(p.stat().st_size for p in payloads) <= 12
    http.objects["/large"] = b"z" * 25
    destination = tmp_path / "large"
    destination.write_bytes(b"previous verified result")
    assert download(http.source("/large"), destination, cache=cache).read_bytes() == b"z" * 25
    assert destination.stat().st_mode & 0o777 == 0o600
    # Uncacheable, yet fetched again to where it already is, it is not downloaded again.
    received = http.received
    assert download(http.source("/large"), destination, cache=cache).read_bytes() == b"z" * 25
    assert http.received == received
    assert not list(tmp_path.glob(".*.partial"))
    assert not list(tmp_path.glob(".*.json"))


def test_download_inherits_caller_allowance(http: HTTP, tmp_path: Path) -> None:
    allowances: list[float | None] = []

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        allowances.append(remaining())
        return http.send(method, url, body, headers)

    cache = Cache(tmp_path / "cache")
    assert (
        download(http.source(), tmp_path / "unlimited", send=send, cache=cache).read_bytes()
        == b"abcdefghijkl"
    )
    home = tmp_path / "sessions" / "session-one"
    home.mkdir(parents=True)
    with limit_time(600):
        courier = Courier(
            home,
            Grant(prefix="out/", form=PostForm(url=http.url + "/upload", fields={})),
            "unused",
            send,
        )
        courier.fetch({"session-one/sources/source.tar": http.source()})
    assert allowances[0] is None
    assert all(left is not None and 300 < left <= 600 for left in allowances[1:])


def test_outer_deadline_cannot_be_extended(http: HTTP, tmp_path: Path) -> None:
    http.fault = "timeout"
    with limit_time(0.02), pytest.raises((StorageUnavailable, TimeoutError)):
        download(http.source(), tmp_path / "result", timeout=30, cache=Cache(tmp_path / "cache"))
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("phase", ["digest", "copy"])
def test_local_expiry_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm: bool, phase: str
) -> None:
    payload = b"verified payload"
    source = Download(
        url="https://fixture/payload",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    cache = Cache(tmp_path / "cache")

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        return Reply(
            status=206,
            body=payload,
            headers={
                "content-range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
                "content-length": str(len(payload)),
                "etag": '"one"',
            },
        )

    if warm:
        download(source, tmp_path / "first", send=send, cache=cache)
    target = tmp_path / "result"
    target.write_bytes(b"previous result")
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    if phase == "digest":
        digest = engine._digest

        def expire_digest(path: Path) -> str:
            result = digest(path)
            clock[0] = 2.0
            return result

        monkeypatch.setattr(engine, "_digest", expire_digest)
    else:
        fsync = os.fsync

        def expire_copy(fd: int) -> None:
            fsync(fd)
            # Only the destination-side temporary file expires, after a real copy.
            inode = os.fstat(fd).st_ino
            if any(p.is_file() and p.stat().st_ino == inode for p in tmp_path.iterdir()):
                clock[0] = 2.0

        monkeypatch.setattr(os, "fsync", expire_copy)
    with pytest.raises(TimeoutError):
        download(source, target, timeout=1, send=send, cache=cache)
    assert target.read_bytes() == b"previous result"
    assert {p.name for p in tmp_path.iterdir()} == (
        {"cache", "result", "first"} if warm else {"cache", "result"}
    )


def test_transient_range_http_retains_both_attempts(http: HTTP, tmp_path: Path) -> None:
    http.fault = "transient"
    events: list[Transfer] = []
    with observe(lambda raw: events.append(Transfer.model_validate_json(raw))):
        result = download(http.source(), tmp_path / "result", cache=Cache(tmp_path / "cache"))
    assert result.read_bytes() == b"abcdefghijkl"
    assert (http.requests, http.received) == (2, 16)
    assert [(e.status, e.received_bytes, e.request_id) for e in events] == [
        (503, 4, "request-1"),
        (206, 12, "request-2"),
    ]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_range_retries_share_one_budget(tmp_path: Path, status: int) -> None:
    payload = b"abcdefghijkl"
    source = Download(
        url="https://fixture/payload", sha256=hashlib.sha256(payload).hexdigest(), size_bytes=12
    )
    ranges: list[str] = []

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        ranges.append(headers["Range"])
        if len(ranges) % 2:
            return Reply(status=status, body=b"busy")
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        return Reply(
            status=206,
            body=payload[start : end + 1],
            headers={
                "content-range": f"bytes {start}-{end}/12",
                "content-length": str(end - start + 1),
                "etag": '"one"',
            },
        )

    target = tmp_path / "result"
    with pytest.raises(StorageUnavailable):
        download(
            source, target, send=send, retries=2, cache=Cache(tmp_path / "cache", range_bytes=4)
        )
    assert ranges == ["bytes=0-3", "bytes=0-3", "bytes=4-7", "bytes=4-7", "bytes=8-11"]
    assert not target.exists()


def test_cost_scenario_and_safe_access_reconciliation(http: HTTP, tmp_path: Path) -> None:
    events: list[Transfer] = []
    cache = Cache(tmp_path / "cache", range_bytes=4)
    with (
        observe(lambda raw: events.append(Transfer.model_validate_json(raw))),
        attribute(purpose="result"),
    ):
        download(http.source(), tmp_path / "cold", cache=cache)
        cold = Usage(events=tuple(events), observed_at=datetime.now(UTC))
        events.clear()
        download(http.source(), tmp_path / "warm", cache=cache)
    warm = Usage(events=tuple(events), observed_at=datetime.now(UTC))
    assert (cold.received_bytes, cold.requests, warm.received_bytes, warm.requests) == (12, 3, 0, 0)
    # Three reads and 12 bytes out: S3 bills the egress, R2 only the reads.
    assert cold.estimate(S3_US_WEST_2) == Decimal("0.00000120108")
    assert cold.estimate(R2) == Decimal("0.00000108")
    assert warm.estimate(S3_US_WEST_2) == warm.estimate(R2) == 0
    first = cold.events[0]
    row = Access(
        backend=first.backend,
        request_id=first.request_id or "",
        key=first.key,
        response_bytes=4,
        status=206,
    )
    missing = row.model_copy(update={"request_id": "unmatched", "response_bytes": 7})
    reconciled = reconcile(cold, [row, row, missing])
    assert reconciled.matched == (row,)
    assert reconciled.unmatched_provider == (missing,)
    assert len(reconciled.unmatched_client) == 2
    complete = reconcile(
        cold,
        [
            Access(
                backend=e.backend,
                request_id=e.request_id or "",
                key=e.key,
                response_bytes=e.received_bytes,
                status=206,
            )
            for e in cold.events
        ],
    )
    assert len(complete.matched) == 3
    assert complete.unmatched_client == ()
    assert complete.unmatched_provider == ()
    assert "fixture-private-query" not in reconciled.model_dump_json()


def test_runtime_bootstrap_failure_reaches_retained_session_usage(
    http: HTTP,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b = bench(tmp_path)
    run = b.scheduler.launch(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
    session = b.scheduler.attempts(run)[0].session
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(pod, "UV_URL", http.url + "/payload")
    log = tmp_path / "bootstrap.log"
    with log_to(log), attribute(session=session), pytest.raises(ValueError, match="checksum"):
        pod.ensure_uv()
    http.objects[f"/p/sessions/{session}/out/bootstrap.log"] = log.read_bytes()
    journal = BucketJournal(
        Storage(name="test", bucket="test", provider="s3", region="r", prefix="p"),
        Bucket("test", "r", Keys(access_key="a", secret_key="s"), http.send),
    )
    monkeypatch.setattr(b.service, "observe", journal.observe)
    b.run(1)
    report = b.scheduler.usage(run=run)
    assert sum(e.received_bytes for e in report.shared_events if e.purpose == "runtime") == 12
    assert len([e for e in b.store.transfers() if e.purpose == "runtime"]) == 1
    b.run(1)
    assert len([e for e in b.store.transfers() if e.purpose == "runtime"]) == 1


def test_completed_workload_before_and_after(
    http: HTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real transport and publication; only provisioning and job execution are fake.
    b = bench(tmp_path, max_machines=1)
    b.scheduler.rent("l4", 1)
    runs = b.scheduler.queue_cohort(
        "experiment", Job(command="true", max_hours=0.25, max_usd=Decimal(1)).runs(1, 2), rent="l4"
    )
    b.scheduler.tick()
    first = b.scheduler.attempts(runs[0])[0]
    session = first.session
    packet = b.service.sessions[session].packet.model_copy(
        update={"work": tuple(w for w, _ in b.service.sessions[session].work())}
    )
    root = f"p/sessions/{session}/"
    http.objects["/" + root + "packet.json"] = packet.model_dump_json().encode()
    http.objects["/" + root + "out/receipt"] = b"started"
    http.objects["/source"] = b"source" * (1024**2 // 3)
    http.objects["/checkpoint"] = b"c" * 1024**2
    payload = b"x" * (24 * 1024**2)

    # Exercise the stock-image runtime path without writing to /usr/local/bin.
    archive = io.BytesIO()
    executable = b"synthetic uv executable"
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        member.size = len(executable)
        tar.addfile(member, io.BytesIO(executable))
    http.objects["/runtime"] = archive.getvalue()
    bootstrap_log = tmp_path / "bootstrap.log"
    with monkeypatch.context() as runtime:
        runtime.setattr("shutil.which", lambda name: None)
        runtime.setattr(pod, "UV_URL", http.url + "/runtime")
        runtime.setattr(pod, "UV_SHA256", hashlib.sha256(archive.getvalue()).hexdigest())
        runtime.setattr(pod, "Path", lambda name: tmp_path / "uv")
        with log_to(bootstrap_log), attribute(session=session):
            pod.ensure_uv()
    assert (tmp_path / "uv").read_bytes() == executable
    http.objects["/" + root + "out/bootstrap.log"] = bootstrap_log.read_bytes()

    # A failed startup input is published before recovery. The two allocations
    # share one source destination; identical checkpoint inputs reuse one digest.
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "worker-cache"))
    home = tmp_path / "worker/sessions" / session
    home.mkdir(parents=True)
    courier = Courier(
        home,
        Grant(prefix=root + "out/", form=PostForm(url=http.url + "/upload", fields={})),
        http.url + "/inbox",
        runs={
            run: Grant(prefix=f"p/{run}/", form=PostForm(url=http.url + "/upload", fields={}))
            for run in runs
        },
    )
    source_path = f"{session}/sources/shared.tar"
    http.fault = "denied"
    with pytest.raises(StorageUnavailable):
        courier.fetch({source_path: http.source("/source")})
    journal = BucketJournal(
        Storage(name="test", bucket="test", provider="s3", region="r", prefix="p"),
        Bucket("test", "r", Keys(access_key="a", secret_key="s"), http.send),
    )
    monkeypatch.setattr(b.service, "observe", journal.observe)
    monkeypatch.setattr(b.service, "fetch", journal.fetch)
    b.run(1)
    failed = b.scheduler.usage()
    assert [(e.status, e.received_bytes) for e in failed.events if e.purpose == "source"] == [
        (403, 0)
    ]
    http.fault = ""
    attempts = []
    for run in runs:
        a = b.scheduler.attempts(run)[0]
        attempts.append(a)
        assert a.session == session
        # The kept worker receives the next allocation through its control inbox.
        http.objects["/" + root + "inbox.json"] = (
            Inbox(
                work=tuple(
                    Delivery.model_validate(
                        {
                            "work": work.model_dump_json(round_trip=True),
                            "access": Grant(
                                prefix=f"p/{work.run}/",
                                form=PostForm(url=http.url + "/upload", fields={}),
                            ),
                            "fetch": {},
                        }
                    )
                    for work, _ in b.service.sessions[session].added
                )
            )
            .model_dump_json(round_trip=True)
            .encode()
        )
        courier.fetch(
            {
                source_path: http.source("/source"),
                f"{session}/attempts/{a.id}/inputs/checkpoint.pt": http.source("/checkpoint"),
            }
        )
        assert (home / "sources/shared.tar").read_bytes() == http.objects["/source"]
        # Held in the cache for the session's attempts to link, not copied beside it.
        assert not (home / f"attempts/{a.id}/inputs/checkpoint.pt").exists()
        held = Cache().held(Content.of(http.objects["/checkpoint"]))
        assert held is not None and held.read_bytes() == http.objects["/checkpoint"]
        outputs = home / "attempts" / a.id / "outputs"
        outputs.mkdir(parents=True)
        (outputs.parent / "run").write_text(a.run)
        (outputs.parent / "claim").write_text(b.clock().isoformat())
        (outputs / "checkpoint.pt").write_bytes(payload)
        output = Output(
            name="checkpoint.pt",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        (outputs / RESULT).write_text(Result().model_dump_json())
        finished = Progress(
            attempt=a.id,
            started_at=b.clock(),
            finished_at=b.clock(),
            outcome="succeeded",
            cause=None,
            clocks={},
            outputs=(output,),
            exit_code=0,
        )
        (outputs.parent / "finished.json").write_text(finished.model_dump_json())
        courier.carry_out()
        b.run(1)
    published = http.objects["/" + root + "out/usage.jsonl"]
    # Collection must cross the ordinary scheduler observation path, twice.
    b.run(1)
    collected = b.scheduler.usage()
    b.run(1)
    replayed = b.scheduler.usage()
    assert len(replayed.events) == len({e.id for e in replayed.events})
    assert {e.id for e in collected.events} <= {e.id for e in replayed.events}
    collected_ids = {e.id for e in collected.events}
    assert all(e.purpose == "control" for e in replayed.events if e.id not in collected_ids)
    assert all(b.scheduler.read(run).outcome == "succeeded" for run in runs)
    worker_events = tuple(Transfer.model_validate_json(line) for line in published.splitlines())
    assert {e.id for e in worker_events} <= {e.id for e in collected.events}
    assert all(e.machine == attempts[0].machine for e in collected.events)

    # A different machine's cache cannot make the first controller download warm.
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "controller-cache"))
    for index, run in enumerate(runs):
        assert (
            b.scheduler.fetch(run, "checkpoint.pt", tmp_path / f"cold-{index}").read_bytes()
            == payload
        )
    cold = b.scheduler.usage()
    replayed_ids = {e.id for e in replayed.events}
    cold_reads = Usage(
        observed_at=cold.observed_at,
        events=tuple(e for e in cold.events if e.id not in replayed_ids),
    )
    # The payload alone: the records fetch consults are already held, and unchanged.
    assert (cold_reads.received_bytes, cold_reads.requests) == (len(payload), 6)
    assert cold_reads.reused_bytes == len(payload)
    for index, run in enumerate(runs):
        assert (
            b.scheduler.fetch(run, "checkpoint.pt", tmp_path / f"warm-{index}").read_bytes()
            == payload
        )
    after = b.scheduler.usage()
    cold_ids = {e.id for e in cold.events}
    warm = Usage(
        observed_at=after.observed_at,
        events=tuple(e for e in after.events if e.id not in cold_ids),
    )
    bulk = Usage(
        observed_at=after.observed_at,
        events=tuple(e for e in after.events if e.purpose != "control"),
    )
    control = Usage(
        observed_at=after.observed_at,
        events=tuple(e for e in after.events if e.purpose == "control"),
    )
    # Inbox + completion per run, each answered "not modified" without a body.
    assert (warm.received_bytes, warm.requests) == (0, 4)
    assert sum(e.received_bytes for e in warm.events if e.purpose == "result") == 0
    assert (bulk.received_bytes, bulk.reused_bytes) == (
        len(archive.getvalue()) + len(http.objects["/source"]) + 1024**2 + len(payload),
        len(http.objects["/source"]) + 1024**2 + 3 * len(payload),
    )
    assert {e.purpose for e in after.events} >= {
        "source",
        "runtime",
        "input",
        "result",
        "control",
    }
    assert sum(e.status == 403 for e in after.events) == 1
    assert control.requests > 0 and control.supplied_bytes > 0
    assert bulk.requests == 8  # runtime, failed source, source, input, 2 uploads, 2 parts
    # Worker publication, scheduler collection and public fetches; a start receipt is read once.
    assert control.requests == 40

    assert sum(e.operation == "GET" for e in after.events) == http.requests
    assert after.received_bytes == http.received
    # Usage journal deliveries are not journaled: each would change the journal.
    assert http.posts - sum(e.operation == "POST" for e in after.events) == 5

    # Measure the predecessor's repeated full payload reads. Preserve the same
    # observed control/upload trace so the comparison changes only payload reuse.
    baseline: list[Transfer] = []
    with observe(lambda raw: baseline.append(Transfer.model_validate_json(raw))):
        for path in ("/runtime", "/source", "/source", "/checkpoint", "/checkpoint"):
            assert _send("GET", http.url + path, None, {}).body == http.objects[path]
        http.objects["/result"] = payload
        for _ in range(4):
            assert _send("GET", http.url + "/result", None, {}).body == payload
    unchanged = tuple(
        e for e in after.events if e.operation != "GET" or e.purpose == "control" or e.status == 403
    )
    before = Usage(events=(*baseline, *unchanged), observed_at=after.observed_at)
    assert before.received_bytes - after.received_bytes == len(
        http.objects["/source"]
    ) + 1024**2 + 3 * len(payload)
    assert (before.requests, after.requests) == (52, 48)
    saved = before.estimate(S3_US_WEST_2) - after.estimate(S3_US_WEST_2)
    assert saved == Decimal("0.00707948782")
    assert after.requests == control.requests + bulk.requests
    own = [b.scheduler.usage(run=run) for run in runs]
    shared = {e.id: e for report in own for e in report.shared_events}
    assert after.received_bytes == sum(report.received_bytes for report in own) + sum(
        e.received_bytes for e in shared.values()
    )
    assert set(shared).isdisjoint(e.id for report in own for e in report.events)
    assert len(shared) == len(own[0].shared_events) == len(own[1].shared_events)

    # Delete the simulated worker and reopen the database: reports need only
    # retained evidence, with no provider/SSH/git/artifact effects.
    b.service.sessions.clear()
    store = Database(tmp_path / "launcher.db")
    counting = Counting(store, b.service)
    restored = Scheduler(store, {})
    requests_before = http.requests
    with counting.record():
        offline = restored.usage()
        offline_run = restored.usage(run=runs[0])
        offline_machine = restored.usage(machine=first.machine)
    assert counting.snapshot() == Counts(database=7)
    assert offline_run == own[0]
    assert offline_machine == after
    assert http.requests == requests_before
    assert offline == after
    assert "unobserved" in " ".join(offline.coverage)
    assert "fixture-private-query" not in offline.model_dump_json()


def test_the_launcher_fetches_a_multipart_output_and_retains_each_part(
    http: HTTP, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The launcher a command opens, with its own bucket send and database; only
    # provisioning, job execution and the bucket's host are fixtures.
    monkeypatch.setattr(downloads, "PART_BYTES", 16)
    b = bench(tmp_path)
    b.scheduler.rent("l4", 1)
    run = b.scheduler.queue(Job(command="true", max_hours=0.25, max_usd=Decimal(1)), rent="l4")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(run)[0]
    session = attempt.session
    root = f"prefix/sessions/{session}/"
    given = b.service.sessions[session]
    packet = given.packet.model_copy(update={"work": tuple(w for w, _ in given.work())})
    http.objects["/" + root + "packet.json"] = packet.model_dump_json().encode()
    http.objects["/" + root + "out/receipt"] = b"started"
    upload = PostForm(url=http.url + "/upload", fields={})
    home = tmp_path / "worker" / session
    courier = Courier(
        home,
        Grant(prefix=root + "out/", form=upload),
        http.url + "/inbox",
        runs={run: Grant(prefix=f"prefix/{run}/", form=upload)},
    )
    payload = b"0123456789" * 4  # three parts
    outputs = home / "attempts" / attempt.id / "outputs"
    outputs.mkdir(parents=True)
    (outputs.parent / "run").write_text(run)
    (outputs.parent / "claim").write_text(b.clock().isoformat())
    (outputs / "checkpoint.pt").write_bytes(payload)
    (outputs / RESULT).write_text(Result().model_dump_json())
    output = Output(
        name="checkpoint.pt", sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
    )
    finished = Progress(
        attempt=attempt.id,
        started_at=b.clock(),
        finished_at=b.clock(),
        outcome="succeeded",
        cause=None,
        clocks={},
        outputs=(output,),
        exit_code=0,
    )
    (outputs.parent / "finished.json").write_text(finished.model_dump_json())
    courier.carry_out()
    journal = BucketJournal(
        Storage(name="test", bucket="bucket", prefix="prefix", provider="s3", region="us-west-2"),
        Bucket("bucket", "us-west-2", Keys(access_key="a", secret_key="s"), http.send),
    )
    monkeypatch.setattr(b.service, "observe", journal.observe)
    b.run(1)
    assert b.scheduler.read(run).outcome == "succeeded"

    monkeypatch.setenv("RUNPOD_API_KEY", "fixture")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setattr(client, "_send", http.send)
    before = {e.id for e in b.store.transfers()}
    fetched = client._open(tmp_path).fetch(run, "checkpoint.pt", tmp_path / "fetched")
    assert fetched.read_bytes() == payload
    parts = [e for e in b.store.transfers() if e.id not in before and e.purpose == "result"]
    assert sorted(e.byte_range or "" for e in parts) == [
        "bytes=0-15",
        "bytes=16-31",
        "bytes=32-39",
    ]
    assert sum(e.received_bytes for e in parts) == len(payload)
    assert {(e.run, e.attempt) for e in parts} == {(run, attempt.id)}


def test_active_cache_capacity_streams_another_digest_without_overfilling(
    http: HTTP,
    tmp_path: Path,
) -> None:
    cache = Cache(tmp_path / "cache", max_bytes=12, range_bytes=4)
    cache.root.mkdir()
    active = cache.root / ("f" * 64 + "-12.partial")
    active.write_bytes(b"reservedsize")
    with active.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = download(http.source(), tmp_path / "result", cache=cache)
        assert result.read_bytes() == b"abcdefghijkl"
        assert list(cache.root.glob("*.partial")) == [active]
    assert active.read_bytes() == b"reservedsize"


class _Bucket:
    """Presigned reads served by a moto bucket, so a job's fetch and a report's read
    reach the same object, counted by one client. Links are the launcher's own."""

    def __init__(self, client: CountedClient, region: str) -> None:
        self.client = client
        self.bucket = Bucket("lab", region, Keys(access_key="a", secret_key="s"))

    def link(self, key: str) -> Download:
        body = self.client.client.get_object(Bucket="lab", Key=key)["Body"].read()
        return Download(
            url=self.bucket.get_url(key, 60),
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )

    def send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        options = {}
        if "Range" in headers:
            options["Range"] = headers["Range"]
        if "If-Match" in headers:
            options["IfMatch"] = headers["If-Match"]
        reply = self.client.get_object(Bucket="lab", Key=urlsplit(url).path[1:], **options)
        data = reply["Body"].read()
        fields = {"etag": reply["ETag"], "content-length": str(len(data))}
        if "ContentRange" in reply:
            fields["content-range"] = reply["ContentRange"]
        return Reply(status=206 if "Range" in options else 200, body=data, headers=fields)


@pytest.mark.parametrize("key", ["run/checkpoints/model.pt", "run/attempts/a1/model.pt"])
@pytest.mark.parametrize("job_first", [True, False])
# boto3 reaches us-east-1 at its legacy global host; the launcher signs regional hosts.
@pytest.mark.parametrize("region", ["us-west-2", "us-east-1"])
def test_a_job_fetch_and_a_report_read_download_once_in_either_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, job_first: bool, region: str
) -> None:
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name=region))
        if region == "us-east-1":
            client.create_bucket(Bucket="lab")
        else:
            client.create_bucket(
                Bucket="lab", CreateBucketConfiguration={"LocationConstraint": region}
            )
        client.put_object(Bucket="lab", Key=key, Body=b"abcdefghijkl")
        bucket = _Bucket(client, region)

        # Each reader opens this computer's cache afresh, as separate processes do.
        def job() -> bytes:
            return download(
                bucket.link(key), tmp_path / "job" / "model.pt", send=bucket.send
            ).read_bytes()

        def report() -> bytes:
            return S3Storage("lab", client=client).get(key)

        first, second = (job, report) if job_first else (report, job)
        assert first() == second() == b"abcdefghijkl"
        # One payload; a mutable object read second by a report is only checked.
        checked = job_first and "checkpoints" not in key
        assert (client.bytes, client.requests) == (12, 2 if checked else 1)

        client.put_object(Bucket="lab", Key=key.replace("model", "other"), Body=b"mnopqrstuvwx")
        if "checkpoints" not in key:
            # A changed object is read again, by either reader, once.
            client.put_object(Bucket="lab", Key=key, Body=b"mnopqrstuvwx")
            assert report() == job() == b"mnopqrstuvwx"
            assert client.bytes == 24


def test_a_failed_refresh_keeps_the_verified_copy_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))

    class Flaky(CountedClient):
        failures = 0

        def get_object(self, **fields: Any) -> Any:
            if self.failures:
                self.failures -= 1
                raise ConnectionError("interrupted")
            return super().get_object(**fields)

    with mock_aws():
        client = Flaky(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="lab")
        storage = S3Storage("lab", client=client)
        storage.put("run/manifest.json", b"first")
        assert storage.get("run/manifest.json") == b"first"
        storage.put("run/manifest.json", b"second")
        client.failures = 1
        with limit_time(30):
            with pytest.raises(ConnectionError):
                storage.get("run/manifest.json")
            assert (storage.cache.root / Content.of(b"first").name).read_bytes() == b"first"
            assert storage.get("run/manifest.json") == b"second"
    assert client.bytes == 5 + 6


def test_changed_and_corrupt_bucket_objects_are_read_again(tmp_path: Path) -> None:
    boto3 = pytest.importorskip("boto3")
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="lab")
        storage = S3Storage("lab", client=client)
        storage.put("run/manifest.json", b"first")
        assert [storage.get("run/manifest.json") for _ in range(2)] == [b"first"] * 2
        assert client.bytes == 5  # the second read asked only whether it changed
        storage.put("run/manifest.json", b"second")
        assert storage.get("run/manifest.json") == b"second"
        held = storage.cache.root / Content.of(b"second").name
        held.chmod(0o644)
        held.write_bytes(b"secoNd")
        assert storage.get("run/manifest.json") == b"second"
    assert client.bytes == 5 + 6 + 6


class _Held:
    """Content a test already has, read without a network."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read_bytes = 0
        self.artifact = Artifact(
            location=f"https://lab.example/{Content.of(body).name}",
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        body = self.body[start : None if end is None else end + 1]
        self.read_bytes += len(body)
        return Fetched(body=body, etag=hashlib.sha256(self.body).hexdigest())

    def changed(self, etag: str) -> Fetched | None:
        return None

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        pass


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("max_bytes", [0, 64])
def test_ranges_preserve_requested_versions_at_one_location(
    tmp_path: Path, reverse: bool, max_bytes: int
) -> None:
    sources = [_Held(b"abcdef"), _Held(b"UVWXYZ")]
    if reverse:
        sources.reverse()
    artifacts = [
        source.artifact.model_copy(update={"location": "https://lab.example/result"})
        for source in sources
    ]
    root = tmp_path / "cache"
    for source, artifact in zip(sources, artifacts, strict=True):
        cache = Cache(root, max_bytes=max_bytes, range_bytes=2)
        assert cache.read(artifact, source, start=1, length=2) == source.body[1:3]

    # Reopening the cache must preserve each version, including for whole reads.
    for source, artifact in zip(sources, artifacts, strict=True):
        cache = Cache(root, max_bytes=max_bytes, range_bytes=2)
        assert cache.read(artifact, source) == source.body
        assert cache.read(artifact, source, start=2, length=3) == source.body[2:5]
        if max_bytes:
            assert source.read_bytes == len(source.body)


def test_a_range_verifies_the_requested_digest_before_exposing_bytes(tmp_path: Path) -> None:
    expected, wrong = _Held(b"abcdef"), _Held(b"abcdeX")
    cache = Cache(tmp_path / "cache", range_bytes=2)
    # The requested range matches; corruption elsewhere still invalidates the version.
    with pytest.raises(engine.Mismatch):
        cache.read(expected.artifact, wrong, start=1, length=2)
    assert cache.read(expected.artifact, expected, start=1, length=2) == b"bc"


def test_a_range_never_exposes_corrupted_cached_content(tmp_path: Path) -> None:
    source = _Held(b"abcdef")
    cache = Cache(tmp_path / "cache")
    assert cache.read(source.artifact, source) == source.body
    held = cache.root / Content.of(source.body).name
    held.chmod(0o644)
    held.write_bytes(b"aXXdef")
    assert cache.read(source.artifact, source, start=1, length=2) == b"bc"
    assert source.read_bytes == 2 * len(source.body)


def test_a_range_past_the_recorded_length_is_refused_before_reading(tmp_path: Path) -> None:
    source = _Held(b"abcdef")
    with pytest.raises(ValueError, match="past"):
        Cache(tmp_path / "cache").read(source.artifact, source, start=4, length=3)
    assert source.read_bytes == 0


def test_one_artifact_at_two_locations_is_read_once(tmp_path: Path) -> None:
    source = _Held(b"abcdef")
    cache = Cache(tmp_path / "cache")
    elsewhere = source.artifact.model_copy(update={"location": "https://mirror.example/result"})
    assert cache.read(source.artifact, source) == cache.read(elsewhere, source) == source.body
    assert cache.read(elsewhere, source, start=1, length=2) == b"bc"
    assert source.read_bytes == len(source.body)


def test_held_content_survives_eviction_until_its_holder_releases_it(tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", max_bytes=12)
    first, second = b"abcdefghijkl", b"mnopqrstuvwx"
    kept = Content.of(first)
    held = cache.hold(_Held(first).artifact, _Held(first), holder="s1", staging=tmp_path / "a")
    assert held == cache.root / kept.name and held.stat().st_mode & 0o777 == 0o444
    cache.place(_Held(second).artifact, tmp_path / "second", _Held(second))
    assert held.read_bytes() == first
    assert not (cache.root / Content.of(second).name).exists()  # streamed, not kept
    cache.release("s1")
    cache.place(_Held(second).artifact, tmp_path / "second", _Held(second))
    assert not held.exists()
    assert (cache.root / Content.of(second).name).read_bytes() == second
    assert not (tmp_path / "a").exists()  # held in place: nothing was copied


def test_held_content_is_verified_when_asked_for_and_discarded_when_corrupt(
    tmp_path: Path,
) -> None:
    cache = Cache(tmp_path / "cache")
    source = _Held(b"abcdef")
    held = cache.hold(source.artifact, source, holder="s1", staging=tmp_path / "a")
    content = Content.of(source.body)
    assert cache.held(content) == held
    held.chmod(0o644)
    held.write_bytes(b"aXXdef")
    assert cache.held(content) is None
    assert not held.exists()


def test_worker_source_and_input_share_verified_transport_bytes(
    http: HTTP,
    tmp_path: Path,
) -> None:
    home = tmp_path / "sessions" / "worker-session"
    target = home / "sources/source.tar"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unverified stale input")
    courier = Courier(
        home, Grant(prefix="out/", form=PostForm(url=http.url + "/upload", fields={})), "unused"
    )
    courier.fetch(
        {
            "worker-session/sources/source.tar": http.source(),
            "worker-session/inputs/weights": http.source(),
        }
    )
    assert target.read_bytes() == b"abcdefghijkl"
    assert not (home / "inputs/weights").exists()  # held in the cache, not copied
    held = Cache().held(Content.of(b"abcdefghijkl"))
    assert held is not None and held.read_bytes() == b"abcdefghijkl"
    events = [
        Transfer.model_validate_json(line)
        for line in (home / "usage.jsonl").read_text().splitlines()
    ]
    assert sum(e.received_bytes for e in events if e.purpose == "source") == 12
    assert sum(e.reused_bytes for e in events if e.purpose == "input") == 12
    assert (http.requests, http.received) == (1, 12)


def test_unchanged_inbox_polls_leave_no_log_lines_or_usage(http: HTTP, tmp_path: Path) -> None:
    home = tmp_path / "sessions" / "polled"
    (home / "stop").mkdir(parents=True)
    http.objects["/inbox"] = Inbox().model_dump_json().encode()
    form = Grant(prefix="out/", form=PostForm(url=http.url + "/upload", fields={}))
    courier = Courier(home, form, http.url + "/inbox")
    log = tmp_path / "worker.log"
    with log_to(log):
        for _ in range(5):
            courier.bring_in()
        http.objects["/inbox"] = Inbox(stop=("attempt",)).model_dump_json().encode()
        for _ in range(5):
            courier.bring_in()
    assert http.requests == 10
    # Unchanged polls are answered without a body: each version's bytes arrive once.
    assert http.received == len(Inbox().model_dump_json()) + len(
        Inbox(stop=("attempt",)).model_dump_json()
    )
    lines = [line.split(" ", 1)[1] for line in log.read_text().splitlines()]
    assert lines == ["inbox: 0 work, 0 to stop", "inbox: 0 work, 1 to stop"]  # changes only
    events = [
        Transfer.model_validate_json(line)
        for line in (home / "usage.jsonl").read_text().splitlines()
    ]
    assert [(e.operation, e.status) for e in events] == [("GET", 200), ("GET", 200)]
    assert (home / "stop" / "attempt").exists()


def test_polling_a_session_reads_each_version_of_its_files_once(http: HTTP, tmp_path: Path) -> None:
    b = bench(tmp_path)
    run = b.scheduler.launch(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
    attempt = b.scheduler.attempts(run)[0]
    b.scheduler.tick()
    fake = b.service.sessions[attempt.session]
    packet = fake.packet.model_copy(update={"work": tuple(w for w, _ in fake.work())})
    session = f"/p/sessions/{attempt.session}/"
    http.objects[session + "packet.json"] = packet.model_dump_json().encode()
    http.objects[session + "inbox.json"] = Inbox().model_dump_json().encode()
    http.objects[session + "out/receipt"] = b"1234"
    lines = b"worker log line\n" * 100
    http.objects[session + "out/" + log_piece(WORKER_LOG, 0)] = lines
    http.objects[session + "out/worker-log.json"] = (
        WorkerLog(ends=(len(lines),)).model_dump_json().encode()
    )
    http.objects[session + "out/usage.jsonl"] = b""
    record = f"/p/{run}/attempts/{attempt.id}.json"
    started = AttemptRecord(started_at=datetime.now(UTC))
    first = b"step 1\n" * 1000
    http.objects[f"/p/{run}/" + log_piece(attempt_log(attempt.id), 0)] = first
    http.objects[record] = (
        started.model_copy(update={"log_ends": (len(first),)}).model_dump_json().encode()
    )
    journal = BucketJournal(
        Storage(name="test", bucket="test", provider="s3", region="r", prefix="p"),
        Bucket("test", "r", Keys(access_key="a", secret_key="s"), http.send),
    )
    written = sum(len(body) for key, body in http.objects.items() if key.startswith("/p/"))

    def poll() -> None:
        for _ in range(10):
            journal.observe(attempt.session)
            journal.logs(attempt.session, attempt.id)
            journal.logs(attempt.session, producer="worker")

    poll()
    assert http.received == written  # ten polls read each file once
    second = b"step 2\n" * 1000
    http.objects[f"/p/{run}/" + log_piece(attempt_log(attempt.id), len(first))] = second
    http.objects[record] = (
        started.model_copy(update={"log_ends": (len(first), len(first) + len(second))})
        .model_dump_json()
        .encode()
    )
    more = b"worker log line\n" * 10
    http.objects[session + "out/" + log_piece(WORKER_LOG, len(lines))] = more
    worker_record = WorkerLog(ends=(len(lines), len(lines) + len(more))).model_dump_json()
    http.objects[session + "out/worker-log.json"] = worker_record.encode()
    poll()
    # Only the new records and the new pieces; the first pieces are never read again.
    added = len(http.objects[record]) + len(second) + len(worker_record) + len(more)
    assert http.received == written + added
    assert journal.logs(attempt.session, attempt.id).text == (first + second).decode()
    assert journal.logs(attempt.session, producer="worker").text == (lines + more).decode()
