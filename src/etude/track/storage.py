"""Where a run's files are kept: the author's own directory or bucket.

Metric blocks are immutable; small manifests are replaced atomically.
Covered blocks may be deleted only after their permanent replacement arrives. Keys are relative,
slash-separated and never leave the configured root.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Protocol

from etude.track.errors import EtudeError


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


class S3Storage:
    """An S3-compatible bucket reached with the author's own credentials.

    `client` is a boto3 S3 client; boto3 is imported only when none is given,
    so the logging client itself has no cloud dependency.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: Any = None) -> None:
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

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)

    def put_file(self, key: str, path: Path) -> None:
        # boto3 splits a large file into parts and retries them itself.
        self.client.upload_file(str(path), self.bucket, self.prefix + key)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}{key}"

    def get(self, key: str) -> bytes:
        return self._get(key)

    def get_range(self, key: str, start: int, length: int) -> bytes:
        if length == 0:
            return b""
        return self._get(key, f"bytes={start}-{start + length - 1}")

    def _get(self, key: str, byte_range: str | None = None) -> bytes:
        # Cloud exceptions are untyped; normalize only expected missing objects.
        options = {} if byte_range is None else {"Range": byte_range}
        try:
            body: bytes = self.client.get_object(
                Bucket=self.bucket, Key=self.prefix + key, **options
            )["Body"].read()
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(key) from error
            raise
        return body

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


def storage_from_uri(uri: str | Path) -> Storage:
    """`s3://bucket/prefix` is a bucket; anything else is a local directory."""
    text = str(uri)
    if text.startswith("s3://"):
        bucket, _, prefix = text[5:].partition("/")
        return S3Storage(bucket, prefix)
    return DirectoryStorage(text)
