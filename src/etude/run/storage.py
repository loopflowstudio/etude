"""Where a run's files are kept: the author's own directory or bucket.

Metric blocks and table files are sealed (`sealed`): written once and never
changed. Small manifests and records are replaced atomically. Covered blocks
may be deleted only after their permanent replacement arrives. A bucket's
reader keeps what it reads in an `ObjectCache`, so no computer reads the same
bytes from a bucket twice.
Keys are relative, slash-separated and never leave the configured root. A run's
folder is named for the run (`run_directory`).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from etude.run.errors import EtudeError

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,199}$")


def sealed(key: str) -> bool:
    """Whether a run's file never changes once written: a metric block or tail, or a table file.

    The key may sit under any folders; its last two parts decide.
    """
    folder, _, name = key.rpartition("/")
    kind = folder.rpartition("/")[2]
    return (kind == "metrics" and name.endswith((".blk", ".tail"))) or (
        kind == "tables" and name.endswith(".parquet")
    )


def run_prefix(run: str) -> str:
    """A run's folder under a root: its name, which is unique there whatever its experiment."""
    return f"{run}/"


class Storage(Protocol):
    """A flat key space. `put` replaces; `keys` lists every key under a prefix."""

    name: str

    def put(self, key: str, data: bytes) -> None: ...

    def put_file(self, key: str, path: Path) -> None:
        """Store a file of any size, such as a checkpoint, without reading it into memory."""
        ...

    def delete(self, key: str) -> bool:
        """Remove a covered object; False when this destination cannot delete."""
        ...

    def get(self, key: str) -> bytes: ...

    def get_range(self, key: str, start: int, length: int) -> bytes: ...

    def uri(self, key: str) -> str:
        """The address a reader with the author's access would fetch this key from."""
        ...

    def keys(self, prefix: str) -> list[str]: ...


class DirectoryStorage:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.name = "directory-" + self.root.resolve().as_posix().strip("/").replace("/", "-")

    def _path(self, key: str) -> Path:
        path = Path(key)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise EtudeError("storage_key_denied", 403)
        return self.root / path

    def put(self, key: str, data: bytes) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(data)
        partial.replace(target)  # Readers never see half an object.

    def put_file(self, key: str, path: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        shutil.copyfile(path, partial)
        partial.replace(target)

    def delete(self, key: str) -> bool:
        self._path(key).unlink(missing_ok=True)
        return True

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def get_range(self, key: str, start: int, length: int) -> bytes:
        with self._path(key).open("rb") as stream:
            stream.seek(start)
            return stream.read(length)

    def uri(self, key: str) -> str:
        return self._path(key).resolve().as_uri()

    def keys(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.is_dir():
            return []
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and not path.name.endswith(".partial")
        )


@dataclass(frozen=True)
class Kept:
    body: bytes
    etag: str | None


class ObjectCache:
    """Objects this computer has already read from one bucket, by key, on its disk.

    A bucket bills every byte it sends, so a reader asks for an object's bytes
    at most once per computer: a sealed object is never asked for again, and
    any other is asked for with the ETag of the bytes kept, which storage
    answers without a body while they are current. Each entry is one file,
    replaced whole, so concurrent readers see an old entry or a new one and
    never half of one. The least recently used go first beyond `max_bytes`.
    The default `root` is `ETUDE_HOME/objects`.
    """

    def __init__(
        self, bucket: str, root: Path | None = None, max_bytes: int = 20 * 1024**3
    ) -> None:
        self.bucket = bucket
        self.root = root or Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser() / "objects"
        self.max_bytes = max_bytes
        self._used: int | None = None  # bytes held, counted once and then kept up to date

    def _path(self, name: str) -> Path:
        return self.root / hashlib.sha256(f"{self.bucket}\0{name}".encode()).hexdigest()

    def load(self, name: str) -> Kept | None:
        path = self._path(name)
        try:
            raw = path.read_bytes()
            os.utime(path)  # recently used
        except FileNotFoundError:
            return None
        # One line of ETag ("" when storage gave none), then the body.
        etag, _, body = raw.partition(b"\n")
        return Kept(body=body, etag=etag.decode() or None)

    def keep(self, name: str, body: bytes, etag: str | None) -> None:
        if len(body) > self.max_bytes:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as partial:
            partial.write((etag or "").encode() + b"\n" + body)
        Path(partial.name).replace(self._path(name))
        if self._used is not None:
            self._used += len(body)
        if self._used is None or self._used > self.max_bytes:
            self._evict()

    def _evict(self) -> None:
        entries: list[tuple[float, int, Path]] = []
        for path in self.root.iterdir():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue  # another process evicted it
            if path.suffix != ".tmp":
                entries.append((stat.st_mtime, stat.st_size, path))
        used = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries):
            if used <= self.max_bytes:
                break
            path.unlink(missing_ok=True)
            used -= size
        self._used = used


class S3Storage:
    """An S3-compatible bucket reached with the author's own credentials.

    `client` is a boto3 S3 client; boto3 is imported only when none is given,
    so the logging client itself has no cloud dependency. Reads go through
    `cache` (by default this computer's): a sealed object is fetched once, any
    other is asked for again only by ETag, and a sealed block's ranges are
    each fetched once. Writes carry a SHA256 that the bucket checks; nothing
    reads an upload back.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        cache: ObjectCache | None = None,
    ) -> None:
        if client is None:
            try:
                # This cloud entry point loads its SDK; local tracking does not need it.
                import boto3  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
            except ImportError:
                raise EtudeError("s3_requires_boto3", 422) from None
            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.name = f"s3-{bucket}-{self.prefix.strip('/').replace('/', '-')}".rstrip("-")
        # The same bucket name may exist at several S3-compatible endpoints.
        self.cache = cache or ObjectCache(f"{client.meta.endpoint_url}/{bucket}")

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self.prefix + key, Body=data, ChecksumAlgorithm="SHA256"
        )

    def put_file(self, key: str, path: Path) -> None:
        # boto3 splits a large file into parts, checksums and retries each itself.
        self.client.upload_file(
            str(path), self.bucket, self.prefix + key, ExtraArgs={"ChecksumAlgorithm": "SHA256"}
        )

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"

    def get(self, key: str) -> bytes:
        name = self.prefix + key
        kept = self.cache.load(name)
        if kept is not None and sealed(key):
            return kept.body
        found = self._get(key, unless=None if kept is None else kept.etag)
        if found is None:
            assert kept is not None  # only a conditional read is answered "not modified"
            return kept.body
        body, etag = found
        self.cache.keep(name, body, etag)
        return body

    def get_range(self, key: str, start: int, length: int) -> bytes:
        if length == 0:
            return b""
        byte_range = f"bytes={start}-{start + length - 1}"
        if not sealed(key):
            found = self._get(key, byte_range)
            assert found is not None
            return found[0]
        name = f"{self.prefix}{key}\0{byte_range}"
        whole = self.cache.load(self.prefix + key)
        if whole is not None:
            return whole.body[start : start + length]
        kept = self.cache.load(name)
        if kept is not None:
            return kept.body
        found = self._get(key, byte_range)
        assert found is not None
        self.cache.keep(name, found[0], found[1])
        return found[0]

    def _get(
        self, key: str, byte_range: str | None = None, *, unless: str | None = None
    ) -> tuple[bytes, str | None] | None:
        """The body and its ETag; None when `unless` names the current version."""
        # Cloud exceptions are untyped; normalize only expected missing objects.
        options = {} if byte_range is None else {"Range": byte_range}
        if unless is not None:
            options["IfNoneMatch"] = unless
        try:
            reply = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key, **options)
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from error
            if unless is not None and code in ("304", "NotModified"):
                return None
            raise
        body: bytes = reply["Body"].read()
        etag: str | None = reply.get("ETag")
        return body, etag

    def delete(self, key: str) -> bool:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.prefix + key)
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") in ("AccessDenied", "403"):
                return False
            raise
        return True

    def keys(self, prefix: str) -> list[str]:
        found: list[str] = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix + prefix
        )
        for page in pages:
            found += [item["Key"][len(self.prefix) :] for item in page.get("Contents", [])]
        return sorted(found)


def default_root() -> Path:
    """Where runs are written and read when nothing says otherwise.

    One place per computer, so a report or the server finds every run started
    here without being told where to look. The launcher sets `ETUDE_TRACK_DIR`
    for a job; `ETUDE_HOME` moves everything Etude keeps.
    """
    given = os.environ.get("ETUDE_TRACK_DIR")
    if given:
        return Path(given)
    return Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser() / "track"


def run_directory(directory: Path | str | None, experiment: str, run: str) -> tuple[Path, Path]:
    """The runs directory and this run's folder in it; refuses names unsafe as paths."""
    for name in (experiment, run):
        if not SAFE_NAME.match(name):
            raise EtudeError("unsafe_run_or_experiment_name", 422)
    base = Path(directory) if directory else default_root()
    return base, base / run_prefix(run)


def storage_from_uri(uri: str | Path) -> Storage:
    """`s3://bucket/prefix` is a bucket; anything else is a local directory."""
    text = str(uri)
    if text.startswith("s3://"):
        bucket, _, prefix = text[5:].partition("/")
        return S3Storage(bucket, prefix)
    return DirectoryStorage(text)
