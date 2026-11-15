"""Dollar attribution and the remaining execution budget; no lifecycle state."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from etude.launch.records import Machine, Rental
from etude.spec import Record


class Budget(Record):
    """What a run has left across all its attempts."""

    seconds: float  # execution time
    usd: Decimal = Field(allow_inf_nan=True)  # attributed dollars; unlimited without max_usd


def rental_usd(rental: Rental, machine: Machine, now: datetime) -> Decimal:
    """Estimate an unconfirmed machine through its paid deadline."""
    if machine.claimed_at is None or machine.refused_at is not None:
        return Decimal(0)
    end = machine.deleted_at or min(now, rental.deadline)
    return cost_between(rental, machine, machine.claimed_at, end)


def cost_between(r: Rental, machine: Machine, start: datetime, end: datetime) -> Decimal:
    """Allocate billed coverage by occupied time; estimate uncovered seconds."""
    origin = machine.claimed_at or start
    through = r.billed_through
    if through is None:
        return usd(r.usd_per_hour, start, end)
    covered_end = min(through, machine.deleted_at or through)
    seconds = max(0.0, (covered_end - origin).total_seconds())
    overlap = max(0.0, (min(end, covered_end) - max(start, origin)).total_seconds())
    paid = r.billed_usd * Decimal(overlap) / Decimal(seconds) if seconds else Decimal(0)
    return paid + usd(r.usd_per_hour, max(start, through), end)


def usd(usd_per_hour: Decimal, start: datetime, end: datetime) -> Decimal:
    seconds = Decimal(max((end - start).total_seconds(), 0))
    return usd_per_hour * seconds / 3600
