"""Sanitized, bounded operational output, usable before client installation.

Each process owns its file. Publication is best effort on a daemon thread;
shutdown and the independent guardian never wait for storage.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

LIMIT = 1024 * 1024
OPENING = 4096
TRUNCATED = "[retention truncated"
CLOSED = "[producer closed]"
_lock = threading.Lock()
_publication_lock = threading.Lock()
_path: ContextVar[Path | None] = ContextVar("log_path", default=None)


class Console:
    """Sanitize complete console lines, even when credentials cross read boundaries.

    Oversized lines are omitted whole: truncating raw input could expose part of
    a credential. Only sanitized text reaches the retained sink.
    """

    def __init__(self, operation: str) -> None:
        self._operation = operation
        self._pending = b""
        self._omitting = False

    def write(self, body: bytes, *, final: bool = False) -> None:
        for piece in body.splitlines(keepends=True):
            if not self._omitting:
                self._pending += piece
                if len(self._pending) > LIMIT:
                    self._pending = b""
                    self._omitting = True
            if piece.endswith(b"\n"):
                self._flush()
        if final:
            self._flush()

    def _flush(self) -> None:
        if self._omitting:
            write_log(self._operation, "[retention truncated: oversized console line omitted]")
        elif self._pending:
            write_log(self._operation, self._pending.decode(errors="replace").rstrip("\n"))
        self._pending = b""
        self._omitting = False


@contextmanager
def log_to(path: Path) -> Iterator[None]:
    """Give this producer its own file without changing another thread's environment."""
    token = _path.set(path)
    try:
        yield
    finally:
        _path.reset(token)


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
    line = sanitize(f"{datetime.now(UTC).isoformat()} {operation}: {message}\n")
    path = _path.get() or os.environ.get("ETUDE_LOG_PATH")
    try:
        if path:
            with _lock:
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                previous = target.read_text() if target.exists() else ""
                partial = target.with_suffix(".partial")
                partial.write_text(bounded(previous + line))
                partial.replace(target)
        else:
            print(line, end="", flush=True)
    except (OSError, UnicodeError):
        # A full disk or a closed console cannot mask the operation's error or stop cleanup.
        try:
            print(line, end="", flush=True)
        except OSError:
            pass


def publish(path: Path, url: str, timeout: float = 2.0) -> None:
    """One fixed-object PUT; failure cannot replace the operation being diagnosed."""
    deadline = time.monotonic() + timeout
    if not _publication_lock.acquire(timeout=timeout):
        return
    try:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        body = bounded(path.read_text()).encode()
        with urlopen(Request(url, data=body, method="PUT"), timeout=left):
            pass
    except (OSError, ValueError):
        pass
    finally:
        _publication_lock.release()


def start_publication(path: Path, url: str) -> threading.Event:
    stopped = threading.Event()

    def deliver() -> None:
        while not stopped.wait(15):
            publish(path, url)

    threading.Thread(target=deliver, daemon=True).start()
    return stopped
