"""Where a run's files are kept: the author's own directory or bucket.

Metric blocks, table files, stored checkpoints and files under a run's
`artifacts/` are immutable (`etude.carriage.immutable`): written once and never
changed. Small manifests
and records are replaced atomically. Covered blocks
may be deleted only after their permanent replacement arrives. A bucket's
reader keeps what it reads in Etude's one cache (`etude.cache`), so no computer
reads the same bytes from a bucket twice, whether tracking, a report or a job
read them first.
Keys are relative, slash-separated and never leave the configured root. A run's
folder is named for the run (`run_directory`).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from etude.cache import Artifact, Cache, Fetched
from etude.carriage import immutable
from etude.deadline import call, remaining
from etude.errors import EtudeError
from etude.identity import SAFE_NAME, run_address
from etude.records import RunRecord
from etude.storage_timeout import StorageUnavailable, socket_timeout


def run_prefix(run: str) -> str:
    """A folder prefix for a run name or its qualified experiment/run address."""
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


def _storage_request[T](operation: Callable[[], T]) -> T:
    # Optional cloud dependency: local storage never loads botocore.
    from botocore.exceptions import (  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
        ConnectionError,
        HTTPClientError,
    )

    try:
        return call(operation)
    except FileNotFoundError:
        raise
    except (ConnectionError, HTTPClientError, OSError) as error:
        logging.getLogger(__name__).warning("storage request failed (%s)", type(error).__name__)
        raise StorageUnavailable(f"storage request failed ({type(error).__name__})") from None


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
        # An object's location is its virtual-hosted URL, as a presigned link
        # names it (`etude.launch.storage.s3.Bucket`), so either reader reuses
        # the other's bytes; the endpoint keeps same-named buckets apart.
        # boto3 reaches us-east-1 at the legacy global host, which names the
        # same buckets as the regional host the launcher signs.
        endpoint = urlsplit(self.client.meta.endpoint_url)
        host = endpoint.netloc
        if host == "s3.amazonaws.com":
            host = f"s3.{self.client.meta.region_name or 'us-east-1'}.amazonaws.com"
        self._where = f"{endpoint.scheme}://{bucket}.{host}/"

    def put(self, key: str, data: bytes) -> None:
        _storage_request(
            lambda: self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + key, Body=data, ChecksumAlgorithm="SHA256"
            )
        )

    def put_file(self, key: str, path: Path) -> None:
        # boto3 splits a large file into parts, checksums and retries each itself.
        _storage_request(
            lambda: self.client.upload_file(
                str(path), self.bucket, self.prefix + key, ExtraArgs={"ChecksumAlgorithm": "SHA256"}
            )
        )

    def uri(self, key: str) -> str:
        return f"{self.provider}://{self.bucket}/{self.prefix}{key}"

    def get(self, key: str) -> bytes:
        return self.cache.read(self._artifact(key), _Object(self, key))

    def get_range(self, key: str, start: int, length: int) -> bytes:
        return self.cache.read(self._artifact(key), _Object(self, key), start=start, length=length)

    def _artifact(self, key: str) -> Artifact:
        location = self._where + quote(self.prefix + key, safe="-_.~/")
        return Artifact(location=location, immutable=immutable(key))

    def delete(self, key: str) -> bool:
        try:
            _storage_request(
                lambda: self.client.delete_object(Bucket=self.bucket, Key=self.prefix + key)
            )
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

        def collect() -> dict[str, str]:
            found: dict[str, str] = {}
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix + prefix
            )
            for page in pages:
                for item in page.get("Contents", []):
                    tag = f"{item.get('ETag')}:{item.get('Size')}:{item.get('LastModified')}"
                    found[item["Key"][len(self.prefix) :]] = tag
            return found

        return _storage_request(collect)


class _Object:
    """One bucket object, read for the cache; reads reused from it go unrecorded."""

    def __init__(self, storage: S3Storage, key: str) -> None:
        self.storage, self.key = storage, key

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        options = {}
        if start or end is not None:
            options["Range"] = f"bytes={start}-{'' if end is None else end}"
        if etag is not None:
            options["IfMatch"] = etag
        found = self._get(options)
        assert found is not None  # only a conditional read answers "not modified"
        return found

    def changed(self, etag: str) -> Fetched | None:
        return self._get({"IfNoneMatch": etag})

    def reused(self, size_bytes: int, start: int | None, end: int | None) -> None:
        pass

    def _get(self, options: dict[str, str]) -> Fetched | None:
        """The body and its ETag; None when `IfNoneMatch` names the current version."""
        # Optional cloud dependency: only bucket reads need streaming exceptions.
        from botocore.exceptions import HTTPClientError  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415

        storage = self.storage

        def fetch() -> Fetched:
            for attempt in range(3):
                remaining()
                # botocore owns request retries. Only failures consuming the
                # returned body need another read of the original range.
                reply = storage.client.get_object(
                    Bucket=storage.bucket, Key=storage.prefix + self.key, **options
                )
                body = reply["Body"]
                try:
                    remaining()
                    return Fetched(body=body.read(), etag=reply.get("ETag"))
                except (HTTPClientError, OSError):
                    if attempt == 2:
                        raise
                finally:
                    # One reader owns the response, even if it arrives after
                    # caller expiry. Closing on the caller could block on read.
                    body.close()
                pause = remaining(0.1 * 2**attempt)
                assert pause is not None
                time.sleep(pause)
            raise AssertionError("read retry loop exhausted without a result")

        try:
            return _storage_request(fetch)
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise FileNotFoundError(self.key) from error
            if "IfNoneMatch" in options and code in ("304", "NotModified"):
                return None
            raise


def _client(provider: Literal["s3", "r2"]) -> Any:
    try:
        # This cloud entry point loads its SDK; local tracking does not need it.
        import boto3  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
        from botocore.config import Config  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
    except ImportError:
        raise EtudeError(f"{provider}_requires_boto3", 422) from None
    timeout = socket_timeout()
    config = Config(connect_timeout=timeout, read_timeout=timeout)
    if provider == "s3":
        return boto3.client("s3", config=config)
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
    config = config.merge(
        Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
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
    local = run.removeprefix(experiment + "/")
    for name in (experiment, local):
        if not SAFE_NAME.match(name):
            raise EtudeError("unsafe_run_or_experiment_name", 422)
    base = Path(directory) if directory else default_root()
    handed = os.environ.get("ETUDE_RUN")
    address = os.environ.get("ETUDE_RUN_ADDRESS") if handed == run else None
    root = base / run_address(address or run, experiment)
    if os.environ.get("ETUDE_ATTEMPT") and handed == local and (out := os.environ.get("ETUDE_OUT")):
        root = Path(out).parent
    if not (root / "run.json").is_file():
        # Runs stored before compact addresses: EXPERIMENT/RUN folders, then flat ones.
        for stored in (base / experiment / local, base / run):
            record = stored / "run.json"
            if (
                record.is_file()
                and RunRecord.model_validate_json(record.read_bytes()).experiment == experiment
            ):
                return base, stored
    return base, root


def storage_from_uri(uri: str | Path) -> Storage:
    """`s3://bucket/prefix` or `r2://bucket/prefix` is a bucket; anything else is a directory."""
    text = str(uri)
    providers: tuple[Literal["s3", "r2"], ...] = ("s3", "r2")
    for provider in providers:
        if text.startswith(f"{provider}://"):
            bucket, _, prefix = text.removeprefix(f"{provider}://").partition("/")
            return S3Storage(bucket, prefix, provider=provider)
    return DirectoryStorage(text)
