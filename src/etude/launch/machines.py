"""Machine acquisition, independent of work and journals.

An allocation is a provider receipt or an owned address. Providers alone know
how to create/delete machines; owned machines use their configured startup path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from etude.launch.records import Record


class Safeguards(Record):
    lifetime: bool = False
    start_command: bool = False


class Allocation(Record):
    id: str
    address: str | None
    usd_per_hour: Decimal
    created_at: datetime
    cleanup: tuple[str, ...] = ()


class Bill(Record):
    usd: Decimal
    through: datetime  # end of the last contiguous billed clock hour


class MachineRequest(Record):
    session: str
    deadline: datetime | None
    cap_usd: Decimal
    bootstrap: str | None = None


class Machines(Protocol):
    safeguards: Safeguards
    owned: bool

    def price(self) -> Decimal: ...
    def create(self, request: MachineRequest) -> Allocation: ...
    def find(self, session_id: str) -> Allocation | None: ...
    def alive(self, session_id: str) -> bool: ...
    def end(self, session_id: str) -> datetime | None: ...
    def bill(self, session_id: str, now: datetime) -> Bill | None: ...


class OwnedMachine:
    """An address already supplied by its owner. Ending work never deletes it."""

    safeguards = Safeguards()
    owned = True

    def __init__(self, address: str | None = None) -> None:
        self.address = address

    def price(self) -> Decimal:
        return Decimal(0)

    def create(self, request: MachineRequest) -> Allocation:
        return Allocation(
            id=request.session,
            address=self.address,
            usd_per_hour=Decimal(0),
            created_at=datetime.now(UTC),
        )

    def find(self, session_id: str) -> Allocation | None:
        return None

    def alive(self, session_id: str) -> bool:
        return True

    def end(self, session_id: str) -> datetime | None:
        return None

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
