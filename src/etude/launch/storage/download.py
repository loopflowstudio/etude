"""Verified immutable downloads over presigned HTTP links, kept in Etude's one cache.

The SHA256 and length identify transport bytes; `etude.cache.Cache` holds,
verifies, resumes and publishes them. This module only reads ranges over HTTP,
validates each reply, retries transient failures within one allowance, and
observes transfers, reuse included.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pydantic import Field

from etude.cache import Cache, Content, Corrupt, Fetched
from etude.cache import Mismatch as ContentMismatch
from etude.deadline import limit_time, remaining
from etude.launch.records import Record
from etude.launch.storage.s3 import TRANSIENT_STATUSES, Denied, Send, StorageUnavailable, _send
from etude.launch.storage.transport import attribute, emit


class Download(Record):
    url: str = Field(repr=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class Mismatch(StorageUnavailable):
    """The bytes arrived but are not the ones named; asking again cannot help."""


class _Link:
    """One presigned link read a range at a time; `retries` is shared by every range."""

    def __init__(self, source: Download, retries: int, send: Send) -> None:
        self.source, self.retries, self.send = source, retries, send

    def read(self, start: int, end: int, etag: str | None) -> Fetched:
        size = self.source.size_bytes
        headers = {"Range": f"bytes={start}-{end}"}
        if etag is not None:
            headers["If-Match"] = etag
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
                if self.retries == 0:
                    raise StorageUnavailable(
                        f"download failed ({failure}); completed ranges retained"
                    ) from None
                self.retries -= 1
        if reply.status == 403:
            raise Denied("download refused (status 403)")
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
) -> Path:
    """Place `source`'s verified bytes at `destination`, through this computer's cache.

    Raises `Mismatch` when storage holds other bytes, and `StorageUnavailable`
    when it cannot be read; `destination` is left as it was.
    """
    if retries < 0:
        raise ValueError("retry allowance must be nonnegative")
    content = Content(sha256=source.sha256, size_bytes=source.size_bytes)
    with (
        limit_time(timeout),
        attribute(transfer=uuid4().hex, sha256=source.sha256, size_bytes=str(source.size_bytes)),
    ):
        try:
            return (cache or Cache()).fetch(content, destination, _Link(source, retries, send))
        except ContentMismatch as error:
            raise Mismatch(str(error)) from None
        except Corrupt as error:
            raise StorageUnavailable(str(error)) from None
