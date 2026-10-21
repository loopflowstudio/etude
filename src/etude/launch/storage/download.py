"""Verified immutable downloads with process-shared, bounded local reuse.

The SHA256 and length identify transport bytes. Completed ranges carry their
own digests and a stable ETag, so interruption can reuse only validated ranges.
Only a verified file is published atomically to the caller's destination. Local
hashing and copying cooperate with the caller's deadline on the invoking thread;
no abandoned background writer may publish after expiry.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from etude.launch.deadline import limit_time, remaining
from etude.launch.records import Record
from etude.launch.storage.s3 import TRANSIENT_STATUSES, Denied, Send, StorageUnavailable, _send
from etude.launch.storage.transport import attribute, emit


class Download(Record):
    url: str = Field(repr=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class Mismatch(StorageUnavailable):
    """The bytes arrived but are not the ones named; asking again cannot help."""


class _Range(Record):
    size_bytes: int
    sha256: str


class _Partial(Record):
    etag: str | None = None
    ranges: tuple[_Range, ...] = ()


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            remaining()
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    remaining()
    return digest.hexdigest()


def _verify(path: Path, source: Download) -> bool:
    valid = (
        path.is_file()
        and path.stat().st_size == source.size_bytes
        and _digest(path) == source.sha256
    )
    remaining()
    return valid


def _discard(path: Path) -> None:
    """Remove payload and range metadata together while their owner holds its lock."""
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    with path.open("a") as lock:
        while True:
            remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, remaining(0.05) or 0.05))
        yield


def _publish(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            with source.open("rb") as stream:
                while True:
                    remaining()
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    temporary.write(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
            remaining()
            name.replace(destination)
        finally:
            name.unlink(missing_ok=True)
    return destination


class Cache:
    def __init__(
        self,
        root: Path | None = None,
        *,
        max_bytes: int = 20 * 1024**3,
        range_bytes: int = 8 * 1024**2,
    ) -> None:
        if max_bytes < 0 or range_bytes <= 0:
            raise ValueError("cache allowance must be nonnegative and range size positive")
        self.root = (
            root or Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser() / "downloads"
        )
        self.max_bytes, self.range_bytes = max_bytes, range_bytes

    def fetch(
        self,
        source: Download,
        destination: Path,
        *,
        timeout: float | None = None,
        retries: int = 2,
        send: Send = _send,
    ) -> Path:
        if retries < 0:
            raise ValueError("retry allowance must be nonnegative")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = f"{source.sha256}-{source.size_bytes}"
        with (
            limit_time(timeout),
            attribute(
                transfer=uuid4().hex, sha256=source.sha256, size_bytes=str(source.size_bytes)
            ),
        ):
            with _lock(self.root / f"{key}.lock"):
                target = self.root / key
                if target.exists():
                    if not _verify(target, source):
                        with _lock(self.root / "capacity.lock"):
                            target.unlink()
                        raise StorageUnavailable("cached download digest mismatch")
                    target.touch()
                    emit("REUSE", source.url, reused=source.size_bytes, completed=True)
                    return _publish(target, destination)
                # Oversized artifacts retain resumable work beside the destination,
                # rather than evicting the entire cache or refusing valid data.
                destination.parent.mkdir(parents=True, exist_ok=True)
                cacheable = source.size_bytes <= self.max_bytes and self._make_room(
                    key, source.size_bytes
                )
                partial = (
                    self.root / f"{key}.partial"
                    if cacheable
                    else destination.parent / f".{key}.partial"
                )
                self._acquire(source, partial, retries, send)
                if not _verify(partial, source):
                    with _lock(self.root / "capacity.lock"):
                        _discard(partial)
                    raise Mismatch("download length or SHA256 mismatch")
                if cacheable:
                    with _lock(self.root / "capacity.lock"):
                        partial.replace(target)
                        partial.with_suffix(".json").unlink(missing_ok=True)
                    return _publish(target, destination)
                # Staging is already beside the destination, on the same filesystem.
                partial.chmod(0o600)
                remaining()
                partial.replace(destination)
                _discard(partial)
                return destination

    def _make_room(self, key: str, size: int) -> bool:
        # Reserve capacity under the cache-wide lock. Other acquisitions hold
        # their digest lock, so eviction never removes an active reader/writer.
        with _lock(self.root / "capacity.lock"):
            candidates = sorted(
                (
                    p
                    for p in self.root.iterdir()
                    if p.suffix not in {".lock", ".json", ".tmp"} and not p.name.startswith(key)
                ),
                key=lambda p: p.stat().st_mtime,
            )
            used = sum(p.stat().st_size for p in candidates)
            for path in candidates:
                if used + size <= self.max_bytes:
                    break
                identity = path.name.removesuffix(".partial")
                with (self.root / f"{identity}.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    used -= path.stat().st_size
                    _discard(path)
            if used + size > self.max_bytes:
                return False
            with (self.root / f"{key}.partial").open("a+b") as reservation:
                reservation.truncate(size)
            return True

    def _acquire(self, source: Download, path: Path, retries: int, send: Send) -> None:
        note = path.with_suffix(".json")
        state = _Partial.model_validate_json(note.read_bytes()) if note.exists() else _Partial()
        offset = 0
        with path.open("r+b" if path.exists() else "w+b") as output:
            for part in state.ranges:
                remaining()
                data = output.read(part.size_bytes)
                if len(data) != part.size_bytes or hashlib.sha256(data).hexdigest() != part.sha256:
                    raise StorageUnavailable("retained download range is corrupt")
                offset += part.size_bytes
            remaining()
            if offset:
                emit(
                    "REUSE",
                    source.url,
                    reused=offset,
                    byte_range=f"bytes=0-{offset - 1}",
                    completed=True,
                )
            output.truncate(source.size_bytes)
            output.seek(offset)
            while offset < source.size_bytes:
                remaining()
                end = min(source.size_bytes, offset + self.range_bytes) - 1
                headers = {"Range": f"bytes={offset}-{end}"}
                if state.etag is not None:
                    headers["If-Match"] = state.etag
                failure = "transport interrupted"
                try:
                    reply = send("GET", source.url, None, headers)
                    remaining()
                    if reply.status in TRANSIENT_STATUSES:
                        failure = f"HTTP {reply.status}"
                        raise OSError(failure)
                except OSError:
                    remaining()
                    if retries == 0:
                        raise StorageUnavailable(
                            f"download failed ({failure}); completed ranges retained"
                        ) from None
                    retries -= 1
                    continue
                if reply.status == 403:
                    raise Denied("download refused (status 403)")
                etag = reply.headers.get("etag")
                if (
                    reply.status != 206
                    or reply.headers.get("content-range")
                    != f"bytes {offset}-{end}/{source.size_bytes}"
                    or len(reply.body) != end - offset + 1
                    or reply.headers.get("content-length") != str(end - offset + 1)
                    or not etag
                    or etag.startswith("W/")
                    or (state.etag is not None and state.etag != etag)
                ):
                    raise StorageUnavailable(
                        f"invalid download range or identity (status {reply.status})"
                    )
                output.write(reply.body)
                output.flush()
                os.fsync(output.fileno())
                state = _Partial(
                    etag=etag,
                    ranges=(
                        *state.ranges,
                        _Range(
                            size_bytes=len(reply.body),
                            sha256=hashlib.sha256(reply.body).hexdigest(),
                        ),
                    ),
                )
                temporary = note.with_suffix(".tmp")
                temporary.write_text(state.model_dump_json())
                temporary.replace(note)
                offset = end + 1
