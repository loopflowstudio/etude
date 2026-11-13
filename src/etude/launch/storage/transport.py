"""Streaming HTTP observation, also usable before client dependencies install.

Events describe bytes read by this process and bodies supplied to urllib, never
provider billing. The sink receives JSON for validation by the retaining owner.
"""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from etude.deadline import call, remaining
from etude.launch.logging import sanitize, write_log
from etude.storage_timeout import socket_timeout

_sink: ContextVar[Callable[[str], None] | None] = ContextVar("transfer_sink", default=None)
_context: ContextVar[dict[str, str]] = ContextVar("transfer_context", default={})
_size_bytes: ContextVar[int | None] = ContextVar("transfer_size_bytes", default=None)


@dataclass(frozen=True)
class HTTPReply:
    """One HTTP response, read whole: its status, body and lower-cased headers."""

    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


# Sends one request and returns its reply; raises OSError when no reply came.
# Custom transports share the enclosing deadline, as the default ones do.
Send = Callable[[str, str, bytes | None, dict[str, str]], HTTPReply]


@contextmanager
def observe(sink: Callable[[str], None]) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def attribute(
    *,
    session: str | None = None,
    machine: str | None = None,
    run: str | None = None,
    attempt: str | None = None,
    purpose: str | None = None,
    transfer: str | None = None,
    key: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> Iterator[None]:
    fields = {
        "session": session,
        "machine": machine,
        "run": run,
        "attempt": attempt,
        "purpose": purpose,
        "transfer": transfer,
        "key": key,
        "sha256": sha256,
    }
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    size = _size_bytes.set(_size_bytes.get() if size_bytes is None else size_bytes)
    try:
        yield
    finally:
        _size_bytes.reset(size)
        _context.reset(token)


def emit(
    method: str,
    url: str,
    *,
    received: int = 0,
    supplied: int = 0,
    status: int | None = None,
    request_id: str | None = None,
    range_start: int | None = None,
    elapsed: float = 0,
    completed: bool = False,
    reused: int = 0,
) -> None:
    parts = urllib.parse.urlsplit(url)
    context = _context.get()
    event = {
        "id": uuid4().hex,
        "observed_at": datetime.now(UTC).isoformat(),
        "backend": sanitize(parts.hostname or "") + (f":{parts.port}" if parts.port else ""),
        "key": sanitize(context.get("key") or urllib.parse.unquote(parts.path)),
        "operation": method,
        "purpose": context.get("purpose", "control"),
        "session": context.get("session", os.environ.get("ETUDE_SESSION")),
        "machine": context.get("machine"),
        "run": context.get("run"),
        "attempt": context.get("attempt"),
        "transfer": context.get("transfer"),
        "sha256": context.get("sha256"),
        "size_bytes": _size_bytes.get(),
        "status": status,
        "request_id": request_id,
        "range_start": range_start,
        "received_bytes": received,
        "supplied_bytes": supplied,
        "elapsed_seconds": elapsed,
        "completed": completed,
        "reused_bytes": reused,
    }
    deliver(json.dumps(event, separators=(",", ":")))


def deliver(raw: str) -> None:
    """Hand one observation to this thread's sink, or the worker log without one."""
    sink = _sink.get()
    if sink is None:
        write_log("transfer", raw)
    else:
        sink(raw)


def request(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    consume: Callable[[bytes], object],
    accept: Callable[[int, Mapping[str, str]], None] | None = None,
) -> int:
    """Read incrementally, retaining partial counts even when a read fails.

    No URL, headers, response body or exception text enters an event. Upload
    counts mean supplied body bytes; urllib does not expose partial socket sends.
    """
    started = time.monotonic()
    status: int | None = None
    request_id: str | None = None
    received = 0
    complete = False

    def read() -> int:
        nonlocal status, request_id, received
        outgoing = urllib.request.Request(
            url, data=body, headers={"User-Agent": "etude-sdk", **headers}, method=method
        )
        try:
            reply = urllib.request.urlopen(
                outgoing, timeout=remaining(socket_timeout(len(body or b"")))
            )  # noqa: S310
        except urllib.error.HTTPError as error:
            reply = error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError("storage socket timed out") from None
            raise
        with reply:
            status = reply.status
            request_id = reply.headers.get("x-amz-request-id")
            if accept is not None:
                accept(status, {k.lower(): v for k, v in reply.headers.items()})
            timeout = socket_timeout()
            while True:
                left = remaining(timeout)
                # HTTPResponse clears fp when the final bytes close its socket;
                # look it up on each read rather than retain a closed socket.
                raw = getattr(getattr(reply, "fp", None), "raw", None)
                connection = getattr(raw, "_sock", None)
                if connection is not None:
                    connection.settimeout(left)
                chunk = reply.read1(65536)
                if not chunk:
                    break
                received += len(chunk)
                consume(chunk)
            length = reply.headers.get("Content-Length")
            if method != "HEAD" and length is not None and received != int(length):
                raise OSError("HTTP response ended before its declared length")
            return status

    # Bound the complete read, but retain its observation on the invoking thread.
    # In particular, a SQLite sink must not run on the deadline worker thread.
    try:
        result = call(read)
        complete = True
        return result
    finally:
        emit(
            method,
            url,
            received=received,
            supplied=len(body or b""),
            status=status,
            request_id=request_id,
            range_start=_range_start(headers),
            elapsed=time.monotonic() - started,
            completed=complete,
        )


def _range_start(headers: Mapping[str, str]) -> int | None:
    """The first byte a single `Range: bytes=START-END` request asks for."""
    requested = headers.get("Range", headers.get("range"))
    if requested is None:
        return None
    start = requested.removeprefix("bytes=").partition("-")[0]
    return int(start) if start.isdigit() else None


def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> HTTPReply:
    output = io.BytesIO()
    status = request(method, url, body, headers, output.write)
    return HTTPReply(status=status, body=output.getvalue())
