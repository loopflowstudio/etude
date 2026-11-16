"""Retained transfer observations and their offline totals.

Transport events are not invoices. Shared session events stay shared rather
than being multiplied by the number of runs. Missing coverage remains unknown.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from etude.spec import Record

__all__ = (
    "Usage",
    "Transfer",
)


Purpose = Literal["source", "input", "runtime", "checkpoint", "result", "control", "report"]


class Transfer(Record):
    id: str
    observed_at: datetime
    backend: str
    key: str
    operation: Literal["GET", "HEAD", "PUT", "POST", "DELETE", "REUSE"]
    purpose: Purpose
    session: str | None = None
    machine: str | None = None
    run: str | None = None
    attempt: str | None = None
    transfer: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    status: int | None = None
    request_id: str | None = None
    range_start: int | None = None  # the first byte a range read asked for
    received_bytes: int = Field(default=0, ge=0)
    supplied_bytes: int = Field(default=0, ge=0)
    reused_bytes: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    completed: bool


class Usage(Record):
    observed_at: datetime | None
    events: tuple[Transfer, ...]
    shared_events: tuple[Transfer, ...] = ()
    coverage: tuple[str, ...] = (
        "Observed HTTP response bodies; upload bodies supplied, not confirmed socket bytes.",
        "Missing events and hard-kill intervals are unknown; totals are lower bounds.",
        "Tracking boto3/report reads, dependency installs, SSH and storage byte-hours are unobserved.",
        "Final usage-journal uploads and HTTP outside observed collection/fetch paths are unknown.",
        "Run selections exclude shared traffic from totals; shared_events remain unallocated.",
        "Retransferred bytes cover overlapping observed SHA256 ranges; other repeats are unknown.",
    )

    @property
    def received_bytes(self) -> int:
        return sum(event.received_bytes for event in self.events)

    @property
    def supplied_bytes(self) -> int:
        return sum(event.supplied_bytes for event in self.events)

    @property
    def reused_bytes(self) -> int:
        return sum(event.reused_bytes for event in self.events)

    @property
    def retransferred_bytes(self) -> int:
        spans: dict[str, list[range]] = {}
        for event in self.events:
            if (
                event.operation != "GET"
                or event.status != 206
                or not event.sha256
                or event.range_start is None
            ):
                continue
            start = event.range_start
            spans.setdefault(event.sha256, []).append(range(start, start + event.received_bytes))
        repeated = 0
        for ranges in spans.values():
            through = 0
            for span in sorted(ranges, key=lambda item: item.start):
                repeated += max(0, min(through, span.stop) - span.start)
                through = max(through, span.stop)
        return repeated

    @property
    def requests(self) -> int:
        return sum(event.operation != "REUSE" for event in self.events)
