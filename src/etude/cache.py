"""One cache for the bytes Etude reads from storage, shared by every reader on this computer.

Content is kept once by its SHA256 and length (`Content`), whoever asked for
it: a table a report read and a checkpoint the launcher downloaded land in the
same place, and either reuses the other's bytes. Content is verified before it
is published and again before it is reused whole.

`Cache.fetch` places named content at a destination, reading it a range at a
time from a `Source`. An interrupted acquisition keeps its completed ranges,
each with its own digest, and resumes from them only under the same ETag, so
ranges never mix object versions. `Cache.read` answers by locator, where bytes
were read from (an endpoint, bucket and key, or a range of one): an immutable
object's locator answers without asking storage again, and a mutable one is
asked for again only with the ETag its bytes came with. ETags establish
freshness, never integrity.

Transports (`etude.run.storage.S3Storage`, `etude.launch.storage.download`)
perform the I/O and keep nothing of their own. Beyond `max_bytes` the least
recently used unpinned content goes first; eviction removes only this
computer's copies. Local hashing and copying share the caller's deadline
(`etude.deadline`) on the invoking thread, so no abandoned writer publishes
after it expires.

Under `root` (default `ETUDE_HOME/cache`):

    SHA256-SIZE           verified content
    SHA256-SIZE.partial   an acquisition in progress; .json notes its ranges
    SHA256-SIZE.lock      held by whoever reads, writes or evicts it
    SHA256-SIZE.pin       kept through eviction
    capacity.lock         held while room is made
    locators/HASH.json    where content was read from, and its ETag
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from etude.deadline import remaining

_CHUNK = 65536


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class Content(_Record):
    """Bytes named by what they are, not where they are kept."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @staticmethod
    def of(body: bytes) -> Content:
        return Content(sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body))

    @property
    def name(self) -> str:
        return f"{self.sha256}-{self.size_bytes}"


class Corrupt(Exception):
    """Bytes this computer kept no longer match their digest; they were discarded."""


class Mismatch(Exception):
    """The bytes arrived but are not the ones named; asking again cannot help."""


@dataclass(frozen=True)
class Piece:
    body: bytes
    etag: str


@dataclass(frozen=True)
class Fetched:
    """An object's bytes and the ETag storage gave them, if any."""

    body: bytes
    etag: str | None


class Source(Protocol):
    """Where one content's bytes are read from, a range at a time."""

    def read(self, start: int, end: int, etag: str | None) -> Piece:
        """Bytes `start` to `end` inclusive, of the version `etag` names when given.

        Raises when it cannot: the acquisition keeps what it completed.
        """
        ...

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        """Note bytes answered from this computer rather than storage."""
        ...


class _Range(_Record):
    size_bytes: int
    sha256: str


class _Partial(_Record):
    etag: str | None = None
    ranges: tuple[_Range, ...] = ()


class _Located(_Record):
    content: Content
    etag: str | None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            remaining()
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    remaining()
    return digest.hexdigest()


def _verify(path: Path, content: Content) -> bool:
    valid = (
        path.is_file()
        and path.stat().st_size == content.size_bytes
        and _digest(path) == content.sha256
    )
    remaining()
    return valid


def _discard(path: Path) -> None:
    """Remove payload and range notes together while their owner holds its lock."""
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


def _replace(target: Path, body: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as partial:
        partial.write(body)
    Path(partial.name).replace(target)


def _publish(source: Path, destination: Path) -> Path:
    """A copy, never a link: a destination is the caller's to change."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            with source.open("rb") as stream:
                while True:
                    remaining()
                    chunk = stream.read(_CHUNK)
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
        self.root = root or Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser() / "cache"
        self.max_bytes, self.range_bytes = max_bytes, range_bytes

    def fetch(
        self, content: Content, destination: Path, source: Source, *, pin: bool = False
    ) -> Path:
        """Place verified content at `destination`, reading only what is not held here.

        Content larger than the whole allowance is staged beside the destination
        instead, and is not read again when the destination already holds it.
        Raises `Corrupt` or `Mismatch` and leaves `destination` as it was.
        """
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = content.name
        with _lock(self.root / f"{key}.lock"):
            target = self.root / key
            if target.exists():
                if not _verify(target, content):
                    with _lock(self.root / "capacity.lock"):
                        target.unlink()
                    raise Corrupt("cached download digest mismatch")
                target.touch()
                source.reused(content.size_bytes, None)
                self._pin(content, pin)
                return _publish(target, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            cacheable = content.size_bytes <= self.max_bytes and self._make_room(
                key, content.size_bytes
            )
            if not cacheable and _verify(destination, content):
                # Too large to cache, but a retried attempt or another job on
                # this machine already holds it where it goes.
                source.reused(content.size_bytes, None)
                return destination
            partial = (
                self.root / f"{key}.partial"
                if cacheable
                else destination.parent / f".{key}.partial"
            )
            self._acquire(content, partial, source)
            if not _verify(partial, content):
                with _lock(self.root / "capacity.lock"):
                    _discard(partial)
                raise Mismatch("download length or SHA256 mismatch")
            if cacheable:
                with _lock(self.root / "capacity.lock"):
                    partial.replace(target)
                    partial.with_suffix(".json").unlink(missing_ok=True)
                self._pin(content, pin)
                return _publish(target, destination)
            # Staging is already beside the destination, on the same filesystem.
            partial.chmod(0o600)
            remaining()
            partial.replace(destination)
            _discard(partial)
            return destination

    def read(
        self, locator: str, get: Callable[[str | None], Fetched | None], *, immutable: bool
    ) -> bytes:
        """An object's bytes by where they are read from, asking storage only when needed.

        `get(etag)` reads the object, or returns None when `etag` names its
        current version. Held bytes of an immutable object are returned without
        asking; a mutable object is asked for with the ETag of the bytes held.
        """
        located = self._located(locator)
        held = None if located is None else self._load(located.content)
        if held is not None and immutable:
            return held
        fetched = get(None if held is None or located is None else located.etag)
        if fetched is None:
            if held is None:
                raise ValueError("storage answered 'not modified' to an unconditional read")
            return held
        content = Content.of(fetched.body)
        if self._keep(content, fetched.body):
            self.root.joinpath("locators").mkdir(exist_ok=True)
            _replace(
                self._locator(locator),
                _Located(content=content, etag=fetched.etag).model_dump_json().encode(),
            )
        return fetched.body

    def read_range(self, locator: str, start: int, length: int) -> bytes | None:
        """A range of an object held whole here, or None when it is not.

        Read from disk without the rest; the whole was verified when kept.
        """
        located = self._located(locator)
        if located is None or start + length > located.content.size_bytes:
            return None
        try:
            with (self.root / located.content.name).open("rb") as stream:
                stream.seek(start)
                body = stream.read(length)
        except FileNotFoundError:
            return None
        return body if len(body) == length else None

    def unpin(self, content: Content) -> None:
        """Let eviction remove this content again; its bytes stay until it does."""
        (self.root / f"{content.name}.pin").unlink(missing_ok=True)

    def _pin(self, content: Content, pin: bool) -> None:
        if pin:
            (self.root / f"{content.name}.pin").touch()

    def _locator(self, locator: str) -> Path:
        return self.root / "locators" / f"{hashlib.sha256(locator.encode()).hexdigest()}.json"

    def _located(self, locator: str) -> _Located | None:
        try:
            return _Located.model_validate_json(self._locator(locator).read_bytes())
        except FileNotFoundError:
            return None

    def _load(self, content: Content) -> bytes | None:
        """Held content, verified; None when absent, and discarded when corrupt."""
        path = self.root / content.name
        try:
            body = path.read_bytes()
            os.utime(path)  # recently used
        except FileNotFoundError:
            return None
        if Content.of(body) == content:
            return body
        with _lock(self.root / f"{content.name}.lock"), _lock(self.root / "capacity.lock"):
            path.unlink(missing_ok=True)
        return None

    def _keep(self, content: Content, body: bytes) -> bool:
        """Hold bytes already read in full; False when they do not fit."""
        if content.size_bytes > self.max_bytes:
            return False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = content.name
        with _lock(self.root / f"{key}.lock"):
            target = self.root / key
            if target.exists():
                target.touch()
                return True
            if not self._make_room(key, content.size_bytes):
                return False
            partial = self.root / f"{key}.partial"
            partial.write_bytes(body)
            with _lock(self.root / "capacity.lock"):
                partial.replace(target)
                partial.with_suffix(".json").unlink(missing_ok=True)
            return True

    def _make_room(self, key: str, size: int) -> bool:
        # Reserve capacity under the cache-wide lock. Other acquisitions hold
        # their content lock, so eviction never removes an active reader/writer.
        with _lock(self.root / "capacity.lock"):
            pinned = {p.stem for p in self.root.glob("*.pin")}
            candidates = sorted(
                (
                    p
                    for p in self.root.iterdir()
                    if p.is_file() and p.suffix in {"", ".partial"} and not p.name.startswith(key)
                ),
                key=lambda p: p.stat().st_mtime,
            )
            used = sum(p.stat().st_size for p in candidates)
            evicted = False
            for path in candidates:
                if used + size <= self.max_bytes:
                    break
                identity = path.name.removesuffix(".partial")
                if identity in pinned:
                    continue
                with (self.root / f"{identity}.lock").open("a") as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    used -= path.stat().st_size
                    _discard(path)
                    evicted = True
            if evicted:
                self._forget_evicted()
            if used + size > self.max_bytes:
                return False
            with (self.root / f"{key}.partial").open("a+b") as reservation:
                reservation.truncate(size)
            return True

    def _forget_evicted(self) -> None:
        """Drop locators whose content is gone, so they stay as few as what is held."""
        for path in self.root.glob("locators/*.json"):
            try:
                located = _Located.model_validate_json(path.read_bytes())
            except (FileNotFoundError, ValueError):
                path.unlink(missing_ok=True)
                continue
            if not (self.root / located.content.name).exists():
                path.unlink(missing_ok=True)

    def _acquire(self, content: Content, path: Path, source: Source) -> None:
        note = path.with_suffix(".json")
        state = _Partial.model_validate_json(note.read_bytes()) if note.exists() else _Partial()
        offset = 0
        with path.open("r+b" if path.exists() else "w+b") as output:
            for part in state.ranges:
                remaining()
                data = output.read(part.size_bytes)
                if len(data) != part.size_bytes or hashlib.sha256(data).hexdigest() != part.sha256:
                    _discard(path)  # the next attempt starts over
                    raise Corrupt("retained download range is corrupt")
                offset += part.size_bytes
            remaining()
            if offset:
                source.reused(offset, f"bytes=0-{offset - 1}")
            output.truncate(content.size_bytes)
            output.seek(offset)
            while offset < content.size_bytes:
                remaining()
                end = min(content.size_bytes, offset + self.range_bytes) - 1
                piece = source.read(offset, end, state.etag)
                if len(piece.body) != end - offset + 1 or (
                    state.etag is not None and piece.etag != state.etag
                ):
                    raise Corrupt("download range does not continue its object's version")
                output.write(piece.body)
                output.flush()
                os.fsync(output.fileno())
                state = _Partial(
                    etag=piece.etag,
                    ranges=(
                        *state.ranges,
                        _Range(
                            size_bytes=len(piece.body),
                            sha256=hashlib.sha256(piece.body).hexdigest(),
                        ),
                    ),
                )
                _replace(note, state.model_dump_json().encode())
                offset = end + 1
