"""Verified immutable downloads over presigned HTTP links, kept in Etude's one cache.

A link names the artifact it reads: its URL without the signature is the
object's location, the same one a bucket reader names, so `etude.cache.Cache`
reuses bytes either side read first. The SHA256 and length identify the bytes;
the cache holds, verifies, resumes and publishes them. This module only reads
over HTTP, validates each reply, retries transient failures within one
allowance, and observes transfers, reuse included.

One connection from a rented pod to R2 measured 4–12 MB/s, and sixteen at once
about 205 MB/s, with an occasional part taking 35 s (eb2edc, 2026-10-26). So a
range the cache asks for is read as concurrent parts of `PART_BYTES`, and the
next range of the same length is read ahead, so that one slow part does not
idle every connection. At most two ranges are held in memory.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field

from etude.cache import Artifact, Cache, Corrupt, Fetched
from etude.cache import Mismatch as ContentMismatch
from etude.deadline import limit_time, remaining
from etude.launch.records import Record
from etude.launch.storage.s3 import (
    TRANSIENT_STATUSES,
    Denied,
    Reply,
    Send,
    StorageUnavailable,
    _send,
)
from etude.launch.storage.transport import Pending, attribute, emit, submit

PART_BYTES = 16 * 1024**2
CONNECTIONS = 32


class Download(Record):
    url: str = Field(repr=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class Mismatch(StorageUnavailable):
    """The bytes arrived but are not the ones named; asking again cannot help."""


class _Link:
    """One presigned link; `retries` is shared by every request through it."""

    def __init__(self, source: Download, retries: int, send: Send) -> None:
        self.source, self.retries, self.send = source, retries, send
        self._retrying = threading.Lock()
        self._pool = ThreadPoolExecutor(CONNECTIONS)
        self._parts: dict[int, Pending[Fetched]] = {}  # by first byte, running or read

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        size = self.source.size_bytes
        end = size - 1 if end is None else end
        if end - start < PART_BYTES and not self._parts:
            return self._part(start, end, etag)
        bounds = self._bounds(start, end)
        # The cache asks next for the range after this one, of the same length.
        for first, last in (*bounds, *self._bounds(end + 1, min(size - 1, 2 * end - start + 1))):
            if first not in self._parts:
                self._parts[first] = submit(self._pool, partial(self._part, first, last, etag))
        pieces = [self._parts.pop(first).result() for first, _ in bounds]
        if any(piece.etag != (etag or pieces[0].etag) for piece in pieces):
            raise StorageUnavailable("download parts came from different object versions")
        return Fetched(body=b"".join(piece.body for piece in pieces), etag=pieces[0].etag)

    def close(self) -> None:
        """Abandon parts read ahead that no range asked for, observing what they read."""
        for part in self._parts.values():
            part.abandon()
        self._parts.clear()
        self._pool.shutdown()

    @staticmethod
    def _bounds(start: int, end: int) -> list[tuple[int, int]]:
        return [
            (first, min(end, first + PART_BYTES - 1)) for first in range(start, end + 1, PART_BYTES)
        ]

    def _part(self, start: int, end: int, etag: str | None) -> Fetched:
        size = self.source.size_bytes
        headers = {"Range": f"bytes={start}-{end}"}
        if etag is not None:
            headers["If-Match"] = etag
        reply = self._get(headers)
        found = reply.headers.get("etag")
        if (
            reply.status != 206
            or reply.headers.get("content-range") != f"bytes {start}-{end}/{size}"
            or len(reply.body) != end - start + 1
            or reply.headers.get("content-length") != str(end - start + 1)
            or not found
            or found.startswith("W/")
            or (etag is not None and etag != found)
        ):
            raise StorageUnavailable(f"invalid download range or identity (status {reply.status})")
        return Fetched(body=reply.body, etag=found)

    def changed(self, etag: str) -> Fetched | None:
        reply = self._get({"If-None-Match": etag})
        if reply.status == 304:
            return None
        if reply.status != 200:
            raise StorageUnavailable(f"download failed (status {reply.status})")
        return Fetched(body=reply.body, etag=reply.headers.get("etag"))

    def _get(self, headers: dict[str, str]) -> Reply:
        while True:
            remaining()
            failure = "transport interrupted"
            try:
                reply = self.send("GET", self.source.url, None, headers)
                remaining()
                if reply.status in TRANSIENT_STATUSES:
                    failure = f"HTTP {reply.status}"
                    raise OSError(failure)
                break
            except OSError:
                remaining()
                with self._retrying:
                    if self.retries == 0:
                        raise StorageUnavailable(
                            f"download failed ({failure}); completed ranges retained"
                        ) from None
                    self.retries -= 1
        if reply.status == 403:
            raise Denied("download refused (status 403)")
        return reply

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        emit("REUSE", self.source.url, reused=size_bytes, byte_range=byte_range, completed=True)


def download(
    source: Download,
    destination: Path,
    *,
    timeout: float | None = None,
    retries: int = 2,
    send: Send = _send,
    cache: Cache | None = None,
    holder: str | None = None,
) -> Path:
    """Place `source`'s verified bytes at `destination`, through this computer's cache.

    With `holder`, nothing is copied: the bytes stay in the cache, kept for
    `holder` (`Cache.hold`), and their path is returned; `destination` is used
    only for content larger than the cache's whole allowance.
    Raises `Mismatch` when storage holds other bytes, and `StorageUnavailable`
    when it cannot be read; `destination` is left as it was.
    """
    if retries < 0:
        raise ValueError("retry allowance must be nonnegative")
    # The signature is how this machine may read the object, not which object it is.
    location = urlsplit(source.url)._replace(query="", fragment="").geturl()
    artifact = Artifact(location=location, sha256=source.sha256, size_bytes=source.size_bytes)
    with (
        limit_time(timeout),
        attribute(transfer=uuid4().hex, sha256=source.sha256, size_bytes=str(source.size_bytes)),
    ):
        cache = cache or Cache()
        link = _Link(source, retries, send)
        try:
            if holder is not None:
                return cache.hold(artifact, link, holder=holder, staging=destination)
            return cache.place(artifact, destination, link)
        except ContentMismatch as error:
            raise Mismatch(str(error)) from None
        except Corrupt as error:
            raise StorageUnavailable(str(error)) from None
        finally:
            link.close()
