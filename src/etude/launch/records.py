"""Launcher records: what a caller passes, what is stored, and what a caller reads back.

A pool is a named set of machines you own; an on-demand (OnDemand) is a
machine template and a budget to rent from. Pools and on-demands share one
namespace, and a run names a pool, an on-demand or both. Machine describes an
added or rented computer; Rental describes only its paid window. RunAttempt allocates
a run to either kind of machine. Session is internal: one start of Etude's
process on a machine, and its journal folder.
Jobs describe what runs and its limits (`etude.jobs` makes them from Python
functions).

Units: a clock is any named count of progress a job keeps, such as steps,
agent_steps or wall_time_ms. Positions on a clock are absolute. Hours and US
dollars bound machines.
Every time is timezone-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from etude.errors import LauncherError as LauncherError
from etude.launch.commands import ProviderKind
from etude.records import RunRecord as RunRecord
from etude.records import RunWriter
from etude.records import Tally as Tally

# Shared job records live in `etude.spec`; released imports from here keep working.
from etude.spec import Input as Input
from etude.spec import Job as Job
from etude.spec import Param as Param
from etude.spec import Preparation as Preparation
from etude.spec import Record as Record
from etude.spec import RunRequest as RunRequest
from etude.spec import Runtime as Runtime
from etude.spec import Smoke as Smoke
from etude.spec import SmokeForm as SmokeForm
from etude.spec import SmokeStep as SmokeStep
from etude.spec import SourcePosition as SourcePosition
from etude.spec import Until as Until
from etude.spec import Variable as Variable
from etude.spec import job_id as job_id
from etude.spec import number as number

__all__ = (
    "MachineTemplate",
    "OnDemand",
    "Provider",
    "Pool",
    "Storage",
    "Image",
    "GPU",
    "CPU",
    "Availability",
    "Offer",
    "Status",
    "Quote",
    "Diagnostic",
    "Machine",
    "MachineStatus",
    "RentalCost",
    "LogStream",
    "Standing",
    "ExperimentLimit",
)


Outcome = Literal[
    "succeeded", "failed", "cancelled", "over_budget", "out_of_time", "lost", "stopped"
]
# Why infrastructure lost an attempt. Only "lost" attempts carry a cause.
Cause = Literal[
    "capacity_refused",
    "setup_failed",
    "input_undelivered",
    "machine_lost_before_start",
    "machine_lost",
    "spot_machine_lost",
    "create_unconfirmed",
]
# A definite refusal acquired no machine and spends no attempt budget.
CAPACITY_REFUSED: Cause = "capacity_refused"
TRY_EXEMPT = (CAPACITY_REFUSED, "spot_machine_lost")
# Confirmed machine loss permits a new attempt; an uncertain start does not, nor
# an input the machine was not given, which a retry would not be given either.
RETRYABLE: frozenset[str] = frozenset(
    {*TRY_EXEMPT, "setup_failed", "machine_lost_before_start", "machine_lost"}
)
# Which of a run's two limits ends it first on the rental it was given.
Limit = Literal["over_budget", "out_of_time"]
Phase = Literal["queued", "renting", "setting_up", "running", "stopping", "done"]


class Stale(LauncherError):
    """A record changed after it was read; the write did not happen."""


def provider_unit(kind: ProviderKind) -> str:
    """Native billing unit for one provider kind (`cu` for Colab, `usd` otherwise)."""
    return "cu" if kind == "colab" else "usd"


class Provider(Record):
    """An account that supplies machines on demand. It is set once and can be changed.

    Secrets are never stored here. The account key comes from the environment
    of whoever runs the launcher.
    """

    name: str  # what a machine template's `provider` names
    kind: ProviderKind
    registry: str  # the provider's id for the credential that pulls private images
    storage: str  # the storage a new pool uses unless it names another
    # Conversion from the provider's native billing unit to USD; None for USD providers.
    usd_per_unit: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @property
    def unit(self) -> str:
        """This provider's native billing unit."""
        return provider_unit(self.kind)

    @model_validator(mode="after")
    def _registry_supported(self) -> Provider:
        if self.kind != "runpod" and self.registry:
            raise ValueError(
                f"{self.kind.title()} supports public images; registry credentials are not stored"
            )
        if self.unit == "usd" and self.usd_per_unit is not None:
            raise ValueError(f"{self.kind} bills directly in USD; --usd-per-unit does not apply")
        return self


class Storage(Record):
    """A named place in a bucket where sessions keep their packets, logs and outputs.

    It never changes once added, because live sessions are read from it.
    Its keys come from the environment of whoever runs the launcher
    (`etude.launch.storage.s3.open_bucket`): an R2 bucket's account is
    `R2_ACCOUNT_ID`, so no account is recorded here.
    """

    name: str
    bucket: str
    prefix: str  # the folder in that bucket its sessions live under
    provider: Literal["s3", "r2"]  # always stated: which service, and so whose keys
    region: str | None = None  # an S3 bucket's region; R2 has none

    @model_validator(mode="after")
    def _region_where_needed(self) -> Storage:
        if (self.provider == "s3") != (self.region is not None):
            raise ValueError("an S3 storage needs its region, and an R2 storage takes none")
        return self

    @property
    def uri(self) -> str:
        """Where it is, as tracking names a bucket folder: `s3://BUCKET/PREFIX` or `r2://...`."""
        return f"{self.provider}://{self.bucket}/{self.prefix}"


class Image(Record):
    """A requested registry reference and the immutable artifact selected for it."""

    requested: str
    resolved: str = Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")
    platform: str | None = None  # unknown for a caller-supplied pinned reference


def _image(value: object) -> object:
    # Published callers pass pinned strings; normalize them into the same record.
    if isinstance(value, str):
        return Image(requested=value, resolved=value)
    return value


class MachineTemplate(Record):
    """What a rented machine is made from: which provider, what kind, and where."""

    spot: bool = False
    provider: str  # the name of the provider that supplies them
    gpu: str | None = None  # the provider's name for the GPU, such as "NVIDIA L4"
    gpu_count: int = Field(default=1, ge=1)
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    image: Image | None = None

    _parse_image = field_validator("image", mode="before")(_image)
    data_center: str | None = None  # provider location: data center, country, region or zone


class GPU(Record):
    """An allocation of GPUs, with CPU/memory floors and disk when the provider quotes them."""

    flavor: str | None = None
    id: str  # the provider's GPU type id, such as "NVIDIA L4"
    count: int = Field(default=1, ge=1)
    min_vcpus: int | None = None
    min_memory_gb: int | None = None
    disk_gb: int | None = None

    def __str__(self) -> str:
        # The host's CPU and memory are only known as the minimums asked for.
        name = self.id if self.count == 1 else f"{self.count} × {self.id}"
        if self.flavor:
            name = f"{self.flavor}: {name}"
        floors = []
        if self.min_vcpus is not None:
            floors.append(f"{self.min_vcpus} vCPUs")
        if self.min_memory_gb is not None:
            floors.append(f"{self.min_memory_gb} GB")
        return f"{name} with at least {', '.join(floors)}" if floors else name


class CPU(Record):
    """A CPU machine of one flavor, sized by vCPU count when the provider quotes it."""

    flavor: str  # the provider's CPU flavor id, such as "cpu3c"
    vcpus: int | None = None

    def __str__(self) -> str:
        return f"{self.flavor} {self.vcpus} vCPUs" if self.vcpus is not None else self.flavor


# What a provider was asked to rent, which may be larger than the template asked for.
Hardware = GPU | CPU


def affordable_seconds(price: Decimal, cap: Decimal, reservation_hours: int = 0) -> float:
    """Seconds a cap funds at an hourly rate in the same currency or provider units."""
    if not price or cap.is_infinite():
        return float("inf")
    if reservation_hours:
        increments = cap // (price * reservation_hours)
        return float(increments * reservation_hours * 3600)
    # A rental deadline has microsecond precision. Round down so its last
    # fraction of a second cannot exceed the dollars that fund the window.
    micros = (cap * 3_600_000_000 / price).to_integral_value(rounding=ROUND_FLOOR)
    return float(micros / 1_000_000)


class HourlyRate(Record):
    """A machine's dollar rate and the native units behind it, when metered in credits."""

    usd_per_hour: Decimal | None  # unknown until assignment for a credit-metered offer
    unit: str = "usd"
    units_per_hour: Decimal | None = None
    usd_per_unit: Decimal | None = Decimal(1)  # None on raw non-USD offers before scheduler pricing


class Offer(HourlyRate):
    """What a provider would rent for a template now, at what price, and whether it has one."""

    hardware: Hardware | None  # None for a machine you own
    reason: str | None = None
    available: bool  # False only when the provider positively reports none
    balance: Decimal | None = None  # account balance in `unit`, when the provider reports one
    active: int | None = None  # active assignments on the provider account, when reported

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(available=False, hardware=None, usd_per_hour=Decimal(0), reason=reason)


class Availability(Record):
    """An on-demand's offer as its provider answered at `observed_at`, beside its budget."""

    spot: bool = False
    ondemand: str
    offer: Offer
    max_usd_per_hour: Decimal | None
    in_use: int  # its machines renting or rented now
    max_machines: int
    observed_at: datetime
    refusal: str | None  # why a launch could not rent one now (`refusal`); None: it could

    @property
    def rentable(self) -> bool:
        """Whether a launch could rent one now."""
        return self.refusal is None


STOCK_IMAGE = Image(
    requested="runpod/pytorch@sha256:4d1721e62b56d345c83b4fd6090664be6daf9312caab5b2e76f23d8231941851",
    resolved="runpod/pytorch@sha256:4d1721e62b56d345c83b4fd6090664be6daf9312caab5b2e76f23d8231941851",
)


class Pool(Record):
    """A named set of machines you own, and the runs waiting for them.

    It knows no provider: its machines were added, and their cost is already
    committed, so a free one is used before anything is rented.
    """

    name: str
    startup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    delivery_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    log_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    off: bool = False  # takes no new runs; what it already holds finishes and stays readable
    slots: int = Field(default=1, ge=1)  # runs at once on each machine, sharing it whole


class OnDemand(Record):
    """What to rent and the budget for renting it: a template, machines at once, a price ceiling.

    Every launch that names it with `rent` shares its limits. Each machine it
    rents records its name, and its Rental is the machine's paid window.
    """

    name: str
    template: MachineTemplate
    max_machines: int = Field(default=1, ge=0)  # rented machines in use at once
    max_usd_per_hour: Decimal | None = None  # above this price, no machine is rented
    # Runs at once on each machine. They share its GPUs and CPUs, not partitioned,
    # and split its cost by the time they occupy it together.
    slots: int = Field(default=1, ge=1)
    startup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    delivery_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    log_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    setup_seconds: int = 900  # added to a rental's life when its dollar caps allow
    # Where sessions on its machines keep records. Fixed when it is added:
    # None then means the provider's storage at that moment.
    storage: str | None = None


# Where machines come from: owned ones in a pool, or rented ones from an on-demand.
Supply = Pool | OnDemand


def refusal(
    ondemand: OnDemand, in_use: int, offer: Offer | None, *, manual: bool = False
) -> str | None:
    """Why the on-demand may not rent one more machine now; None when it may.

    `in_use` counts its machines renting or rented. Without an offer only its
    own limits are checked, so a caller can refuse before asking the provider.
    A machine rented by hand (`manual`) is allowed under a zero machine limit.
    """
    if in_use >= ondemand.max_machines and not (manual and ondemand.max_machines == 0):
        return "at its machine limit"
    if ondemand.storage is None:
        return "a rented machine needs a storage"
    if offer is None:
        return None
    if not offer.available:
        return offer.reason or "provider reports no available machines"
    ceiling = ondemand.max_usd_per_hour
    if ceiling is not None and offer.usd_per_hour is not None and offer.usd_per_hour > ceiling:
        return "price is above its ceiling"
    return None


class Target(Record):
    """Where a launch's runs may go: a pool's machines, an on-demand's rentals, or both."""

    pool: str | None = None
    rent: str | None = None

    def ask_usd(self, job: Job) -> Decimal:
        """Dollars a new run reserves against the experiment's spending limit."""
        return (job.max_usd or Decimal(0)) if self.rent is not None else Decimal(0)


MachineState = Literal["starting", "available", "unavailable", "deleted"]


class Machine(Record):
    """A machine you own, in a pool, or one rented from an on-demand.

    Kind describes how it was acquired: this computer, an added SSH host, or
    rented. Provider names the account separately. What the provider said about
    making and deleting a rented machine is recorded on its one session and read
    here; only rented machines may be deleted. An owned machine's cost is
    committed when it is added and it leaves its pool at `expires_at`.
    Unfinished RunAttempts decide whether it is busy.
    """

    name: str
    kind: Literal["local", "ssh", "rented"]
    pool: str | None = None  # an owned machine's pool
    ondemand: str | None = None  # the on-demand a rented machine came from
    cost_usd: Decimal = Decimal(0)  # an owned machine's committed cost
    expires_at: datetime | None = None  # None: an owned machine never expires
    image: Image | None = None
    provider: str | None = None  # account that supplied a rented machine
    claimed_at: datetime | None = None  # when its session claimed the right to ask for it
    provider_id: str | None = None  # the provider's name for it, once its create is confirmed
    started_at: datetime | None = None  # first worker receipt, read from its session
    hardware: Hardware | None = None  # None: not rented, or found by name without its receipt
    refused_at: datetime | None = None
    address: str | None = None  # SSH address for an added host; never a provider receipt
    storage: str | None = None
    deleted_at: datetime | None = None  # only a rented machine is ever deleted

    @property
    def supply(self) -> str:
        """The pool or on-demand this machine belongs to."""
        supply = self.pool or self.ondemand
        assert supply is not None  # the database stores exactly one
        return supply

    def state(
        self,
        now: datetime,
        *,
        off: bool = False,
    ) -> MachineState:
        """Acquisition state at `now`; pool withdrawal also makes a machine unavailable."""
        if self.deleted_at is not None:
            return "deleted"
        if off or self.refused_at is not None or self.expired(now):
            return "unavailable"
        if self.kind == "rented" and self.started_at is None:
            return "starting"
        return "available"

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    @property
    def held(self) -> bool:
        """Someone holds this machine now: neither deleted nor released with nothing rented."""
        return self.deleted_at is None and self.refused_at is None


class Output(Record):
    name: str  # path below the run's output directory
    sha256: str
    size_bytes: int


class Rental(HourlyRate):
    """The paid window of a rented machine, independent of work and acquisition.

    A rented machine has exactly one, so the machine's name identifies it. Machine
    owns the request, provider receipt, refusal and deletion; RunAttempt owns allocation.
    """

    machine: str
    usd_per_hour: Decimal
    usd_per_unit: Decimal = Decimal(1)
    cap_usd: Decimal
    deadline: datetime
    billed_usd: Decimal = Decimal(0)
    billed_through: datetime | None = None
    kept: bool = False  # stays when idle until its deadline or explicit termination

    @field_validator("unit", mode="before")
    @classmethod
    def _default_unit(cls, value: object) -> object:
        return "usd" if value is None or value == "" else value

    @field_validator("usd_per_unit", mode="before")
    @classmethod
    def _default_usd_per_unit(cls, value: object) -> object:
        return Decimal(1) if value is None or value == "" else value


class RentalCost(Record):
    """A rental's paid window and what it has cost so far (`Scheduler.rentals`)."""

    rental: Rental
    usd: Decimal  # its retained bills, plus an estimate of any time they do not cover yet
    estimated: bool  # part of `usd` is an estimate
    unbilled_usd: Decimal  # the estimated part: `usd` beyond its retained bills
    over_cap: bool  # its bills exceed its cap
    units: Decimal | None = None  # estimated native units consumed when rental.unit != "usd"


class MachineStatus(Record):
    """A machine at observation time, its paid window and the runs holding its slots."""

    machine: Machine
    observed_at: datetime
    pool_off: bool
    spot: bool
    cost: RentalCost | None
    runs: tuple[str, ...]

    @property
    def state(self) -> MachineState:
        return self.machine.state(self.observed_at, off=self.pool_off)


class Session(Record):
    """One start of Etude's process on a machine, and the journal folder it writes.

    Internal: callers see machines and run attempts. A rented machine has one
    session for its whole life; a machine you added has one per start, one at a
    time. Starting a session on a rented machine is what asks the provider for
    the machine, so the provider's replies are recorded here and read as Machine.
    """

    id: str
    supply: str  # read from its machine: the pool or on-demand it belongs to
    machine: str
    planned_at: datetime
    claimed_at: datetime | None  # the right to create it was taken; only the claimer asks
    startup_deadline: datetime | None = None
    # Confirmed creation: the provider's name for a rented machine, and for a
    # machine you own the session's own id.
    provider_id: str | None
    started_at: datetime | None
    refused_at: datetime | None
    refusal: str | None = None
    ended_at: datetime | None
    # When it was told no more work will come; it is given none after.
    finish_requested_at: datetime | None = None
    version: int
    autorun: bool = False
    hardware: Hardware | None = None


class Prepared(Record):
    """What an attempt's preparation found and did (`etude.launch.worker.prepared`).

    `rejected` is always followed by a build; `size_bytes` is None when the
    build failed.
    """

    key: str
    outcome: Literal["reused", "built", "rejected"]
    finished_at: datetime  # when the directory was ready, or the build failed
    seconds: float = Field(ge=0)  # verifying a reused directory, or building one
    size_bytes: int | None = None
    cause: str | None = None  # why a kept directory was rejected


class RunAttempt(Record):
    """Allocation of one run to one machine for a bounded execution window.

    An unfinished attempt holds the machine busy, on added and rented hosts alike.
    The internal session identifies its journal; it is not the allocation.
    """

    id: str
    run: str
    session: str
    machine: str
    number: int  # 1 for a run's first try
    position: int  # order on its session
    limit_seconds: int
    limit_kind: Limit
    started_at: datetime | None  # the code started; it never starts twice
    finished_at: datetime | None
    outcome: Outcome | None
    cause: Cause | None
    clocks: dict[str, float]  # where the run last reported each of its clocks
    outputs: tuple[Output, ...]
    exit_code: int | None
    version: int
    # When it was placed. Its share of the cost starts no earlier.
    planned_at: datetime | None = None
    added: bool = False  # placed on a session that already existed
    prepared: Prepared | None = None  # from the finished journal, when the job prepares
    tally: Tally | None = None  # where the job last said its counted work stood
    # Tracking and eval contexts this allocation ran; they add no attempt or cost.
    writers: tuple[RunWriter, ...] = ()


class Spend(Record):
    """Attributed dollars, with native consumption when the rentals share a billing unit."""

    usd: Decimal
    unit: str = "usd"
    units: Decimal | None = None

    def format_cost(self, *, estimated: bool) -> str:
        if self.unit != "usd" and self.units is not None:
            dollars = f"{'~' if estimated and self.usd > 0 else ''}${self.usd:.2f}"
            return f"~{self.units:.2f} {self.unit.upper()} ({dollars})"
        if self.unit != "usd" and estimated:
            return f"{self.unit.upper()} rate unobserved"
        return f"{'~' if estimated else ''}${self.usd:.2f}"


class Status(Spend):
    """Everything `status` says about a run, computed from the stored records."""

    run: str
    phase: Phase
    reason: str | None  # why a queued run is waiting
    outcome: Outcome | None
    cause: Cause | None
    until: Until | None  # the job's target
    clocks: dict[str, float]  # where the run last reported each of its clocks
    attempts: int
    rental_ended: bool | None  # True only on a dated confirmation

    attempt: str | None = None  # latest allocation after the explicit resume boundary
    machine: str | None = None
    hardware: Hardware | None = None
    estimated: bool = False
    shared: int = 1  # most runs on the latest attempt's machine at once during it
    tally: Tally | None = None

    # Age at this status read, from the latest retained successful write, or
    # allocation if no delivery has arrived. None for completed/local work.
    delivery_age_seconds: float | None = None

    @property
    def done(self) -> bool:
        return self.phase == "done"

    def phase_and_clock(self) -> str:
        if self.phase == "queued":
            head = f"queued ({self.reason})"
        elif self.phase == "done":
            head = f"done: {self.outcome}" + (f" ({self.cause})" if self.cause else "")
        else:
            head = self.phase
        clock = self.until.clock if self.until else next(iter(self.clocks), None)
        at = f", {clock} {number(self.clocks[clock])}" if clock in self.clocks else ""
        return head + at

    @property
    def formatted_spend(self) -> str:
        return self.format_cost(estimated=self.estimated)

    def facts(self) -> list[str]:
        """Counted work, shared placement, spend and delivery evidence."""
        facts = []
        if self.tally is not None:
            # A finished job has no time left to give, whatever its last pace.
            shown = self.tally.model_copy(update={"per_hour": None}) if self.done else self.tally
            facts.append(str(shown))
        if self.shared > 1:
            facts.append(f"shared by {self.shared} runs")
        facts.append(self.formatted_spend)
        if self.delivery_age_seconds is not None:
            facts.append(
                f"no delivery confirmed for {self.delivery_age_seconds / 60:.1f} minutes"
                " (retained evidence; collection may be stale)"
            )
        return facts

    def __str__(self) -> str:
        return ", ".join([self.phase_and_clock(), *self.facts()])


class QuotedMachine(HourlyRate):
    """One first-wave run's placement and its maximum admitted rental allocation.

    Cost assumes the quoted rate and timely deletion, and excludes external storage.
    Provider delays can exceed the admitted budget.
    """

    spot: bool = False
    run: str  # the run's name
    machine: str | None  # an existing machine; None means one would be rented
    hardware: Hardware | None  # for a rental, what the provider would be asked for
    # This run's share over its planned time on the machine; None when rate is observed on assign.
    max_usd: Decimal | None
    slots: int = 1  # runs the machine takes at once
    alongside: tuple[str, ...] = ()  # the other first-wave runs placed on the same machine


class Quote(Record):
    """What a launch would take now, without taking it."""

    machines: tuple[QuotedMachine, ...]  # the first wave, as a launch would place it
    limit_usd: Decimal | None  # job rental admission budget; None: it cannot rent

    waiting_usd: Decimal = Decimal(0)  # extra window reserved for runs waiting after the wave

    @property
    def max_usd(self) -> Decimal | None:
        """Maximum admitted first-wave rental allocation, or None when any rate is observed on assign."""
        total = self.waiting_usd
        for machine in self.machines:
            if machine.max_usd is None:
                return None
            total += machine.max_usd
        return total


class ExperimentLimit(Record):
    """The stored spending ceiling and total committed under one experiment label."""

    experiment: str
    max_usd: Decimal
    set_at: datetime
    committed_usd: Decimal = Decimal(0)
    estimated: bool = False

    def format_committed(self, committed: Decimal | None = None) -> str:
        """Keep sub-cent spend visible when it can still block admission."""
        amount = self.committed_usd if committed is None else committed
        dollars = f"{amount:.3g}" if 0 < amount < Decimal("0.01") else f"{amount:.2f}"
        return f"{'~' if self.estimated else ''}${dollars}"


class LaunchRequest(Record):
    run: str
    fingerprint: str
    requested_revision: str | None
    resolved_revision: str | None = None
    resume_version: int | None = None  # compare-and-set the existing run at admission
    experiment: str | None = None
    ask_usd: Decimal = Decimal(0)


class Launch(Record):
    """Evidence of admission intent; never an executable or retryable run."""

    id: str
    requests: tuple[LaunchRequest, ...]
    created_at: datetime
    target: Target | None = None  # absent from evidence written before experiment limits
    state: Literal["preparing", "admitting", "admitted", "failed"] = "preparing"

    def addressed(self, runs: Sequence[RunRequest]) -> tuple[RunRequest, ...]:
        """Give the original requests their retained run addresses."""
        return tuple(
            run.model_copy(update={"id": request.run})
            for run, request in zip(runs, self.requests, strict=True)
        )


class Diagnostic(Record):
    id: str
    observed_at: datetime
    produced_at: datetime | None = None
    operation: str
    message: str
    launch: str | None = None
    session: str | None = None
    attempt: str | None = None
    exception: str | None = None
    exit_code: int | None = None
    timeout: float | None = None


LogProducer = Literal["bootstrap", "worker", "job"]


class LogChunk(Record):
    """Retained characters at an absolute position in one sanitized producer."""

    offset: int
    text: str


class LogStream(Record):
    """A retained producer snapshot; missing and silent are distinct."""

    id: str
    producer: LogProducer
    session: str
    attempt: str | None = None
    machine: str | None = None
    chunks: tuple[LogChunk, ...] = ()
    position: int = 0
    observed_at: datetime | None = None
    received: bool = False
    complete: bool = False
    truncated: bool = False
    retrieval_error: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def text(self) -> str:
        return "".join(chunk.text for chunk in self.chunks)


class LogSnapshot(Record):
    settled: bool = False
    observed_at: datetime
    launches: tuple[Launch, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    streams: tuple[LogStream, ...] = ()

    @property
    def complete(self) -> bool:
        return (
            self.settled
            and all(launch.state not in {"preparing", "admitting"} for launch in self.launches)
            and all(stream.complete and not stream.retrieval_error for stream in self.streams)
        )


class BootstrapLinks(Record):
    """Read bootstrap instructions and write exactly the two startup log objects."""

    url: str
    log_url: str
    worker_log_url: str


class Standing(Record):
    """One run and the scheduler's retained status, without remote observation."""

    record: RunRecord
    status: Status

    @property
    def committed_usd(self) -> Decimal:
        """Actual spend plus any unspent dollars an unfinished run may still use."""
        if self.status.done:
            return self.status.usd
        launch = self.record.launch
        return max(self.status.usd, Target(pool=launch.pool, rent=launch.rent).ask_usd(launch.spec))

    @property
    def placement(self) -> Literal["queued", "running"]:
        """Whether a run that has not ended still waits for a machine."""
        return "queued" if self.status.phase in {"queued", "renting"} else "running"
