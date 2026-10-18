"""Retained transfer observations, offline totals and explicit cost projections.

Transport events are not invoices. Shared session events stay shared rather
than being multiplied by the number of runs. Missing coverage remains unknown.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from etude.launch.records import Record

Purpose = Literal["source", "runtime", "checkpoint", "result", "control", "report"]


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
    byte_range: str | None = None
    received_bytes: int = Field(default=0, ge=0)
    supplied_bytes: int = Field(default=0, ge=0)
    reused_bytes: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    completed: bool


class Rates(Record):
    """Caller-selected dated USD rates; GB means 1,000,000,000 bytes.

    Download rate must match the chosen route. Storage requires byte-hours from
    an inventory, which transport observations do not establish.
    """

    as_of: datetime
    description: str
    download_usd_per_gb: Decimal = Field(ge=0)
    request_usd_per_1000: Decimal = Field(ge=0)


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
                or not event.byte_range
            ):
                continue
            start, _, end = event.byte_range.removeprefix("bytes=").partition("-")
            if not start.isdigit() or not end.isdigit():
                continue
            offset = int(start)
            spans.setdefault(event.sha256, []).append(range(offset, offset + event.received_bytes))
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

    def estimate(self, rates: Rates) -> Decimal:
        """A download/request scenario, excluding unknown storage and other traffic."""
        return (
            Decimal(self.received_bytes) / 10**9 * rates.download_usd_per_gb
            + Decimal(self.requests) / 1000 * rates.request_usd_per_1000
        )


class Access(Record):
    """Safe projection of a provider access-log row; omit its raw request URI."""

    backend: str
    request_id: str
    key: str
    response_bytes: int = Field(ge=0)
    status: int


class Reconciliation(Record):
    matched: tuple[Access, ...]
    unmatched_provider: tuple[Access, ...]
    unmatched_client: tuple[Transfer, ...]


def reconcile(usage: Usage, records: Sequence[Access]) -> Reconciliation:
    """Deduplicate request IDs; conflicting provider rows are evidence errors."""
    unique: dict[tuple[str, str], Access] = {}
    for row in records:
        identity = (row.backend, row.request_id)
        if identity in unique and unique[identity] != row:
            raise ValueError("conflicting access records for one request")
        unique[identity] = row
    client = {(e.backend, e.request_id) for e in usage.events if e.request_id}
    return Reconciliation(
        matched=tuple(row for key, row in unique.items() if key in client),
        unmatched_provider=tuple(row for key, row in unique.items() if key not in client),
        unmatched_client=tuple(
            e
            for e in usage.events
            if e.operation != "REUSE" and (e.backend, e.request_id) not in unique
        ),
    )
