"""Placement values selected before attempts and machines are claimed."""

from decimal import Decimal

from pydantic import Field

from etude.launch.records import Hardware, Limit, Machine, Session
from etude.records import RunRecord
from etude.spec import Record


class PlannedRun(Record):
    """One placement's run and budget, before any attempt or machine is claimed."""

    run: RunRecord
    cap_usd: Decimal = Field(allow_inf_nan=True)  # unlimited for owned machines
    seconds: int
    limit: Limit


class Placement(Record):
    """A machine selected under its supplies' locks and the runs it takes at once.

    None machine means a new rented machine.
    """

    runs: tuple[RunRecord, ...]
    free: int  # runs this machine may take now; `runs` fills some of them
    supply: str  # the pool or on-demand the machine belongs to, or would be rented from
    session: Session | None
    machine: Machine | None
    price: Decimal
    hardware: Hardware | None = None  # what a new rental would ask for
