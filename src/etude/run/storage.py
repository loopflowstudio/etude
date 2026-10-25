"""Where a run's files are kept: the author's own directory or bucket.

Metric blocks, table files, stored checkpoints and files under a run's
`artifacts/` are immutable (`etude.carriage.immutable`): written once and never
changed. Small manifests
and records are replaced atomically. Covered blocks
may be deleted only after their permanent replacement arrives. A bucket's
reader keeps what it reads in Etude's one cache (`etude.cache`), so no computer
reads the same bytes from a bucket twice.
Keys are relative, slash-separated and never leave the configured root. A run's
folder is named for the run (`run_directory`).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, Protocol

from etude.cache import Cache, Fetched
from etude.carriage import immutable
from etude.run.errors import EtudeError

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,199}$")


def run_prefix(run: str) -> str:
    """A run's folder under a root: its name, which is unique there whatever its experiment."""
    return f"{run}/"


class Storage(Protocol):
    """A flat key space. `put` replaces; `keys` lists every key under a prefix, and
    `versions` lists them with a tag that changes whenever an object's bytes do."""

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

    def versions(self, prefix: str) -> dict[str, str]: ...


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
        return sorted(self.versions(prefix))

    def versions(self, prefix: str) -> dict[str, str]:
        """Each file's modification time and size: a replaced file changes one or both."""
        base = self._path(prefix) if prefix else self.root
        if not base.is_dir():
            return {}
        found = {}
        for path in base.rglob("*"):
            if path.name.endswith(".partial"):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue  # replaced or removed while listed
            if S_ISREG(stat.st_mode):
                found[path.relative_to(self.root).as_posix()] = f"{stat.st_mtime_ns}:{stat.st_size}"
        return found


class S3Storage:
    """An S3 or R2 bucket reached with the author's own credentials.

    `client` is a boto3 S3 client. When none is given, boto3 is imported and
    configured from the environment: for S3 as boto3 itself is, and for R2 at
    `https://R2_ACCOUNT_ID.r2.cloudflarestorage.com` with `R2_ACCESS_KEY_ID`
    and `R2_SECRET_ACCESS_KEY`, as DuckDB's `r2://` reads them. So the logging
    client itself has no cloud dependency. Reads go through `cache` (by default
    this computer's, shared with the launcher): an immutable object is fetched
    once, any other is asked for again only by ETag, and an immutable object's
    ranges are each fetched once, or read from the whole object when it is
    held. Writes carry a SHA256 that the bucket checks; nothing reads an upload
    back.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        cache: Cache | None = None,
        provider: Literal["s3", "r2"] = "s3",
    ) -> None:
        self.client = client or _client(provider)
        self.bucket, self.provider = bucket, provider
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.name = f"{provider}-{bucket}-{self.prefix.strip('/').replace('/', '-')}".rstrip("-")
        self.cache = cache or Cache()
        # The same bucket name may exist at several S3-compatible endpoints.
        self._where = f"{self.client.meta.endpoint_url}/{bucket}/{self.prefix}"

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
        return f"{self.provider}://{self.bucket}/{self.prefix}{key}"

    def get(self, key: str) -> bytes:
        return self.cache.read(
            self._where + key, lambda etag: self._get(key, unless=etag), immutable=immutable(key)
        )

    def get_range(self, key: str, start: int, length: int) -> bytes:
        if length == 0:
            return b""
        byte_range = f"bytes={start}-{start + length - 1}"
        if not immutable(key):
            found = self._get(key, byte_range)
            assert found is not None
            return found.body
        whole = self.cache.read_range(self._where + key, start, length)
        if whole is not None:
            return whole
        return self.cache.read(
            f"{self._where}{key}\0{byte_range}",
            lambda etag: self._get(key, byte_range),
            immutable=True,
        )

    def _get(
        self, key: str, byte_range: str | None = None, *, unless: str | None = None
    ) -> Fetched | None:
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
        return Fetched(body=reply["Body"].read(), etag=reply.get("ETag"))

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
        return sorted(self.versions(prefix))

    def versions(self, prefix: str) -> dict[str, str]:
        """Each object's ETag, size and time, from the listing alone."""
        found: dict[str, str] = {}
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix + prefix
        )
        for page in pages:
            for item in page.get("Contents", []):
                tag = f"{item.get('ETag')}:{item.get('Size')}:{item.get('LastModified')}"
                found[item["Key"][len(self.prefix) :]] = tag
        return found


def _client(provider: Literal["s3", "r2"]) -> Any:
    try:
        # This cloud entry point loads its SDK; local tracking does not need it.
        import boto3  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
        from botocore.config import Config  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
    except ImportError:
        raise EtudeError(f"{provider}_requires_boto3", 422) from None
    if provider == "s3":
        return boto3.client("s3")
    try:
        account, access, secret = (
            os.environ[name]
            for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        )
    except KeyError as missing:
        raise EtudeError(f"r2_requires_{missing.args[0].lower()}", 422) from None
    # Uploads still name SHA256 themselves; this stops boto3 adding CRC checksums
    # of its own, which R2 is not relied on to accept. R2 has no object versions,
    # so nothing here asks for one.
    config = Config(
        request_checksum_calculation="when_required", response_checksum_validation="when_required"
    )
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto",
        config=config,
    )


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
    """`s3://bucket/prefix` or `r2://bucket/prefix` is a bucket; anything else is a directory."""
    text = str(uri)
    providers: tuple[Literal["s3", "r2"], ...] = ("s3", "r2")
    for provider in providers:
        if text.startswith(f"{provider}://"):
            bucket, _, prefix = text.removeprefix(f"{provider}://").partition("/")
            return S3Storage(bucket, prefix, provider=provider)
    return DirectoryStorage(text)
