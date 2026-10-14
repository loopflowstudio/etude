"""A provider that exists only in memory and runs on a clock the test moves.

Every attempt takes `seconds` of clock time, one after another, from the moment
its session was created. The session ends at its deadline whether or not anyone looks.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etude.launch.database import Database
from etude.launch.executor import Refused
from etude.launch.machines import Bill
from etude.launch.records import (
    MachineTemplate,
    Outcome,
    Output,
    Pool,
    Provider,
    Storage,
    parse_target,
)
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import CheckpointRef, Observation, Packet, Progress, Work

L4 = MachineTemplate(
    provider="fake", gpu="NVIDIA L4", vcpus=6, memory_gb=48, image="test@sha256:abc"
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeSession:
    packet: Packet
    created_at: datetime
    killed_at: datetime | None = None
    stops: dict[str, datetime] = field(default_factory=dict)
    added: list[tuple[Work, datetime]] = field(default_factory=list)

    def work(self) -> list[tuple[Work, datetime]]:
        """Every piece of work with the time it arrived."""
        return [(work, self.created_at) for work in self.packet.work] + self.added


def checkpoint_bytes(attempt: str) -> bytes:
    return f"weights after {attempt}".encode()


class FakeService:
    def __init__(self, clock: Clock, usd_per_hour: str = "0.59", seconds: int = 600) -> None:
        self.calls: Counter[str] = Counter()
        self.delay_seconds = 0.0
        self.clock = clock
        self.usd_per_hour = Decimal(usd_per_hour)
        self.seconds = seconds
        self.outcome: Callable[[Work], Outcome] = lambda work: "succeeded"
        self.refuse = 0  # how many of the next creates the provider turns down
        self.lose_reply = 0  # how many of the next creates succeed but never answer
        self.creates = 0
        self.sessions: dict[str, FakeSession] = {}
        self.most_live = 0

    def _call(self, boundary: str) -> None:
        self.calls[boundary] += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef:
        self._call("bucket")
        return ref

    def started(self, session_id: str) -> bool:
        self._call("bucket")
        return session_id in self.sessions

    def charged_price(self, session_id: str) -> Decimal | None:
        self._call("provider")
        return self.usd_per_hour

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        self._call("provider")
        return None

    def price(self) -> Decimal:
        self._call("provider")
        return self.usd_per_hour

    def create(self, packet: Packet) -> str:
        self._call("provider")
        self.creates += 1
        if self.refuse:
            self.refuse -= 1
            raise Refused("no capacity")
        self.sessions[packet.session] = FakeSession(packet, self.clock())
        self.most_live = max(self.most_live, sum(self._alive(p) for p in self.sessions.values()))
        if self.lose_reply:
            self.lose_reply -= 1
            raise ConnectionError("the reply never arrived")
        return f"provider-{packet.session}"

    def add(self, session_id: str, work: Work) -> None:
        self._call("bucket")
        held = self.sessions[session_id]
        if all(work.attempt != known.attempt for known, _ in held.work()):
            held.added.append((work, self.clock()))

    def find(self, session_id: str) -> str | None:
        self._call("provider")
        return f"provider-{session_id}" if session_id in self.sessions else None

    def observe(self, session_id: str) -> Observation:
        self._call("bucket")
        held = self.sessions[session_id]
        return Observation(alive=self._alive(held), progress=self._progress(held))

    def stop(self, session_id: str, attempt: str) -> None:
        self._call("bucket")
        self.sessions[session_id].stops.setdefault(attempt, self.clock())

    def end(self, session_id: str) -> datetime | None:
        self._call("provider")
        held = self.sessions[session_id]
        if self._alive(held):
            held.killed_at = self.clock()
        return self._ended_at(held)

    def kill(self, session_id: str) -> None:
        self.sessions[session_id].killed_at = self.clock()

    def logs(self, session_id: str, attempt: str) -> str:
        self._call("bucket")
        return f"log of {attempt}\n"

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        self._call("bucket")
        destination.mkdir(parents=True, exist_ok=True)
        copy = destination / name
        copy.write_bytes(checkpoint_bytes(attempt))
        return copy

    def _ended_at(self, session: FakeSession) -> datetime:
        """When the session stopped: killed, out of work, or at its deadline."""
        limit = self._limit(session)
        if session.packet.kept:
            return limit
        progress = self._progress(session, limit)
        done = [p.finished_at for p in progress if p.finished_at is not None]
        whole = len(done) == len(session.work())
        return min(max(done) if whole and done else limit, limit)

    def _limit(self, session: FakeSession) -> datetime:
        deadline = session.packet.deadline or datetime.max.replace(tzinfo=UTC)
        return min(session.killed_at or deadline, deadline)

    def _alive(self, session: FakeSession) -> bool:
        return self.clock() < self._ended_at(session)

    def _progress(self, session: FakeSession, at: datetime | None = None) -> tuple[Progress, ...]:
        now = min(at or self.clock(), self._limit(session))
        at_deadline = session.packet.deadline is not None and now >= session.packet.deadline
        cursor = session.created_at
        progress: list[Progress] = []
        for work, arrived in session.work():
            cursor = max(cursor, arrived)
            stop = session.stops.get(work.attempt)
            if stop is not None and stop <= cursor:
                progress.append(self._finished(work, None, cursor, "cancelled"))
                continue
            if cursor >= now:
                break
            natural = cursor + timedelta(seconds=min(self.seconds, work.limit_seconds))
            end = natural if stop is None else min(natural, stop)
            if end > now and not at_deadline:
                progress.append(
                    Progress(
                        attempt=work.attempt,
                        started_at=cursor,
                        finished_at=None,
                        outcome=None,
                        cause=None,
                        clocks={},
                        checkpoint=None,
                        outputs=(),
                        exit_code=None,
                    )
                )
                break
            if end > now:
                outcome: Outcome = "lost"
                end = now
            elif stop is not None and stop <= natural:
                outcome = "cancelled"
            elif self.seconds > work.limit_seconds:
                outcome = work.limit_kind
            else:
                outcome = self.outcome(work)
            progress.append(self._finished(work, cursor, end, outcome))
            cursor = end
        return tuple(progress)

    @staticmethod
    def _finished(work: Work, started: datetime | None, at: datetime, outcome: Outcome) -> Progress:
        if outcome != "succeeded":
            return Progress(
                attempt=work.attempt,
                started_at=started,
                finished_at=at,
                outcome=outcome,
                cause="machine_lost" if outcome == "lost" else None,
                clocks={},
                checkpoint=None,
                outputs=(),
                exit_code=1,
            )
        data = checkpoint_bytes(work.attempt)
        clocks = dict([parse_target(work.until)]) if work.until else {}
        output = Output(
            name="checkpoint.pt", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
        )
        return Progress(
            attempt=work.attempt,
            started_at=started,
            finished_at=at,
            outcome=outcome,
            cause=None,
            clocks=clocks,
            checkpoint=output.name,
            outputs=(output,),
            exit_code=0,
        )


@dataclass
class Bench:
    clock: Clock
    service: FakeService
    scheduler: Scheduler
    store: Database

    def run(self, seconds: float) -> None:
        """Let time pass and let the launcher look once."""
        self.clock.advance(seconds)
        self.scheduler.tick()


def bench(tmp_path: Path, *, max_on_demand: int = 4, usd_per_hour: str = "0.59") -> Bench:
    clock = Clock()
    service = FakeService(clock, usd_per_hour)
    store = Database(tmp_path / "launcher.db")
    free = FakeService(clock, "0")
    scheduler = Scheduler(
        store,
        {"runpod": lambda pool, machine: service, "ssh": lambda pool, machine: free},
        clock=clock,
        sleep=clock.advance,
    )
    scheduler.add_storage(
        Storage(name="test", bucket="bucket", prefix="prefix", region="us-west-2")
    )
    scheduler.set_provider(Provider(name="fake", kind="runpod", registry="", storage="test"))
    scheduler.add_pool(
        Pool(name="l4", on_demand_template=L4, max_on_demand=max_on_demand, setup_seconds=0)
    )
    return Bench(clock, service, scheduler, store)
