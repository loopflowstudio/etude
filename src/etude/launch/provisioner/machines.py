"""Machine acquisition, independent of work and journals.

An allocation is a provider receipt or an owned address. Providers alone know
how to create/delete machines; owned machines use their configured startup path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from etude.launch.records import BootstrapLinks, Hardware, Offer
from etude.spec import Record

_creation_check: ContextVar[Callable[[], None] | None] = ContextVar("creation_check", default=None)


@contextmanager
def creation_guard(check: Callable[[], None]) -> Iterator[None]:
    """Carry the scheduler's placement fence through provider preparation."""
    token = _creation_check.set(check)
    try:
        yield
    finally:
        _creation_check.reset(token)


def check_creation() -> None:
    check = _creation_check.get()
    if check is not None:
        check()


class Safeguards(Record):
    reservation_hours: int = 0  # round fallback-timer budget to this many hours
    lifetime: bool = False
    machine_timer: bool = False


class Allocation(Record):
    id: str
    address: str | None
    usd_per_hour: Decimal | None = None
    created_at: datetime
    cleanup: tuple[str, ...] = ()
    hardware: Hardware | None = None  # what the provider was asked for; None for owned
    unit: str = "usd"
    units_per_hour: Decimal | None = None
    deadline: datetime | None = None


class Bill(Record):
    usd: Decimal
    through: datetime  # end of the contiguous interval covered by provider charges


class MachineRequest(Record):
    log_seconds: float | None = None
    startup_deadline: datetime | None = None
    session: str
    deadline: datetime | None
    cap_usd: Decimal
    cap_units: Decimal | None = None
    max_units_per_hour: Decimal | None = None
    bootstrap: BootstrapLinks | None = None


class Machines(Protocol):
    safeguards: Safeguards
    owned: bool

    def offer(self) -> Offer: ...
    def create(self, request: MachineRequest) -> Allocation: ...
    def find(self, session_id: str) -> str | None:
        """Read identity only; None confirms none occurred. Raise while uncertain."""
        ...

    def allocation(self, session_id: str) -> Allocation | None:
        """Read price, hardware and address; raise while facts are unreadable."""
        ...

    def alive(self, session_id: str) -> bool: ...
    def end(self, session_id: str) -> datetime | None: ...
    def bill(self, session_id: str, now: datetime) -> Bill | None: ...


class OwnedMachine:
    """An address already supplied by its owner. Ending work never deletes it."""

    safeguards = Safeguards()
    owned = True

    def __init__(self, address: str | None = None) -> None:
        self.address = address

    def offer(self) -> Offer:
        return Offer(hardware=None, usd_per_hour=Decimal(0), available=True)

    def create(self, request: MachineRequest) -> Allocation:
        return Allocation(
            id=request.session,
            address=self.address,
            usd_per_hour=Decimal(0),
            created_at=datetime.now(UTC),
        )

    def find(self, session_id: str) -> str | None:
        return None

    def allocation(self, session_id: str) -> Allocation | None:
        return None

    def alive(self, session_id: str) -> bool:
        return True

    def end(self, session_id: str) -> datetime | None:
        return None

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
