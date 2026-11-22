"""Dollar attribution and the remaining execution budget; no lifecycle state."""

from collections import Counter, defaultdict
from collections.abc import Collection, Iterator, Mapping
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import Field

from etude.launch.records import Machine, Rental, RentalCost, RunAttempt, Session
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
    def from_attempt(
        cls,
        attempt: RunAttempt,
        session: Session,
        end: datetime,
        *,
        projected_from: datetime | None = None,
    ) -> "Place":
        assert session.claimed_at is not None
        start = max(session.claimed_at, attempt.planned_at or session.claimed_at)
        if projected_from is not None and attempt.finished_at is None:
            projected_limit = (attempt.started_at or projected_from) + timedelta(
                seconds=attempt.limit_seconds
            )
            end = min(end, max(projected_from, projected_limit))
        active = max(start, min(end, attempt.finished_at or end))
        held = active
        origin = attempt.started_at
        if origin is None and attempt.finished_at is not None:
            # A pre-start cancellation still holds a place beside peers that start later.
            origin = max(attempt.finished_at, session.started_at or attempt.finished_at)
        if origin is not None:
            limit = origin + timedelta(seconds=attempt.limit_seconds)
            held = max(active, min(end, limit))
        return cls(run=attempt.run, start=start, active_until=active, held_until=held)


def _occupied_intervals(
    places: Collection[Place],
) -> Iterator[tuple[datetime, datetime, Counter[str]]]:
    """Yield each interval with its live share counts. Empty machines release every place."""
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
    previous: datetime | None = None
    for moment in sorted(starts.keys() | finishes.keys() | expiries.keys()):
        if previous is not None and counts:
            yield previous, moment, counts
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


def run_share(run: str, places: Collection[Place], rental: Rental, machine: Machine) -> Decimal:
    return sum(
        (
            cost_between(rental, machine, start, end) / len(counts)
            for start, end, counts in _occupied_intervals(places)
            if run in counts
        ),
        Decimal(0),
    )


def group_shares(
    seconds: Mapping[str, int],
    price: Decimal,
    now: datetime,
    peers: Collection[Place],
    *,
    setup_seconds: float = 0,
    reservation_hours: int = 0,
) -> dict[str, Decimal]:
    """Price the requested limits together, including any earlier place held by a retry."""
    places = list(peers)
    for run, duration in seconds.items():
        end = now + timedelta(seconds=setup_seconds + duration)
        places.append(Place(run=run, start=now, active_until=end, held_until=end))
    window = setup_seconds + max(seconds.values())
    billed = rental_cost(price, window, reservation_hours)
    shares = dict.fromkeys(seconds, Decimal(0))
    for start, end, counts in _occupied_intervals(places):
        if end <= now:
            continue
        duration_seconds = Decimal(str((end - max(start, now)).total_seconds()))
        cost = (
            billed * duration_seconds / Decimal(str(window))
            if reservation_hours
            else price * duration_seconds / 3600
        )
        share = cost / len(counts)
        for run in shares.keys() & counts.keys():
            shares[run] += share
    return shares


def rental_cost(price: Decimal, seconds: float | Decimal, reservation_hours: int) -> Decimal:
    duration = Decimal(str(seconds))
    if not reservation_hours:
        return price * duration / 3600
    hours = duration / 3600
    hours = (hours / reservation_hours).to_integral_value(
        rounding=ROUND_CEILING
    ) * reservation_hours
    return price * hours


def affordable_seconds(price: Decimal, cap: Decimal, reservation_hours: int) -> float:
    if not price or cap.is_infinite():
        return float("inf")
    if reservation_hours:
        increments = cap // (price * reservation_hours)
        return float(increments * reservation_hours * 3600)
    # A rental deadline has microsecond precision. Round down so its last
    # fraction of a second cannot exceed the dollars that fund the window.
    micros = (cap * 3_600_000_000 / price).to_integral_value(rounding=ROUND_FLOOR)
    return float(micros / 1_000_000)


def has_unbilled_time(rental: Rental, machine: Machine, now: datetime) -> bool:
    return (
        rental.usd_per_hour > 0
        and machine.claimed_at is not None
        and machine.refused_at is None
        and (
            rental.billed_through is None
            or rental.billed_through < (machine.deleted_at or min(now, rental.deadline))
        )
    )


def price_rental(rental: Rental, machine: Machine, now: datetime) -> RentalCost:
    """Combine retained bills and uncovered time through deletion or the paid deadline."""
    usd = Decimal(0)
    if machine.claimed_at is not None and machine.refused_at is None:
        end = machine.deleted_at or min(now, rental.deadline)
        usd = cost_between(rental, machine, machine.claimed_at, end)
    return RentalCost(
        rental=rental,
        usd=usd,
        estimated=has_unbilled_time(rental, machine, now),
        unbilled_usd=usd - rental.billed_usd,
        over_cap=rental.billed_usd > rental.cap_usd,
    )


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
