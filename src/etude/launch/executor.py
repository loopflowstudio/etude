"""Layer 2: acquire a machine, start its session and read its journal.

Only this composition joins machine lifecycle, startup and evidence transport.
The launcher depends on its public execution protocol, never on SSH or a provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from etude.launch.machines import Allocation, Bill, MachineRequest, Machines, OwnedMachine
from etude.launch.records import BootstrapLinks, LogStream
from etude.launch.schemas import CheckpointRef, Observation, Packet, Work
from etude.launch.startup import ProcessStart
from etude.launch.storage.disk import DirectoryJournal

_creation_check: ContextVar[Callable[[], None] | None] = ContextVar("creation_check", default=None)


@contextmanager
def creation_guard(check: Callable[[], None]) -> Iterator[None]:
    """Carry the controller's placement fence through provider preparation."""
    token = _creation_check.set(check)
    try:
        yield
    finally:
        _creation_check.reset(token)


def check_creation() -> None:
    check = _creation_check.get()
    if check is not None:
        check()


class Refused(Exception):
    """The provider definitely supplied no machine; other errors mean unknown."""


class Execution(Protocol):
    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef: ...
    def started(self, session_id: str) -> bool: ...
    def charged_price(self, session_id: str) -> Decimal | None: ...
    def bill(self, session_id: str, now: datetime) -> Bill | None: ...

    def price(self) -> Decimal:
        """US dollars per hour for a rented machine created now."""
        ...

    def create(self, packet: Packet) -> str:
        """Get the machine for this packet's working session and return the provider's name for it.

        Called at most once per packet. Raise Refused only when no machine was supplied.
        """
        ...

    def add(self, session_id: str, work: Work) -> None:
        """Hand one more piece of work to a kept session. Safe to repeat."""
        ...

    def find(self, session_id: str) -> str | None:
        """The provider's name for a session whose create reply was lost, if it exists."""
        ...

    def observe(self, session_id: str) -> Observation: ...

    def stop(self, session_id: str, attempt: str) -> None:
        """Ask one attempt to stop. Safe to repeat."""
        ...

    def end(self, session_id: str) -> datetime | None:
        """End the session and return when it ended, or None if that is not confirmed."""
        ...

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream: ...

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        """Copy one output into the destination directory and return its path."""
        ...


class Journal(Protocol):
    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef: ...
    def add(self, session_id: str, work: Work) -> None: ...
    def observe(self, session_id: str) -> Observation: ...
    def stop(self, session_id: str, attempt: str) -> None: ...
    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream: ...
    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path: ...
    def started(self, session_id: str) -> bool: ...
    def ended(self, session_id: str) -> datetime | None: ...
    def prepare(
        self, packet: Packet, *, expires_at: datetime | None = None
    ) -> BootstrapLinks | None: ...
    def end(self, session_id: str) -> datetime | None: ...


class Starter(Protocol):
    def start(
        self, machine: Allocation, packet: Packet, bootstrap: BootstrapLinks | None
    ) -> str: ...


class Executor:
    def __init__(
        self,
        machines: Machines,
        starter: Starter | None,
        journal: Journal,
        *,
        control: Journal | None = None,
    ) -> None:
        self.machines, self.starter, self.journal = machines, starter, journal
        self.control = control or journal

    def price(self) -> Decimal:
        return self.machines.price()

    def create(self, packet: Packet) -> str:
        deadline = packet.deadline
        # Storage needs time to deliver its final evidence before the machine ends.
        if not self.machines.owned and packet.delivery_seconds is not None:
            assert deadline is not None
            life = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
            packet = packet.model_copy(
                update={
                    "deadline": deadline - timedelta(seconds=min(packet.delivery_seconds, life))
                }
            )
        bootstrap = self.journal.prepare(packet, expires_at=deadline)
        check_creation()
        machine = self.machines.create(
            MachineRequest(
                session=packet.session,
                startup_deadline=packet.startup_deadline,
                deadline=deadline,
                cap_usd=packet.cap_usd,
                log_seconds=packet.log_seconds,
                bootstrap=bootstrap,
            )
        )
        if self.machines.owned:
            assert self.starter is not None
            self.starter.start(machine, packet, bootstrap)

        return machine.id

    def find(self, session_id: str) -> str | None:
        if self.machines.owned:
            return session_id if self.journal.started(session_id) else None
        machine = self.machines.find(session_id)
        return None if machine is None else machine.id

    def observe(self, session_id: str) -> Observation:
        seen = self.journal.observe(session_id)
        return seen.model_copy(update={"alive": seen.alive and self.machines.alive(session_id)})

    def started(self, session_id: str) -> bool:
        return self.journal.started(session_id)

    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef:
        return self.journal.checkpoint(ref)

    def charged_price(self, session_id: str) -> Decimal | None:
        if self.machines.owned:
            return Decimal(0)
        machine = self.machines.find(session_id)
        return None if machine is None else machine.usd_per_hour

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return self.machines.bill(session_id, now)

    def end(self, session_id: str) -> datetime | None:
        if self.machines.owned:
            return self.control.end(session_id)
        note = self.journal.ended(session_id)
        gone = self.machines.end(session_id)
        return (note or gone) if gone is not None else None

    def add(self, session_id: str, work: Work) -> None:
        self.journal.add(session_id, work)

    def stop(self, session_id: str, attempt: str) -> None:
        self.journal.stop(session_id, attempt)

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        return self.journal.logs(session_id, attempt, producer=producer)

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        return self.journal.fetch(session_id, attempt, name, destination)


def local(root: Path) -> Executor:
    return Executor(OwnedMachine(), ProcessStart(root), DirectoryJournal(root))
