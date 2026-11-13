"""Count effects, including SQL statements and actual git/SSH subprocesses.

SQL text, subprocess arguments and network payloads are never retained. Counts
start after fixture setup; resetting does not change the sessions or database.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal
from unittest.mock import patch

from etude.launch.database import Database
from etude.launch.records import LogSnapshot, Record
from tests.launch.fake import FakeService


class Counts(Record):
    provider: int = 0
    bucket: int = 0
    ssh: int = 0
    git: int = 0
    database: int = 0


class Counting:
    def __init__(self, database: Database, *services: FakeService) -> None:
        self.database = database
        self.services = services
        self.calls: Counter[str] = Counter()

    def reset(self) -> None:
        self.calls.clear()
        for service in self.services:
            service.calls.clear()

    def snapshot(self) -> Counts:
        return Counts(
            provider=self.calls["provider"]
            + sum(service.calls["provider"] for service in self.services),
            bucket=self.calls["bucket"] + sum(service.calls["bucket"] for service in self.services),
            ssh=self.calls["ssh"],
            git=self.calls["git"],
            database=self.calls["database"],
        )

    def transport[T](
        self,
        boundary: Literal["provider", "bucket"],
        send: Callable[[str, str, bytes | None, dict[str, str]], T],
    ) -> Callable[[str, str, bytes | None, dict[str, str]], T]:
        """Count a physical provider or bucket request while using a supplied reply."""

        def invoke(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> T:
            self.calls[boundary] += 1
            return send(method, url, body, headers)

        return invoke

    @contextmanager
    def record(self) -> Iterator[Counting]:
        self.reset()
        original = subprocess.run

        def run(*args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("args", ())
            if command and command[0] in ("git", "ssh"):
                self.calls[command[0]] += 1
            return original(*args, **kwargs)

        def statement(sql: str) -> None:
            self.calls["database"] += 1

        self.database._db.set_trace_callback(statement)
        try:
            with patch("subprocess.run", run):
                yield self
        finally:
            self.database._db.set_trace_callback(None)


def job_text(snapshot: LogSnapshot) -> str:
    return "".join(stream.text for stream in snapshot.streams if stream.producer == "job")
