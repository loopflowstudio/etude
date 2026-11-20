"""Dollar attribution and the remaining execution budget; no lifecycle state."""

from collections import Counter, defaultdict
from collections.abc import Collection
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field

from etude.launch.records import Machine, Rental, RunAttempt, Session
from etude.spec import Record


class Budget(Record):
    """What a run has left across all its attempts."""

    seconds: float  # execution time
    usd: Decimal = Field(allow_inf_nan=True)  # attributed dollars; unlimited without max_usd


class Place(Record):
    """One attempt's occupancy and the time through which it shares the bill."""

    run: str
    start: datetime
    active_until: datetime
    held_until: datetime

    @classmethod
    def from_attempt(cls, attempt: RunAttempt, session: Session, end: datetime) -> "Place":
        assert session.claimed_at is not None
        start = max(session.claimed_at, attempt.planned_at or session.claimed_at)
        active = max(start, min(end, attempt.finished_at or end))
        held = active
        origin = attempt.started_at or (
            max(attempt.finished_at, session.started_at or attempt.finished_at)
            if attempt.finished_at is not None
            else None
        )
        if origin is not None:
            limit = origin + timedelta(seconds=attempt.limit_seconds)
            held = max(active, min(end, limit))
        return cls(run=attempt.run, start=start, active_until=active, held_until=held)


def run_share(run: str, places: Collection[Place], rental: Rental, machine: Machine) -> Decimal:
    """Split occupied time by distinct runs. An empty machine releases every place."""
    starts: dict[datetime, list[Place]] = defaultdict(list)
    finishes: Counter[datetime] = Counter()
    expiries: dict[datetime, list[Place]] = defaultdict(list)
    for place in places:
        if place.held_until > place.start:
            starts[place.start].append(place)
            if place.active_until > place.start:
                finishes[place.active_until] += 1
            expiries[place.held_until].append(place)

    active = 0
    empty_at: datetime | None = None
    counts: Counter[str] = Counter()
    dollars = Decimal(0)
    previous = machine.claimed_at
    for moment in sorted(starts.keys() | finishes.keys() | expiries.keys()):
        if previous is not None and run in counts:
            dollars += cost_between(rental, machine, previous, moment) / len(counts)
        previous = moment
        active -= finishes[moment]
        if active == 0:
            empty_at = moment
            counts.clear()
        else:
            for place in expiries.get(moment, ()):
                if empty_at is None or place.start >= empty_at:
                    counts[place.run] -= 1
                    if not counts[place.run]:
                        del counts[place.run]
        arriving = starts.get(moment, [])
        active += sum(place.active_until > moment for place in arriving)
        if active:
            counts.update(place.run for place in arriving)
    return dollars


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
