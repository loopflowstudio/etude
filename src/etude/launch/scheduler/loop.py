"""The scheduler: owner of one launcher database, and the one loop that moves work forward.

It queues and admits runs, matches waiting runs to machines and enforces limits.
A run may use a pool's machines you own, rent from an on-demand, or both; a
free owned machine is always used before anything is rented. Each pool and
on-demand (a supply) has its own placement lock, and `tick` visits every pool
before any on-demand.

Four rules hold whatever fails:

1. A rental's identity is stored and claimed before the provider is asked for it.
   Only the claimer asks, once. A lost reply is looked up, never asked again.
2. A rental carries its own deadline and independent best-effort shutdown.
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
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import chain
from pathlib import Path
from typing import get_args
from uuid import uuid4

from etude.carriage import RecordMismatch
from etude.deadline import limit_time, remaining
from etude.errors import LauncherError, explain, sanitize
from etude.identity import resume_conflicts, run_address
from etude.launch import source
from etude.launch.commands import ProviderKind
from etude.launch.database import Database
from etude.launch.errors import LaunchError, ProviderUnavailable
from etude.launch.executor import Execution
from etude.launch.logging import bounded
from etude.launch.provisioner.machines import Refused, Safeguards, creation_guard
from etude.launch.records import (
    CAPACITY_REFUSED,
    RETRYABLE,
    STOCK_IMAGE,
    TRY_EXEMPT,
    Availability,
    Cause,
    Diagnostic,
    Hardware,
    Image,
    Launch,
    LaunchRequest,
    Limit,
    LogProducer,
    LogSnapshot,
    LogStream,
    Machine,
    MachineStatus,
    Offer,
    OnDemand,
    Pool,
    Provider,
    Quote,
    QuotedMachine,
    Rental,
    RentalCost,
    RunAttempt,
    Session,
    Stale,
    Standing,
    Status,
    Storage,
    Supply,
    Target,
    refusal,
)
from etude.launch.scheduler.costs import (
    Budget,
    Place,
    affordable_seconds,
    has_unbilled_time,
    price_rental,
    rental_cost,
    run_share,
)
from etude.launch.scheduler.placement import Placement, PlannedRun, funded_group
from etude.launch.schemas import (
    Observation,
    Packet,
    Progress,
    Work,
)
from etude.launch.storage.download import StoredContentMismatch
from etude.launch.storage.s3 import longest_grant
from etude.launch.storage.transport import attribute, observe
from etude.launch.usage import Usage
from etude.records import RunRecord, RunSubmission
from etude.spec import Job, Record, RunRequest, expand_runs, job_id
from etude.storage_timeout import StorageUnavailable

Clock = Callable[[], datetime]
# How the scheduler reaches a machine you own, or the provider behind an on-demand.
Reach = Callable[[Machine | OnDemand], Execution]
# What a run folder's relay raises while its holder cannot give it; the work waits.
# An input's file that is gone or does not match its record ends the attempt
# instead, in `_relay`; a retry's restore set waits on either.
_RELAY_WAITS = (OSError, RecordMismatch, LauncherError, StorageUnavailable)
# How often a pass may fetch a running job's log. Reading it over SSH sends it all.
_JOB_LOG_SECONDS = 30.0
# A log read without a supply's `log_seconds`: one stalled bucket must not hold
# the pass that ends every other machine at its deadline.
_LOG_SECONDS = 30.0


def _limit_usd(job: Job) -> Decimal:
    """A job's dollar limit. None is allowed only where nothing can be spent."""
    return Decimal("Infinity") if job.max_usd is None else job.max_usd


def _remaining_budget(
    job: Job, paid: Sequence[RunAttempt], spent: Decimal, now: datetime
) -> Budget:
    seconds = sum(
        max(0, ((a.finished_at or now) - a.started_at).total_seconds())
        for a in paid
        if a.started_at is not None
    )
    return Budget(
        seconds=max(0, job.require_hours() * 3600 - seconds),
        usd=max(Decimal(0), _limit_usd(job) - spent),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id() -> str:
    return uuid4().hex[:12]


def _refused(session: Session) -> str:
    return (
        f"provider refused: {session.refusal}"
        if session.refusal is not None
        else "provider refused capacity"
    )


def _paid(attempts: Sequence[RunAttempt]) -> list[RunAttempt]:
    """Attempts that held a machine: a definite refusal acquired none."""
    return [attempt for attempt in attempts if attempt.cause != CAPACITY_REFUSED]


def _tries(attempts: Sequence[RunAttempt]) -> int:
    return sum(attempt.cause not in TRY_EXEMPT for attempt in attempts)


def _new_session(id: str, supply: Supply, machine: Machine, now: datetime) -> Session:
    """A session not yet claimed; only a rented machine's starts by itself."""
    return Session(
        id=id,
        supply=supply.name,
        machine=machine.name,
        planned_at=now,
        claimed_at=None,
        provider_id=None,
        started_at=None,
        refused_at=None,
        ended_at=None,
        version=1,
        autorun=machine.kind == "rented",
    )


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
        # By attempt: how much of a running job's log was stored when last fetched, and when.
        self._job_logs: dict[str, tuple[int, datetime]] = {}
        self._grants: dict[str, float] = {}  # by storage: a storage never changes once added
        self._relayed: set[str] = set()  # attempts handed their run's folder by this process
        self._clock = clock
        self._sleep = sleep

    def _usd_per_hour(self, session: Session) -> Decimal:
        rental = self._store.rental_for(session)
        return Decimal(0) if rental is None else rental.usd_per_hour

    def _kept_rental(self, session: Session) -> bool:
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

    def set_ondemand_image(self, name: str, image: Image) -> None:
        """Change the image an on-demand's next machines boot. Running ones are untouched."""
        ondemand = self._store.ondemand(name)
        template = ondemand.template.model_copy(update={"image": image})
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
                life = min(life, self._affordable_seconds(supply, price, max_usd))
            if life < 1:
                raise LauncherError("a rental needs hours, and dollars for at least a second")
            longest = self._longest_rental(supply)
            if life > longest:
                raise LauncherError(
                    f"on-demand {ondemand!r} keeps records in a storage whose grants last at "
                    f"most {longest / 3600:g} hours"
                )
            session, rented, rental = self._make_rental(supply, price, life, now, kept=True)
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
            if held.ended_at is None and held.refused_at is None:
                self._terminate(held)
            return self._store.machine(machine)

    def _terminate(self, held: Session) -> None:
        if held.provider_id is None:
            # Its create reply was lost: delete what the provider holds under its
            # identity, or record that it holds nothing.
            provider_id = self._service(held).find(held.id)
            if provider_id is None:
                self._store.record_refusal(held, self._clock(), None)
                return
            self._store.record_provider_id(held, provider_id)
            held = self._store.session(held.id)
        self._end_session(held)
        if self._store.session(held.id).ended_at is None:
            raise LauncherError(f"machine {held.machine!r} did not confirm it ended")

    def session(self, id: str) -> Session:
        """Internal journal session named in a run attempt."""
        return self._store.session(id)

    def rentals(self) -> tuple[RentalCost, ...]:
        """Paid windows and their cost from retained bills, including terminated machines."""
        now = self._clock()
        return tuple(
            price_rental(rental, self._store.machine(rental.machine), now)
            for rental in self._store.rentals()
        )

    def machine_statuses(self, *, ended: bool = False) -> tuple[MachineStatus, ...]:
        """Machines in CLI order, with their current allocations. All reads are local."""
        with self._store.reading():
            now = self._clock()
            supplies: tuple[Supply, ...] = (*self.pools(), *self.ondemands())
            machines = self._store.machines(held_only=not ended)
            windows = {r.machine: r for r in self._store.rentals(held_only=not ended)}
            allocated = self._store.allocated_runs()
        by_supply: dict[str, list[Machine]] = {}
        for machine in machines:
            by_supply.setdefault(machine.supply, []).append(machine)
        statuses = []
        for supply in supplies:
            for machine in by_supply.get(supply.name, []):
                cost = None
                if machine.kind == "rented" and machine.refused_at is None:
                    rental = windows.get(machine.name)
                    if rental is None:
                        continue
                    cost = price_rental(rental, machine, now)
                statuses.append(
                    MachineStatus(
                        machine=machine,
                        observed_at=now,
                        pool_off=isinstance(supply, Pool) and supply.off,
                        spot=isinstance(supply, OnDemand) and supply.template.spot,
                        cost=cost,
                        runs=tuple(allocated.get(machine.name, ())),
                    )
                )
        return tuple(statuses)

    def now(self) -> datetime:
        """The time by the clock this scheduler reads every record against."""
        return self._clock()

    def turn_pool(self, name: str, *, off: bool) -> None:
        """Stop or resume sending new runs to a pool. Runs already there are untouched."""
        self._store.turn_pool(name, off=off)

    def pools(self) -> tuple[Pool, ...]:
        return self._store.pools()

    def safeguards(self, ondemand: str) -> tuple[str, ...]:
        """What ends an on-demand's machines if the launcher stops watching."""
        declared = self._safeguards(self._store.ondemand(ondemand))
        return tuple(
            name
            for enabled, name in (
                (declared.lifetime, "provider lifetime"),
                (declared.machine_timer, "machine timer"),
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
        in_use = self._in_use(ondemand)
        return Availability(
            ondemand=rent,
            offer=offer,
            spot=ondemand.template.spot,
            max_usd_per_hour=ondemand.max_usd_per_hour,
            in_use=in_use,
            max_machines=ondemand.max_machines,
            observed_at=self._clock(),
            refusal=refusal(ondemand, in_use, offer),
        )

    def queue(
        self,
        runs: RunRequest | Job | Sequence[RunRequest | Job],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> tuple[str, ...]:
        """Record runs for the next free slots, without requiring capacity now."""
        given = [runs] if isinstance(runs, (Job, RunRequest)) else runs
        return self._submit(expand_runs(given), self._target(pool, rent), queued=True)

    def launch(
        self,
        runs: RunRequest | Job | Sequence[RunRequest | Job],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> tuple[str, ...]:
        """Place the runs that fit now and queue the rest; refuse if none fit."""
        given = [runs] if isinstance(runs, (Job, RunRequest)) else runs
        return self._submit(expand_runs(given), self._target(pool, rent), queued=False)

    def quote(
        self,
        runs: Sequence[RunRequest],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> Quote:
        """The machines a launch would take now and planned rental costs. Rents nothing.

        It asks the on-demand's provider for its price, so it waits on the provider.
        Without capacity it raises, as the launch would. Costs assume the quoted
        rate and timely deletion, and exclude external storage. Provider delays
        can exceed the admitted rental budget.
        """
        target = self._target(pool, rent)
        records = []
        labels = {}
        requested = self._address_requests(runs, target)
        for index, (run, addressed) in enumerate(zip(runs, requested, strict=True)):
            prior = self._resumed(addressed) if run.resume else None
            assert addressed.id is not None
            identity = addressed.id
            labels[identity] = (
                run.id
                or "@".join(
                    ([run.job.name] if run.job.name else [])
                    + ([str(run.seed)] if run.seed is not None else [])
                )
                or f"run{index + 1}"
            )
            records.append(
                RunRecord(
                    id=identity,
                    experiment=run.job.experiment or "",
                    job="",
                    submission=RunSubmission(
                        spec=run.job,
                        pool=target.pool,
                        rent=target.rent,
                        resume_after=prior.launch.resume_after if prior else 0,
                        created_at=self._clock(),
                    ),
                    seed=run.seed,
                )
            )
        quoted = []
        waiting_usd = Decimal(0)
        for name in (target.pool, target.rent):
            if name is not None:
                with self._store.lock(name, self._clock) as acquired:
                    if acquired:
                        self._confirm_ends(self._store.supply(name))
        for placement in self._capacity(records):
            supply = self._store.supply(placement.supply)
            waiting_usd += placement.waiting_usd
            for planned in placement.runs:
                record = planned.run
                quoted.append(
                    QuotedMachine(
                        run=labels[record.id],
                        machine=placement.machine.name if placement.machine else None,
                        hardware=placement.hardware,
                        usd_per_hour=placement.price,
                        max_usd=planned.usd,
                        slots=supply.slots,
                        spot=isinstance(supply, OnDemand) and supply.template.spot,
                        alongside=tuple(
                            labels[p.run.id] for p in placement.runs if p.run.id != record.id
                        ),
                    )
                )
        caps = sum((_limit_usd(r.launch.spec) for r in records), Decimal(0))
        return Quote(
            machines=tuple(quoted),
            waiting_usd=waiting_usd,
            limit_usd=None if target.rent is None else caps,
        )

    def _submit(
        self, runs: Sequence[RunRequest], target: Target, *, queued: bool
    ) -> tuple[str, ...]:
        launch = self.prepare_launch(runs, target.pool, rent=target.rent)
        requested = tuple(
            run.model_copy(update={"id": request.run})
            for run, request in zip(runs, launch.requests, strict=True)
        )
        return self.submit_launch(launch.id, requested, target, queued=queued)

    def _address_requests(
        self, runs: Sequence[RunRequest], target: Target
    ) -> tuple[RunRequest, ...]:
        if not runs:
            raise LauncherError("a launch needs at least one run")
        for run in runs:
            self._validate_job(run.job, target)
            if run.resume and run.id is None:
                raise LauncherError("resume needs the existing run id")
        requested = tuple(
            run.model_copy(update={"id": run_address(run.id or _id(), run.job.experiment)})
            for run in runs
        )
        if len({run.id for run in requested}) != len(requested):
            raise LauncherError("a launch cannot request the same run ID twice")
        return requested

    def prepare_launch(
        self, runs: Sequence[RunRequest], pool: str | None = None, *, rent: str | None = None
    ) -> Launch:
        """Retain valid intent before the submitting client publishes source."""
        target = self._target(pool, rent)
        requested = self._address_requests(runs, target)
        launch = Launch(
            id=_id(),
            created_at=self._clock(),
            requests=tuple(
                LaunchRequest(
                    run=str(run.id),
                    fingerprint=hashlib.sha256(run.model_dump_json().encode()).hexdigest(),
                    requested_revision=sanitize(run.job.revision) if run.job.revision else None,
                    # Refuses a resume before the client publishes source.
                    resume_version=self._resumed(run).launch.version if run.resume else None,
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
        runs: Sequence[RunRequest],
        target: Target,
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
            records = tuple(self._freeze(run, selected) for run in requested)
            launch = launch.model_copy(
                update={
                    "requests": tuple(
                        request.model_copy(
                            update={"resolved_revision": record.launch.spec.revision}
                        )
                        for request, record in zip(launch.requests, records, strict=True)
                    )
                }
            )
            for request, record in zip(launch.requests, records, strict=True):
                if (
                    request.resume_version is not None
                    and record.launch.version != request.resume_version
                ):
                    raise Stale(f"run {record.id!r} changed during source preparation")
            self._store.save_launch(launch)
            operation = "admission"
            if queued:
                self._store.launch(records, launch=launch.id)
            else:
                self._admit(records, launch.id)
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
        placed: Sequence[RunRecord],
        records: Sequence[RunRecord],
        price: Decimal,
    ) -> float:
        """Seconds a first-wave rental is held: its share of the hours, within the dollars.

        The target covers the longest placed run; the available dollars may shorten it.
        """
        machines = max(
            1,
            min(
                ondemand.max_machines - self._in_use(ondemand),
                (len(records) + ondemand.slots - 1) // ondemand.slots,
            ),
        )
        now = self._clock()
        budgets = {r.id: self._admission_budget(r, now) for r in records}
        hours = max(
            sum(budgets[r.id].seconds / 3600 for r in records) / (machines * ondemand.slots),
            max(budgets[r.id].seconds / 3600 for r in placed),
        )
        cap = sum((budgets[r.id].usd for r in records), Decimal(0)) / machines
        return min(
            hours * 3600 + ondemand.setup_seconds,
            self._affordable_seconds(ondemand, price, cap),
        )

    def _stage(self, ondemand: OnDemand, runs: Sequence[RunRecord]) -> None:
        """Put the runs' source archives in the on-demand's storage before renting for them.

        A rental's window starts when it is made, so no paid machine waits on an upload.
        """
        service = self._reach(ondemand)
        for repo, commit in sorted(
            {
                (r.launch.spec.repo, r.launch.spec.revision)
                for r in runs
                if r.launch.spec.repo and r.launch.spec.revision
            }
        ):
            service.stage(repo, commit)

    def _admit(self, records: Sequence[RunRecord], launch: str) -> None:
        target = Target(pool=records[0].launch.pool, rent=records[0].launch.rent)
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
            for name in names:
                self._confirm_ends(self._store.supply(name))
            placements = self._capacity(records)
            made: list[Session] = []
            attempts: list[RunAttempt] = []
            try:
                for placement in placements:
                    existing = placement.session
                    machine, price = placement.machine, placement.price
                    self._check_locks(names)
                    now = self._clock()
                    if existing is None:
                        supply = self._store.supply(placement.supply)
                        rented: Machine | None = None
                        rental: Rental | None = None
                        if machine is None:
                            assert isinstance(supply, OnDemand)  # only an on-demand rents
                            assert placement.seconds is not None
                            session, rented, rental = self._make_rental(
                                supply, price, placement.seconds, now, kept=False
                            )
                        else:
                            session = _new_session(_id(), supply, machine, now)
                        self._store.plan(session, (), rented, rental, launch=launch)
                        made.append(session)
                        if rented is not None:
                            # Boot an empty inbox. No executable work leaves before
                            # the entire first wave has confirmed capacity.
                            self._create(session, now, idle=True)
                            session = self._store.session(session.id)
                            if session.provider_id is None:
                                if attempts:
                                    break  # keep confirmed placements; later runs stay queued
                                reason = "create reply unknown"
                                if session.refused_at is not None:
                                    reason = _refused(session)
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
                    held = len(self._store.attempts(session=session.id))
                    for index, planned in enumerate(placement.runs):
                        attempts.append(
                            self._make_attempt(
                                planned,
                                session,
                                held + index,
                                now,
                                added=session.provider_id is not None,
                            )
                        )
                self._check_locks(names)
                if any(
                    not self._live(self._store.session(a.session), self._clock()) for a in attempts
                ):
                    raise LauncherError("capacity expired during launch; etude queue would wait")
                # Runs and claims become visible together, under the same locks.
                self._store.launch(records, attempts, launch=launch)
            except BaseException:
                for session in made:
                    try:
                        self._check_locks(names)
                        session = self._store.session(session.id)
                        if session.provider_id is not None:
                            self._end_session(session)
                        elif session.claimed_at is None:
                            self._store.record_refusal(session, self._clock(), None)
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
    ) -> list[Placement]:
        """Choose the first wave, checking all limits before spending.

        A chosen machine's free slots fill before another is taken. Machines
        with free slots come first, then the pool's free machines, then rentals
        from the on-demand, up to its remaining machine capacity.
        Runs left over wait in the queue. Nothing is claimed here.
        Both quote and admission pass records for one pool/on-demand target.
        """
        placements: list[Placement] = []
        target = Target(pool=records[0].launch.pool, rent=records[0].launch.rent)
        pool = None if target.pool is None else self._store.pool(target.pool)
        ondemand = None if target.rent is None else self._store.ondemand(target.rent)
        label = " or ".join(name for name in (target.pool, target.rent) if name is not None)
        now = self._clock()
        pending = {r.id: r for r in records}
        budgets = {r.id: self._admission_budget(r, now, committed=True) for r in records}
        reason = "no free machine"
        underfunded = False
        if pool is not None and pool.off:
            raise LauncherError(f"pool {pool.name!r} is turned off")

        def place(
            supply: Supply,
            free: int,
            machine: Machine | None,
            session: Session | None,
            price: Decimal,
            hardware: Hardware | None = None,
            rental: Rental | None = None,
        ) -> bool:
            nonlocal reason, underfunded
            available = budgets
            peers: Collection[Place] = ()
            if session and rental:
                peers = self._places(session, rental, now, project=True).values()
                available = {
                    r.id: self._remaining(r, now, committed=True, joining=session.id)
                    if r.launch.resume_after
                    else budgets[r.id]
                    for r in pending.values()
                }
            new = supply if machine is None and isinstance(supply, OnDemand) else None
            setup = new.setup_seconds if new else 0
            left = (rental.deadline - now).total_seconds() if rental else float("inf")
            eligible = (r for r in pending.values() if 1 <= available[r.id].seconds <= left)
            first = next(eligible, None)
            if first is None:
                reason = "rented machine has too little time remaining"
                return False
            planned = funded_group(
                chain([first], eligible),
                free,
                available,
                price,
                now,
                peers,
                setup_seconds=setup,
                reservation_hours=self._safeguards(new).reservation_hours if new else 0,
            )
            if not planned:
                underfunded = True
                return False
            placements.append(
                Placement(
                    runs=planned,
                    seconds=setup + max(p.seconds for p in planned) if new else None,
                    supply=supply.name,
                    session=session,
                    machine=machine,
                    price=price,
                    hardware=hardware,
                )
            )
            for p in planned:
                del pending[p.run.id]
            return True

        for supply in (pool, ondemand):
            if supply is None:
                continue
            for session, free in self._open_sessions(supply, now):
                if not pending:
                    break
                rental = self._store.rental_for(session)
                place(
                    supply,
                    free,
                    self._store.machine(session.machine),
                    session,
                    rental.usd_per_hour if rental else Decimal(0),
                    rental=rental,
                )
        if pool is not None:
            used: set[str] = set()
            while pending:
                owned = self._free_owned(pool, now, excluded=used)
                if owned is None:
                    reason = self._trouble.get(pool.name, reason)
                    break
                used.add(owned.name)
                place(pool, pool.slots, owned, None, Decimal(0))
        if ondemand is not None and pending:
            first = len(placements)
            requested_machines = (len(pending) + ondemand.slots - 1) // ondemand.slots
            work = sum(budgets[key].seconds for key in pending)
            while pending:
                offer = self._rentable(ondemand, additional=len(placements) - first)
                if offer is None:
                    reason = self._trouble.get(ondemand.name, reason)
                    break
                if not place(
                    ondemand, ondemand.slots, None, None, offer.usd_per_hour, offer.hardware
                ):
                    break
            if pending and len(placements) > first:
                # Only runs still waiting after the wave can fund extra machine time.
                machines = max(
                    len(placements) - first,
                    min(requested_machines, ondemand.max_machines - self._in_use(ondemand)),
                )
                waiting_cap = sum((budgets[key].usd for key in pending), Decimal(0)) / machines
                target_seconds = min(
                    self._longest_rental(ondemand),
                    ondemand.setup_seconds + work / (machines * ondemand.slots),
                )
                increment = self._safeguards(ondemand).reservation_hours
                for index in range(first, len(placements)):
                    placements[index] = placements[index].extend(
                        target_seconds, waiting_cap, increment
                    )
        if not placements:
            if underfunded:
                reason = "job dollars do not cover the hours"
            raise LauncherError(f"cannot launch on {label}: {reason}; etude queue would wait")
        return placements

    def status(self, run: str) -> Status:
        """Look once at everything, then say where this run stands."""
        self.tick()
        return self.read(run)

    def standing(
        self, experiment: str | None = None, *, job: str | None = None
    ) -> tuple[Standing, ...]:
        """Read all runs labelled with this experiment, including completed runs."""
        return tuple(
            Standing(record=r, status=self.read(r.id))
            for r in self._store.runs(experiment=experiment, job=job)
        )

    def runs(
        self,
        *,
        done: bool = False,
        job: str | None = None,
        experiment: str | None = None,
    ) -> tuple[RunRecord, ...]:
        """Runs oldest first, filtered without advancing execution. `done` includes history."""
        return tuple(
            r
            for r in self._store.runs(experiment=experiment, job=job)
            if done or not self.read(r.id).done
        )

    def wait(self, *runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
        """Work until the selected runs and their requested machine shutdowns settle.

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
            ending = self._store.finishing_sessions(runs)
            if not unfinished and not ending:
                return statuses
            if give_up is not None and self._clock() >= give_up:
                if ending:
                    raise TimeoutError(
                        "machines still finishing: " + ", ".join(s.machine for s in ending)
                    )
                raise TimeoutError("runs still running: " + ", ".join(runs))
            self._sleep(every)

    def cancel(self, run: str) -> Status:
        record = self._store.run(run)
        if record.launch.cancel_requested_at is None:
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
        self,
        *,
        run: str | None = None,
        machine: str | None = None,
        refresh: bool = False,
        timeout: float | None = None,
    ) -> LogSnapshot:
        """All stages and attempts with explicit gaps, as retained in the database.

        `refresh` first collects new evidence from machines and buckets, within
        `timeout`; without it, nothing outside the database is touched.
        """
        if timeout is not None and not refresh:
            raise ValueError("a log timeout bounds a refresh")
        selection = self._log_selection(run, machine)
        streams = (
            self._collect_logs(selection.sessions, selection.attempts, timeout)
            if refresh
            else self._retained_logs(selection.sessions, selection.attempts)
        )
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
        with limit_time(timeout):
            return tuple(
                self._collect_log(session, attempt, producer)
                for session, attempt, producer in self._producers(sessions, attempts)
            )

    def _retained_logs(
        self, sessions: Sequence[Session], attempts: Sequence[RunAttempt]
    ) -> tuple[LogStream, ...]:
        return tuple(
            self._store.log(f"{session.id}/{attempt or producer}")
            or self._unreceived(session, attempt, producer)
            for session, attempt, producer in self._producers(sessions, attempts)
        )

    @staticmethod
    def _producers(
        sessions: Sequence[Session], attempts: Sequence[RunAttempt]
    ) -> Iterator[tuple[Session, str | None, LogProducer]]:
        """Each started session's bootstrap and worker, then its attempts' jobs."""
        for session in sessions:
            if session.refused_at is not None:
                continue
            yield session, None, "bootstrap"
            yield session, None, "worker"
            for attempt in attempts:
                if attempt.session == session.id:
                    yield session, attempt.id, "job"

    @staticmethod
    def _unreceived(session: Session, attempt: str | None, producer: LogProducer) -> LogStream:
        return LogStream(
            id=f"{session.id}/{attempt or producer}",
            producer=producer,
            session=session.id,
            attempt=attempt,
            machine=session.machine,
        )

    def _collect_log(
        self, session: Session, attempt: str | None, producer: LogProducer
    ) -> LogStream:
        """Refresh one producer under the caller's deadline, preserving its cached evidence."""
        cached = self._store.log(f"{session.id}/{attempt or producer}")
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
            stream = (cached or self._unreceived(session, attempt, producer)).model_copy(
                update={"retrieval_error": explain(error)}
            )
        return stream

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        """Copy an attempt output or out member, verified against its recorded fingerprint."""
        attempt, session = self._latest(run)
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
        paid = _paid(attempts)
        last = attempts[-1] if attempts else None
        refused = last is not None and last.cause == CAPACITY_REFUSED
        session = None if last is None else self._store.session(last.session)
        rental = None if session is None else self._store.rental_for(session)
        delivery_age: float | None = None
        if last is not None and last.outcome is None and session is not None:
            if self._storage(session) is not None:
                delivered = self._store.last_delivery(last.id)
                since = delivered or last.planned_at or session.planned_at
                delivery_age = max(0.0, (now - since).total_seconds())
        superseded = last is not None and last.number <= record.launch.resume_after
        seen = Status(
            attempt=last.id if last is not None and not superseded else None,
            run=run,
            delivery_age_seconds=delivery_age,
            phase="queued",
            reason=None,
            outcome=None,
            cause=None,
            until=record.launch.spec.until,
            clocks={} if last is None else last.clocks,
            tally=None if last is None else last.tally,
            usd=self._run_usd(run, paid, now),
            attempts=_tries(attempts),
            machine=None if last is None or refused else last.machine,
            hardware=None if session is None or refused else session.hardware,
            rental_ended=None
            if rental is None
            else self._store.machine(rental.machine).deleted_at is not None,
            estimated=any(self._estimated(self._store.session(a.session), now) for a in paid),
            shared=1
            if last is None or session is None
            else self._shared(last, session, rental, now),
        )
        if last is not None and session is not None and last.outcome is None:
            if session.provider_id is None:
                return seen.model_copy(update={"phase": "renting"})
            if record.launch.cancel_requested_at is not None:
                return seen.model_copy(
                    update={"phase": "stopping"}
                )  # asked to stop; not yet seen to have
            return seen.model_copy(
                update={"phase": "setting_up" if last.started_at is None else "running"}
            )
        retry = (
            last is None
            or superseded
            or (
                (last.outcome == "stopped" or last.outcome == "lost" and last.cause in RETRYABLE)
                and seen.attempts < record.launch.spec.max_attempts
            )
        )
        if last is not None and not retry and last.outcome != "stopped":
            return seen.model_copy(
                update={"phase": "done", "outcome": last.outcome, "cause": last.cause}
            )
        if record.launch.cancel_requested_at is not None:
            return seen.model_copy(update={"phase": "done", "outcome": "cancelled"})
        left = _remaining_budget(record.launch.spec, paid, seen.usd, now)
        # Under a second cannot be placed (`_limit`), so no retry can fit: say
        # what ended the last attempt beside why no other follows.
        if left.seconds < 1 or left.usd <= 0:
            return seen.model_copy(
                update={
                    "phase": "done",
                    "outcome": "out_of_time" if left.seconds < 1 else "over_budget",
                    "cause": None if last is None else last.cause,
                }
            )
        if last is not None and not retry:
            return seen.model_copy(update={"phase": "done", "outcome": last.outcome})
        if last is not None and session is not None:
            if superseded:
                reason = "explicit resume"
            elif refused:
                reason = _refused(session)
            else:
                reason = f"retrying after {last.cause or last.outcome}"
            return seen.model_copy(update={"reason": reason})
        waits = [
            name + (f": {self._trouble[name]}" if name in self._trouble else "")
            for name in (record.launch.pool, record.launch.rent)
            if name is not None
        ]
        return seen.model_copy(update={"reason": "waiting for " + "; ".join(waits)})

    # The loop

    def tick(self) -> None:
        """Take every step that can be taken now. Safe to call from anywhere, often.

        Every pool goes before any on-demand, so a run that may use both takes a
        free owned machine before anything is rented for it.
        """
        self._store.check_schema()
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
            elif not self._kept_rental(session) and not self._store.attempts(session=session.id):
                # A crashed admission never asked for this machine. Release its claim.
                self._store.record_refusal(session, now, None)
        self._store.check_lock(supply.name)
        self._plan(supply, now)
        for idle in self._idle_sessions(supply, now):
            if self._kept_rental(idle):
                continue
            try:
                if self._store.rental_for(idle) is None:
                    self._end_session(idle)
                elif idle.finish_requested_at is None:
                    # A rented machine delivers its last files and end receipt before it
                    # is deleted: `_confirm_ends` ends it once its journal says so, or at
                    # its deadline. Recorded before it is sent: a session that may have
                    # read it might end at any moment, so it is placed no more work.
                    self._store.record_finish(idle, now)
            except (OSError, LauncherError, StorageUnavailable):
                continue  # cleanup retained its error and remains unconfirmed
        self._confirm_ends(supply)
        for session in self._store.unsettled_sessions():
            if session.supply == supply.name and session.claimed_at is None:
                self._create(session, now)

    def _confirm_ends(self, supply: Supply) -> None:
        """Settle only shutdowns already requested; never place work or decide to end a rental."""
        for session in self._store.finishing_sessions(supply=supply.name):
            self._store.check_lock(supply.name)
            try:
                if self._service(session).finish(session.id) is not None:
                    self._end_session(session)
            except (OSError, LauncherError, StorageUnavailable):
                continue  # the existing cleanup path retains uncertainty for a later pass

    def _estimated(self, session: Session, now: datetime) -> bool:
        rental = self._store.rental_for(session)
        if rental is None:
            return False
        machine = self._store.machine(rental.machine)
        return has_unbilled_time(rental, machine, now)

    def _bills(self, now: datetime) -> None:
        for rental in self._store.rentals():
            machine = self._store.machine(rental.machine)
            if machine.provider_id is None or not has_unbilled_time(rental, machine, now):
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
        """Send waiting runs to free slots of running machines, free owned machines, or new rentals.

        A new machine is given up to its slots of waiting runs at once.
        """
        off = isinstance(supply, Pool) and supply.off
        waiting = (
            [] if off else [r for r in self._store.waiting(supply.name) if not self.read(r.id).done]
        )
        for held, free in self._open_sessions(supply, now):
            if not waiting:
                return
            rental = self._store.rental_for(held)
            left = None if rental is None else int((rental.deadline - now).total_seconds())
            price = Decimal(0) if rental is None else rental.usd_per_hour
            for _ in range(free):
                if not waiting:
                    return
                planned = self._next_run(waiting, price, now, fit=left)
                if planned is None:
                    break
                placed = len(self._store.attempts(session=held.id))
                attempt = self._make_attempt(planned, held, placed, now, added=True)
                try:
                    self._store.place(held, (attempt,))
                except Stale:
                    return  # another process placed this run
                self._store.check_lock(supply.name)
                waiting.remove(planned.run)
                held = self._store.session(held.id)
                self._hand(held, attempt)
        while waiting:
            if isinstance(supply, Pool):
                owned = self._free_owned(supply, now)
                if owned is None:
                    return
                batch = self._batch(waiting, Decimal(0), now, supply.slots)
                if not batch:
                    return
                session = _new_session(_id(), supply, owned, now)
                rented, rental = None, None
            else:
                if self._store.recent_refusal(supply.name, now - timedelta(seconds=60)):
                    return  # the provider just said no; the refused run's status says why
                offer = self._rentable(supply)
                if offer is None:
                    return
                price = offer.usd_per_hour
                batch = self._batch(waiting, price, now, supply.slots)
                if not batch:
                    return
                try:
                    self._stage(supply, [planned.run for planned in batch])
                except (OSError, LauncherError, StorageUnavailable) as error:
                    self._diagnose("source upload", "upload failed; nothing rented", error)
                    self._trouble[supply.name] = "source upload failed"
                    return
                now = self._clock()  # the rental's window starts after the upload
                self._store.check_lock(supply.name)
                life = self._life(supply, [planned.run for planned in batch], waiting, price)
                if life < 1:
                    self._trouble[supply.name] = "budget cannot cover the provider fallback timer"
                    return
                session, rented, rental = self._make_rental(supply, price, life, now, kept=False)
            attempts = tuple(
                self._make_attempt(planned, session, position, now)
                for position, planned in enumerate(batch)
            )
            try:
                self._store.plan(session, attempts, rented, rental)
            except Stale:
                return  # another process planned this run
            for planned in batch:
                waiting.remove(planned.run)

    def _batch(
        self, waiting: Sequence[RunRecord], price: Decimal, now: datetime, slots: int
    ) -> list[PlannedRun]:
        """Up to `slots` eligible waiting runs, oldest first, for one new machine."""
        batch: list[PlannedRun] = []
        left = list(waiting)
        while len(batch) < slots:
            planned = self._next_run(left, price, now)
            if planned is None:
                break
            batch.append(planned)
            left.remove(planned.run)
        return batch

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
        """What the on-demand would rent now, if `refusal` allows one more machine.

        None when it may not; the reason is kept for status. Its own limits are
        checked before its provider is asked.
        """
        in_use = self._in_use(ondemand) + additional
        problem = refusal(ondemand, in_use, None, manual=manual)
        if problem is None:
            try:
                offer = self._reach(ondemand).offer()
            except Exception as trouble:  # noqa: BLE001
                # No price, no rental: the runs stay queued, and status says why.
                self._trouble[ondemand.name] = str(trouble) or type(trouble).__name__
                return None
            problem = refusal(ondemand, in_use, offer, manual=manual)
        if problem == "at its machine limit":
            ending = [s.machine for s in self._store.finishing_sessions(supply=ondemand.name)]
            if ending:
                problem += f" (finishing: {', '.join(ending)})"
        if problem is not None:
            self._trouble[ondemand.name] = problem
            return None
        self._trouble.pop(ondemand.name, None)
        return offer

    def _in_use(self, ondemand: OnDemand) -> int:
        """The on-demand's machines renting or rented now."""
        return sum(s.supply == ondemand.name for s in self._store.unsettled_sessions())

    def _safeguards(self, supply: OnDemand) -> Safeguards:
        declared: Safeguards = getattr(
            self._services[self._provider_service(supply.template.provider)],
            "safeguards",
            Safeguards(),
        )
        return declared

    def _affordable_seconds(self, supply: OnDemand, price: Decimal, cap: Decimal) -> float:
        return affordable_seconds(price, cap, self._safeguards(supply).reservation_hours)

    def _make_rental(
        self, ondemand: OnDemand, price: Decimal, life: float, now: datetime, *, kept: bool
    ) -> tuple[Session, Machine, Rental]:
        """A new rented machine's one session, the machine and its paid window of `life` seconds."""
        id = _id()
        template = ondemand.template
        life = min(life, self._longest_rental(ondemand))
        machine = Machine(
            name=f"{ondemand.name}-{id}",
            ondemand=ondemand.name,
            kind="rented",
            provider=template.provider,
            image=template.image
            or (
                STOCK_IMAGE
                if self._provider_service(template.provider) in get_args(ProviderKind)
                else None
            ),
        )
        rental = Rental(
            machine=machine.name,
            usd_per_hour=price,
            cap_usd=rental_cost(price, life, self._safeguards(ondemand).reservation_hours),
            deadline=now + timedelta(seconds=life),
            kept=kept,
        )
        return _new_session(id, ondemand, machine, now), machine, rental

    def _make_attempt(
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

    def _idle_sessions(self, supply: Supply, now: datetime) -> list[Session]:
        """Sessions of a pool or on-demand that are up and have nothing left to run."""
        return [session for session, free in self._free_slots(supply, now) if free == supply.slots]

    def _open_sessions(self, supply: Supply, now: datetime) -> list[tuple[Session, int]]:
        """Sessions that may be given work, each with its free slots: up, and not told to finish."""
        return [
            (session, free)
            for session, free in self._free_slots(supply, now)
            if session.finish_requested_at is None
        ]

    def _free_slots(self, supply: Supply, now: datetime) -> list[tuple[Session, int]]:
        """Sessions of a pool or on-demand that are up, each with its free slots."""
        opened = []
        for session in self._store.unsettled_sessions():
            if (
                session.supply != supply.name
                or session.provider_id is None
                or not self._live(session, now)
            ):
                continue
            held = self._store.open_attempts(session.machine)
            if held < supply.slots:
                opened.append((session, supply.slots - held))
        return opened

    def _next_run(
        self,
        waiting: Sequence[RunRecord],
        price: Decimal,
        now: datetime,
        fit: int | None = None,
    ) -> PlannedRun | None:
        """Oldest eligible run, bounded by its remaining time and dollars.

        Each placement is one attempt, priced as if it had its machine alone.
        Later passes fill free slots as attempts finish, checking the limits again.
        """
        for run in waiting:
            budget = self._remaining(run, now)
            cap = budget.usd
            limit = self._limit(budget.seconds, cap, price)
            if limit is None:
                continue
            if fit is not None and limit[0] > fit:
                continue  # an existing rental must have time for the whole run
            return PlannedRun(
                run=run, usd=price * limit[0] / 3600, seconds=limit[0], limit=limit[1]
            )
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
        except _RELAY_WAITS as error:
            self._await_relay(session, error)
            return
        if not packet.work and not packet.kept:
            self._store.confirm_ended(session, now)  # every attempt ended undelivered
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
                self._store.run(a.run).launch.cancel_requested_at is not None
                for a in self._store.attempts(session=session.id)
            ):
                raise Refused("startup cancelled during preparation; no machine created")

        try:
            limits = [d for d in (packet.startup_deadline, packet.deadline) if d is not None]
            seconds = (min(limits) - self._clock()).total_seconds() if limits else None
            if seconds is not None and seconds <= 0:
                raise Refused("startup or rental deadline reached before creation")
            with creation_guard(check), limit_time(seconds):
                provider_id = service.create(
                    packet.model_copy(update={"kept": True}) if idle else packet
                )
        except Refused as error:
            self._diagnose(
                "machine create", "provider refused; no machine acquired", error, session=session.id
            )
            self._store.record_refusal(session, now, sanitize(str(error)) or "no reason given")
            for attempt in self._store.attempts(session=session.id):
                self._store.fail_before_start(attempt, CAPACITY_REFUSED, now)
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
            self._store.record_provider_id(session, provider_id)
            self._diagnose("machine create", "machine confirmed", session=session.id)
            self._creation_facts(self._store.session(session.id), now)
        except Stale:
            pass  # another process already found the rental by its identity

    def _creation_facts(self, session: Session, now: datetime) -> Session:
        service = self._service(session)
        try:
            allocation = service.allocation(session.id)
        except ProviderUnavailable as error:
            self._diagnose(
                "machine readiness", "machine is not ready yet", error, session=session.id
            )
            allocation = None
        if allocation is not None and allocation.usd_per_hour != self._usd_per_hour(session):
            self._store.record_price(session, allocation.usd_per_hour)
            session = self._store.session(session.id)
        hardware = None if allocation is None else allocation.hardware
        if hardware is not None and hardware != session.hardware:
            self._store.record_hardware(session, hardware)
            session = self._store.session(session.id)
        if session.started_at is None and service.started(session.id):
            self._store.record_started(session, now)
            session = self._store.session(session.id)
        return session

    def _relay(self, attempt: RunAttempt, work: Work) -> bool:
        """Hand an attempt what it needs of runs' folders from the machines that last ran them.

        A later attempt gets its run's records, out and latest
        checkpoint. Initial directory inputs share one relay of the parent;
        other run inputs get just their named file. A machine that reads the holder's
        storage reads them from there; any other is given them from the
        holder's storage, or else its disk (`Execution.held`). Raises while
        those cannot be reached, and the attempt waits: nothing is silently
        left behind. An input whose record or file is gone, or whose bytes do
        not match its record, ends the attempt `input_undelivered` and returns
        False: asking again would find the same.
        """
        if attempt.id in self._relayed:
            return True
        if not work.resumes and all(given.run is None for given in work.inputs):
            self._relayed.add(attempt.id)
            return True
        session = self._store.session(attempt.session)
        if work.resumes:
            self._carry(attempt.run, session, before=attempt.number)
        copied: set[str] = set()
        for given in work.inputs:
            if given.run is None:
                continue
            try:
                if given.out is not None:
                    if given.run not in copied:
                        self._carry(given.run, session)
                        copied.add(given.run)
                else:
                    self._carry(given.run, session, key=given.key)
            except (FileNotFoundError, StoredContentMismatch, RecordMismatch) as error:
                # The error says whether the file is gone or differs from its record.
                self._diagnose(
                    "run folder relay",
                    f"run {given.run} cannot give its recorded {given.key}",
                    error,
                    attempt=attempt.id,
                )
                self._store.fail_before_start(attempt, "input_undelivered", self._clock())
                return False
        self._relayed.add(attempt.id)
        return True

    def _carry(
        self, run: str, session: Session, *, before: int | None = None, key: str | None = None
    ) -> None:
        """Copy a run's folder, or one `key` of it, from its latest started attempt's machine."""
        holder = self._holder(run, before)
        if holder is None or holder.machine == session.machine:
            return
        storage = self._storage(session)
        if storage is not None and storage == self._storage(holder):
            return  # the session's machine reads the run from the holder's storage
        self._service(session).hold(run, self._service(holder).held(run, key))

    def _storage(self, session: Session) -> str | None:
        """The storage the session's machine keeps records in; None where only its disk does."""
        machine = self._store.machine(session.machine)
        if machine.kind == "rented":
            return self._store.ondemand(session.supply).storage
        return machine.storage

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
        if holder is None or self._storage(holder) is not None:
            return None
        return self._service(holder).held(run)

    def _work(self, attempt: RunAttempt) -> Work | None:
        """What the attempt's machine is sent; None when the attempt ended undelivered."""
        run = self._store.run(attempt.run)
        spec = run.launch.spec
        holder = self._holder(run.id, before=attempt.number) if attempt.number > 1 else None
        # Relay and delivery use the same selection: a claimed child resumes
        # its own state; an unclaimed child still needs the parent's initial out.
        work = Work(
            attempt=attempt.id,
            command=spec.command,
            runtime=spec.runtime,
            prepare=spec.prepare,
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
            resumes=holder is not None,
            here=holder is not None and holder.machine == attempt.machine,
            job=run.job,
            job_name=spec.name or None,
            experiment=spec.experiment,
            inputs=tuple(i for i in spec.inputs if holder is None or i.out is None),
            environment=spec.environment,
        )
        return work if self._relay(attempt, work) else None

    def _hand(self, session: Session, attempt: RunAttempt) -> None:
        """Add placed work to a kept session; while its run's folder cannot be relayed, it waits.

        A placed attempt not yet in the session's journal is handed again on
        each pass, so the wait ends when the holding machine answers.
        """
        try:
            work = self._work(attempt)
        except _RELAY_WAITS as error:
            self._await_relay(session, error, attempt=attempt.id)
            return
        if work is not None:
            self._service(session).add(session.id, work)

    def _await_relay(
        self, session: Session, error: Exception, *, attempt: str | None = None
    ) -> None:
        """Note why work for `session` waits: what holds a run's folder did not give it."""
        self._diagnose(
            "run folder relay",
            "waiting for its host",
            error,
            session=None if attempt else session.id,
            attempt=attempt,
        )
        self._trouble[session.supply] = "waiting for the machine or storage holding a run's folder"

    def _packet(self, session: Session) -> Packet:
        """What one session is sent: its deadline, its work and where its records go."""
        rental = self._store.rental_for(session)
        attempts = self._store.attempts(session=session.id)
        first = [a for a in attempts if not a.added and a.outcome is None]
        work = tuple(w for w in map(self._work, first) if w is not None)
        supply = self._store.supply(session.supply)
        return Packet(
            delivery_seconds=supply.delivery_seconds,
            log_seconds=supply.log_seconds,
            session=session.id,
            deadline=None if rental is None else rental.deadline,
            work=work,
            # A machine with slots takes work as they free, so its session is kept.
            kept=rental is not None or supply.slots > 1 or any(a.added for a in attempts),
            cap_usd=Decimal(0) if rental is None else rental.cap_usd,
            autorun=session.autorun,
            slots=supply.slots,
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
        if session.provider_id is None:
            provider_id = service.find(session.id)
            if provider_id is not None:
                self._store.record_provider_id(session, provider_id)
                self._diagnose(
                    "reconciliation",
                    "existing machine confirmed; no duplicate create",
                    session=session.id,
                )
            elif deadline is not None and now >= deadline:
                # The provider confirmed no machine on every pass of the paid
                # window: none was made, so none is paid for or holds the pool.
                for attempt in attempts:
                    if attempt.outcome is None:
                        self._store.finish(attempt, now, "lost", cause="create_unconfirmed")
                self._store.record_refusal(session, now, None)
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
        for entry in seen.progress:
            self._retain_job_log(session, entry, now)
        open_attempts = any(a.outcome is None for a in self._store.attempts(session=session.id))
        finished = not open_attempts and rental is None
        if finished or (deadline is not None and now >= deadline):
            self._end_session(session)

    def _retain_job_log(self, session: Session, seen: Progress, now: datetime) -> None:
        """Keep what a running job has printed, so `etude logs`, a read, can show it.

        Fetches only when more of the log is stored, at most every
        `_JOB_LOG_SECONDS`. Each stored piece is read once per server.
        """
        size, at = self._job_logs.get(seen.attempt, (0, None))
        if seen.log_bytes == size:
            return
        if at is not None and (now - at).total_seconds() < _JOB_LOG_SECONDS:
            return
        self._job_logs[seen.attempt] = (seen.log_bytes, now)
        with limit_time(self._log_seconds(session)):
            self._collect_log(session, seen.attempt, "job")

    def _final_logs(self, session: Session) -> None:
        """Retain each producer's last output, within the supply's `log_seconds`."""
        attempts = self._store.attempts(session=session.id)
        self._collect_logs((session,), attempts, self._log_seconds(session))

    def _log_seconds(self, session: Session) -> float:
        return self._store.supply(session.supply).log_seconds or _LOG_SECONDS

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
                self._final_logs(session)
                raise
            if gone is None:
                self._diagnose("cleanup", "machine deletion unconfirmed", session=session.id)
                return
            self._store.confirm_ended(session, gone)
            self._diagnose("cleanup", "machine deletion confirmed", session=session.id)
        # Nothing more will be written. Take what the journal holds now, and
        # count any attempt still open as lost with the machine.
        self._final_logs(session)
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
        if (
            seen is not None
            and seen.finished_at is not None
            and seen.outcome is not None
            and (seen.delivered or not alive)
        ):
            outcome = seen.outcome
            if outcome == "cancelled" and run.launch.cancel_requested_at is None:
                outcome = "over_budget"
            self._store.finish(
                attempt,
                seen.finished_at,
                outcome,
                cause=seen.cause,
                clocks=seen.clocks,
                outputs=seen.outputs,
                exit_code=seen.exit_code,
                prepared=seen.prepared,
                tally=seen.tally,
                writers=seen.writers,
            )
            self._diagnose(
                "execution",
                f"attempt finished: {outcome}; exit status {seen.exit_code if seen.exit_code is not None else 'not received'}"
                + ("" if seen.delivered else "; its files were not confirmed stored"),
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
            if cause == "machine_lost":
                supply = self._store.supply(session.supply)
                if isinstance(supply, OnDemand) and supply.template.spot:
                    cause = "spot_machine_lost"
            self._store.finish(
                attempt,
                now,
                "lost",
                cause=cause,
                clocks=seen.clocks if seen else attempt.clocks,
                outputs=seen.outputs if seen else attempt.outputs,
                prepared=seen.prepared if seen else None,
                tally=seen.tally if seen else attempt.tally,
                writers=seen.writers if seen else attempt.writers,
            )
            self._diagnose(
                "execution",
                f"attempt lost ({cause}); final exit status not received",
                session=session.id,
                attempt=attempt.id,
            )
        else:
            if seen is not None:
                self._store.record_progress(attempt, seen.clocks, seen.tally, seen.writers)
            if run.launch.cancel_requested_at is not None or self._remaining(run, now).usd <= 0:
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
            if job.require_hours() * 3600 + ondemand.setup_seconds > longest:
                # One rental holds a run from setup to its last hour; none outlasts its grant.
                raise LauncherError(
                    f"a job on on-demand {target.rent!r} may ask for at most "
                    f"{(longest - ondemand.setup_seconds) / 3600:g} hours: its storage's "
                    f"grants last {longest / 3600:g} hours, including "
                    f"{ondemand.setup_seconds} setup seconds"
                )
        if not job.command.strip() or job.require_hours() <= 0:
            raise LauncherError("a job needs a command and hours")
        if job.max_usd is None and target.rent is not None:
            raise LauncherError(
                f"on-demand {target.rent!r} can spend money, so the job needs max_usd"
            )
        if job.max_usd is not None and job.max_usd <= 0:
            raise LauncherError("a job's max_usd must be more than zero")
        if job.max_attempts < 1:
            raise LauncherError("a job needs at least one attempt")

    def _resumed(self, request: RunRequest) -> RunRecord:
        """Check identity and cumulative allowances before source publication or renting."""
        if request.id is None:
            raise LauncherError("resume needs the existing run id")
        prior = self._store.run(request.id)
        attempts = self._store.attempts(run=prior.id)
        if any(a.outcome is None for a in attempts):
            raise LauncherError(f"run {prior.id!r} still has an allocated attempt")
        job = request.job
        differences = resume_conflicts(
            config=job.config,
            seed=request.seed,
            prior_config=prior.launch.spec.config,
            prior_seed=prior.seed,
        )
        if job.experiment != prior.launch.spec.experiment:
            differences.append("experiment differs")
        if differences:
            raise LauncherError("run identity conflict: " + "; ".join(differences))
        target = job.until or prior.launch.spec.until
        if attempts and attempts[-1].outcome == "succeeded":
            reached = (
                max((a.clocks.get(target.clock, 0) for a in attempts), default=0)
                if target is not None
                else 0
            )
            if target is None or target.value <= reached:
                raise LauncherError("run already finished; give --until with a later target")
        now = self._clock()
        paid = _paid(attempts)
        left = _remaining_budget(job, paid, self._run_usd(prior.id, paid, now), now)
        if _tries(attempts) >= job.max_attempts or left.seconds < 1 or left.usd <= 0:
            raise LauncherError(
                "run has no remaining hours, dollars or attempts; raise its job limits"
            )
        return prior.model_copy(
            update={"submission": prior.launch.model_copy(update={"resume_after": len(attempts)})}
        )

    def _freeze(self, run: RunRequest, target: Target) -> RunRecord:
        job = run.job
        self._validate_job(job, target)
        prior = self._resumed(run) if run.resume else None
        if prior is not None and job.until is None:
            run = run.model_copy(
                update={"job": job.model_copy(update={"until": prior.launch.spec.until})}
            )
        run = source.freeze(run)
        job = run.job
        assert run.id is not None  # source.freeze assigns the submitting client's identity
        if prior is not None:
            return prior.model_copy(
                update={
                    "submission": prior.launch.model_copy(
                        update={
                            "spec": job,
                            "source": run.source,
                            "pool": target.pool,
                            "rent": target.rent,
                            "cancel_requested_at": None,
                            "params": dict(run.params),
                            "smoke": run.smoke,
                        }
                    )
                }
            )
        return RunRecord(
            id=run.id,
            experiment=job.experiment or "",
            submission=RunSubmission(
                source=run.source,
                spec=job,
                pool=target.pool,
                rent=target.rent,
                created_at=self._clock(),
                autorun=True,
                params=dict(run.params),
                smoke=run.smoke,
            ),
            job=job_id(job),
            seed=run.seed,
        )

    def _latest(self, run: str) -> tuple[RunAttempt, Session]:
        for attempt in reversed(self._store.attempts(run=run)):
            session = self._store.session(attempt.session)
            if session.provider_id is not None:
                return attempt, session
        raise LauncherError(f"run {run} has not reached a session")

    def _admission_budget(
        self, run: RunRecord, now: datetime, *, committed: bool = False
    ) -> Budget:
        # Admission sees newly frozen requests. Only an explicit resume has
        # previous allocations; fresh work needs no history queries.
        if run.launch.resume_after:
            return self._remaining(run, now, committed=committed)
        return Budget(
            seconds=run.launch.spec.require_hours() * 3600, usd=_limit_usd(run.launch.spec)
        )

    def _remaining(
        self, run: RunRecord, now: datetime, *, committed: bool = False, joining: str | None = None
    ) -> Budget:
        attempts = _paid(self._store.attempts(run=run.id))
        dollars = self._run_usd(run.id, attempts, now, committed=committed, joining=joining)
        return _remaining_budget(run.launch.spec, attempts, dollars, now)

    def _places(
        self, session: Session, rental: Rental | None, now: datetime, *, project: bool = False
    ) -> dict[str, Place]:
        assert session.claimed_at is not None
        horizon = rental.deadline if project and rental else now
        end = min(horizon, session.ended_at or horizon)
        if rental is not None:
            end = min(end, rental.deadline)
        return {
            attempt.id: Place.from_attempt(
                attempt, session, end, projected_from=now if project else None
            )
            for attempt in self._store.attempts(session=session.id)
        }

    def _run_usd(
        self,
        run: str,
        attempts: Sequence[RunAttempt],
        now: datetime,
        *,
        committed: bool = False,
        joining: str | None = None,
    ) -> Decimal:
        dollars = Decimal(0)
        for session_id in {a.session for a in attempts}:
            session = self._store.session(session_id)
            rental = self._store.rental_for(session)
            if rental is not None and session.claimed_at is not None and session.refused_at is None:
                places = self._places(
                    session, rental, now, project=committed and session.id != joining
                )
                dollars += run_share(
                    run, places.values(), rental, self._store.machine(session.machine)
                )
        return dollars

    def _shared(
        self, attempt: RunAttempt, session: Session, rental: Rental | None, now: datetime
    ) -> int:
        """Peak concurrent runs while the attempt occupied its machine."""
        if session.claimed_at is None:
            return 1
        places = self._places(session, rental, now)
        own = places[attempt.id]
        moments = [own.start] + [
            p.start for p in places.values() if own.start < p.start < own.active_until
        ]
        return max(
            1,
            max(
                len({p.run for p in places.values() if p.start <= t < p.active_until})
                for t in moments
            ),
        )
