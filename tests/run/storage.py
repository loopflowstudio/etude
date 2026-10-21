"""Count bytes actually returned by a directory or moto, without simulated latency."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from etude.run.storage import Storage


class MeasuredStorage:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.name = storage.name
        self.bytes = 0
        self.requests = 0
        self.ranges = 0
        self.uploads = 0
        self.listings = 0

    def put(self, key: str, data: bytes) -> None:
        self.storage.put(key, data)
        self.uploads += 1

    def put_file(self, key: str, path: Path) -> None:
        self.storage.put_file(key, path)
        self.uploads += 1

    def delete(self, key: str) -> bool:
        return self.storage.delete(key)

    def get(self, key: str) -> bytes:
        body = self.storage.get(key)
        self.bytes += len(body)
        self.requests += 1
        return body

    def get_range(self, key: str, start: int, length: int) -> bytes:
        body = self.storage.get_range(key, start, length)
        self.bytes += len(body)
        self.requests += 1
        self.ranges += 1
        return body

    def keys(self, prefix: str) -> list[str]:
        self.listings += 1
        return self.storage.keys(prefix)

    def uri(self, key: str) -> str:
        return self.storage.uri(key)


class CountedClient:
    """A boto3 S3 client that counts the body bytes the bucket actually sends."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.bytes = 0

    def get_object(self, **fields: Any) -> Any:
        reply = self.client.get_object(**fields)
        body = reply["Body"].read()
        self.bytes += len(body)
        reply["Body"] = io.BytesIO(body)
        return reply

    def stored(self, bucket: str) -> int:
        """Every byte the bucket holds: what has been written there and not replaced."""
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=bucket)
        return sum(item["Size"] for page in pages for item in page.get("Contents", []))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)
