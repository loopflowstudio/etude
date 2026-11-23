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

import hashlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import copy
from datetime import UTC, datetime
from itertools import islice, pairwise
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, NamedTuple, Protocol, Self, cast
from urllib.parse import quote, unquote, urlsplit

from pydantic import ValidationError

from etude.cache import Artifact, Cache, Fetched, Source
from etude.carriage import immutable
from etude.deadline import call, remaining
from etude.errors import EtudeError
from etude.home import home
from etude.identity import SAFE_NAME, run_address
from etude.records import Record, RunRecord
from etude.storage_timeout import StorageUnavailable, socket_timeout

__all__ = (
    "default_root",
    "storage_from_uri",
    "Storage",
    "DirectoryStorage",
    "S3Storage",
    "ObjectVersion",
)


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


class ObjectVersion(str):
    """A storage version tag with its listing's modification time, if supplied.

    Tags remain strings so existing Storage implementations and consumers can
    compare or persist them. An opaque string supplies no evidence of age.
    """

    __slots__ = ("modified", "etag")
    etag: str | None
    modified: datetime | None

    def __new__(
        cls, tag: str, modified: datetime | None = None, *, etag: str | None = None
    ) -> Self:
        if modified is not None and modified.utcoffset() is None:
            raise ValueError("an object's modification time must include a time zone")
        version = super().__new__(cls, tag)
        version.etag = etag
        version.modified = modified.astimezone(UTC) if modified is not None else None
        return version


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
        """List file times and sizes. An unreadable subtree invalidates the listing."""
        pending = [self._path(prefix) if prefix else self.root]
        found: dict[str, str] = {}
        while pending:
            folder = pending.pop()
            try:
                entries = os.scandir(folder)
            except (FileNotFoundError, NotADirectoryError):
                continue  # Missing prefix or a directory removed during traversal.
            with entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(folder / entry.name)
                            continue
                        if entry.name.endswith(".partial"):
                            continue
                        stat = entry.stat()
                    except FileNotFoundError:
                        continue  # Dangling symlink or file removed while listed.
                    if S_ISREG(stat.st_mode):
                        key = (folder / entry.name).relative_to(self.root).as_posix()
                        found[key] = ObjectVersion(
                            f"{stat.st_mtime_ns}:{stat.st_size}",
                            datetime.fromtimestamp(stat.st_mtime, UTC),
                        )
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


_LIST_RANGES = 48


class _KeyRange(NamedTuple):
    """Keys under a bucket prefix, after an exclusive lower bound through an inclusive upper."""

    prefix: str
    after: str | None = None
    through: str | None = None


class _ListingHint(Record):
    """Remembered key count and quantile boundaries for one bucket prefix."""

    count: int
    split_points: tuple[str, ...]


def store_identity(storage: Storage) -> str:
    """Canonical location of `storage` for local caches, distinct across endpoints and paths."""
    if isinstance(storage, S3Storage):
        return storage._where + storage.prefix
    return storage.uri("_")


def with_versions(storage: Storage, versions: Mapping[str, str]) -> Storage:
    """Pin an S3/R2 reader to `versions` for ETag checks without affecting other readers."""
    return storage._with_versions(versions) if isinstance(storage, S3Storage) else storage


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
        self._listed_versions: dict[str, str] | None = None

    def _with_versions(self, versions: Mapping[str, str]) -> S3Storage:
        """Read against this listing without changing what concurrent readers trust."""
        listed = copy(self)
        listed._listed_versions = dict(versions)
        return listed

    def put(self, key: str, data: bytes) -> None:
        if self._listed_versions is not None:
            self._listed_versions.pop(key, None)
        _storage_request(
            lambda: self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + key, Body=data, ChecksumAlgorithm="SHA256"
            )
        )

    def put_file(self, key: str, path: Path) -> None:
        if self._listed_versions is not None:
            self._listed_versions.pop(key, None)
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
        if self._listed_versions is not None:
            self._listed_versions.pop(key, None)
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

    @staticmethod
    def _split_points(sorted_keys: Sequence[str]) -> tuple[str, ...]:
        """Upper bounds for equally populated ranges in a later listing. S3 pages hold 1,000 keys."""
        count = len(sorted_keys)
        ranges = min(_LIST_RANGES, (count + 999) // 1000)
        return tuple(sorted_keys[i * count // ranges - 1] for i in range(1, ranges))

    def _listing_hint_path(self, prefix: str) -> Path:
        digest = hashlib.sha256(f"{self._where}{self.prefix}{prefix}".encode()).hexdigest()[:24]
        return self.cache.root / "listings" / f"{digest}.json"

    def _read_listing_hint(self, prefix: str) -> _ListingHint | None:
        try:
            return _ListingHint.model_validate_json(self._listing_hint_path(prefix).read_bytes())
        except (OSError, ValidationError):
            return None

    def _write_listing_hint(
        self, prefix: str, found: Mapping[str, str], prior: _ListingHint | None
    ) -> None:
        path = self._listing_hint_path(prefix)
        count = len(found)
        if count <= 1000:
            if prior is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        # Refresh once the prefix has grown or shrunk enough to unbalance ranges.
        if prior is not None and abs(count - prior.count) * 5 <= max(prior.count, 1):
            return
        hint = _ListingHint(count=count, split_points=self._split_points(sorted(found)))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=path.parent) as staging:
                pending = Path(staging) / path.name
                pending.write_bytes(hint.model_dump_json().encode())
                pending.replace(path)
        except OSError:
            pass

    def versions(self, prefix: str, *, split_points: Sequence[str] | None = None) -> dict[str, str]:
        """List current versions. Remembered prefix boundaries divide large listings into ranges."""
        base = self.prefix + prefix
        prior = self._read_listing_hint(prefix) if split_points is None else None
        active_splits: Sequence[str] = (
            (prior.split_points if prior is not None else ())
            if split_points is None
            else split_points
        )

        def pages(span: _KeyRange, **options: str) -> Iterator[dict[str, Any]]:
            if span.after is not None:
                options["StartAfter"] = span.after
            while True:
                remaining()
                page = self.client.list_objects_v2(
                    Bucket=self.bucket, Prefix=span.prefix, **options
                )
                yield page
                if not page.get("IsTruncated"):
                    return
                options["ContinuationToken"] = page["NextContinuationToken"]

        def objects(page: dict[str, Any], through: str | None = None) -> dict[str, str]:
            found: dict[str, str] = {}
            for item in page.get("Contents", ()):
                if through is None or item["Key"] <= through:
                    etag, modified = item.get("ETag"), item.get("LastModified")
                    key = item["Key"][len(self.prefix) :]
                    found[key] = ObjectVersion(
                        f"{etag}:{item.get('Size')}:{modified}", modified, etag=etag
                    )
            return found

        def read_range(span: _KeyRange) -> dict[str, str]:
            part: dict[str, str] = {}
            for page in pages(span):
                part.update(objects(page, span.through))
                contents = page.get("Contents", ())
                if span.through is not None and contents and contents[-1]["Key"] >= span.through:
                    break
            return part

        def ranges(span: _KeyRange, bounds: Sequence[str]) -> list[_KeyRange]:
            return [
                _KeyRange(span.prefix, a, b)
                for a, b in pairwise((span.after, *bounds, span.through))
            ]

        def discover(
            span: _KeyRange, *, root: bool = False
        ) -> tuple[dict[str, str], list[_KeyRange]]:
            # Small prefixes finish in one or two pages. For larger ones the directory listing
            # supplies boundaries and any direct objects between the subdirectories.
            part: dict[str, str] = {}
            after = span.after
            for page in islice(pages(span), 1 if root else 2):
                part.update(objects(page))
                if not page.get("IsTruncated"):
                    return part, []
                if contents := page.get("Contents"):
                    after = contents[-1]["Key"]

            children: list[str] = []
            # List from the beginning here: StartAfter would omit the directory containing 'after'.
            for page in pages(_KeyRange(span.prefix), Delimiter="/"):
                part.update(objects(page))
                children.extend(child["Prefix"] for child in page.get("CommonPrefixes", ()))
            children = [
                key for key in children if after is None or key > after or after.startswith(key)
            ]
            if not children:
                return part, []
            if root and len(children) < 32:
                return part, [
                    _KeyRange(key, after if after is not None and after.startswith(key) else None)
                    for key in children
                ]
            count = min(_LIST_RANGES, len(children))
            bounds = [children[i * len(children) // count] for i in range(1, count)]
            return part, ranges(_KeyRange(span.prefix, after), bounds)

        def collect() -> dict[str, str]:
            root = _KeyRange(base)
            warm = (
                0 < len(active_splits) < _LIST_RANGES
                and all(key and key.startswith(prefix) for key in active_splits)
                and all(a < b for a, b in pairwise(active_splits))
            )
            if warm:
                work = ranges(root, [self.prefix + key for key in active_splits])
                found: dict[str, str] = {}
            else:
                found, work = discover(root, root=True)
                if not work:
                    return found

            with ThreadPoolExecutor(max_workers=32) as pool:
                # A few top-level folders can each conceal many runs. Probe one more level
                # in parallel before issuing all ranges; workers never wait on this pool.
                if not warm and work[0].prefix != base:
                    probes = [pool.submit(copy_context().run, discover, span) for span in work]
                    work = []
                    for probe in probes:
                        part, children = probe.result()
                        found.update(part)
                        work.extend(children)
                reads = [pool.submit(copy_context().run, read_range, span) for span in work]
                for read in reads:
                    found.update(read.result())
            return found

        found = _storage_request(collect)
        if split_points is None:
            self._write_listing_hint(prefix, found, prior)
        if self._listed_versions is not None:
            for key in [k for k in self._listed_versions if k.startswith(prefix)]:
                del self._listed_versions[key]
            self._listed_versions.update(found)
        return found


class ListedStorage:
    """Read objects against a listing using the same verified cache as every bucket reader.

    A matching listed version answers a mutable object's freshness check. Bucket
    locators and ETags are identical to those of unlisted readers. A compaction
    race can be retried by the next ingestion with a fresh listing.
    """

    def __init__(self, storage: Storage, objects: dict[str, str], cache: Cache) -> None:
        self.storage, self.objects = with_versions(storage, objects), objects
        self.cache = storage.cache if isinstance(storage, S3Storage) else cache
        self.name = storage.name
        self.uri = storage.uri
        self.put, self.put_file, self.delete = storage.put, storage.put_file, storage.delete

    def versions(self, prefix: str) -> dict[str, str]:
        return {k: v for k, v in self.objects.items() if k.startswith(prefix)}

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def _object(self, key: str) -> tuple[Artifact, Source]:
        version = self.objects.get(key)
        if version is None:
            raise FileNotFoundError(key)
        if isinstance(self.storage, S3Storage):
            return self.storage._artifact(key), _Object(self.storage, key)
        return Artifact(location=self.uri(key), immutable=immutable(key)), _StoredFile(
            self.storage, key, version=version
        )

    def get(self, key: str) -> bytes:
        artifact, source = self._object(key)
        return self.cache.read(artifact, source)

    def get_range(self, key: str, start: int, length: int) -> bytes:
        artifact, source = self._object(key)
        return self.cache.read(artifact, source, start=start, length=length)


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
        listed = self.storage._listed_versions
        version = listed.get(self.key) if listed is not None else None
        if isinstance(version, ObjectVersion) and version.etag == etag:
            return None
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
        from botocore.session import get_session  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
        from botocore.utils import parse_timestamp  # type: ignore[import-untyped,import-not-found,unused-ignore]  # noqa: PLC0415
    except ImportError:
        raise EtudeError(f"{provider}_requires_boto3", 422) from None
    timeout = socket_timeout()
    config = Config(connect_timeout=timeout, read_timeout=timeout, max_pool_connections=64)
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

    def _parse_r2_timestamp(value: str | int | float) -> datetime:
        # R2 lists one UTC timestamp per key. Other response fields can use HTTP dates or epochs.
        if isinstance(value, str) and value.endswith("Z") and "T" in value:
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return cast(datetime, parse_timestamp(value))  # Botocore's untyped datetime parser.

    session = get_session()
    session.get_component("response_parser_factory").set_parser_defaults(
        timestamp_parser=_parse_r2_timestamp
    )
    return boto3.Session(botocore_session=session).client(
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
    return home() / "track"


def run_directory(directory: Path | str | None, experiment: str, run: str) -> tuple[Path, Path]:
    """The runs directory and this run's folder in it; refuses names unsafe as paths."""
    local = run.removeprefix(experiment + "/")
    for name in (experiment, local):
        if not SAFE_NAME.match(name):
            raise EtudeError("unsafe_run_or_experiment_name", 422)
    base = Path(directory) if directory else default_root()
    handed = os.environ.get("ETUDE_RUN")
    out = os.environ.get("ETUDE_OUT")
    assigned = Path(out).parent if out and os.environ.get("ETUDE_ATTEMPT") else None
    # The assigned folder holds the launch. A tournament declared inside it
    # retains its own address even if its local name matches the launch.
    address = os.environ.get("ETUDE_RUN_ADDRESS") if handed == run and base != assigned else None
    root = (
        assigned
        if assigned and base != assigned and handed == local
        else base / run_address(address or run, experiment)
    )
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
    """`s3://bucket/prefix` or `r2://bucket/prefix` is a bucket; anything else is a directory.

    A directory may be a path or the `file://` URI that `DirectoryStorage.uri` prints.
    """
    text = str(uri)
    providers: tuple[Literal["s3", "r2"], ...] = ("s3", "r2")
    for provider in providers:
        if text.startswith(f"{provider}://"):
            bucket, _, prefix = text.removeprefix(f"{provider}://").partition("/")
            return S3Storage(bucket, prefix, provider=provider)
    if text.startswith("file://"):
        parsed = urlsplit(text)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"a file URI names no other host: {text}")
        return DirectoryStorage(unquote(parsed.path))
    return DirectoryStorage(text)


class _StoredFile:
    """A file transport. A listing supplies a version; a declared artifact supplies a size."""

    def __init__(
        self, storage: Storage, key: str, size: int | None = None, *, version: str | None = None
    ) -> None:
        self.version = version
        self.storage, self.key, self.size = storage, key, size

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        stop = end + 1 if end is not None else self.size
        if stop is None:
            body = self.storage.get(self.key)[start:]
        else:
            if self.size is not None:
                stop = min(stop, self.size)
            body = self.storage.get_range(self.key, start, stop - start)
        return Fetched(body, self.version)

    def changed(self, etag: str) -> Fetched | None:
        return None if etag == self.version else self.read(0, None, None)

    def reused(self, size_bytes: int, start: int | None, end: int | None) -> None:
        pass


def place_artifact(artifact: Artifact, destination: Path) -> Path:
    """Fetch a file or bucket artifact through the shared cache, verifying its content."""
    if artifact.content is None:
        raise ValueError("placing an artifact requires its digest and byte length")
    uri = artifact.location
    if uri.startswith(("s3://", "r2://")):
        provider, _, address = uri.partition("://")
        bucket, _, key = address.partition("/")
        storage = S3Storage(bucket, provider="r2" if provider == "r2" else "s3")
        located = storage._artifact(key).model_copy(
            update={"sha256": artifact.sha256, "size_bytes": artifact.size_bytes}
        )
        return storage.cache.place(located, destination, _Object(storage, key))
    parsed = urlsplit(uri)
    if parsed.scheme not in ("", "file") or parsed.netloc:
        raise ValueError(f"unsupported artifact location: {parsed.scheme}")
    path = Path(unquote(parsed.path))
    return Cache().place(
        artifact,
        destination,
        _StoredFile(DirectoryStorage(path.parent), path.name, artifact.content.size_bytes),
    )
