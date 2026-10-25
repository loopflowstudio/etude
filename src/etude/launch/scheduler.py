"""The scheduler: owner of one launcher database, and the one loop that moves work forward.

It queues and admits runs, matches waiting runs to machines and enforces limits.
A run may use a pool's machines you own, rent from an on-demand, or both; a
free owned machine is always used before anything is rented. Each pool and
on-demand (a supply) has its own placement lock, and `tick` visits every pool
before any on-demand.

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
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from etude.carriage import Mismatch
from etude.launch import source
from etude.launch.database import Database
from etude.launch.deadline import limit_time, remaining
from etude.launch.errors import LaunchError
from etude.launch.executor import Execution, Refused, creation_guard
from etude.launch.logging import bounded, explain, sanitize
from etude.launch.machines import Safeguards
from etude.launch.records import (
    RETRYABLE,
    STOCK_IMAGE,
    Availability,
    Cause,
    Cohort,
    Diagnostic,
    Hardware,
    Image,
    Job,
    Launch,
    LauncherError,
    LaunchRequest,
    Limit,
    LogProducer,
    LogSnapshot,
    LogStream,
    Machine,
    Offer,
    OnDemand,
    Pool,
    Provider,
    Quote,
    QuotedMachine,
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
    Supply,
    Target,
    job_identity,
    number,
    parse_target,
)
from etude.launch.schemas import (
    Observation,
    Packet,
    Progress,
    Work,
)
from etude.launch.storage.s3 import StorageUnavailable, longest_grant
from etude.launch.storage.transport import attribute, observe
from etude.launch.usage import Usage

Clock = Callable[[], datetime]
# How the scheduler reaches a machine you own, or the provider behind an on-demand.
Reach = Callable[[Machine | OnDemand], Execution]


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
    """Capacity selected under its supplies' locks; None machine means a new rented machine."""

    run: RunRecord
    supply: str  # the pool or on-demand the machine belongs to, or would be rented from
    session: Session | None
    machine: Machine | None
    price: Decimal
    hardware: Hardware | None = None  # what a new rental would ask for


class LogSelection(Record):
    """Shared launch/session evidence and only the requested run's attempts."""

    launches: tuple[Launch, ...]
    sessions: tuple[Session, ...]
    attempts: tuple[RunAttempt, ...]


class Scheduler:
    def __init__(
        self,
        store: Database,
        services: Mapping[str, Reach],
        clock: Clock = _utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._services = dict(services)  # by service name: a machine kind or a provider kind
        self._reached: dict[tuple[str, str, str | None], Execution] = {}
        self._trouble: dict[str, str] = {}  # why a pool or on-demand could not place work
        self._grants: dict[str, float] = {}  # by storage: a storage never changes once added
        self._relayed: set[str] = set()  # attempts handed their run's folder by this process
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
        """Make a pool of machines you own available to launch on."""
        self._store.add_pool(pool)

    def add_ondemand(self, ondemand: OnDemand) -> None:
        """Make an on-demand available to rent from. Its provider's service must be held here."""
        self._provider_service(ondemand.template.provider)  # raises if it cannot be reached
        known = {each.name: each.storage for each in self._store.providers()}
        # Fixed here, so changing the provider later cannot move a live rental's records.
        named = ondemand.storage or known.get(ondemand.template.provider)
        if named is not None:
            ondemand = ondemand.model_copy(update={"storage": self._store.storage(named).name})
        self._store.add_ondemand(ondemand)

    def ondemands(self) -> tuple[OnDemand, ...]:
        return self._store.ondemands()

    def set_startup(self, name: str, seconds: float | None) -> None:
        """Change a pool's or on-demand's startup allowance for future sessions; None removes it."""
        supply = self._store.supply(name)
        if isinstance(supply, Pool):
            self._store.set_pool_startup(name, seconds)
        else:
            self._store.change_ondemand(
                OnDemand.model_validate({**supply.model_dump(), "startup_seconds": seconds})
            )

    def set_ondemand_max(self, name: str, maximum: int) -> None:
        """Bound automatic renting; machines already rented keep their windows."""
        if maximum < 0:
            raise LauncherError("an on-demand's machine limit must be zero or greater")
        self._store.change_ondemand(
            self._store.ondemand(name).model_copy(update={"max_machines": maximum})
        )

    def set_ondemand_image(self, name: str, image: Image | str) -> None:
        """Change the image an on-demand's next machines boot. Running ones are untouched."""
        selected = Image(requested=image, resolved=image) if isinstance(image, str) else image
        ondemand = self._store.ondemand(name)
        template = ondemand.template.model_copy(update={"image": selected})
        self._store.change_ondemand(ondemand.model_copy(update={"template": template}))

    def set_default(self, pool: str | None = None, *, rent: str | None = None) -> None:
        """Choose where runs go when a launch names neither a pool nor an on-demand."""
        if pool is None and rent is None:
            raise LauncherError("a default needs a pool, an on-demand or both")
        self._store.set_default_target(Target(pool=pool, rent=rent))

    def default_target(self) -> Target:
        """Where a queue or launch goes when it names neither a pool nor an on-demand."""
        return self._store.default_target()

    def _target(self, pool: str | None, rent: str | None) -> Target:
        if pool is not None or rent is not None:
            return Target(pool=pool, rent=rent)
        target = self._store.default_target()
        if target.pool is not None and self._store.pool(target.pool).off:
            if target.rent is None:
                raise LauncherError(
                    f"default pool {target.pool!r} is turned off; name a pool with --pool"
                    " or an on-demand with --rent"
                )
            target = Target(rent=target.rent)
        if target.pool is None and target.rent is None:
            raise LauncherError("no default; name a pool with --pool or an on-demand with --rent")
        return target

    def add_machine(
        self,
        name: str,
        address: str,
        *,
        pool: str | None = None,
        storage: str | None = None,
        cost_usd: Decimal = Decimal(0),
        expires_at: datetime | None = None,
    ) -> None:
        """Add an owned SSH host, with what it cost and when it stops being yours.

        This computer is already the local pool. Without a pool, the host gets
        a pool of its own name.
        """
        if name == "local":
            raise LauncherError("this computer is already the local pool; use pool on/off local")
        if cost_usd < 0:
            raise LauncherError("a machine's cost must be zero or more")
        if storage is not None:
            self._store.storage(storage)
        if pool is None:
            self._store.add_pool(Pool(name=name))
        self._store.add_machine(
            Machine(
                name=name,
                pool=pool or name,
                kind="ssh",
                address=address,
                storage=storage,
                cost_usd=cost_usd,
                expires_at=expires_at,
            )
        )

    def machines(self, supply: str) -> tuple[Machine, ...]:
        """A pool's machines, or every machine rented from an on-demand so far."""
        return self._store.machines(supply)

    def rent(self, ondemand: str, hours: float, max_usd: Decimal | None = None) -> str:
        """Hold a machine from an on-demand until a deadline, for runs to be launched onto.

        Launching does this for itself when no existing machine has room; doing
        it by hand pays for setup once across several launches. Returns the
        machine's name. It ends at its deadline, or sooner with `terminate`.
        """
        with self._store.lock(ondemand, self._clock) as acquired:
            if not acquired:
                raise LauncherError(f"on-demand {ondemand!r} is busy")
            now = self._clock()
            supply = self._store.ondemand(ondemand)
            offer = self._rentable(supply, manual=True)
            if offer is None:
                raise LauncherError(
                    f"on-demand {ondemand!r} has no machine to rent right now: "
                    + self._trouble.get(ondemand, "unknown")
                )
            price = offer.usd_per_hour
            life = hours * 3600
            if price > 0 and max_usd is not None:
                life = min(life, float(max_usd / price * 3600))
            if life < 1:
                raise LauncherError("a rental needs hours, and dollars for at least a second")
            longest = self._longest_rental(supply)
            if life > longest:
                raise LauncherError(
                    f"on-demand {ondemand!r} keeps records in a storage whose grants last at "
                    f"most {longest / 3600:g} hours"
                )
            session, rented, rental = self._session(supply, None, price, life, now, kept=True)
            self._store.plan(session, (), rented, rental)
            self._tick_supply(supply, self._clock())
            return session.machine

    def terminate(self, machine: str) -> Machine:
        """Delete a rented machine now; refuse while an allocation is unfinished."""
        named = self._store.machine(machine)
        if named.kind != "rented":
            raise LauncherError(f"machine {machine!r} was added; it cannot be terminated")
        self.tick()
        with self._store.lock(named.supply, self._clock) as acquired:
            if not acquired:
                raise LauncherError(f"on-demand {named.supply!r} is busy")
            if self._store.machine_busy(machine):
                raise LauncherError(f"machine {machine!r} still has a run on it")
            held = self._store.rented_session(machine)
            if held.ended_at is None and held.refused_at is None and held.receipt is not None:
                self._end_session(held)
                if self._store.session(held.id).ended_at is None:
                    raise LauncherError(f"machine {machine!r} did not confirm it ended")
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

    def safeguards(self, ondemand: str) -> tuple[str, ...]:
        """What ends an on-demand's machines if the launcher stops watching."""
        template = self._store.ondemand(ondemand).template
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

    def availability(self, rent: str, *, timeout: float | None = None) -> Availability:
        """Ask an on-demand's provider what it would rent now and at what price.

        This waits on the provider, within `timeout` seconds when given. It rents nothing.
        """
        ondemand = self._store.ondemand(rent)
        with limit_time(timeout):
            offer = self._reach(ondemand).offer()
        return Availability(
            ondemand=rent,
            offer=offer,
            max_usd_per_hour=ondemand.max_usd_per_hour,
            in_use=sum(s.supply == rent for s in self._store.unsettled_sessions()),
            max_machines=ondemand.max_machines,
            observed_at=self._clock(),
        )

    def queue(self, run: Run | Job, pool: str | None = None, *, rent: str | None = None) -> str:
        """Queue one run; it waits for a free machine, or for its on-demand to rent one."""
        return self._submit(
            (run if isinstance(run, Run) else Run(job=run),),
            self._target(pool, rent),
            None,
            queued=True,
        )[0]

    def queue_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        rent: str | None = None,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        """Queue a cohort under its shared limits; capacity shortages wait."""
        cohort = self._new_cohort(name, runs, parallel, max_usd)
        return self._submit(runs, self._target(pool, rent), cohort, queued=True)

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

    def launch(self, run: Run | Job, pool: str | None = None, *, rent: str | None = None) -> str:
        """Acquire confirmed capacity, then record and hand over one run."""
        return self._submit(
            (run if isinstance(run, Run) else Run(job=run),),
            self._target(pool, rent),
            None,
            queued=False,
        )[0]

    def launch_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        rent: str | None = None,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        """Record a named set of runs sharing rentals-at-once and total-dollar limits."""
        cohort = self._new_cohort(name, runs, parallel, max_usd)
        return self._submit(runs, self._target(pool, rent), cohort, queued=False)

    def quote(
        self,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        rent: str | None = None,
        cohort: str | None = None,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> Quote:
        """The machines a launch would take now and the most they could cost. Rents nothing.

        It asks the on-demand's provider for its price, so it waits on the provider.
        Without capacity it raises, as the launch would.
        """
        target = self._target(pool, rent)
        limits = None if cohort is None else self._new_cohort(cohort, runs, parallel, max_usd)
        records = []
        for index, run in enumerate(runs):
            self._validate_job(run.job, target)
            records.append(
                RunRecord(
                    id=run.id or f"run{index + 1}",
                    job="",
                    spec=run.job,
                    seed=run.seed,
                    pool=target.pool,
                    rent=target.rent,
                    cohort=cohort,
                    created_at=self._clock(),
                    cancel_requested_at=None,
                    version=1,
                )
            )
        if not records:
            raise LauncherError("a launch needs at least one run")
        quoted = []
        for placement in self._capacity(records, limits):
            price = placement.price
            record = placement.run
            if placement.machine is None:
                ondemand = self._store.ondemand(placement.supply)
                life = self._life(ondemand, records, limits, price)
                seconds = min(life, self._longest_rental(ondemand))
                most = price * Decimal(seconds) / 3600
            else:
                most = min(price * Decimal(str(record.spec.max_hours)), _limit_usd(record.spec))
            quoted.append(
                QuotedMachine(
                    run=record.name,
                    machine=placement.machine.name if placement.machine else None,
                    hardware=placement.hardware,
                    usd_per_hour=price,
                    max_usd=most,
                )
            )
        caps = sum((_limit_usd(r.spec) for r in records), Decimal(0))
        if limits is not None and limits.max_usd is not None:
            caps = min(caps, limits.max_usd)
        return Quote(machines=tuple(quoted), limit_usd=None if target.rent is None else caps)

    def _submit(
        self, runs: Sequence[Run], target: Target, cohort: Cohort | None, *, queued: bool
    ) -> tuple[str, ...]:
        launch = self.prepare_launch(runs, target.pool, rent=target.rent)
        requested = tuple(
            run.model_copy(update={"id": request.run})
            for run, request in zip(runs, launch.requests, strict=True)
        )
        return self.submit_launch(launch.id, requested, target, cohort, queued=queued)

    def prepare_launch(
        self, runs: Sequence[Run], pool: str | None = None, *, rent: str | None = None
    ) -> Launch:
        """Retain valid intent before the submitting client publishes source."""
        target = self._target(pool, rent)
        if not runs:
            raise LauncherError("a launch needs at least one run")
        for run in runs:
            self._validate_job(run.job, target)
        requested = tuple(run.model_copy(update={"id": run.id or _id()}) for run in runs)
        ids = tuple(str(run.id) for run in requested)
        if len(set(ids)) != len(ids):
            raise LauncherError("a launch cannot request the same run ID twice")
        launch = Launch(
            id=_id(),
            created_at=self._clock(),
            requests=tuple(
                LaunchRequest(
                    run=str(run.id),
                    fingerprint=hashlib.sha256(run.model_dump_json().encode()).hexdigest(),
                    requested_revision=sanitize(run.job.revision) if run.job.revision else None,
                )
                for run in requested
            ),
        )
        self._store.begin_launch(launch)
        self._diagnose(
            "preparation", "awaiting prepared source; job command not started", launch=launch.id
        )
        return launch

    def fail_launch(self, identity: str, message: str) -> None:
        launch = self._store.claim_launch(identity)
        self._store.save_launch(launch.model_copy(update={"state": "failed"}))
        self._diagnose("source preparation", message, launch=identity)

    def submit_launch(
        self,
        identity: str,
        runs: Sequence[Run],
        target: Target,
        cohort: Cohort | None,
        *,
        queued: bool,
    ) -> tuple[str, ...]:
        launch = self._store.claim_launch(identity)
        selected = self._target(target.pool, target.rent)
        requested = tuple(runs)
        ids = tuple(request.run for request in launch.requests)
        if tuple(run.id for run in requested) != ids:
            self._store.save_launch(launch.model_copy(update={"state": "failed"}))
            raise LauncherError("prepared run identities differ from launch intent")
        operation = "source preparation"
        self._diagnose(
            operation, "preparing requested work; job command not started", launch=launch.id
        )
        try:
            records = tuple(
                self._freeze(run, selected, cohort.name if cohort else None) for run in requested
            )
            launch = launch.model_copy(
                update={
                    "requests": tuple(
                        request.model_copy(update={"resolved_revision": record.spec.revision})
                        for request, record in zip(launch.requests, records, strict=True)
                    )
                }
            )
            self._store.save_launch(launch)
            operation = "admission"
            if queued:
                self._store.launch(records, cohort, launch=launch.id)
            else:
                self._admit(records, cohort, launch.id)
        except BaseException as error:
            if self._store.launch_evidence(launch.id).state == "admitted":
                self._diagnose(
                    "work delivery", "run admitted; delivery unconfirmed", error, launch=launch.id
                )
                if not isinstance(error, Exception):
                    raise
                raise LaunchError(
                    launch.id, ids, "run admitted; delivery unconfirmed; " + explain(error)
                ) from error
            self._store.save_launch(launch.model_copy(update={"state": "failed"}))
            self._diagnose(operation, "admission failed; no run recorded", error, launch=launch.id)
            if not isinstance(error, Exception):
                raise
            sessions = self._store.launch_sessions(launch.id)
            details = [explain(error), "job command not started"]
            for session in sessions:
                certainty = (
                    "confirmed deleted"
                    if session.ended_at
                    else "no machine acquired"
                    if session.refused_at
                    else "unconfirmed; reconciliation continues"
                )
                details.append(f"machine {session.machine}: cleanup {certainty}")
            details.extend(
                d.message
                for d in self.diagnostics(run=ids[0])
                if d.exception and d.session in {s.id for s in sessions}
            )
            raise LaunchError(launch.id, ids, "; ".join(details)) from error
        self._diagnose("admission", "run admitted", launch=launch.id)
        return ids

    def _diagnose(
        self,
        operation: str,
        message: str,
        error: BaseException | None = None,
        *,
        launch: str | None = None,
        session: str | None = None,
        attempt: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        message = bounded(message + ("; " + explain(error) if error else ""))
        identity = hashlib.sha256(
            json.dumps([launch, session, attempt, operation, message]).encode()
        ).hexdigest()
        self._store.retain_diagnostic(
            Diagnostic(
                id=identity,
                observed_at=self._clock(),
                operation=operation,
                message=message,
                launch=launch,
                session=session,
                attempt=attempt,
                exception=type(error).__name__ if error else None,
                exit_code=error.returncode
                if isinstance(error, subprocess.CalledProcessError)
                else exit_code,
                timeout=error.timeout if isinstance(error, subprocess.TimeoutExpired) else None,
            )
        )

    def _check_locks(self, names: Sequence[str]) -> None:
        for name in names:
            self._store.check_lock(name)

    def _life(
        self,
        ondemand: OnDemand,
        records: Sequence[RunRecord],
        cohort: Cohort | None,
        price: Decimal,
    ) -> float:
        """Seconds a first-wave rental is held: its share of the hours, within the dollars."""
        slots = cohort.parallel if cohort else 1
        hours = sum(r.spec.max_hours for r in records) / slots
        cap = (
            cohort.max_usd
            if cohort and cohort.max_usd is not None
            else sum((_limit_usd(r.spec) for r in records), Decimal(0))
        ) / slots
        return min(
            hours * 3600 + ondemand.setup_seconds,
            float(cap / price * 3600) if price else float("inf"),
        )

    def _stage(self, ondemand: OnDemand, runs: Sequence[RunRecord]) -> None:
        """Put the runs' source archives in the on-demand's storage before renting for them.

        A rental's window starts when it is made, so no paid machine waits on an upload.
        """
        service = self._reach(ondemand)
        for repo, commit in sorted(
            {(r.spec.repo, r.spec.revision) for r in runs if r.spec.repo and r.spec.revision}
        ):
            service.stage(repo, commit)

    def _admit(self, records: Sequence[RunRecord], cohort: Cohort | None, launch: str) -> None:
        target = Target(pool=records[0].pool, rent=records[0].rent)
        names = [name for name in (target.pool, target.rent) if name is not None]
        if target.rent is not None:
            # Before the locks: an upload can outlast their lease.
            self._stage(self._store.ondemand(target.rent), records)
        with ExitStack() as locks:
            for name in names:  # pools before on-demands, as tick takes them
                if not locks.enter_context(self._store.lock(name, self._clock)):
                    raise LauncherError(
                        f"cannot launch on {name!r}: it is busy; etude queue would wait"
                    )
            placements = self._capacity(records, cohort)
            made: list[Session] = []
            attempts: list[RunAttempt] = []
            try:
                for placement in placements:
                    run, existing = placement.run, placement.session
                    machine, price = placement.machine, placement.price
                    self._check_locks(names)
                    now = self._clock()
                    if existing is None:
                        supply = self._store.supply(placement.supply)
                        life = (
                            self._life(supply, records, cohort, price)
                            if isinstance(supply, OnDemand)
                            else 0.0
                        )
                        session, rented, rental = self._session(
                            supply, machine, price, life, now, kept=False
                        )
                        self._store.plan(session, (), rented, rental, launch=launch)
                        made.append(session)
                        if rented is not None:
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
                                    f"cannot launch on {placement.supply!r}: {reason};"
                                    " etude queue would wait"
                                )
                    else:
                        session = self._store.session(existing.id)
                        if not self._live(session, now):
                            raise LauncherError(
                                "rented machine expired during launch; etude queue would wait"
                            )
                    self._store.link_launch(launch, session.id)
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
                self._check_locks(names)
                if any(
                    not self._live(self._store.session(a.session), self._clock()) for a in attempts
                ):
                    raise LauncherError("capacity expired during launch; etude queue would wait")
                # Runs and claims become visible together, under the same locks.
                self._store.launch(records, cohort, attempts, launch=launch)
            except BaseException:
                for session in made:
                    try:
                        self._check_locks(names)
                        session = self._store.session(session.id)
                        if session.receipt is not None:
                            self._end_session(session)
                        elif session.claimed_at is None:
                            self._store.record_refusal(session, self._clock())
                    except Exception as error:
                        self._diagnose("cleanup", "cleanup unconfirmed", error, session=session.id)
                        # Next tick cleans up; unknown creates are only looked up.
                raise
            # Delivery is retryable from committed attempts; capacity is already held.
            for attempt in attempts:
                session = self._store.session(attempt.session)
                try:
                    self._check_locks(names)
                    if session.claimed_at is None:
                        self._create(session, self._clock())
                    else:
                        self._observe(session, self._clock())
                except (Stale, OSError, LauncherError, StorageUnavailable) as error:
                    self._diagnose(
                        "work delivery",
                        "delivery pending",
                        error,
                        session=session.id,
                        attempt=attempt.id,
                    )
                    continue  # committed attempts retain delivery and startup recovery

    def _capacity(
        self,
        records: Sequence[RunRecord],
        cohort: Cohort | None,
    ) -> list[Placement]:
        """Choose the first wave, checking all limits before spending.

        Idle machines come first, then the pool's free machines, then rentals
        from the on-demand. Nothing is claimed here.
        """
        placements: list[Placement] = []
        target = Target(pool=records[0].pool, rent=records[0].rent)
        pool = None if target.pool is None else self._store.pool(target.pool)
        ondemand = None if target.rent is None else self._store.ondemand(target.rent)
        label = " or ".join(name for name in (target.pool, target.rent) if name is not None)
        now = self._clock()
        used: set[str] = set()
        made = 0
        left = None if cohort is None else cohort.max_usd
        first = records[: 1 if cohort is None else cohort.parallel]
        if pool is not None and pool.off:
            raise LauncherError(f"pool {pool.name!r} is turned off")
        if cohort is not None and self._store.cohort_exists(cohort.name):
            raise LauncherError(f"cohort {cohort.name!r} already exists")
        idle = [
            session
            for name in (target.pool, target.rent)
            if name is not None
            for session in self._idle_sessions(name, now)
        ]
        for run in first:
            held: Placement | None = None
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
                held = Placement(
                    run=run,
                    supply=session.supply,
                    session=session,
                    machine=self._store.machine(session.machine),
                    price=Decimal(0) if rental is None else rental.usd_per_hour,
                )
                break
            if held is None and pool is not None:
                owned = self._free_owned(pool, now, excluded=used)
                reason = self._trouble.get(pool.name, reason)
                if owned is not None:
                    used.add(owned.name)
                    held = Placement(
                        run=run, supply=pool.name, session=None, machine=owned, price=Decimal(0)
                    )
            if held is None and ondemand is not None:
                offer = self._rentable(ondemand, additional=made)
                reason = self._trouble.get(ondemand.name, reason)
                if offer is not None:
                    made += 1
                    held = Placement(
                        run=run,
                        supply=ondemand.name,
                        session=None,
                        machine=None,
                        price=offer.usd_per_hour,
                        hardware=offer.hardware,
                    )
            if held is not None:
                hours = Decimal(str(run.spec.max_hours))
                if held.machine is None and ondemand is not None:
                    hours += Decimal(ondemand.setup_seconds) / 3600
                cost = held.price * hours
                if cost > _limit_usd(run.spec):
                    reason = "job dollars do not cover the hours"
                    held = None
                elif left is not None and cost > left:
                    reason = "cohort dollars do not cover the hours"
                    held = None
                elif left is not None:
                    left -= cost
            if held is None:
                raise LauncherError(f"cannot launch on {label}: {reason}; etude queue would wait")
            placements.append(held)
        return placements

    def start_phases(self, run: str) -> tuple[StartPhase, ...]:
        """Read retained timestamps only; never tick or ask a machine for freshness."""
        selection = self._log_selection(run, None)
        phases = [
            StartPhase(name=f"launch {launch.id} {launch.state}", at=launch.created_at)
            for launch in selection.launches
        ]
        if not selection.launches or any(
            launch.state == "admitted" for launch in selection.launches
        ):
            phases.append(StartPhase(name="queued", at=self._store.run(run).created_at))
        phases.extend(
            StartPhase(name=d.operation, at=d.observed_at, attempt=d.attempt, evidence=d.message)
            for d in self._diagnostics(selection)
        )
        sessions = {session.id: session for session in selection.sessions}
        seen_sessions: set[str] = set()
        for attempt in selection.attempts:
            session = sessions[attempt.session]
            if session.id not in seen_sessions:
                seen_sessions.add(session.id)
                startup = self._store.log(f"{session.id}/bootstrap")
                if startup is not None and startup.received:
                    phases.append(
                        StartPhase(
                            name="retained bootstrap output",
                            at=startup.observed_at,
                            evidence=startup.text[-1000:],
                            attempt=attempt.id,
                        )
                    )
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
        rent: str | None = None,
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
            and (rent is None or r.rent == rent)
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

    def _log_selection(self, run: str | None, machine: str | None) -> LogSelection:
        if (run is None) == (machine is None):
            raise LauncherError("name exactly one run or machine")
        launches = self._store.launches(run) if run is not None else ()
        attempts = self._store.attempts(run=run) if run is not None else ()
        sessions = {s.id: s for launch in launches for s in self._store.launch_sessions(launch.id)}
        for attempt in attempts:
            if attempt.session not in sessions:
                sessions[attempt.session] = self._store.session(attempt.session)
        if machine is not None:
            self._store.machine(machine)
            sessions.update((s.id, s) for s in self._store.sessions() if s.machine == machine)
        elif not launches and not attempts:
            self._store.run(str(run))  # distinguish a queued run from an unknown identity
        return LogSelection(launches=launches, sessions=tuple(sessions.values()), attempts=attempts)

    def diagnostics(
        self, *, run: str | None = None, machine: str | None = None
    ) -> tuple[Diagnostic, ...]:
        """Retained facts only: no provider, bucket, or scheduling work."""
        return self._diagnostics(self._log_selection(run, machine))

    def _diagnostics(self, selection: LogSelection) -> tuple[Diagnostic, ...]:
        launch_ids = {launch.id for launch in selection.launches}
        session_ids = {session.id for session in selection.sessions}
        attempt_ids = {attempt.id for attempt in selection.attempts}
        return tuple(
            d
            for d in self._store.diagnostics()
            if d.launch in launch_ids
            or (d.session in session_ids and (d.attempt is None or d.attempt in attempt_ids))
        )

    def logs(
        self, *, run: str | None = None, machine: str | None = None, timeout: float | None = None
    ) -> LogSnapshot:
        """All retained stages and attempts, with bounded refresh and explicit gaps."""
        selection = self._log_selection(run, machine)
        streams = self._collect_logs(selection.sessions, selection.attempts, timeout)
        settled = all(
            session.ended_at is not None or session.refused_at is not None
            for session in selection.sessions
        )
        if run is not None:
            if not selection.launches or any(
                launch.state == "admitted" for launch in selection.launches
            ):
                settled = settled and self.read(run).done
            else:
                settled = settled and all(launch.state == "failed" for launch in selection.launches)
        return LogSnapshot(
            settled=settled,
            observed_at=self._clock(),
            launches=selection.launches,
            diagnostics=self._diagnostics(selection),
            streams=streams,
        )

    def _collect_logs(
        self, sessions: Sequence[Session], attempts: Sequence[RunAttempt], timeout: float | None
    ) -> tuple[LogStream, ...]:
        streams: list[LogStream] = []
        with limit_time(timeout):
            for session in sessions:
                if session.refused_at is not None:
                    continue
                selected: list[tuple[str | None, LogProducer]] = [
                    (None, "bootstrap"),
                    (None, "worker"),
                ]
                selected.extend((a.id, "job") for a in attempts if a.session == session.id)
                for attempt, producer in selected:
                    streams.append(self._collect_log(session, attempt, producer))
        return tuple(streams)

    def _collect_log(
        self, session: Session, attempt: str | None, producer: LogProducer
    ) -> LogStream:
        """Refresh one producer under the caller's deadline, preserving its cached evidence."""
        identity = f"{session.id}/{attempt or producer}"
        cached = self._store.log(identity)
        if cached is not None and cached.complete:
            return cached
        try:
            remaining()
            stream = self._service(session).logs(session.id, attempt, producer=producer)
            if not stream.received and cached is not None:
                stream = cached.model_copy(
                    update={"retrieval_error": "previously received object is now unavailable"}
                )
            for diagnostic in stream.diagnostics:
                self._store.retain_diagnostic(
                    diagnostic.model_copy(
                        update={
                            "session": session.id,
                            "produced_at": diagnostic.observed_at,
                            "observed_at": self._clock(),
                        }
                    )
                )
            # Diagnostics travel with the stream, but the table owns retained
            # observations and selection. Persist them before caching final closure.
            stream = stream.model_copy(update={"machine": session.machine, "diagnostics": ()})
            self._store.retain_log(stream)
        except Exception as error:
            stream = (
                cached
                or LogStream(
                    id=identity,
                    producer=producer,
                    session=session.id,
                    attempt=attempt,
                    machine=session.machine,
                )
            ).model_copy(update={"retrieval_error": explain(error)})
        return stream

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        """Copy one recorded output into a directory and check it against its digest."""
        attempt, session = self._latest(run)
        if not any(output.name == name for output in attempt.outputs):
            raise LauncherError(f"run {run} recorded no output {name!r}")
        with (
            observe(self._store.retain_transfer_json),
            attribute(session=session.id, machine=session.machine, run=run, attempt=attempt.id),
        ):
            return self._service(session).fetch(session.id, attempt.id, name, destination)

    def usage(self, *, run: str | None = None, machine: str | None = None) -> Usage:
        """Read retained observations offline; shared session traffic stays shared."""
        sessions = set() if run is None else {a.session for a in self._store.attempts(run=run)}
        machines = {session.id: session.machine for session in self._store.sessions()}
        retained = tuple(
            event.model_copy(update={"machine": event.machine or machines.get(event.session or "")})
            for event in self._store.transfers()
        )
        retained = tuple(e for e in retained if machine is None or e.machine == machine)
        events = tuple(e for e in retained if run is None or e.run == run)
        shared = tuple(
            e for e in retained if run is not None and e.run is None and e.session in sessions
        )
        return Usage(
            events=events,
            shared_events=shared,
            observed_at=max((e.observed_at for e in (*events, *shared)), default=None),
        )

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
            hardware=None if session is None else session.hardware,
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
        waits = [
            name + (f": {self._trouble[name]}" if name in self._trouble else "")
            for name in (record.pool, record.rent)
            if name is not None
        ]
        return seen.model_copy(update={"reason": "waiting for " + "; ".join(waits)})

    # The loop

    def tick(self) -> None:
        """Take every step that can be taken now. Safe to call from anywhere, often.

        Every pool goes before any on-demand, so a run that may use both takes a
        free owned machine before anything is rented for it.
        """
        supplies: tuple[Supply, ...] = (*self._store.pools(), *self._store.ondemands())
        for supply in supplies:
            with self._store.lock(supply.name, self._clock) as acquired:
                if acquired:
                    try:
                        self._tick_supply(supply, self._clock())
                    except Stale:
                        continue
        self._bills(self._clock())

    def _tick_supply(self, supply: Supply, now: datetime) -> None:
        for session in self._store.unsettled_sessions():
            if session.supply != supply.name:
                continue
            self._store.check_lock(supply.name)
            if (
                session.ended_at is None
                and isinstance(supply, Pool)
                and self._store.machine(session.machine).expired(now)
            ):
                # An owned machine leaves its pool at expiry. Its unfinished
                # attempts end as lost with it, and retry elsewhere.
                self._diagnose(
                    "machine expiry",
                    "owned machine expired; ending its session",
                    session=session.id,
                )
                try:
                    self._end_session(session)
                except (OSError, LauncherError, StorageUnavailable):
                    pass  # cleanup retained its error and remains unconfirmed
                continue
            if session.claimed_at is not None:
                try:
                    self._observe(session, now)
                except (Stale, OSError, LauncherError, StorageUnavailable) as error:
                    self._diagnose(
                        "observation",
                        "observation failed; state remains uncertain",
                        error,
                        session=session.id,
                    )
                    continue  # storage or the provider will answer on a later pass
            elif not self._kept(session) and not self._store.attempts(session=session.id):
                # A crashed admission never asked for this machine. Release its claim.
                self._store.record_refusal(session, now)
        self._store.check_lock(supply.name)
        self._plan(supply, now)
        for idle in self._idle_sessions(supply.name, now):
            if not self._kept(idle):
                try:
                    self._end_session(idle)
                except (OSError, LauncherError, StorageUnavailable):
                    continue  # cleanup retained its error and remains unconfirmed
        for session in self._store.unsettled_sessions():
            if session.supply == supply.name and session.claimed_at is None:
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

    def _plan(self, supply: Supply, now: datetime) -> None:
        """Send waiting runs to a free kept machine, a free owned machine, or a new rental."""
        off = isinstance(supply, Pool) and supply.off
        waiting = (
            [] if off else [r for r in self._store.waiting(supply.name) if not self.read(r.id).done]
        )
        for kept in self._idle_sessions(supply.name, now):
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
            self._store.check_lock(supply.name)
            waiting.remove(planned.run)
            self._hand(kept, attempt)
        while waiting:
            if isinstance(supply, Pool):
                owned = self._free_owned(supply, now)
                if owned is None:
                    return
                planned = self._next_run(waiting, Decimal(0), now)
                if planned is None:
                    return
                session, _, _ = self._session(supply, owned, Decimal(0), 0.0, now, kept=False)
                rented, rental = None, None
            else:
                offer = self._rentable(supply)
                if offer is None:
                    return
                price = offer.usd_per_hour
                planned = self._next_run(waiting, price, now)
                if planned is None:
                    return
                try:
                    self._stage(supply, [planned.run])
                except (OSError, LauncherError, StorageUnavailable) as error:
                    self._diagnose("source upload", "upload failed; nothing rented", error)
                    self._trouble[supply.name] = "source upload failed"
                    return
                now = self._clock()  # the rental's window starts after the upload
                self._store.check_lock(supply.name)
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
                life = hours * 3600 / slots + supply.setup_seconds
                if price > 0:
                    life = min(life, float(cap / slots / price * 3600))
                session, rented, rental = self._session(supply, None, price, life, now, kept=False)
            try:
                self._store.plan(
                    session, (self._attempt(planned, session, 0, now),), rented, rental
                )
            except Stale:
                return  # another process planned this run
            waiting.remove(planned.run)

    def _free_owned(
        self, pool: Pool, now: datetime, *, excluded: set[str] | None = None
    ) -> Machine | None:
        """A machine of the pool with no session or unfinished run, and not expired.

        Its cost is already committed, so it never limits placement.
        """
        busy = {s.machine for s in self._store.unsettled_sessions() if s.supply == pool.name}
        busy |= excluded or set()
        for machine in self._store.machines(pool.name):
            if (
                machine.name not in busy
                and not machine.expired(now)
                and not self._store.machine_busy(machine.name)
            ):
                self._trouble.pop(pool.name, None)
                return machine
        self._trouble[pool.name] = "no free machine"
        return None

    def _rentable(
        self, ondemand: OnDemand, *, manual: bool = False, additional: int = 0
    ) -> Offer | None:
        """What the on-demand would rent now, if its budget allows one more machine.

        None when it may rent no more, its provider has none or asks more than
        its ceiling; the reason is kept for status. A machine rented by hand is
        allowed under a zero limit.
        """
        live = sum(s.supply == ondemand.name for s in self._store.unsettled_sessions())
        if live + additional >= ondemand.max_machines and not (
            manual and ondemand.max_machines == 0
        ):
            self._trouble[ondemand.name] = "at its machine limit"
            return None
        if ondemand.storage is None:
            problem = "a rented machine needs a storage"
            if manual:
                raise LauncherError(problem)
            self._trouble[ondemand.name] = problem
            return None
        try:
            offer = self._reach(ondemand).offer()
        except Exception as trouble:  # noqa: BLE001
            # No price, no rental: the runs stay queued, and status says why.
            self._trouble[ondemand.name] = str(trouble) or type(trouble).__name__
            return None
        ceiling = ondemand.max_usd_per_hour
        if not offer.available:
            self._trouble[ondemand.name] = "provider reports no available machines"
            return None
        if ceiling is not None and offer.usd_per_hour > ceiling:
            self._trouble[ondemand.name] = "price is above its ceiling"
            return None  # dearer than this on-demand allows: wait for the price to come back
        self._trouble.pop(ondemand.name, None)
        return offer

    def _session(
        self,
        supply: Supply,
        owned: Machine | None,
        price: Decimal,
        life: float,
        now: datetime,
        *,
        kept: bool,
    ) -> tuple[Session, Machine | None, Rental | None]:
        """A working session, with a machine and a bounded window only when renting."""
        id = _id()
        rented = None
        rental = None
        if owned is None:
            assert isinstance(supply, OnDemand)  # only an on-demand rents
            template = supply.template
            life = min(life, self._longest_rental(supply))
            rented = Machine(
                name=f"{supply.name}-{id}",
                ondemand=supply.name,
                kind="rented",
                provider=template.provider,
                image=template.image
                or (STOCK_IMAGE if self._provider_service(template.provider) == "runpod" else None),
            )
            machine = rented
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
            supply=supply.name,
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
        return session, rented, rental

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
            outputs=(),
            exit_code=None,
            version=1,
            planned_at=now,
            added=added,
        )

    def _idle_sessions(self, supply: str, now: datetime) -> list[Session]:
        """Sessions of a pool or on-demand that are up and have nothing left to run."""
        return [
            r
            for r in self._store.unsettled_sessions()
            if r.supply == supply
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
        self._store.check_lock(session.supply)
        try:
            packet = self._packet(session)
        except (OSError, Mismatch, LauncherError) as error:
            self._await_relay(session, error)
            return
        try:
            self._store.claim(session, self._clock())
        except Stale:
            return  # someone else holds the right to create it
        session = self._store.session(session.id)
        packet = packet.model_copy(update={"startup_deadline": session.startup_deadline})
        service = self._service(session)

        def check() -> None:
            self._store.check_lock(session.supply)
            if any(
                self._store.run(a.run).cancel_requested_at is not None
                for a in self._store.attempts(session=session.id)
            ):
                raise Refused("startup cancelled during preparation; no machine created")

        try:
            limits = [d for d in (packet.startup_deadline, packet.deadline) if d is not None]
            seconds = (min(limits) - self._clock()).total_seconds() if limits else None
            if seconds is not None and seconds <= 0:
                raise Refused("startup or rental deadline reached before creation")
            with creation_guard(check), limit_time(seconds):
                receipt = service.create(
                    packet.model_copy(update={"kept": True}) if idle else packet
                )
        except Refused as error:
            self._diagnose(
                "machine create", "provider refused; no machine acquired", error, session=session.id
            )
            self._store.record_refusal(session, now)
            for attempt in self._store.attempts(session=session.id):
                self._store.fail_before_start(attempt, "capacity_refused", now)
            return
        except Exception as error:  # noqa: BLE001
            self._diagnose(
                "machine create",
                "create reply unknown; reconciling existing request",
                error,
                session=session.id,
            )
            # No answer is not a refusal: a rental may exist. It is looked up by its
            # identity on later ticks and is never asked for a second time.
            return
        try:
            self._store.record_receipt(session, receipt)
            self._diagnose("machine create", "machine confirmed", session=session.id)
            self._creation_facts(self._store.session(session.id), now)
        except Stale:
            pass  # another process already found the rental by its identity

    def _creation_facts(self, session: Session, now: datetime) -> Session:
        service = self._service(session)
        receipt = service.allocation(session.id)
        if receipt is not None and receipt.usd_per_hour != self._usd_per_hour(session):
            self._store.record_price(session, receipt.usd_per_hour)
            session = self._store.session(session.id)
        hardware = None if receipt is None else receipt.hardware
        if hardware is not None and hardware != session.hardware:
            self._store.record_hardware(session, hardware)
            session = self._store.session(session.id)
        if session.started_at is None and service.started(session.id):
            self._store.record_started(session, now)
            session = self._store.session(session.id)
        return session

    def _relay(self, attempt: RunAttempt, run: RunRecord) -> None:
        """Hand an attempt what it needs of runs' folders from the machines that last ran them.

        A later attempt gets its own run's records, declaration and latest
        checkpoint file, so ordinals continue; an input that is a run's file
        (`Input.run`) gets just that file. Only between machines without shared
        storage (`Execution.held`). Raises while a holding machine cannot be
        reached, and the attempt waits: nothing is silently left behind.
        """
        if attempt.id in self._relayed:
            return
        carried: list[tuple[str, int | None, str | None]] = [
            (given.run, None, given.key) for given in run.spec.inputs if given.run is not None
        ]
        if attempt.number > 1:
            carried.insert(0, (attempt.run, attempt.number, None))
        if carried:
            session = self._store.session(attempt.session)
            for source, before, key in carried:
                self._carry(source, session, before=before, key=key)
        self._relayed.add(attempt.id)

    def _carry(
        self, run: str, session: Session, *, before: int | None = None, key: str | None = None
    ) -> None:
        """Copy a run's folder, or one `key` of it, from its latest started attempt's machine."""
        holder = self._holder(run, before)
        if holder is not None and holder.machine != session.machine:
            files = self._service(holder).held(run, key)
            if files is not None:
                self._service(session).hold(run, files)

    def _holder(self, run: str, before: int | None = None) -> Session | None:
        """The session of the run's latest started attempt (numbered below `before`)."""
        prior = [
            a
            for a in self._store.attempts(run=run)
            if a.started_at is not None and (before is None or a.number < before)
        ]
        if not prior:
            return None
        return self._store.session(max(prior, key=lambda a: a.number).session)

    def held(self, run: str) -> dict[str, bytes] | None:
        """The run's folder as the machine that last ran it holds it (`Execution.held`).

        None where storage holds the run, or it never started. A wait: it
        reaches that machine, and raises while the machine cannot be reached.
        """
        holder = self._holder(run)
        return None if holder is None else self._service(holder).held(run)

    def _work(self, attempt: RunAttempt) -> Work:
        run = self._store.run(attempt.run)
        self._relay(attempt, run)
        spec = run.spec
        return Work(
            attempt=attempt.id,
            command=spec.command,
            runtime=spec.runtime,
            setup=spec.setup,
            cleanup=spec.cleanup,
            cleanup_seconds=spec.cleanup_seconds,
            stop_seconds=spec.stop_seconds,
            limit_seconds=attempt.limit_seconds,
            limit_kind=attempt.limit_kind,
            seed=run.seed,
            until=spec.until,
            config=spec.config,
            repo=spec.repo,
            commit=None if spec.repo is None else spec.revision,
            run=run.id,
            resumes=attempt.number > 1,
            job=run.job,
            job_name=spec.name or None,
            cohort=run.cohort,
            inputs=spec.inputs,
            environment=spec.environment,
        )

    def _hand(self, session: Session, attempt: RunAttempt) -> None:
        """Add placed work to a kept session; while its run's folder cannot be relayed, it waits.

        A placed attempt not yet in the session's journal is handed again on
        each pass, so the wait ends when the holding machine answers.
        """
        try:
            work = self._work(attempt)
        except (OSError, Mismatch, LauncherError) as error:
            self._await_relay(session, error, attempt=attempt.id)
            return
        self._service(session).add(session.id, work)

    def _await_relay(
        self, session: Session, error: Exception, *, attempt: str | None = None
    ) -> None:
        """Note why work for `session` waits: the machine holding a run's folder did not answer."""
        self._diagnose(
            "run folder relay",
            "waiting for its host",
            error,
            session=None if attempt else session.id,
            attempt=attempt,
        )
        self._trouble[session.supply] = "waiting for the machine holding a run's folder"

    def _packet(self, session: Session) -> Packet:
        """What one session is sent: its deadline, its work and where its records go."""
        rental = self._store.rental_for(session)
        attempts = self._store.attempts(session=session.id)
        first = [a for a in attempts if not a.added]
        work = tuple(self._work(attempt) for attempt in first)
        supply = self._store.supply(session.supply)
        return Packet(
            delivery_seconds=supply.delivery_seconds,
            log_seconds=supply.log_seconds,
            session=session.id,
            deadline=None if rental is None else rental.deadline,
            work=work,
            kept=rental is not None or any(a.added for a in attempts),
            cap_usd=Decimal(0) if rental is None else rental.cap_usd,
            autorun=session.autorun,
        )

    def _read_journal(self, session: Session, service: Execution) -> Observation:
        """Retain both collection traffic and worker events, including final collection."""
        with (
            observe(self._store.retain_transfer_json),
            attribute(session=session.id, machine=session.machine, purpose="control"),
        ):
            seen = service.observe(session.id)
        for event in seen.transfers:
            self._store.retain_transfer(event.model_copy(update={"machine": session.machine}))
        return seen

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
                self._diagnose(
                    "reconciliation",
                    "existing machine confirmed; no duplicate create",
                    session=session.id,
                )
            elif deadline is not None and now >= deadline:
                for attempt in attempts:
                    if attempt.outcome is None:
                        self._store.finish(attempt, now, "lost", cause="create_unconfirmed")
            return
        if not attempts and (rental is None or not rental.kept):
            self._end_session(session)
            return
        session = self._creation_facts(session, now)
        if session.started_at is None:
            limits = [d for d in (deadline, session.startup_deadline) if d is not None]
            if limits and now >= min(limits):
                self._diagnose(
                    "worker startup",
                    f"{'startup_seconds' if min(limits) == session.startup_deadline else 'rental'} "
                    f"deadline {min(limits).isoformat()} reached before first receipt; "
                    "cleanup requested",
                    session=session.id,
                )
                self._end_session(session)
                return
        seen = self._read_journal(session, service)
        if not seen.alive:
            # Progress and machine state are separate reads. Completion may land
            # between them; settle only from the final journal after shutdown.
            self._end_session(session)
            return
        progress = {entry.attempt: entry for entry in seen.progress}
        for attempt in attempts:
            if attempt.outcome is None:
                if seen.alive and attempt.added and attempt.id not in progress:
                    self._store.check_lock(session.supply)
                    self._hand(session, attempt)
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
        self._store.check_lock(session.supply)
        service = self._service(session)
        gone = session.ended_at
        if gone is None:
            try:
                gone = service.end(session.id)
            except Exception as error:
                self._diagnose("cleanup", "machine deletion unconfirmed", error, session=session.id)
                self._collect_logs((session,), self._store.attempts(session=session.id), 2)
                raise
            if gone is None:
                self._diagnose("cleanup", "machine deletion unconfirmed", session=session.id)
                return
            self._store.confirm_ended(session, gone)
            self._diagnose("cleanup", "machine deletion confirmed", session=session.id)
        # Nothing more will be written. Take what the journal holds now, and
        # count any attempt still open as lost with the machine.
        self._collect_logs((session,), self._store.attempts(session=session.id), 2)
        final = self._read_journal(session, service)
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
                outputs=seen.outputs,
                exit_code=seen.exit_code,
            )
            self._diagnose(
                "execution",
                f"attempt finished: {outcome}; exit status {seen.exit_code if seen.exit_code is not None else 'not received'}",
                session=session.id,
                attempt=attempt.id,
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
                outputs=seen.outputs if seen else attempt.outputs,
            )
            self._diagnose(
                "execution",
                f"attempt lost: {cause}; final exit status not received",
                session=session.id,
                attempt=attempt.id,
            )
        else:
            if seen is not None:
                self._store.record_progress(attempt, seen.clocks)
            if run.cancel_requested_at is not None or self._remaining(run, now)[1] <= 0:
                self._service(session).stop(session.id, attempt.id)

    # Reading the records

    def _service(self, session: Session) -> Execution:
        """Reach this session through its owned machine or its on-demand's provider."""
        machine = self._store.machine(session.machine)
        if machine.kind != "rented":
            return self._reach(machine)
        ondemand = self._store.ondemand(session.supply)
        if machine.image is not None:  # the image it booted, whatever the template says now
            template = ondemand.template.model_copy(update={"image": machine.image})
            ondemand = ondemand.model_copy(update={"template": template})
        return self._reach(ondemand)

    def _provider_service(self, provider: str) -> str:
        """The service a provider uses. A provider with no record is its own service's name."""
        known = {each.name: each.kind for each in self._store.providers()}
        service = known.get(provider, provider)
        if service not in self._services:
            raise LauncherError(f"no provider named {provider!r}")
        return service

    def _reach(self, held: Machine | OnDemand) -> Execution:
        key: tuple[str, str, str | None]
        if isinstance(held, Machine):
            key = ("machine", held.name, None)
        else:
            image = held.template.image
            key = ("ondemand", held.name, image.resolved if image else None)
        if key not in self._reached:
            service = (
                held.kind
                if isinstance(held, Machine)
                else self._provider_service(held.template.provider)
            )
            self._reached[key] = self._services[service](held)
        return self._reached[key]

    def _longest_rental(self, ondemand: OnDemand) -> float:
        """Seconds a rental from this on-demand may last: as long as its storage's grants."""
        name = ondemand.storage
        if name is None:
            return float("inf")  # nothing is rented without a storage
        if name not in self._grants:
            self._grants[name] = float(longest_grant(self._store.storage(name)))
        return self._grants[name]

    def _validate_job(self, job: Job, target: Target) -> None:
        if target.pool is not None and self._store.pool(target.pool).off:
            raise LauncherError(f"pool {target.pool!r} is turned off")
        if target.rent is not None:
            ondemand = self._store.ondemand(target.rent)  # raises if no such on-demand
            longest = self._longest_rental(ondemand)
            if job.max_hours * 3600 + ondemand.setup_seconds > longest:
                # One rental holds a run from setup to its last hour; none outlasts its grant.
                raise LauncherError(
                    f"a job on on-demand {target.rent!r} may ask for at most "
                    f"{(longest - ondemand.setup_seconds) / 3600:g} hours: its storage's "
                    f"grants last {longest / 3600:g} hours, including "
                    f"{ondemand.setup_seconds} setup seconds"
                )
        if not job.command.strip() or job.max_hours <= 0:
            raise LauncherError("a job needs a command and hours")
        if job.max_usd is None and target.rent is not None:
            raise LauncherError(
                f"on-demand {target.rent!r} can spend money, so the job needs max_usd"
            )
        if job.max_usd is not None and job.max_usd <= 0:
            raise LauncherError("a job's max_usd must be more than zero")
        if job.max_attempts < 1:
            raise LauncherError("a job needs at least one attempt")
        if job.until is not None:
            parse_target(job.until)

    def _freeze(self, run: Run, target: Target, cohort: str | None) -> RunRecord:
        job = run.job
        self._validate_job(job, target)
        run = source.freeze(run)
        job = run.job
        if job.until is not None:
            clock, position = parse_target(job.until)
            job = job.model_copy(update={"until": f"{clock}={number(position)}"})
        frozen = json.dumps(job_identity(job), sort_keys=True).encode()
        assert run.id is not None  # source.freeze assigns the submitting client's identity
        return RunRecord(
            id=run.id,
            source=run.source,
            job=hashlib.sha256(frozen).hexdigest()[:12],
            spec=job,
            seed=run.seed,
            pool=target.pool,
            rent=target.rent,
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
