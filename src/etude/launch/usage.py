"""Retained transfer observations, offline totals and explicit cost projections.

Transport events are not invoices. Shared session events stay shared rather
than being multiplied by the number of runs. Missing coverage remains unknown.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from etude.launch.records import Record

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
    byte_range: str | None = None
    received_bytes: int = Field(default=0, ge=0)
    supplied_bytes: int = Field(default=0, ge=0)
    reused_bytes: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    completed: bool


class Rates(Record):
    """A storage provider's dated USD list prices; GB means 1,000,000,000 bytes.

    The download rate must match the route the bytes took. Storage is billed by
    byte-hours from an inventory, which transport observations do not
    establish, so `Usage.estimate` leaves it out.
    """

    as_of: datetime
    description: str
    download_usd_per_gb: Decimal = Field(ge=0)
    write_usd_per_million: Decimal = Field(ge=0)  # PUT, POST, LIST: S3's PUT tier, R2's Class A
    read_usd_per_million: Decimal = Field(ge=0)  # GET, HEAD: S3's GET tier, R2's Class B
    storage_usd_per_gb_month: Decimal = Field(ge=0)


# Published list prices on 2026-10-24. Deleting is free on both.
S3_US_WEST_2 = Rates(
    as_of=datetime(2026, 10, 24, tzinfo=UTC),
    description="S3 Standard in us-west-2, first tier of internet egress",
    download_usd_per_gb=Decimal("0.09"),
    write_usd_per_million=Decimal("5.00"),
    read_usd_per_million=Decimal("0.40"),
    storage_usd_per_gb_month=Decimal("0.023"),
)
R2 = Rates(
    as_of=datetime(2026, 10, 24, tzinfo=UTC),
    description="Cloudflare R2 Standard, beyond the free tier; egress is free",
    download_usd_per_gb=Decimal(0),
    write_usd_per_million=Decimal("4.50"),
    read_usd_per_million=Decimal("0.36"),
    storage_usd_per_gb_month=Decimal("0.015"),
)


def _writes(event: Transfer) -> bool:
    """Billed at the write rate. A listing is a GET of the bucket's root (virtual-host style)."""
    return event.operation in ("PUT", "POST") or (event.operation == "GET" and event.key == "/")


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
        """Downloads and requests at these rates, excluding storage and unobserved traffic."""
        writes = sum(_writes(event) for event in self.events)
        reads = sum(e.operation in ("GET", "HEAD") and not _writes(e) for e in self.events)
        return (
            Decimal(self.received_bytes) / 10**9 * rates.download_usd_per_gb
            + Decimal(writes) / 10**6 * rates.write_usd_per_million
            + Decimal(reads) / 10**6 * rates.read_usd_per_million
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
