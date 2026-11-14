"""Launcher records: what a caller passes, what is stored, and what a caller reads back.

A pool is a named set of machines you own; an on-demand (OnDemand) is a
machine template and a budget to rent from. Pools and on-demands share one
namespace, and a run names a pool, an on-demand or both. Machine describes an
added or rented computer; Rental describes only its paid window. RunAttempt allocates
a run to either kind of machine. Session is internal: one start of Etude's
process on a machine, and its journal folder.
Jobs describe what runs and its limits (`etude.jobs` makes them from Python
functions); cohorts group runs.

Units: a clock is any named count of progress a job keeps, such as steps,
agent_steps or wall_time_ms. Positions on a clock are absolute. Hours and US
dollars bound machines.
Every time is timezone-aware UTC.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from etude.errors import LauncherError as LauncherError

# Shared job records live in `etude.job`; released imports from here keep working.
from etude.job import Input as Input
from etude.job import Job as Job
from etude.job import Param as Param
from etude.job import Preparation as Preparation
from etude.job import Record as Record
from etude.job import RunRequest as RunRequest
from etude.job import Runtime as Runtime
from etude.job import Smoke as Smoke
from etude.job import SmokeForm as SmokeForm
from etude.job import SmokeStep as SmokeStep
from etude.job import SourcePosition as SourcePosition
from etude.job import Until as Until
from etude.job import Variable as Variable
from etude.job import job_id as job_id
from etude.job import number as number
from etude.records import RunRecord as RunRecord

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
    "create_unconfirmed",
]
# Confirmed machine loss permits a new attempt; an uncertain start does not, nor
# an input the machine was not given, which a retry would not be given either.
RETRYABLE: frozenset[str] = frozenset(
    {"capacity_refused", "setup_failed", "machine_lost_before_start", "machine_lost"}
)
# Which of a run's two limits ends it first on the rental it was given.
Limit = Literal["over_budget", "out_of_time"]
Phase = Literal["queued", "renting", "setting_up", "running", "stopping", "done"]


class Stale(LauncherError):
    """A record changed after it was read; the write did not happen."""


class Provider(Record):
    """An account that supplies machines on demand. It is set once and can be changed.

    Secrets are never stored here. The account key comes from the environment
    of whoever runs the launcher.
    """

    name: str  # what a machine template's `provider` names
    kind: Literal["runpod", "vast", "lium"]
    registry: str  # the provider's id for the credential that pulls private images
    storage: str  # the storage a new pool uses unless it names another

    @model_validator(mode="after")
    def _registry_supported(self) -> Provider:
        if self.kind in {"vast", "lium"} and self.registry:
            raise ValueError(
                f"{self.kind.title()} supports public images; registry credentials are not stored"
            )
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

    provider: str  # the name of the provider that supplies them
    gpu: str | None = None  # the provider's name for the GPU, such as "NVIDIA L4"
    gpu_count: int = Field(default=1, ge=1)
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    image: Image | None = None

    _parse_image = field_validator("image", mode="before")(_image)
    data_center: str | None = None  # RunPod data center id; Vast/Lium country code; None: anywhere


class GPU(Record):
    """An allocation of GPUs with at least this much CPU, memory and known disk."""

    id: str  # the provider's GPU type id, such as "NVIDIA L4"
    count: int = Field(default=1, ge=1)
    min_vcpus: int
    min_memory_gb: int
    disk_gb: int | None = None

    def __str__(self) -> str:
        # The host's CPU and memory are only known as the minimums asked for.
        name = self.id if self.count == 1 else f"{self.count} × {self.id}"
        return f"{name} with at least {self.min_vcpus} vCPUs, {self.min_memory_gb} GB"


class CPU(Record):
    """A CPU machine of one flavor, sized by vCPU count; memory follows from the flavor."""

    flavor: str  # the provider's CPU flavor id, such as "cpu3c"
    vcpus: int

    def __str__(self) -> str:
        return f"{self.flavor} {self.vcpus} vCPUs"


# What a provider was asked to rent, which may be larger than the template asked for.
Hardware = GPU | CPU


class Offer(Record):
    """What a provider would rent for a template now, at what price, and whether it has one."""

    hardware: Hardware | None  # None for a machine you own
    usd_per_hour: Decimal
    available: bool  # False only when the provider positively reports none


class Availability(Record):
    """An on-demand's offer as its provider answered at `observed_at`, beside its budget."""

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
        return "provider reports no available machines"
    ceiling = ondemand.max_usd_per_hour
    if ceiling is not None and offer.usd_per_hour > ceiling:
        return "price is above its ceiling"
    return None


class Target(Record):
    """Where a launch's runs may go: a pool's machines, an on-demand's rentals, or both."""

    pool: str | None = None
    rent: str | None = None


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

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


class Cohort(Record):
    name: str
    parallel: int  # rentals at once
    max_usd: Decimal | None  # shared budget for admitting rental allocations
    created_at: datetime


class Output(Record):
    name: str  # path below the run's output directory
    sha256: str
    size_bytes: int


class Rental(Record):
    """The paid window of a rented machine, independent of work and acquisition.

    A rented machine has exactly one, so the machine's name identifies it. Machine
    owns the request, provider receipt, refusal and deletion; RunAttempt owns allocation.
    """

    machine: str
    usd_per_hour: Decimal
    cap_usd: Decimal
    deadline: datetime
    billed_usd: Decimal = Decimal(0)
    billed_through: datetime | None = None
    kept: bool = False  # stays when idle until its deadline or explicit termination


class RentalCost(Record):
    """A rental's paid window and what it has cost so far (`Scheduler.rentals`)."""

    rental: Rental
    usd: Decimal  # its retained bills, plus an estimate of any time they do not cover yet
    estimated: bool  # part of `usd` is an estimate
    unbilled_usd: Decimal  # the estimated part: `usd` beyond its retained bills
    over_cap: bool  # its bills exceed its cap


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


class Tally(Record):
    """How far a job has got through work it counted, as the job last reported it.

    A clock says where a job stands; a tally also says how much there is, so
    it gives a fraction and, with a recent pace, a time left.
    """

    unit: str  # what is counted, plural: "matches"
    done: int = Field(ge=0)
    total: int = Field(ge=0)
    failed: int = Field(default=0, ge=0)
    # Recent pace, in `unit` per hour; None until the job has seen enough to say.
    per_hour: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    def __str__(self) -> str:
        said = f"{self.done}/{self.total} {self.unit}"
        if self.failed:
            said += f" ({self.failed} failed)"
        left = self.total - self.done - self.failed
        if self.per_hour is not None and left > 0:
            hours = left / self.per_hour
            minutes = max(1, round(hours * 60))
            when = f"{hours:.1f} hours" if hours >= 1 else f"{minutes} minute{'s' * (minutes > 1)}"
            said += f", {self.per_hour:.0f}/hour, ~{when} left"
        return said


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


class Status(Record):
    """Everything `status` says about a run, computed from the stored records."""

    run: str
    phase: Phase
    reason: str | None  # why a queued run is waiting
    outcome: Outcome | None
    cause: Cause | None
    until: Until | None  # the job's target
    clocks: dict[str, float]  # where the run last reported each of its clocks
    usd: Decimal
    attempts: int
    rental_ended: bool | None  # True only on a dated confirmation

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

    def __str__(self) -> str:
        if self.phase == "queued":
            head = f"queued ({self.reason})"
        elif self.phase == "done":
            head = f"done: {self.outcome}" + (f" ({self.cause})" if self.cause else "")
        else:
            head = self.phase
        clock = self.until.clock if self.until else next(iter(self.clocks), None)
        at = f", {clock} {number(self.clocks[clock])}" if clock in self.clocks else ""
        if self.tally is not None:
            # A finished job has no time left to give, whatever its last pace.
            shown = self.tally.model_copy(update={"per_hour": None}) if self.done else self.tally
            at += f", {shown}"
        shared = f", shared by {self.shared} runs" if self.shared > 1 else ""
        delivery = ""
        if self.delivery_age_seconds is not None:
            delivery = (
                f", no delivery confirmed for {self.delivery_age_seconds / 60:.1f} minutes"
                " (retained evidence; collection may be stale)"
            )
        return f"{head}{at}{shared}, {'~' if self.estimated else ''}${self.usd:.2f}{delivery}"


class CohortSpec(Record):
    """Named limits shared by a submitted group of runs."""

    name: str
    parallel: int = 1
    max_usd: Decimal | None = None


class QuotedMachine(Record):
    """One first-wave run's placement and its maximum admitted rental allocation.

    Cost assumes the quoted rate and timely deletion, and excludes external storage.
    Provider delays can exceed the admitted budget.
    """

    run: str  # the run's name
    machine: str | None  # an existing machine; None means one would be rented
    hardware: Hardware | None  # for a rental, what the provider would be asked for
    usd_per_hour: Decimal
    # Planned cost: an equal share of a new rental's whole window among the first-wave
    # runs placed on it, or this run's share of an existing machine.
    max_usd: Decimal
    slots: int = 1  # runs the machine takes at once
    alongside: tuple[str, ...] = ()  # the other first-wave runs placed on the same machine


class Quote(Record):
    """What a launch would take now, without taking it."""

    machines: tuple[QuotedMachine, ...]  # the first wave, as a launch would place it
    limit_usd: Decimal | None  # job/cohort rental admission budget; None: it cannot rent

    @property
    def max_usd(self) -> Decimal:
        """Maximum admitted first-wave rental allocation, not a provider-enforced bill cap."""
        return sum((machine.max_usd for machine in self.machines), Decimal(0))


class StartPhase(Record):
    """A recorded start milestone, or an explicitly unavailable timestamp.

    The database retains no provider reply timestamp. That milestone remains
    unavailable rather than borrowing the session readiness timestamp.
    """

    name: str
    at: datetime | None
    attempt: str | None = None
    evidence: str | None = None


class LaunchRequest(Record):
    run: str
    fingerprint: str
    requested_revision: str | None
    resolved_revision: str | None = None
    resume_version: int | None = None  # compare-and-set the existing run at admission


class Launch(Record):
    """Evidence of admission intent; never an executable or retryable run."""

    id: str
    requests: tuple[LaunchRequest, ...]
    created_at: datetime
    state: Literal["preparing", "admitting", "admitted", "failed"] = "preparing"


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


def __getattr__(name: str) -> object:
    if name == "Run":
        warnings.warn(
            "etude.launch.records.Run is now RunRequest; removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
        return RunRequest
    raise AttributeError(name)
