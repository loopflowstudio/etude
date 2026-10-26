"""One cache for the bytes Etude reads from storage, shared by every reader on this computer.

Every read names an `Artifact`: where its bytes are kept (the object's unsigned
URL) and, when recorded, their SHA256 and length. `Cache.read` answers with its
bytes or a range of them, `Cache.place` copies them to a path and `Cache.hold`
keeps them in place for a holder until it releases them; these are views
of one acquisition, so a checkpoint a job fetched and the same object a report
reads are read from storage once, in either order, by any process. Content is
kept once by SHA256 and length (`Content`), verified before it is published and
again before it, or a range of it, is reused. A whole-file digest cannot
authenticate a range, so a range of a recorded digest is read from verified
whole content, acquired first when not held.

Each location notes the content it holds and the ETag that came with it. A
recorded digest names bytes outright; without one, a location's held bytes are
reused only when it is immutable, or when storage answers by ETag that it has
not changed. That answer is a metadata check, never a payload transfer; a
changed object is read again, and until it arrives the last verified copy
stays. ETags establish freshness, never integrity. An interrupted acquisition
keeps its completed ranges, each with its own digest, and resumes from them
only under the same ETag, so ranges never mix object versions.

Transports (`etude.run.storage.S3Storage`, `etude.launch.storage.download`)
are `Source`s: they perform the I/O and keep nothing of their own. Beyond
`max_bytes` the least recently used content no holder pins goes first;
eviction removes only this computer's copies. Held content is read-only
(0444), so a holder may link it rather than copy it. Local hashing and copying share the
caller's deadline (`etude.deadline`) on the invoking thread, so no abandoned
writer publishes after it expires.

Under `root` (default `ETUDE_HOME/cache`):

    SHA256-SIZE             verified content, read-only
    SHA256-SIZE.partial     an acquisition in progress; .json notes its ranges
    SHA256-SIZE.lock        held by whoever reads, writes or evicts it
    SHA256-SIZE.HOLDER.pin  kept through eviction until HOLDER releases it
    capacity.lock           held while room is made
    locators/HASH.json      which content a location holds, and its ETag
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
import time
from collections.abc import Iterator
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
class Fetched:
    """An object's bytes, or a range of them, and the ETag storage gave them, if any."""

    body: bytes
    etag: str | None


class Artifact(_Record):
    """Bytes at a stable location, with whatever is recorded about which bytes they are.

    `location` is the object's unsigned URL, endpoint included
    (`https://BUCKET.HOST/KEY`), the same whoever reads it: a signed link is
    how bytes are reached, never which. A recorded SHA256 and length name the
    bytes outright; without them the location's held bytes are reused only
    when `immutable` says they cannot have changed, or storage says by ETag
    that they have not.
    """

    location: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    immutable: bool = False

    @property
    def content(self) -> Content | None:
        if self.sha256 is None or self.size_bytes is None:
            return None
        return Content(sha256=self.sha256, size_bytes=self.size_bytes)


class Source(Protocol):
    """How one artifact's bytes are read from storage: a transport, keeping nothing."""

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        """Bytes `start` to `end` inclusive, or to the end, of the version `etag` names when given.

        Raises when it cannot: an acquisition keeps what it completed.
        """
        ...

    def changed(self, etag: str) -> Fetched | None:
        """The whole object unless `etag` names its current version; None is a check alone."""
        ...

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        """Note bytes answered from this computer rather than storage."""
        ...


class _Part:
    """One range of a source, read as an object of its own; a range never changes version."""

    def __init__(self, source: Source, start: int, end: int) -> None:
        self.source, self.start, self.end = source, start, end

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        last = self.end if end is None else self.start + end
        return self.source.read(self.start + start, last, etag)

    def changed(self, etag: str) -> Fetched | None:
        raise AssertionError("a range is immutable and never asked whether it changed")

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        self.source.reused(size_bytes, f"bytes={self.start}-{self.end}")


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


def _admit(partial: Path, target: Path) -> None:
    """Publish a completed acquisition as held content, under the capacity lock."""
    with _lock(target.parent / "capacity.lock"):
        partial.chmod(0o444)  # verified content never changes; holders may link it
        partial.replace(target)
        partial.with_suffix(".json").unlink(missing_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    with path.open("a") as lock:
        while True:
            remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(remaining(0.05) or 0.05)
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

    def read(
        self, artifact: Artifact, source: Source, *, start: int = 0, length: int | None = None
    ) -> bytes:
        """The artifact's bytes, or `length` of them from `start`, asking storage only as needed.

        Held bytes of a named or immutable artifact answer without asking; a
        mutable one is asked for again with the ETag its held bytes came with.
        A recorded digest requires verifying the whole artifact before exposing
        a range. Location-only immutable objects can fetch and keep ranges alone.
        """
        if length is not None:
            return self._read_range(artifact, source, start, length)
        if start:
            raise ValueError("a read from an offset names its length")
        content = artifact.content
        if content is None:
            return self._read_located(artifact, source)
        held = self._load(content)
        if held is not None:
            source.reused(content.size_bytes, None)
            self._locate(artifact.location, content, None)
            return held
        with tempfile.TemporaryDirectory(dir=self._made()) as scratch:
            return self.place(artifact, Path(scratch) / content.name, source).read_bytes()

    def place(self, artifact: Artifact, destination: Path, source: Source) -> Path:
        """Copy the artifact's verified bytes to `destination`, reading only what is not held.

        Content larger than the whole allowance is staged beside the destination
        instead, and is not read again when the destination already holds it.
        Raises `Corrupt` or `Mismatch` and leaves `destination` as it was.
        """
        content = artifact.content
        if content is None:
            body = self.read(artifact, source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _replace(destination, body)
            return destination
        with self._holding(artifact, content, destination, source, None) as held:
            return held if held == destination else _publish(held, destination)

    def hold(self, artifact: Artifact, source: Source, *, holder: str, staging: Path) -> Path:
        """The artifact's verified, read-only bytes in this cache, kept until `holder` releases them.

        Nothing is copied: the caller links the returned path. Content larger
        than the whole allowance is staged at `staging` instead, as `place` does.
        """
        content = artifact.content
        if content is None:
            raise ValueError("only content named by its digest can be held")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", holder):
            raise ValueError(f"holder {holder!r} must be a plain name")
        with self._holding(artifact, content, staging, source, holder) as held:
            return held

    def held(self, content: Content) -> Path | None:
        """Where held content is, verified now; None when absent, and discarded when corrupt."""
        path = self.root / content.name
        if not path.exists():
            return None
        with _lock(self._made() / f"{content.name}.lock"):
            if _verify(path, content):
                return path
            with _lock(self.root / "capacity.lock"):
                path.unlink(missing_ok=True)
        return None

    def release(self, holder: str) -> None:
        """Let eviction remove what `holder` kept; its bytes stay until it does."""
        for pin in self.root.glob(f"*.{holder}.pin"):
            pin.unlink(missing_ok=True)

    @contextmanager
    def _holding(
        self,
        artifact: Artifact,
        content: Content,
        destination: Path,
        source: Source,
        holder: str | None,
    ) -> Iterator[Path]:
        """Verified bytes, held under their lock while the caller uses them.

        The cache's own copy, pinned for `holder` when given; or, for content
        larger than the whole allowance, `destination` itself.
        """
        key = content.name
        with _lock(self._made() / f"{key}.lock"):
            target = self.root / key
            if target.exists():
                if not _verify(target, content):
                    with _lock(self.root / "capacity.lock"):
                        target.unlink()
                    raise Corrupt("cached download digest mismatch")
                target.touch()
                source.reused(content.size_bytes, None)
                self._pin(content, holder)
                self._locate(artifact.location, content, None)
                yield target
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            cacheable = content.size_bytes <= self.max_bytes and self._make_room(
                key, content.size_bytes
            )
            if not cacheable and _verify(destination, content):
                # Too large to cache, but a retried attempt or another job on
                # this machine already holds it where it goes.
                source.reused(content.size_bytes, None)
                yield destination
                return
            partial = (
                self.root / f"{key}.partial"
                if cacheable
                else destination.parent / f".{key}.partial"
            )
            etag = self._acquire(content, partial, source)
            if not _verify(partial, content):
                with _lock(self.root / "capacity.lock"):
                    _discard(partial)
                raise Mismatch("download length or SHA256 mismatch")
            if cacheable:
                _admit(partial, target)
                self._pin(content, holder)
                self._locate(artifact.location, content, etag)
                yield target
                return
            # Staging is already beside the destination, on the same filesystem.
            partial.chmod(0o600)
            remaining()
            partial.replace(destination)
            _discard(partial)
            yield destination

    def _read_located(self, artifact: Artifact, source: Source) -> bytes:
        """Bytes whose digest is not recorded: the location says which bytes are held."""
        located = self._located(artifact.location)
        held = None if located is None else self._load(located.content)
        if held is not None and artifact.immutable:
            source.reused(len(held), None)
            return held
        if held is not None and located is not None and located.etag is not None:
            fetched = source.changed(located.etag)
            if fetched is None:
                source.reused(len(held), None)
                return held
        else:
            fetched = source.read(0, None, None)
        content = Content.of(fetched.body)
        if self._keep(content, fetched.body):
            self._locate(artifact.location, content, fetched.etag)
        return fetched.body

    def _read_range(self, artifact: Artifact, source: Source, start: int, length: int) -> bytes:
        if start < 0 or length < 0:
            raise ValueError("a range starts and runs forward from a nonnegative offset")
        if length == 0:
            return b""
        end = start + length - 1
        if artifact.content is not None and end >= artifact.content.size_bytes:
            raise ValueError("a range ends past the artifact's recorded length")
        if artifact.content is None and not artifact.immutable:
            # A mutable object's range cannot be checked against what is held.
            return source.read(start, end, None).body
        located = self._located(artifact.location)
        content = artifact.content or (None if located is None else located.content)
        held = None if content is None else self._slice(content, start, length)
        if held is not None:
            source.reused(length, f"bytes={start}-{end}")
            return held
        if artifact.content is not None:
            # A whole-file digest cannot authenticate an isolated range. Use the
            # same verified acquisition as a whole read, including when too large
            # to retain, and expose only the requested bytes after it succeeds.
            with tempfile.TemporaryDirectory(dir=self._made()) as scratch:
                path = self.place(artifact, Path(scratch) / artifact.content.name, source)
                with path.open("rb") as stream:
                    stream.seek(start)
                    return stream.read(length)
        part = Artifact(location=f"{artifact.location}\0bytes={start}-{end}", immutable=True)
        return self._read_located(part, _Part(source, start, end))

    def _slice(self, content: Content, start: int, length: int) -> bytes | None:
        """A range of held content, verified and locked against eviction while read."""
        if start + length > content.size_bytes:
            return None
        path = self.root / content.name
        with _lock(self._made() / f"{content.name}.lock"):
            if not path.exists():
                return None
            if not _verify(path, content):
                with _lock(self.root / "capacity.lock"):
                    path.unlink(missing_ok=True)
                return None
            path.touch()
            with path.open("rb") as stream:
                stream.seek(start)
                body = stream.read(length)
        return body if len(body) == length else None

    def _pin(self, content: Content, holder: str | None) -> None:
        if holder is not None:
            (self.root / f"{content.name}.{holder}.pin").touch()

    def _made(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.root

    def _locator(self, location: str) -> Path:
        return self.root / "locators" / f"{hashlib.sha256(location.encode()).hexdigest()}.json"

    def _located(self, location: str) -> _Located | None:
        try:
            return _Located.model_validate_json(self._locator(location).read_bytes())
        except FileNotFoundError:
            return None

    def _locate(self, location: str, content: Content, etag: str | None) -> None:
        """Note which held bytes a location holds; an unknown ETag keeps a known one of the same bytes."""
        located = self._located(location)
        if located is not None and located.content == content and etag is None:
            return
        self.root.joinpath("locators").mkdir(exist_ok=True)
        _replace(
            self._locator(location), _Located(content=content, etag=etag).model_dump_json().encode()
        )

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
        key = content.name
        with _lock(self._made() / f"{key}.lock"):
            target = self.root / key
            if target.exists():
                target.touch()
                return True
            if not self._make_room(key, content.size_bytes):
                return False
            partial = self.root / f"{key}.partial"
            partial.write_bytes(body)
            _admit(partial, target)
            return True

    def _make_room(self, key: str, size: int) -> bool:
        # Reserve capacity under the cache-wide lock. Other acquisitions hold
        # their content lock, so eviction never removes an active reader/writer.
        with _lock(self.root / "capacity.lock"):
            pinned = {p.name.partition(".")[0] for p in self.root.glob("*.pin")}
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

    def _acquire(self, content: Content, path: Path, source: Source) -> str | None:
        """Read what the partial lacks, a range at a time; the version's ETag, if any."""
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
        return state.etag
