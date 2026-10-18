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
from urllib.parse import urlsplit

import pytest

from etude.launch.database import Database
from etude.launch.deadline import limit_time, remaining
from etude.launch.logging import log_to
from etude.launch.providers.runpod import pod
from etude.launch.records import Job, Output, Storage
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import Delivery, Inbox, Progress, Result, RunAccess
from etude.launch.storage import download
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.download import Cache, Download
from etude.launch.storage.files import RESULT
from etude.launch.storage.s3 import Bucket, Keys, PostForm, Reply, StorageUnavailable, _send
from etude.launch.storage.transport import attribute, observe
from etude.launch.usage import Access, Rates, Transfer, Usage, reconcile
from etude.launch.worker.courier import Courier
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench


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
                requested = self.headers.get("Range")
                content_range = None
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
                self.send_header("ETag", owner.etag)
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


def test_cold_warm_concurrent_and_new_process_retrieval(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", range_bytes=4)
    events: list[Transfer] = []
    with observe(lambda raw: events.append(Transfer.model_validate_json(raw))):
        cache.fetch(http.source(), tmp_path / "first")
        cache.fetch(http.source(), tmp_path / "second")
    assert (sum(e.received_bytes for e in events), sum(e.reused_bytes for e in events)) == (12, 12)
    assert (http.requests, http.received) == (3, 12)
    script = """from pathlib import Path
import sys
from etude.launch.storage.download import Cache, Download
Cache(Path(sys.argv[1])).fetch(Download.model_validate_json(sys.argv[2]), Path(sys.argv[3]))
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
        paths = list(pool.map(lambda n: another.fetch(http.source(), tmp_path / str(n)), [1, 2]))
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
            cache.fetch(http.source(), tmp_path / "result", retries=0)
        assert not (tmp_path / "result").exists()
        assert sum(e.received_bytes for e in events) == 6
        assert not events[-1].completed
        http.fault = ""
        cache.fetch(http.source(), tmp_path / "result")
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
        Cache(tmp_path / "cache", range_bytes=4).fetch(source, target, timeout=0.05, retries=0)
    assert target.read_bytes() == b"previous verified result"


def test_changed_identity_and_corrupt_retained_range_fail(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", range_bytes=4)
    http.fault = "interrupted"
    with pytest.raises(StorageUnavailable):
        cache.fetch(http.source(), tmp_path / "result", retries=0)
    http.fault = ""
    http.etag = '"version-two"'
    with pytest.raises(StorageUnavailable, match="identity"):
        cache.fetch(http.source(), tmp_path / "result")
    partial = next(cache.root.glob("*.partial"))
    with partial.open("r+b") as stream:
        stream.write(b"corrupt")
    with pytest.raises(StorageUnavailable, match="corrupt"):
        cache.fetch(http.source(), tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_bounded_cache_and_oversized_download(http: HTTP, tmp_path: Path) -> None:
    cache = Cache(tmp_path / "cache", max_bytes=12, range_bytes=4)
    cache.fetch(http.source(), tmp_path / "a")
    http.objects["/other"] = b"mnopqrstuvwx"
    cache.fetch(http.source("/other"), tmp_path / "b")
    payloads = [p for p in cache.root.iterdir() if p.suffix not in {".lock", ".json"}]
    assert sum(p.stat().st_size for p in payloads) <= 12
    http.objects["/large"] = b"z" * 25
    destination = tmp_path / "large"
    destination.write_bytes(b"previous verified result")
    assert cache.fetch(http.source("/large"), destination).read_bytes() == b"z" * 25
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.partial"))
    assert not list(tmp_path.glob(".*.json"))


def test_download_inherits_caller_allowance(http: HTTP, tmp_path: Path) -> None:
    allowances: list[float | None] = []

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        allowances.append(remaining())
        return http.send(method, url, body, headers)

    cache = Cache(tmp_path / "cache")
    assert (
        cache.fetch(http.source(), tmp_path / "unlimited", send=send).read_bytes()
        == b"abcdefghijkl"
    )
    home = tmp_path / "sessions" / "session-one"
    home.mkdir(parents=True)
    with limit_time(600):
        courier = Courier(
            home,
            PostForm(url=http.url + "/upload", prefix="out/", fields={}),
            "unused",
            send,
        )
        courier.fetch({"session-one/sources/source.tar": http.source()})
    assert allowances[0] is None
    assert all(left is not None and 300 < left <= 600 for left in allowances[1:])


def test_outer_deadline_cannot_be_extended(http: HTTP, tmp_path: Path) -> None:
    http.fault = "timeout"
    with limit_time(0.02), pytest.raises((StorageUnavailable, TimeoutError)):
        Cache(tmp_path / "cache").fetch(http.source(), tmp_path / "result", timeout=30)
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
        cache.fetch(source, tmp_path / "first", send=send)
    target = tmp_path / "result"
    target.write_bytes(b"previous result")
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    if phase == "digest":
        digest = download._digest

        def expire_digest(path: Path) -> str:
            result = digest(path)
            clock[0] = 2.0
            return result

        monkeypatch.setattr(download, "_digest", expire_digest)
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
        cache.fetch(source, target, timeout=1, send=send)
    assert target.read_bytes() == b"previous result"
    assert {p.name for p in tmp_path.iterdir()} == (
        {"cache", "result", "first"} if warm else {"cache", "result"}
    )


def test_transient_range_http_retains_both_attempts(http: HTTP, tmp_path: Path) -> None:
    http.fault = "transient"
    events: list[Transfer] = []
    with observe(lambda raw: events.append(Transfer.model_validate_json(raw))):
        result = Cache(tmp_path / "cache").fetch(http.source(), tmp_path / "result")
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
        Cache(tmp_path / "cache", range_bytes=4).fetch(source, target, send=send, retries=2)
    assert ranges == ["bytes=0-3", "bytes=0-3", "bytes=4-7", "bytes=4-7", "bytes=8-11"]
    assert not target.exists()


def test_cost_scenario_and_safe_access_reconciliation(http: HTTP, tmp_path: Path) -> None:
    events: list[Transfer] = []
    cache = Cache(tmp_path / "cache", range_bytes=4)
    with (
        observe(lambda raw: events.append(Transfer.model_validate_json(raw))),
        attribute(purpose="result"),
    ):
        cache.fetch(http.source(), tmp_path / "cold")
        cold = Usage(events=tuple(events), observed_at=datetime.now(UTC))
        events.clear()
        cache.fetch(http.source(), tmp_path / "warm")
    warm = Usage(events=tuple(events), observed_at=datetime.now(UTC))
    rates = Rates(
        as_of=datetime(2026, 10, 18, tzinfo=UTC),
        description="synthetic route",
        download_usd_per_gb=Decimal("0.09"),
        request_usd_per_1000=Decimal("0.0004"),
    )
    assert (cold.received_bytes, cold.requests, warm.received_bytes, warm.requests) == (12, 3, 0, 0)
    assert cold.estimate(rates) == Decimal("0.00000120108")
    assert warm.estimate(rates) == 0
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
    run = b.scheduler.launch(Job(command="true", max_hours=1, max_usd=Decimal(1)), "l4")
    session = b.scheduler.attempts(run)[0].session
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(pod, "UV_URL", http.url + "/payload")
    log = tmp_path / "bootstrap.log"
    with log_to(log), attribute(session=session), pytest.raises(ValueError, match="checksum"):
        pod.ensure_uv()
    http.objects[f"/p/sessions/{session}/out/bootstrap.log"] = log.read_bytes()
    journal = BucketJournal(
        Storage(name="test", bucket="test", region="r", prefix="p"),
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
    b = bench(tmp_path, max_on_demand=1)
    b.scheduler.rent("l4", 1)
    runs = b.scheduler.queue_cohort(
        "experiment", Job(command="true", max_hours=0.25, max_usd=Decimal(1)).runs(1, 2), "l4"
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
        PostForm(url=http.url + "/upload", prefix=root + "out/", fields={}),
        http.url + "/inbox",
        runs={
            run: RunAccess(form=PostForm(url=http.url + "/upload", prefix=f"p/{run}/", fields={}))
            for run in runs
        },
    )
    source_path = f"{session}/sources/shared.tar"
    http.fault = "denied"
    with pytest.raises(StorageUnavailable):
        courier.fetch({source_path: http.source("/source")})
    journal = BucketJournal(
        Storage(name="test", bucket="test", region="r", prefix="p"),
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
                            "access": RunAccess(
                                form=PostForm(
                                    url=http.url + "/upload", prefix=f"p/{work.run}/", fields={}
                                )
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
        assert (home / f"attempts/{a.id}/inputs/checkpoint.pt").read_bytes() == http.objects[
            "/checkpoint"
        ]
        outputs = home / "attempts" / a.id / "outputs"
        outputs.mkdir()
        (outputs.parent / "run").write_text(a.run)
        (outputs.parent / "claim").write_text(b.clock().isoformat())
        (outputs / "checkpoint.pt").write_bytes(payload)
        output = Output(
            name="checkpoint.pt",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
        (outputs / RESULT).write_text(Result(checkpoint=output.name).model_dump_json())
        finished = Progress(
            attempt=a.id,
            started_at=b.clock(),
            finished_at=b.clock(),
            outcome="succeeded",
            cause=None,
            clocks={},
            checkpoint=output.name,
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
    assert (cold_reads.received_bytes, cold_reads.requests) == (25169124, 7)
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
    assert (warm.received_bytes, warm.requests) == (3300, 4)  # inbox + completion per run
    assert sum(e.received_bytes for e in warm.events if e.purpose == "result") == 0
    assert (bulk.received_bytes, bulk.reused_bytes) == (
        len(archive.getvalue()) + len(http.objects["/source"]) + 1024**2 + len(payload),
        len(http.objects["/source"]) + 1024**2 + 3 * len(payload),
    )
    assert {e.purpose for e in after.events} >= {
        "source",
        "runtime",
        "checkpoint",
        "result",
        "control",
    }
    assert sum(e.status == 403 for e in after.events) == 1
    assert control.requests > 0 and control.supplied_bytes > 0
    assert bulk.requests == 9  # runtime, failed source, source, input, 2 uploads, 3 ranges
    assert control.requests == 47  # worker publication, scheduler collection and public fetches

    assert sum(e.operation == "GET" for e in after.events) == http.requests
    assert after.received_bytes == http.received
    # Final journal publication cannot report its own completion in that body.
    assert http.posts - sum(e.operation == "POST" for e in after.events) == 2

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
    rates = Rates(
        as_of=datetime(2026, 10, 18, tzinfo=UTC),
        description="synthetic same-route response bytes and supplied request counts",
        download_usd_per_gb=Decimal("0.09"),
        request_usd_per_1000=Decimal("0.0004"),
    )
    assert (before.requests, after.requests) == (59, 56)
    assert before.estimate(rates) - after.estimate(rates) == Decimal("0.00707908782")
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
        result = cache.fetch(http.source(), tmp_path / "result")
        assert result.read_bytes() == b"abcdefghijkl"
        assert list(cache.root.glob("*.partial")) == [active]
    assert active.read_bytes() == b"reservedsize"


def test_worker_source_and_checkpoint_share_verified_transport_bytes(
    http: HTTP,
    tmp_path: Path,
) -> None:
    home = tmp_path / "sessions" / "worker-session"
    target = home / "sources/source.tar"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unverified stale input")
    courier = Courier(home, PostForm(url=http.url + "/upload", prefix="out/", fields={}), "unused")
    courier.fetch(
        {
            "worker-session/sources/source.tar": http.source(),
            "previous-session/attempts/attempt/outputs/checkpoint.pt": http.source(),
        }
    )
    assert target.read_bytes() == b"abcdefghijkl"
    checkpoint = tmp_path / "sessions/previous-session/attempts/attempt/outputs/checkpoint.pt"
    assert checkpoint.read_bytes() == b"abcdefghijkl"
    events = [
        Transfer.model_validate_json(line)
        for line in (home / "usage.jsonl").read_text().splitlines()
    ]
    assert sum(e.received_bytes for e in events if e.purpose == "source") == 12
    assert sum(e.reused_bytes for e in events if e.purpose == "checkpoint") == 12
    assert (http.requests, http.received) == (1, 12)
