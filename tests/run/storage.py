"""Count bytes actually returned by a directory or moto, without simulated latency."""

from __future__ import annotations

from pathlib import Path

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
