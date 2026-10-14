"""The scheduler: owner of one launcher database, and the one loop that moves work forward.

It queues and admits runs, matches waiting runs to machines and enforces limits.

Four rules hold whatever fails:

1. A rental's identity is stored and claimed before the provider is asked for it.
   Only the claimer asks, once. A lost reply is looked up, never asked again.
2. A rental carries its own deadline and ends itself there with nobody watching.
3. A run's code starts at most once per attempt; its session enforces it.
4. A rental has ended only on a dated confirmation, never on an empty listing.

`tick` may run in any number of processes at once. Versioned writes decide who
takes each step.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import Field

from etude.launch import source
from etude.launch.database import Database
from etude.launch.executor import Execution, Refused
from etude.launch.machines import Safeguards
from etude.launch.records import (
    RETRYABLE,
    Cause,
    Checkpoint,
    Cohort,
    Job,
    LauncherError,
    Limit,
    Machine,
    MachineTemplate,
    Pool,
    Provider,
    Record,
    Rental,
    Run,
    RunAttempt,
    RunRecord,
    Session,
    Stale,
    StartPhase,
    Status,
    Storage,
    job_identity,
    number,
    parse_target,
)
from etude.launch.schemas import (
    CheckpointRef,
    Packet,
    Progress,
    Work,
)
from etude.launch.storage.s3 import StorageUnavailable

Clock = Callable[[], datetime]


def _limit_usd(job: Job) -> Decimal:
    """A job's dollar limit. None is allowed only where nothing can be spent."""
    return Decimal("Infinity") if job.max_usd is None else job.max_usd


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id() -> str:
    return uuid4().hex[:12]


def _rental_usd(rental: Rental, machine: Machine, now: datetime) -> Decimal:
    """Estimate an unconfirmed machine through its paid deadline."""
    if machine.requested_at is None or machine.refused_at is not None:
        return Decimal(0)
    end = machine.deleted_at or min(now, rental.deadline)
    return _cost_between(rental, machine, machine.requested_at, end)


def _cost_between(r: Rental, machine: Machine, start: datetime, end: datetime) -> Decimal:
    """Allocate billed coverage by occupied time; estimate uncovered seconds."""
    origin = machine.requested_at or start
    through = r.billed_through
    if through is None:
        return _usd(r.usd_per_hour, start, end)
    covered_end = min(through, machine.deleted_at or through)
    seconds = max(0.0, (covered_end - origin).total_seconds())
    overlap = max(0.0, (min(end, covered_end) - max(start, origin)).total_seconds())
    paid = r.billed_usd * Decimal(overlap) / Decimal(seconds) if seconds else Decimal(0)
    return paid + _usd(r.usd_per_hour, max(start, through), end)


def _usd(usd_per_hour: Decimal, start: datetime, end: datetime) -> Decimal:
    seconds = Decimal(max((end - start).total_seconds(), 0))
    return usd_per_hour * seconds / 3600


class PlannedRun(Record):
    """One placement's run and budget, before any attempt or machine is claimed."""

    run: RunRecord
    cap_usd: Decimal = Field(allow_inf_nan=True)  # unlimited for owned machines
    seconds: int
    limit: Limit


class Placement(Record):
    """Capacity selected under a pool lock; None machine means a new paid machine."""

    run: RunRecord
    session: Session | None
    machine: Machine | None
    price: Decimal


class Scheduler:
    def __init__(
        self,
        store: Database,
        services: Mapping[str, Callable[[Pool, Machine | None], Execution]],
        clock: Clock = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._services = dict(services)  # by service name
        self._reached: dict[tuple[str, str | None], Execution] = {}
        self._trouble: dict[str, str] = {}  # why a pool could not place waiting work
        self._clock = clock
        self._sleep = sleep

    def _usd_per_hour(self, session: Session) -> Decimal:
        rental = self._store.rental_for(session)
        return Decimal(0) if rental is None else rental.usd_per_hour

    def _kept(self, session: Session) -> bool:
        rental = self._store.rental_for(session)
        return rental is not None and rental.kept

    def _live(self, session: Session, now: datetime) -> bool:
        rental = self._store.rental_for(session)
        return (
            session.ended_at is None
            and session.refused_at is None
            and (rental is None or now < rental.deadline)
        )

    # What a caller uses

    def set_provider(self, provider: Provider) -> None:
        """Record an account that supplies machines on demand. Its service must be one held here."""
        if provider.kind not in self._services:
            raise LauncherError(f"unsupported provider kind {provider.kind!r}")
        self._store.set_provider(provider)

    def providers(self) -> tuple[Provider, ...]:
        return self._store.providers()

    def add_storage(self, storage: Storage) -> None:
        """Record a place in a bucket for rentals' records. It cannot be changed afterwards."""
        self._store.add_storage(storage)

    def storages(self) -> tuple[Storage, ...]:
        return self._store.storages()

    def cohort_runs(self, name: str) -> tuple[RunRecord, ...]:
        """A cohort's runs in the order launched."""
        return self._store.cohort_runs(name)

    def cohort(self, name: str) -> Cohort:
        return self._store.cohort(name)

    def add_pool(self, pool: Pool) -> None:
        """Make a pool available to launch on. A pool with a template needs that provider's service here."""
        if pool.on_demand_template is not None:
            self._provider_service(
                pool.on_demand_template.provider
            )  # raises if it cannot be reached
            known = {each.name: each.storage for each in self._store.providers()}
            # Fixed here, so changing the provider later cannot move a live rental's records.
            named = pool.storage or known.get(pool.on_demand_template.provider)
            if named is not None:
                pool = pool.model_copy(update={"storage": self._store.storage(named).name})
        elif pool.storage is not None:
            raise LauncherError("only a pool that makes machines on demand has a storage")
        self._store.add_pool(pool)

    def set_pool_max(self, name: str, maximum: int) -> None:
        """Bound automatic growth; existing machines keep their windows."""
        self._store.set_pool_max(name, maximum)

    def set_default_pool(self, name: str) -> None:
        self._store.set_default_pool(name)

    def default_pool(self) -> str:
        """The pool a queue or launch goes to when none is named."""
        return self._store.default_pool()

    def _pool_name(self, name: str | None) -> str:
        if name is not None:
            return name
        pool = self._store.default_pool()
        if self._store.pool(pool).off:
            raise LauncherError(f"default pool {pool!r} is turned off; name a pool with --pool")
        return pool

    def set_pool_image(self, name: str, image: str) -> None:
        """Change the image a pool's next on-demand machines boot. Running ones are untouched."""
        self._store.set_pool_image(name, image)
        self._reached.pop((name, None), None)  # its service is built again with the new image

    def add_machine(
        self,
        name: str,
        address: str,
        *,
        pool: str | None = None,
        storage: str | None = None,
    ) -> None:
        """Add an owned SSH host; this computer is already the local pool."""
        if name == "local":
            raise LauncherError("this computer is already the local pool; use pool on/off local")
        if storage is not None:
            self._store.storage(storage)
        if pool is None:
            self._store.add_pool(Pool(name=name))
        self._store.add_machine(
            Machine(name=name, pool=pool or name, kind="ssh", address=address, storage=storage)
        )

    def machines(self, pool: str, *, rented: bool = False) -> tuple[Machine, ...]:
        """A pool's machines: the ones you own, or every one made on demand for it so far."""
        return self._store.machines(pool, rented=rented)

    def rent(self, on: str, hours: float, max_usd: Decimal | None = None) -> str:
        """Hold a machine in a pool until a deadline, for runs to be launched onto.

        Launching does this for itself when no existing machine has room; doing
        it by hand pays for setup once across several launches. Returns the
        machine's name. It ends at its deadline, or sooner with `terminate`.
        Rented machines start themselves; added machines use SSH.
        """
        with self._store.pool_lock(on, self._clock) as acquired:
            if not acquired:
                raise LauncherError(f"pool {on!r} is busy")
            now = self._clock()
            pool = self._store.pool(on)
            if pool.on_demand_template is None:
                raise LauncherError(
                    f"pool {on!r} has only machines you added; it cannot make one to rent"
                )
            held = None if pool.off else self._hold(pool, manual=True)
            if held is None:
                raise LauncherError(f"pool {on!r} has no machine to rent right now")
            _, price = held  # explicit rent always makes an on-demand machine
            life = hours * 3600
            if price > 0 and max_usd is not None:
                life = min(life, float(max_usd / price * 3600))
            if life < 1:
                raise LauncherError("a rental needs hours, and dollars for at least a second")
            session, on_demand, rental = self._session(pool, None, price, life, now, kept=True)
            self._store.plan(session, (), on_demand, rental)
            self._tick_pool(pool, self._clock())
            return session.machine

    def terminate(self, machine: str) -> Machine:
        """Delete a rented machine now; refuse while an allocation is unfinished."""
        named = self._store.machine(machine)
        if named.kind != "rented":
            raise LauncherError(f"machine {machine!r} was added; it cannot be terminated")
        self.tick()
        with self._store.pool_lock(named.pool, self._clock) as acquired:
            if not acquired:
                raise LauncherError(f"pool {named.pool!r} is busy")
            if self._store.machine_busy(machine):
                raise LauncherError(f"machine {machine!r} still has a run on it")
            held = self._store.rented_session(machine)
            if held.ended_at is None and held.refused_at is None and held.receipt is not None:
                gone = self._service(held).end(held.id)
                if gone is None:
                    raise LauncherError(f"machine {machine!r} did not confirm it ended")
                self._store.confirm_ended(held, gone)
            return self._store.machine(machine)

    def session(self, id: str) -> Session:
        """Internal journal session named in a run attempt."""
        return self._store.session(id)

    def rentals(self) -> tuple[tuple[Rental, Decimal], ...]:
        """Paid windows and their current cost, including terminated machines."""
        self.tick()
        now = self._clock()
        return tuple(
            (r, _rental_usd(r, self._store.machine(r.machine), now)) for r in self._store.rentals()
        )

    def turn_pool(self, name: str, *, off: bool) -> None:
        """Stop or resume sending new runs to a pool. Runs already there are untouched."""
        self._store.turn_pool(name, off=off)

    def pools(self) -> tuple[Pool, ...]:
        return self._store.pools()

    def safeguards(self, pool: str) -> tuple[str, ...]:
        template = self._store.pool(pool).on_demand_template
        if template is None:
            return ()

        declared = getattr(
            self._services[self._provider_service(template.provider)], "safeguards", Safeguards()
        )
        return tuple(
            name
            for enabled, name in (
                (declared.lifetime, "provider lifetime"),
                (declared.start_command, "machine timer"),
                (True, "launcher cleanup"),
            )
            if enabled
        )

    def queue(self, run: Run | Job, pool: str | None = None) -> str:
        """Queue one run; its pool may grow within its limits, or the run waits."""
        record = self._freeze(
            run if isinstance(run, Run) else Run(job=run), self._pool_name(pool), None
        )
        self._store.launch((record,), None)
        return record.id

    def queue_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        """Queue a cohort under its shared limits; capacity shortages wait."""
        cohort = self._new_cohort(name, runs, parallel, max_usd)
        selected = self._pool_name(pool)
        records = tuple(self._freeze(run, selected, name) for run in runs)
        self._store.launch(records, cohort)
        return tuple(record.id for record in records)

    def _new_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        parallel: int,
        max_usd: Decimal | None,
    ) -> Cohort:
        if not runs or parallel < 1 or (max_usd is not None and max_usd <= 0):
            raise LauncherError("a cohort needs runs and positive limits")
        return Cohort(name=name, parallel=parallel, max_usd=max_usd, created_at=self._clock())

    def launch(self, run: Run | Job, pool: str | None = None) -> str:
        """Acquire confirmed capacity, then record and hand over one run."""
        record = self._freeze(
            run if isinstance(run, Run) else Run(job=run), self._pool_name(pool), None
        )
        self._admit((record,), None)
        return record.id

    def launch_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        """Record a named set of runs sharing rentals-at-once and total-dollar limits."""
        cohort = self._new_cohort(name, runs, parallel, max_usd)
        selected = self._pool_name(pool)
        records = tuple(self._freeze(run, selected, name) for run in runs)
        self._admit(records, cohort)
        return tuple(record.id for record in records)

    def _admit(self, records: Sequence[RunRecord], cohort: Cohort | None) -> None:
        pool_name = records[0].pool
        with self._store.pool_lock(pool_name, self._clock) as acquired:
            if not acquired:
                raise LauncherError(
                    f"cannot launch on {pool_name!r}: pool is busy; etude queue would wait"
                )
            placements = self._capacity(records, cohort)
            slots = cohort.parallel if cohort else 1
            hours = sum(r.spec.max_hours for r in records) / slots
            cap = (
                cohort.max_usd
                if cohort and cohort.max_usd is not None
                else sum((_limit_usd(r.spec) for r in records), Decimal(0))
            ) / slots
            made: list[Session] = []
            attempts: list[RunAttempt] = []
            try:
                for placement in placements:
                    run, existing = placement.run, placement.session
                    machine, price = placement.machine, placement.price
                    self._store.check_pool_lock(pool_name)
                    now = self._clock()
                    if existing is None:
                        pool = self._store.pool(pool_name)
                        session, on_demand, rental = self._session(
                            pool,
                            machine,
                            price,
                            min(
                                hours * 3600 + pool.setup_seconds,
                                float(cap / price * 3600) if price else float("inf"),
                            ),
                            now,
                            kept=False,
                        )
                        self._store.plan(session, (), on_demand, rental)
                        made.append(session)
                        if on_demand is not None:
                            # Boot an empty inbox. No executable work leaves before
                            # the entire first wave has confirmed capacity.
                            self._create(session, now, idle=True)
                            session = self._store.session(session.id)
                            if session.receipt is None:
                                reason = (
                                    "provider refused capacity"
                                    if session.refused_at
                                    else "create reply unknown"
                                )
                                raise LauncherError(
                                    f"cannot launch on {pool_name!r}: {reason}; etude queue would wait"
                                )
                    else:
                        session = self._store.session(existing.id)
                        if not self._live(session, now):
                            raise LauncherError(
                                "rented machine expired during launch; etude queue would wait"
                            )
                    planned = PlannedRun(
                        run=run,
                        cap_usd=_limit_usd(run.spec),
                        seconds=int(run.spec.max_hours * 3600),
                        limit="out_of_time",
                    )
                    attempts.append(
                        self._attempt(
                            planned,
                            session,
                            len(self._store.attempts(session=session.id)),
                            now,
                            added=session.receipt is not None,
                        )
                    )
                self._store.check_pool_lock(pool_name)
                if any(
                    not self._live(self._store.session(a.session), self._clock()) for a in attempts
                ):
                    raise LauncherError("capacity expired during launch; etude queue would wait")
                # Runs and claims become visible together, under the same lock.
                self._store.launch(records, cohort, attempts)
            except BaseException:
                for session in made:
                    try:
                        self._store.check_pool_lock(pool_name)
                        session = self._store.session(session.id)
                        if session.receipt is not None:
                            self._end_session(session)
                        elif session.claimed_at is None:
                            self._store.record_refusal(session, self._clock())
                    except Exception:
                        pass  # next tick cleans up; unknown creates are only looked up
                raise
            # Delivery is retryable from committed attempts; capacity is already held.
            for attempt in attempts:
                session = self._store.session(attempt.session)
                try:
                    self._store.check_pool_lock(pool_name)
                    if session.claimed_at is None:
                        self._create(session, self._clock())
                    else:
                        self._observe(session, self._clock())
                except (Stale, OSError, LauncherError, StorageUnavailable):
                    continue  # committed attempts retain delivery and startup recovery

    def _capacity(
        self,
        records: Sequence[RunRecord],
        cohort: Cohort | None,
    ) -> list[Placement]:
        """Choose the first wave under the pool lock, checking all limits before spending."""
        placements: list[Placement] = []
        pool = self._store.pool(records[0].pool)
        now = self._clock()
        used: set[str] = set()
        made = 0
        left = None if cohort is None else cohort.max_usd
        first = records[: 1 if cohort is None else cohort.parallel]
        if pool.off:
            raise LauncherError(f"pool {pool.name!r} is turned off")
        if cohort is not None and self._store.cohort_exists(cohort.name):
            raise LauncherError(f"cohort {cohort.name!r} already exists")
        idle = self._idle_sessions(pool.name, now)
        for run in first:
            existing: Session | None = None
            held: tuple[Machine | None, Decimal] | None = None
            reason = "no free machine"
            for session in idle:
                if session.id in used:
                    continue
                rental = self._store.rental_for(session)
                if (
                    rental is not None
                    and (rental.deadline - now).total_seconds() < run.spec.max_hours * 3600
                ):
                    reason = "rented machine has too little time remaining"
                    continue
                used.add(session.id)
                existing = session
                held = (
                    self._store.machine(session.machine),
                    Decimal(0) if rental is None else rental.usd_per_hour,
                )
                break
            if held is None:
                held = self._hold(pool, excluded=used, additional=made)
                reason = self._trouble.get(pool.name, reason)
                if held is not None:
                    machine, _ = held
                    if machine is None:
                        made += 1
                    else:
                        used.add(machine.name)
            if held is not None:
                machine, price = held
                hours = Decimal(str(run.spec.max_hours))
                if machine is None:
                    hours += Decimal(pool.setup_seconds) / 3600
                cost = price * hours
                if cost > _limit_usd(run.spec):
                    reason = "job dollars do not cover the hours"
                    held = None
                elif left is not None and cost > left:
                    reason = "cohort dollars do not cover the hours"
                    held = None
                elif left is not None:
                    left -= cost
            if held is None:
                raise LauncherError(
                    f"cannot launch on {pool.name!r}: {reason}; etude queue would wait"
                )
            placements.append(Placement(run=run, session=existing, machine=held[0], price=held[1]))
        return placements

    def continue_from(self, run: str, until: str) -> Run:
        """The same seed, resumed from `run`'s checkpoint, on its job with a further target.

        `until` is "clock=value" on any clock the run reported, and absolute:
        "steps=63000" trains to step 63,000. The result is a plain Run to pass to launch or
        launch_cohort; it names its parent and the checkpoint's digest.
        """
        parent = self._store.run(run)
        won = next((a for a in self._store.attempts(run=run) if a.outcome == "succeeded"), None)
        if won is None or won.checkpoint is None:
            raise LauncherError(f"run {run} has no succeeded attempt that named a checkpoint")
        output = next((o for o in won.outputs if o.name == won.checkpoint), None)
        if output is None:
            raise LauncherError(f"run {run} named a checkpoint it did not write")
        checkpoint = Checkpoint(
            run=run, name=output.name, sha256=output.sha256, clocks=won.checkpoint_clocks
        )
        return Run(
            job=parent.spec.model_copy(update={"until": until}),
            seed=parent.seed,
            checkpoint=checkpoint,
            params=dict(parent.params),
            source=parent.source,
        )

    def start_phases(self, run: str) -> tuple[StartPhase, ...]:
        """Read retained timestamps only; never tick or ask a machine for freshness."""
        record = self._store.run(run)
        phases = [StartPhase(name="queued", at=record.created_at)]
        for attempt in self._store.attempts(run=run):
            session = self._store.session(attempt.session)
            phases.extend(
                StartPhase(name=name, at=at, attempt=attempt.id, evidence=evidence)
                for name, at, evidence in (
                    ("machine asked for", session.claimed_at, None),
                    ("machine confirmed", None, "receipt retained" if session.receipt else None),
                    ("worker up", session.started_at, None),
                    ("code started", attempt.started_at, None),
                    ("finished", attempt.finished_at, None),
                )
            )
        return tuple(phases)

    def status(self, run: str) -> Status:
        """Look once at everything, then say where this run stands."""
        self.tick()
        return self.read(run)

    def runs(
        self,
        *,
        done: bool = False,
        cohort: str | None = None,
        pool: str | None = None,
        job: str | None = None,
        contains: str | None = None,
    ) -> tuple[RunRecord, ...]:
        """Runs oldest first, filtered without advancing execution. `done` includes history."""
        return tuple(
            r
            for r in self._store.runs()
            if (done or not self.read(r.id).done)
            and (cohort is None or r.cohort == cohort)
            and (pool is None or r.pool == pool)
            and (job is None or r.job == job or r.spec.name == job)
            and (contains is None or source.contains(r, contains))
        )

    def wait(self, *runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
        """Work until every named run is done, then return their statuses in order.

        With no runs named, work until nothing launched is left unfinished. That
        is the loop to leave running so a queue keeps moving.
        """
        give_up = None if timeout is None else self._clock() + timedelta(seconds=timeout)
        while True:
            self.tick()
            statuses = [self.read(run) for run in runs]
            unfinished = not all(status.done for status in statuses)
            if not runs:
                unfinished = bool(self.runs())
            if not unfinished:
                return statuses
            if give_up is not None and self._clock() >= give_up:
                raise TimeoutError("runs still running: " + ", ".join(runs))
            self._sleep(every)

    def cancel(self, run: str) -> Status:
        record = self._store.run(run)
        if record.cancel_requested_at is None:
            self._store.cancel(record, self._clock())
        return self.status(run)

    def logs(self, run: str) -> str:
        """Everything the latest attempt has printed so far."""
        attempt, session = self._latest(run)
        return self._service(session).logs(session.id, attempt.id)

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        """Copy one recorded output into a directory and check it against its digest."""
        attempt, session = self._latest(run)
        if not any(output.name == name for output in attempt.outputs):
            raise LauncherError(f"run {run} recorded no output {name!r}")
        return self._service(session).fetch(session.id, attempt.id, name, destination)

    def record(self, run: str) -> RunRecord:
        """The run as frozen at launch, and where it was sent."""
        return self._store.run(run)

    def attempts(self, run: str) -> tuple[RunAttempt, ...]:
        return self._store.attempts(run=run)

    def read(self, run: str) -> Status:
        """A run's state from the records as they are, without looking at any pool."""
        now = self._clock()
        record = self._store.run(run)
        attempts = self._store.attempts(run=run)
        last = attempts[-1] if attempts else None
        session = None if last is None else self._store.session(last.session)
        rental = None if session is None else self._store.rental_for(session)
        seen = Status(
            run=run,
            phase="queued",
            reason=None,
            outcome=None,
            cause=None,
            until=record.spec.until,
            clocks={} if last is None else last.clocks,
            usd=sum((self._attempt_usd(a, now) for a in attempts), Decimal(0)),
            attempts=len(attempts),
            machine=None if last is None else last.machine,
            rental_ended=None
            if rental is None
            else self._store.machine(rental.machine).deleted_at is not None,
            estimated=any(self._estimated(self._store.session(a.session), now) for a in attempts),
        )
        if last is not None and session is not None and last.outcome is None:
            if session.receipt is None:
                return seen.model_copy(update={"phase": "renting"})
            if record.cancel_requested_at is not None:
                return seen.model_copy(
                    update={"phase": "stopping"}
                )  # asked to stop; not yet seen to have
            return seen.model_copy(
                update={"phase": "setting_up" if last.started_at is None else "running"}
            )
        retry = last is None or (
            last.outcome == "lost"
            and last.cause in RETRYABLE
            and len(attempts) < record.spec.max_attempts
        )
        if last is not None and not retry:
            return seen.model_copy(
                update={"phase": "done", "outcome": last.outcome, "cause": last.cause}
            )
        if record.cancel_requested_at is not None:
            return seen.model_copy(update={"phase": "done", "outcome": "cancelled"})
        if record.cohort is not None and self._cohort_spent(record.cohort, now):
            return seen.model_copy(update={"phase": "done", "outcome": "over_budget"})
        hours, dollars = self._remaining(record, now)
        if hours < 1 or dollars <= 0:
            return seen.model_copy(
                update={"phase": "done", "outcome": "out_of_time" if hours < 1 else "over_budget"}
            )
        if last is not None:
            return seen.model_copy(update={"reason": f"retrying after {last.cause}"})
        cohort = None if record.cohort is None else self._store.cohort(record.cohort)
        if cohort is not None and self._cohort_live(cohort.name, now) >= cohort.parallel:
            return seen.model_copy(update={"reason": f"cohort runs {cohort.parallel} at a time"})
        trouble = self._trouble.get(record.pool)
        why = f"waiting for {record.pool}" + (f": {trouble}" if trouble else "")
        return seen.model_copy(update={"reason": why})

    # The loop

    def tick(self) -> None:
        """Take every step that can be taken now. Safe to call from anywhere, often."""
        for pool in self._store.pools():
            with self._store.pool_lock(pool.name, self._clock) as acquired:
                if acquired:
                    try:
                        self._tick_pool(pool, self._clock())
                    except Stale:
                        continue
        self._bills(self._clock())

    def _tick_pool(self, pool: Pool, now: datetime) -> None:
        for session in self._store.unsettled_sessions():
            if session.pool != pool.name:
                continue
            self._store.check_pool_lock(pool.name)
            if session.claimed_at is not None:
                try:
                    self._observe(session, now)
                except (Stale, OSError, LauncherError, StorageUnavailable):
                    continue  # storage or the provider will answer on a later pass
            elif not self._kept(session) and not self._store.attempts(session=session.id):
                # A crashed admission never asked for this machine. Release its claim.
                self._store.record_refusal(session, now)
        self._store.check_pool_lock(pool.name)
        self._plan(pool, now)
        for idle in self._idle_sessions(pool.name, now):
            if not self._kept(idle):
                self._end_session(idle)
        for session in self._store.unsettled_sessions():
            if session.pool == pool.name and session.claimed_at is None:
                self._create(session, now)

    def _estimated(self, record: Session | Rental, now: datetime) -> bool:
        rental = self._store.rental_for(record) if isinstance(record, Session) else record
        if rental is None:
            return False
        machine = self._store.machine(rental.machine)
        return (
            rental.usd_per_hour > 0
            and machine.requested_at is not None
            and machine.refused_at is None
            and (
                rental.billed_through is None
                or rental.billed_through < (machine.deleted_at or min(now, rental.deadline))
            )
        )

    def _bills(self, now: datetime) -> None:
        for rental in self._store.rentals():
            machine = self._store.machine(rental.machine)
            if machine.provider_id is None or not self._estimated(rental, now):
                continue
            if rental.billed_through is not None and rental.billed_through >= now.replace(
                minute=0, second=0, microsecond=0
            ):
                continue
            try:
                session = self._store.rented_session(rental.machine)
                bill = self._service(session).bill(session.id, now)
                if bill is not None and (
                    rental.billed_through is None or bill.through > rental.billed_through
                ):
                    self._store.record_bill(session, bill.usd, bill.through)
            except Exception:  # unavailable billing leaves an estimate
                continue

    def _plan(self, pool: Pool, now: datetime) -> None:
        """Send waiting runs to a free rented machine, an added machine, or a new one."""
        waiting = (
            []
            if pool.off
            else [r for r in self._store.waiting(pool.name) if not self.read(r.id).done]
        )
        for kept in self._idle_sessions(pool.name, now):
            if not waiting:
                return
            rental = self._store.rental_for(kept)
            left = None if rental is None else int((rental.deadline - now).total_seconds())
            price = Decimal(0) if rental is None else rental.usd_per_hour
            planned = self._next_run(waiting, price, now, fit=left)
            if planned is None:
                continue
            placed = len(self._store.attempts(session=kept.id))
            attempt = self._attempt(planned, kept, placed, now, added=True)
            try:
                self._store.place(kept, (attempt,))
            except Stale:
                return  # another process placed this run
            self._store.check_pool_lock(pool.name)
            self._service(kept).add(kept.id, self._work(attempt))
            waiting.remove(planned.run)
        while waiting:
            held = self._hold(pool)
            if held is None:
                return
            owned, price = held
            planned = self._next_run(waiting, price, now)
            if planned is None:
                return
            hours = planned.run.spec.max_hours
            cap = planned.cap_usd
            slots = 1
            if planned.run.cohort is not None:
                cohort = self._store.cohort(planned.run.cohort)
                slots = max(1, cohort.parallel - self._cohort_live(cohort.name, now))
                pending = [r for r in waiting if r.cohort == cohort.name]
                hours = sum(r.spec.max_hours for r in pending)
                cap = sum((_limit_usd(r.spec) for r in pending), Decimal(0))
                remaining = self._cohort_left(cohort, now)
                if remaining is not None:
                    cap = min(cap, remaining)
            life = hours * 3600 / slots + pool.setup_seconds
            if price > 0:
                life = min(life, float(cap / slots / price * 3600))
            session, on_demand, rental = self._session(
                pool,
                owned,
                price,
                life,
                now,
                kept=False,
            )
            try:
                self._store.plan(
                    session,
                    (self._attempt(planned, session, 0, now),),
                    on_demand,
                    rental,
                )
            except Stale:
                return  # another process planned this run
            waiting.remove(planned.run)

    def _hold(
        self,
        pool: Pool,
        *,
        manual: bool = False,
        excluded: set[str] | None = None,
        additional: int = 0,
    ) -> tuple[Machine | None, Decimal] | None:
        """A machine for a new session and its price: owned first, else on demand.

        The machine is None when one would be made on demand. The result is None when
        the pool has nothing free, may make no more, or asks more than its ceiling.
        """
        live = [r for r in self._store.unsettled_sessions() if r.pool == pool.name]
        mine = self._store.machines(pool.name)
        busy = {r.machine for r in live} | (excluded or set())
        busy |= {m.name for m in mine if self._store.machine_busy(m.name)}
        owned = None if manual else next((m for m in mine if m.name not in busy), None)
        on_demand = sum(self._store.rental_for(r) is not None for r in live) + additional
        if owned is None and (
            pool.on_demand_template is None
            or (on_demand >= pool.max_on_demand and not (manual and pool.max_on_demand == 0))
        ):
            self._trouble[pool.name] = (
                "pool cannot make more machines"
                if pool.on_demand_template is None
                else "pool is at its machine limit"
            )
            return None
        if owned is None and pool.storage is None:
            problem = "a rented machine needs a pool storage"
            if manual:
                raise LauncherError(problem)
            self._trouble[pool.name] = problem
            return None
        try:
            price = self._reach(pool, owned).price()
        except Exception as trouble:  # noqa: BLE001
            # No price, no rental: the runs stay queued, and status says why.
            self._trouble[pool.name] = str(trouble) or type(trouble).__name__
            return None
        self._trouble.pop(pool.name, None)
        ceiling = (
            None
            if owned is not None or pool.on_demand_template is None
            else pool.on_demand_template.max_usd_per_hour
        )
        if ceiling is not None and price > ceiling:
            self._trouble[pool.name] = "price is above the pool ceiling"
            return None  # dearer than this pool allows: wait for the price to come back
        return owned, price

    def _session(
        self,
        pool: Pool,
        owned: Machine | None,
        price: Decimal,
        life: float,
        now: datetime,
        *,
        kept: bool,
    ) -> tuple[Session, Machine | None, Rental | None]:
        """A working session, with a bounded window only when making a machine."""
        id = _id()
        on_demand = None
        rental = None
        if owned is None:
            template = pool.on_demand_template
            assert template is not None  # _hold already checked on-demand capability
            life = min(life, float(pool.max_rental_seconds))
            on_demand = Machine(
                name=f"{pool.name}-{id}",
                pool=pool.name,
                kind="rented",
                provider=template.provider,
            )
            machine = on_demand
            rental = Rental(
                machine=machine.name,
                usd_per_hour=price,
                cap_usd=price * Decimal(life) / 3600,
                deadline=now + timedelta(seconds=life),
                kept=kept,
            )
        else:
            machine = owned
        session = Session(
            id=id,
            pool=pool.name,
            machine=machine.name,
            planned_at=now,
            claimed_at=None,
            receipt=None,
            started_at=None,
            refused_at=None,
            ended_at=None,
            version=1,
            autorun=owned is None,
        )
        return session, on_demand, rental

    def _attempt(
        self,
        planned: PlannedRun,
        session: Session,
        position: int,
        now: datetime,
        *,
        added: bool = False,
    ) -> RunAttempt:
        return RunAttempt(
            id=_id(),
            run=planned.run.id,
            session=session.id,
            machine=session.machine,
            number=len(self._store.attempts(run=planned.run.id)) + 1,
            position=position,
            limit_seconds=planned.seconds,
            limit_kind=planned.limit,
            started_at=None,
            finished_at=None,
            outcome=None,
            cause=None,
            clocks={},
            checkpoint=None,
            outputs=(),
            exit_code=None,
            version=1,
            planned_at=now,
            added=added,
        )

    def _idle_sessions(self, pool: str, now: datetime) -> list[Session]:
        """Sessions in a pool that are up and have nothing left to run."""
        return [
            r
            for r in self._store.unsettled_sessions()
            if r.pool == pool
            and r.receipt is not None
            and self._live(r, now)
            and not self._store.machine_busy(r.machine)
        ]

    def _next_run(
        self,
        waiting: Sequence[RunRecord],
        price: Decimal,
        now: datetime,
        fit: int | None = None,
    ) -> PlannedRun | None:
        """Oldest eligible run, bounded by its remaining time and shared dollars.

        Deliver one attempt at a time. The next scheduling pass reuses the
        machine after that attempt finishes, checking the limits again.
        """
        for run in waiting:
            seconds, cap = self._remaining(run, now)
            if run.cohort is not None:
                cohort = self._store.cohort(run.cohort)
                if self._cohort_live(cohort.name, now) >= cohort.parallel:
                    continue
                left = self._cohort_left(cohort, now)
                if left is not None:
                    cap = min(cap, left)
            limit = self._limit(seconds, cap, price)
            if limit is None:
                continue
            if fit is not None and limit[0] > fit:
                continue  # an existing rental must have time for the whole run
            return PlannedRun(run=run, cap_usd=cap, seconds=limit[0], limit=limit[1])
        return None

    @staticmethod
    def _limit(remaining_seconds: float, cap: Decimal, price: Decimal) -> tuple[int, Limit] | None:
        seconds = int(remaining_seconds)
        if seconds < 1 or cap <= 0:
            return None
        if price <= 0:
            return seconds, "out_of_time"
        paid = int(cap / price * 3600)
        if paid < 1:
            return None
        return (paid, "over_budget") if paid < seconds else (seconds, "out_of_time")

    def _create(self, session: Session, now: datetime, *, idle: bool = False) -> None:
        self._store.check_pool_lock(session.pool)
        try:
            packet = self._packet(session)
        except (OSError, LauncherError):
            self._trouble[session.pool] = "waiting for checkpoint access"
            return
        try:
            self._store.claim(session, now)
        except Stale:
            return  # someone else holds the right to create it
        session = self._store.session(session.id)
        service = self._service(session)
        try:
            receipt = service.create(packet.model_copy(update={"kept": True}) if idle else packet)
        except Refused:
            self._store.record_refusal(session, now)
            for attempt in self._store.attempts(session=session.id):
                self._store.fail_before_start(attempt, "capacity_refused", now)
            return
        except Exception:  # noqa: BLE001
            # No answer is not a refusal: a rental may exist. It is looked up by its
            # identity on later ticks and is never asked for a second time.
            return
        try:
            self._store.record_receipt(session, receipt)
            self._creation_facts(self._store.session(session.id), now)
        except Stale:
            pass  # another process already found the rental by its identity

    def _creation_facts(self, session: Session, now: datetime) -> Session:
        service = self._service(session)
        price = service.charged_price(session.id)
        if price is not None and price != self._usd_per_hour(session):
            self._store.record_price(session, price)
            session = self._store.session(session.id)
        if session.started_at is None and service.started(session.id):
            self._store.record_started(session, now)
            session = self._store.session(session.id)
        return session

    def _work(self, attempt: RunAttempt) -> Work:
        run = self._store.run(attempt.run)
        spec, held = run.spec, run.checkpoint
        ref: CheckpointRef | None = None
        prior = [a for a in self._store.attempts(run=run.id) if a.number < attempt.number]
        checkpoint_attempt = next((a for a in reversed(prior) if a.checkpoint is not None), None)
        if checkpoint_attempt is not None:
            output = next(
                o for o in checkpoint_attempt.outputs if o.name == checkpoint_attempt.checkpoint
            )
            held = Checkpoint(
                run=run.id,
                name=output.name,
                sha256=output.sha256,
                clocks=checkpoint_attempt.checkpoint_clocks,
            )
        elif held is not None:
            checkpoint_attempt = next(
                a for a in self._store.attempts(run=held.run) if a.outcome == "succeeded"
            )
        if held is not None and checkpoint_attempt is not None:
            ref = self._service(self._store.session(checkpoint_attempt.session)).checkpoint(
                CheckpointRef(
                    run=checkpoint_attempt.run,
                    session=checkpoint_attempt.session,
                    attempt=checkpoint_attempt.id,
                    name=held.name,
                    sha256=held.sha256,
                )
            )
        return Work(
            attempt=attempt.id,
            command=spec.command,
            limit_seconds=attempt.limit_seconds,
            limit_kind=attempt.limit_kind,
            seed=run.seed,
            until=spec.until,
            start=None if held is None else held.clocks,
            config=spec.config,
            repo=spec.repo,
            commit=None if spec.repo is None else spec.revision,
            checkpoint=ref,
            run=run.id,
            job=run.job,
            job_name=spec.name or None,
            cohort=run.cohort,
        )

    def _packet(self, session: Session) -> Packet:
        """What one session is sent: its deadline, its work and where its records go."""
        rental = self._store.rental_for(session)
        attempts = self._store.attempts(session=session.id)
        first = [a for a in attempts if not a.added]
        work = tuple(self._work(attempt) for attempt in first)
        return Packet(
            session=session.id,
            deadline=None if rental is None else rental.deadline,
            work=work,
            kept=rental is not None or any(a.added for a in attempts),
            cap_usd=Decimal(0) if rental is None else rental.cap_usd,
            autorun=session.autorun,
        )

    def _observe(self, session: Session, now: datetime) -> None:
        if session.ended_at is not None:
            self._end_session(session)  # ended, but its attempts were never settled
            return
        service = self._service(session)
        attempts = self._store.attempts(session=session.id)
        rental = self._store.rental_for(session)
        deadline = None if rental is None else rental.deadline
        if session.receipt is None:
            receipt = service.find(session.id)
            if receipt is not None:
                self._store.record_receipt(session, receipt)
            elif deadline is not None and now >= deadline:
                for attempt in attempts:
                    if attempt.outcome is None:
                        self._store.finish(attempt, now, "lost", cause="create_unconfirmed")
            return
        if not attempts and (rental is None or not rental.kept):
            self._end_session(session)
            return
        session = self._creation_facts(session, now)
        machine = self._store.machine(session.machine)
        if session.started_at is None and machine.kind == "rented":
            assert session.claimed_at is not None
            if now >= min(
                deadline or now + timedelta(minutes=5),
                session.claimed_at + timedelta(minutes=5),
            ):
                self._end_session(session)
                return
        seen = service.observe(session.id)
        if not seen.alive:
            # Progress and machine state are separate reads. Completion may land
            # between them; settle only from the final journal after shutdown.
            self._end_session(session)
            return
        progress = {entry.attempt: entry for entry in seen.progress}
        for attempt in attempts:
            if attempt.outcome is None:
                if seen.alive and attempt.added and attempt.id not in progress:
                    self._store.check_pool_lock(session.pool)
                    service.add(session.id, self._work(attempt))
                self._record(
                    session,
                    attempt,
                    progress.get(attempt.id),
                    seen.alive,
                    now,
                    unknown_start=seen.start_unknown,
                )
        open_attempts = any(a.outcome is None for a in self._store.attempts(session=session.id))
        finished = not open_attempts and rental is None
        if finished or (deadline is not None and now >= deadline):
            self._end_session(session)

    def _end_session(self, session: Session) -> None:
        """Confirm shutdown, retain final evidence, then settle any unfinished attempts."""
        self._store.check_pool_lock(session.pool)
        service = self._service(session)
        gone = session.ended_at
        if gone is None:
            gone = service.end(session.id)
            if gone is None:
                return
            self._store.confirm_ended(session, gone)
        # Nothing more will be written. Take what the journal holds now, and
        # count any attempt still open as lost with the machine.
        final = service.observe(session.id)
        progress = {entry.attempt: entry for entry in final.progress}
        for attempt in self._store.attempts(session=session.id):
            if attempt.outcome is None:
                self._record(
                    session,
                    attempt,
                    progress.get(attempt.id),
                    False,
                    gone,
                    unknown_start=final.start_unknown,
                )

    def _record(
        self,
        session: Session,
        attempt: RunAttempt,
        seen: Progress | None,
        alive: bool,
        now: datetime,
        *,
        unknown_start: bool = False,
    ) -> None:
        started = None if seen is None else seen.started_at
        if started is not None and attempt.started_at is None:
            self._store.start(attempt, started)
            attempt = attempt.model_copy(
                update={"started_at": started, "version": attempt.version + 1}
            )
        run = self._store.run(attempt.run)
        if seen is not None and seen.finished_at is not None and seen.outcome is not None:
            outcome = seen.outcome
            if outcome == "cancelled" and run.cancel_requested_at is None:
                outcome = "over_budget"
            self._store.finish(
                attempt,
                seen.finished_at,
                outcome,
                cause=seen.cause,
                clocks=seen.clocks,
                checkpoint=seen.checkpoint,
                outputs=seen.outputs,
                exit_code=seen.exit_code,
            )
        elif not alive:
            # Confirmed shutdown fences the old attempt before a replacement is allowed.
            cause: Cause = (
                "machine_lost_before_start"
                if attempt.started_at is None and not unknown_start
                else "create_unconfirmed"
                if attempt.started_at is None
                else "machine_lost"
            )
            self._store.finish(
                attempt,
                now,
                "lost",
                cause=cause,
                clocks=seen.clocks if seen else attempt.clocks,
                checkpoint=seen.checkpoint if seen else None,
                outputs=seen.outputs if seen else attempt.outputs,
            )
        else:
            if seen is not None:
                self._store.record_progress(attempt, seen.clocks, seen.checkpoint, seen.outputs)
            if run.cancel_requested_at is not None or self._remaining(run, now)[1] <= 0:
                self._service(session).stop(session.id, attempt.id)

    # Reading the records

    def _service(self, session: Session) -> Execution:
        """Reach this session through its added machine or its pool provider."""
        pool = self._store.pool(session.pool)
        machine = self._store.machine(session.machine)
        return self._reach(pool, machine if (machine.kind != "rented") else None)

    def _provider_service(self, provider: str) -> str:
        """The service a provider uses. A provider with no record is its own service's name."""
        known = {each.name: each.kind for each in self._store.providers()}
        service = known.get(provider, provider)
        if service not in self._services:
            raise LauncherError(f"no provider named {provider!r}")
        return service

    def _reach(self, pool: Pool, machine: Machine | None) -> Execution:
        key = (pool.name, None if machine is None else machine.name)
        if key not in self._reached:
            name = (
                machine.kind
                if machine
                else self._provider_service(cast(MachineTemplate, pool.on_demand_template).provider)
            )
            self._reached[key] = self._services[name](pool, machine)
        return self._reached[key]

    def _freeze(self, run: Run, on: str, cohort: str | None) -> RunRecord:
        job = run.job
        pool = self._store.pool(on)
        if pool.off:
            raise LauncherError(f"pool {on!r} is turned off")
        if not job.command.strip() or job.max_hours <= 0:
            raise LauncherError("a job needs a command and hours")
        if job.max_usd is None and pool.on_demand_template is not None:
            raise LauncherError(f"pool {on!r} can spend money, so the job needs max_usd")
        if job.max_usd is not None and job.max_usd <= 0:
            raise LauncherError("a job's max_usd must be more than zero")
        if job.max_attempts < 1:
            raise LauncherError("a job needs at least one attempt")
        run = source.freeze(run)
        job = run.job
        if job.until is not None:
            clock, target = parse_target(job.until)
            job = job.model_copy(update={"until": f"{clock}={number(target)}"})
        held = run.checkpoint
        if held is not None:
            written = any(
                attempt.outcome == "succeeded"
                and attempt.checkpoint_clocks == held.clocks
                and any(o.name == held.name and o.sha256 == held.sha256 for o in attempt.outputs)
                for attempt in self._store.attempts(run=held.run)
            )
            if not written:
                raise LauncherError("a continuation needs its parent's recorded checkpoint")
            if job.until is None or clock not in held.clocks:
                raise LauncherError(
                    f"a continuation needs a target on a clock its parent reported: "
                    f"{', '.join(sorted(held.clocks)) or 'it reported none'}"
                )
            if target <= held.clocks[clock]:
                raise LauncherError(
                    f"the target {job.until} is not beyond the checkpoint's "
                    f"{clock}={number(held.clocks[clock])}"
                )
        frozen = json.dumps(job_identity(job), sort_keys=True).encode()
        assert run.id is not None  # source.freeze assigns the submitting client's identity
        return RunRecord(
            id=run.id,
            source=run.source,
            job=hashlib.sha256(frozen).hexdigest()[:12],
            spec=job,
            seed=run.seed,
            checkpoint=held,
            pool=on,
            cohort=cohort,
            created_at=self._clock(),
            cancel_requested_at=None,
            version=1,
            autorun=True,
            params=dict(run.params),
        )

    def _latest(self, run: str) -> tuple[RunAttempt, Session]:
        for attempt in reversed(self._store.attempts(run=run)):
            session = self._store.session(attempt.session)
            if session.receipt is not None:
                return attempt, session
        raise LauncherError(f"run {run} has not reached a session")

    def _remaining(self, run: RunRecord, now: datetime) -> tuple[float, Decimal]:
        """Execution seconds and attributed dollars left across all attempts."""
        attempts = self._store.attempts(run=run.id)
        seconds = sum(
            max(0, ((a.finished_at or now) - a.started_at).total_seconds())
            for a in attempts
            if a.started_at is not None
        )
        dollars = sum((self._attempt_usd(a, now) for a in attempts), Decimal(0))
        return max(0, run.spec.max_hours * 3600 - seconds), max(
            Decimal(0), _limit_usd(run.spec) - dollars
        )

    def _attempt_usd(self, attempt: RunAttempt, now: datetime, *, billed: bool = True) -> Decimal:
        """One attempt's share of its rental.

        It starts when the rental did, when the attempt before it ended, or
        when this one was placed, whichever is latest. Idle time on a kept
        rental therefore belongs to the rental, not to the next run.
        """
        session = self._store.session(attempt.session)
        rental = self._store.rental_for(session)
        if rental is None:
            return Decimal(0)
        if session.claimed_at is None or session.refused_at is not None:
            return Decimal(0)
        start = max(session.claimed_at, attempt.planned_at or session.claimed_at)
        for earlier in self._store.attempts(session=session.id)[: attempt.position]:
            if earlier.finished_at is None:
                return Decimal(0)
            start = max(start, earlier.finished_at)
        end = attempt.finished_at or session.ended_at or min(now, rental.deadline)
        return (
            _cost_between(rental, self._store.machine(session.machine), start, end)
            if billed
            else _usd(rental.usd_per_hour, start, end)
        )

    def _cohort_live(self, name: str, now: datetime) -> int:
        """Sessions a cohort's runs are on or waiting on right now."""
        held = {a.session for a in self._store.cohort_attempts(name) if a.outcome is None}
        return sum(1 for id in held if self._live(self._store.session(id), now))

    def _cohort_left(self, cohort: Cohort, now: datetime) -> Decimal | None:
        """Dollars a new attempt may still use: unfinished ones count at their full limit."""
        if cohort.max_usd is None:
            return None
        used = Decimal(0)
        for attempt in self._store.cohort_attempts(cohort.name):
            if attempt.outcome is not None:
                used += self._attempt_usd(attempt, now, billed=False)
                continue
            session = self._store.session(attempt.session)
            if session.refused_at is None:
                used += self._usd_per_hour(session) * attempt.limit_seconds / 3600
        return cohort.max_usd - used

    def _cohort_spent(self, name: str, now: datetime) -> bool:
        """True when the cohort can never start another attempt."""
        cohort = self._store.cohort(name)
        left = self._cohort_left(cohort, now)
        if left is None or any(a.outcome is None for a in self._store.cohort_attempts(name)):
            return False
        price = max(
            (self._usd_per_hour(session) for session in self._store.cohort_sessions(name)),
            default=None,
        )
        return left <= 0 or (price is not None and price > 0 and left / price * 3600 < 1)
