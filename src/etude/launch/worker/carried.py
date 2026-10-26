"""A preparation's carried directory, kept in the bucket so a new rental starts warm.

On a machine the directory is `ETUDE_HOME/carried/CARRY/`
(`etude.launch.worker.prepared.carry`). In the bucket, under the storage's
`prepared/` folder, which every machine of the storage shares:

    CARRY.json          the `Locator`: which archive holds the directory
    SHA256-SIZE.tar     an uncompressed tar of it, named by its content

A build that does not find the directory on its machine reads the locator,
one bucket read, and `restore`s the archive it names through this machine's
cache (`etude.cache`), verified like any input, with tarfile's `data` filter.
After a build that restored nothing, the attempt asks its courier to `save`
the directory (`attempts/ATTEMPT/carry` names its key); the courier stores
the archive, then the locator, so a locator never names an archive that is
not there. Two first builds both store valid archives and the last locator
wins. Nothing here expires: archives stay until deleted.
"""

from __future__ import annotations

import hashlib
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from etude.cache import Cache
from etude.launch.records import Record
from etude.launch.storage.download import Download, download
from etude.launch.storage.s3 import PART_BYTES, Bucket, Grant, Send, StorageUnavailable, _send
from etude.launch.worker.prepared import carried_home, lock

GRANT = "prepared-grant.json"  # in the session's folder, written by its courier
REQUEST = "carry"  # in an attempt's folder: the key of a directory to save
_LINK_SECONDS = 3600  # a read link is used at once


class Locator(Record):
    """`prepared/CARRY.json`: the archive that holds a carried directory."""

    sha256: str
    size_bytes: int

    @property
    def archive(self) -> str:
        return f"{self.sha256}-{self.size_bytes}.tar"


def restore(grant: Grant, key: str, directory: Path, holder: str, send: Send = _send) -> int | None:
    """Fill `directory` from the bucket's archive for `key`: its size, or None without one.

    Raises `Mismatch` for an archive that is not the one its locator names,
    `StorageUnavailable` when storage cannot be read, and `tarfile.TarError`
    for an archive that cannot be unpacked; `directory` may then hold part of it.
    """
    bucket = _bucket(grant, send)
    raw = bucket.get(grant.prefix + f"{key}.json")
    if raw is None:
        return None
    locator = Locator.model_validate_json(raw)
    cache = Cache()
    staging = directory.with_name(f"{key}.tar")  # only for an archive too large to cache
    source = Download(
        url=bucket.get_url(grant.prefix + locator.archive, _LINK_SECONDS),
        sha256=locator.sha256,
        size_bytes=locator.size_bytes,
    )
    try:
        held = download(source, staging, send=send, cache=cache, holder=holder)
        with tarfile.open(held) as archive:
            archive.extractall(directory, filter="data")
    finally:
        cache.release(holder)
        staging.unlink(missing_ok=True)
    return locator.size_bytes


def save(grant: Grant, key: str, send: Send = _send) -> Locator | None:
    """Store the carried directory for `key`, then its locator; None while a build uses it.

    Raises `StorageUnavailable` when storage refuses either; a locator is then
    not written, or still names the archive it named before.
    """
    directory = carried_home() / key
    with lock(carried_home() / f"{key}.lock", lambda: False) as held:
        if held is None:
            return None
        if not directory.is_dir():
            raise StorageUnavailable(f"carried directory {key[:12]} is no longer on this machine")
        bucket = _bucket(grant, send)
        with tempfile.TemporaryFile(dir=carried_home()) as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for path in sorted(directory.iterdir()):
                    tar.add(path, arcname=path.name)
            size = archive.tell()
            archive.seek(0)
            locator = Locator(
                sha256=hashlib.file_digest(archive, "sha256").hexdigest(), size_bytes=size
            )
            bucket.put_parts(grant.prefix + locator.archive, _chunks(archive))
    bucket.put(grant.prefix + f"{key}.json", locator.model_dump_json().encode())
    return locator


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    stream.seek(0)
    while chunk := stream.read(PART_BYTES):
        yield chunk


def _bucket(grant: Grant, send: Send) -> Bucket:
    access = grant.bucket
    if access is None:
        raise StorageUnavailable("the prepared grant has no keys to read with")
    return Bucket(access.name, access.region, access.keys, send, host=access.host)
