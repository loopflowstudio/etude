"""Sanitized, bounded operational output, usable before client installation.

Each process owns its file. Publication is best effort on a daemon thread;
shutdown and the independent guardian never wait for storage.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from etude.deadline import call, limit_time, remaining
from etude.storage_timeout import socket_timeout

LIMIT = 1024 * 1024
OPENING = 4096
TRUNCATED = "[retention truncated"
CLOSED = "[producer closed]"
lock = threading.Lock()
publication_lock = threading.Lock()
log_path: ContextVar[Path | None] = ContextVar("log_path", default=None)


class Console:
    """Sanitize complete console lines, even when credentials cross read boundaries.

    Oversized lines are omitted whole: truncating raw input could expose part of
    a credential. Only sanitized text reaches the retained sink.
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.pending = b""
        self.omitting = False

    def write(self, body: bytes, *, final: bool = False) -> None:
        for piece in body.splitlines(keepends=True):
            if not self.omitting:
                self.pending += piece
                if len(self.pending) > LIMIT:
                    self.pending = b""
                    self.omitting = True
            if piece.endswith(b"\n"):
                self.flush()
        if final:
            self.flush()

    def flush(self) -> None:
        if self.omitting:
            write_log(self.operation, "[retention truncated: oversized console line omitted]")
        elif self.pending:
            write_log(self.operation, self.pending.decode(errors="replace").rstrip("\n"))
        self.pending = b""
        self.omitting = False


@contextmanager
def log_to(path: Path) -> Iterator[None]:
    """Give this producer its own file without changing another thread's environment."""
    token = log_path.set(path)
    try:
        yield
    finally:
        log_path.reset(token)


def sanitize(text: str) -> str:
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 4
            and any(
                part in name.upper()
                for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "URL", "CREDENTIAL")
            )
        ):
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://[^\s<>\"']+", "[redacted URL]", text)
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    return re.sub(
        r"(?i)((?:authorization|password|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )


def explain(error: BaseException) -> str:
    """Never include subprocess argv or traceback locals in retained errors."""
    if isinstance(error, subprocess.TimeoutExpired):
        detail = f"timed out after {error.timeout} seconds"
        stderr = error.stderr
    elif isinstance(error, subprocess.CalledProcessError):
        detail = f"exit status {error.returncode}"
        stderr = error.stderr
    else:
        detail, stderr = str(error), None
    if stderr:
        detail += "\n" + (stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
    if not isinstance(error, (OSError, ValueError, subprocess.SubprocessError)):
        frames = traceback.extract_tb(error.__traceback__)[-3:]
        if frames:
            detail += "\n" + " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames
            )
    return sanitize(f"{type(error).__name__}: {detail}")


def portions(text: str) -> tuple[str, str, int]:
    match = re.search(r"\n\[retention truncated: (\d+) characters\]\n", text)
    if match is None:
        return "", text, 0
    return text[: match.start()], text[match.end() :], int(match.group(1))


def bounded(text: str) -> str:
    text = sanitize(text)
    opening, tail, omitted = portions(text)
    raw = (opening + tail).encode()
    if len(raw) <= LIMIT and not omitted:
        return raw.decode()
    head = raw[:OPENING].decode(errors="ignore")
    recent = raw[-(LIMIT - OPENING - 100) :].decode(errors="ignore")
    omitted += len(opening + tail) - len(head) - len(recent)
    return head + f"\n[retention truncated: {omitted} characters]\n" + recent


def write_log(operation: str, message: str) -> None:
    """Append one sanitized line. A log is only appended to, so it can be carried in
    pieces each sent once; what is retained or shown of it is `bounded`."""
    line = bounded(sanitize(f"{datetime.now(UTC).isoformat()} {operation}: {message}\n"))
    path = log_path.get() or os.environ.get("ETUDE_LOG_PATH")
    try:
        if path:
            with lock:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("a") as log:
                    log.write(line)
        else:
            print(line, end="", file=sys.stderr, flush=True)
    except (OSError, UnicodeError):
        # A full disk or a closed console cannot mask the operation's error or stop cleanup.
        try:
            print(line, end="", file=sys.stderr, flush=True)
        except OSError:
            pass


def publish(path: Path, url: str, timeout: float | None = None) -> None:
    """One best-effort PUT, within the configured log and enclosing rental allowance."""
    limits = [timeout] if timeout is not None else []
    if value := os.environ.get("ETUDE_LOG_SECONDS"):
        limits.append(float(value))
    if value := os.environ.get("ETUDE_DEADLINE"):
        limits.append((datetime.fromisoformat(value) - datetime.now(UTC)).total_seconds())
    seconds = min(limits) if limits else None
    if seconds is not None and seconds <= 0:
        return

    def deliver() -> None:
        left = remaining()
        acquired = (
            publication_lock.acquire() if left is None else publication_lock.acquire(timeout=left)
        )
        if not acquired:
            return
        try:
            body = bounded(path.read_text()).encode()
            with urlopen(
                Request(url, data=body, method="PUT"), timeout=remaining(socket_timeout(len(body)))
            ):
                pass
        finally:
            publication_lock.release()

    try:
        with limit_time(seconds):
            call(deliver)
    except (OSError, ValueError):
        pass  # diagnostic delivery cannot replace the error or prevent parent cleanup


def start_publication(path: Path, url: str) -> threading.Event:
    stopped = threading.Event()

    def deliver() -> None:
        while not stopped.wait(15):
            publish(path, url)

    threading.Thread(target=deliver, daemon=True).start()
    return stopped
