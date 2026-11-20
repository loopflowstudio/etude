"""Placement values selected before attempts and machines are claimed."""

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from itertools import islice

from etude.launch.records import Hardware, Limit, Machine, Session
from etude.launch.scheduler.costs import (
    Budget,
    Place,
    affordable_seconds,
    group_shares,
    rental_cost,
)
from etude.records import RunRecord
from etude.spec import Record


class PlannedRun(Record):
    """One placement's run and budget, before any attempt or machine is claimed."""

    run: RunRecord
    usd: Decimal
    seconds: int
    limit: Limit


class Placement(Record):
    """A machine selected under its supplies' locks and the runs it takes at once.

    None machine means a new rented machine.
    """

    runs: tuple[PlannedRun, ...]
    seconds: float | None  # paid window for a new rental
    supply: str  # the pool or on-demand the machine belongs to, or would be rented from
    session: Session | None
    machine: Machine | None
    price: Decimal
    waiting_usd: Decimal = Decimal(0)  # extension funded by waiting runs
    hardware: Hardware | None = None  # what a new rental would ask for

    def extend(
        self, target_seconds: float, waiting_usd: Decimal, reservation_hours: int
    ) -> "Placement":
        """Reserve extra rental time only as far as the waiting runs pay for it."""
        assert self.seconds is not None
        base = rental_cost(self.price, self.seconds, reservation_hours)
        affordable = affordable_seconds(self.price, base + waiting_usd, reservation_hours)
        seconds = max(self.seconds, min(target_seconds, affordable))
        extra = rental_cost(self.price, seconds, reservation_hours) - base
        return self.model_copy(update={"seconds": seconds, "waiting_usd": extra})


def funded_group(
    candidates: Iterable[RunRecord],
    free: int,
    budgets: Mapping[str, Budget],
    price: Decimal,
    now: datetime,
    peers: Collection[Place],
    *,
    setup_seconds: float,
    reservation_hours: int,
) -> tuple[PlannedRun, ...]:
    """Keep queue order, replacing the least funded place until every member fits."""
    waiting = iter(candidates)
    group = list(islice(waiting, free))
    while group:
        durations = {r.id: int(budgets[r.id].seconds) for r in group}
        shares = group_shares(
            durations,
            price,
            now,
            peers,
            setup_seconds=setup_seconds,
            reservation_hours=reservation_hours,
        )
        worst = max(group, key=lambda r: shares[r.id] - budgets[r.id].usd)
        if shares[worst.id] <= budgets[worst.id].usd:
            return tuple(
                PlannedRun(run=r, seconds=durations[r.id], usd=shares[r.id], limit="out_of_time")
                for r in group
            )
        group.remove(worst)
        group.extend(islice(waiting, 1))
    return ()
