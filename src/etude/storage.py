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
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, unquote, urlsplit

from etude.cache import Artifact, Cache, Fetched
from etude.carriage import immutable
from etude.deadline import call, remaining
from etude.errors import EtudeError
from etude.identity import SAFE_NAME, run_address
from etude.records import RunRecord
from etude.storage_timeout import StorageUnavailable, socket_timeout

__all__ = ("default_root", "storage_from_uri", "Storage", "DirectoryStorage", "S3Storage")


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


_LIST_RANGES = 48


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
        # Scoped ETags from a fresh `versions` listing; trusted only inside `_revalidating`.
        self._listed_etags: dict[str, str] | None = None

    @contextmanager
    def _revalidating(self, listed: Mapping[str, str]) -> Iterator[None]:
        """Trust ETags already returned by a fresh `versions` listing during one read pass."""
        previous = self._listed_etags
        etags: dict[str, str] = {}
        for key, tag in listed.items():
            etag = tag.partition(":")[0]
            if etag and etag != "None":
                etags[key] = etag
        self._listed_etags = etags
        try:
            yield
        finally:
            self._listed_etags = previous

    def put(self, key: str, data: bytes) -> None:
        if self._listed_etags is not None:
            self._listed_etags.pop(key, None)
        _storage_request(
            lambda: self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + key, Body=data, ChecksumAlgorithm="SHA256"
            )
        )

    def put_file(self, key: str, path: Path) -> None:
        if self._listed_etags is not None:
            self._listed_etags.pop(key, None)
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
        if self._listed_etags is not None:
            self._listed_etags.pop(key, None)
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

    def versions(self, prefix: str, *, split_points: Sequence[str] = ()) -> dict[str, str]:
        """List current versions. Optional source-relative keys divide the prefix into ranges."""
        base = self.prefix + prefix
        strip = len(self.prefix)

        def tag(item: dict[str, Any]) -> str:
            return f"{item.get('ETag')}:{item.get('Size')}:{item.get('LastModified')}"

        def list_range(
            target: str,
            start_after: str | None = None,
            stop_at: str | None = None,
            token: str | None = None,
        ) -> dict[str, str]:
            part: dict[str, str] = {}
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": target}
            if start_after is not None:
                kwargs["StartAfter"] = start_after
            if token is not None:
                kwargs["ContinuationToken"] = token
            while True:
                remaining()
                page = self.client.list_objects_v2(**kwargs)
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if stop_at is None or key <= stop_at:
                        part[key[strip:]] = tag(item)
                    if stop_at is not None and key >= stop_at:
                        return part
                if not page.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = page["NextContinuationToken"]
            return part

        def list_delimiter(target: str) -> tuple[dict[str, str], list[str]]:
            part: dict[str, str] = {}
            prefixes: list[str] = []
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": target, "Delimiter": "/"}
            while True:
                remaining()
                page = self.client.list_objects_v2(**kwargs)
                for item in page.get("Contents", []):
                    part[item["Key"][strip:]] = tag(item)
                for common in page.get("CommonPrefixes", []):
                    prefixes.append(common["Prefix"])
                if not page.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = page["NextContinuationToken"]
            prefixes.sort()
            return part, prefixes

        def first_two_pages(target: str) -> tuple[dict[str, str], str | None, str | None]:
            remaining()
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=target)
            contents = page.get("Contents", [])
            items = {item["Key"][strip:]: tag(item) for item in contents}
            if not page.get("IsTruncated"):
                return items, None, None
            remaining()
            page2 = self.client.list_objects_v2(
                Bucket=self.bucket, Prefix=target, ContinuationToken=page["NextContinuationToken"]
            )
            contents2 = page2.get("Contents", [])
            for item in contents2:
                items[item["Key"][strip:]] = tag(item)
            if not page2.get("IsTruncated"):
                return items, None, None
            last_key = (
                contents2[-1]["Key"] if contents2 else (contents[-1]["Key"] if contents else None)
            )
            return items, page2.get("NextContinuationToken"), last_key

        def submit_ranges(
            pool: ThreadPoolExecutor, target: str, after: str | None, bounds: Sequence[str]
        ) -> list[Future[dict[str, str]]]:
            # StartAfter is exclusive. The preceding range owns the boundary key if it exists.
            return [
                pool.submit(copy_context().run, list_range, target, lower, upper)
                for lower, upper in pairwise((after, *bounds, None))
            ]

        def prefix_ranges(
            pool: ThreadPoolExecutor, target: str, prefixes: list[str], after: str | None = None
        ) -> list[Future[dict[str, str]]]:
            if after is not None:
                prefixes = [p for p in prefixes if p > after or after.startswith(p)]
            if not prefixes:
                return []
            ranges = min(_LIST_RANGES, len(prefixes))
            bounds = [prefixes[i * len(prefixes) // ranges] for i in range(1, ranges)]
            return submit_ranges(pool, target, after, bounds)

        def collect() -> dict[str, str]:
            if (
                0 < len(split_points) < _LIST_RANGES
                and all(key and key.startswith(prefix) for key in split_points)
                and all(a < b for a, b in pairwise(split_points))
            ):
                bounds = [self.prefix + key for key in split_points]
                with ThreadPoolExecutor(max_workers=32) as pool:
                    listed: dict[str, str] = {}
                    for future in submit_ranges(pool, base, None, bounds):
                        listed.update(future.result())
                    return listed

            first = self.client.list_objects_v2(Bucket=self.bucket, Prefix=base)
            if not first.get("IsTruncated"):
                return {item["Key"][strip:]: tag(item) for item in first.get("Contents", [])}
            # Fan out breadth-first from the caller thread so pool workers never wait on the pool.
            part, prefixes = list_delimiter(base)
            if not prefixes:
                return part
            with ThreadPoolExecutor(max_workers=32) as pool:
                if len(prefixes) >= 32:
                    for future in prefix_ranges(pool, base, prefixes):
                        part.update(future.result())
                    return part
                first_futures = [(sub, pool.submit(first_two_pages, sub)) for sub in prefixes]
                truncated: list[tuple[str, str, str | None]] = []
                for sub, page_fut in first_futures:
                    items, next_token, last_key = page_fut.result()
                    part.update(items)
                    if next_token is not None:
                        truncated.append((sub, next_token, last_key))
                if not truncated:
                    return part
                delim_futures = [
                    (sub, next_token, last_key, pool.submit(list_delimiter, sub))
                    for sub, next_token, last_key in truncated
                ]
                range_tasks: list[Future[dict[str, str]]] = []
                for sub, next_token, last_key, delim_fut in delim_futures:
                    direct, sub_prefixes = delim_fut.result()
                    part.update(direct)
                    if len(sub_prefixes) >= 32:
                        range_tasks.extend(prefix_ranges(pool, sub, sub_prefixes, last_key))
                    elif len(sub_prefixes) >= 2:
                        for child in sub_prefixes:
                            if (
                                last_key is not None
                                and child < last_key
                                and not last_key.startswith(child)
                            ):
                                continue
                            sa = (
                                last_key
                                if last_key is not None and last_key.startswith(child)
                                else None
                            )
                            range_tasks.append(pool.submit(list_range, child, sa))
                    else:
                        range_tasks.append(pool.submit(list_range, sub, None, None, next_token))
                for future in range_tasks:
                    part.update(future.result())
                return part

        found = _storage_request(collect)
        if self._listed_etags is not None:
            for key in [k for k in self._listed_etags if k.startswith(prefix)]:
                del self._listed_etags[key]
            for key, value in found.items():
                etag = value.partition(":")[0]
                if etag and etag != "None":
                    self._listed_etags[key] = etag
        return found


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
        if (
            self.storage._listed_etags is not None
            and self.storage._listed_etags.get(self.key) == etag
        ):
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


class _StoredFile:
    """Transport for a file with a declared length; the cache verifies its content."""

    def __init__(self, storage: Storage, key: str, size: int) -> None:
        self.storage, self.key, self.size = storage, key, size

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        stop = self.size if end is None else min(end + 1, self.size)
        return Fetched(self.storage.get_range(self.key, start, stop - start), None)

    def changed(self, etag: str) -> Fetched | None:
        return self.read(0, None, None)

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
