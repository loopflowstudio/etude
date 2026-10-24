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

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from etude.launch.errors import LauncherError as LauncherError


class Record(BaseModel):
    """Shared validation and JSON policy for frozen launcher records."""

    model_config = ConfigDict(defer_build=True, frozen=True)


Outcome = Literal["succeeded", "failed", "cancelled", "over_budget", "out_of_time", "lost"]
# Why infrastructure lost an attempt. Only "lost" attempts carry a cause.
Cause = Literal[
    "capacity_refused",
    "setup_failed",
    "machine_lost_before_start",
    "machine_lost",
    "create_unconfirmed",
]
# Confirmed machine loss permits a new attempt; an uncertain start does not.
RETRYABLE: frozenset[str] = frozenset(
    {"capacity_refused", "setup_failed", "machine_lost_before_start", "machine_lost"}
)
# Which of a run's two limits ends it first on the rental it was given.
Limit = Literal["over_budget", "out_of_time"]
# A value a runs() function can be given from the command line.
Param = str | int | float | bool
Phase = Literal["queued", "renting", "setting_up", "running", "stopping", "done"]


def number(value: float) -> str:
    """A clock position as written: whole numbers without a decimal point."""
    return str(int(value)) if float(value).is_integer() else str(value)


def parse_target(text: str) -> tuple[str, float]:
    """Split "clock=value" into the clock's name and a position on it."""
    clock, equals, value = text.partition("=")
    try:
        position = float(value) if "." in value or "e" in value.lower() else int(value)
    except ValueError:
        position = None
    if not equals or not clock.replace("_", "").isalnum() or position is None or position < 0:
        raise LauncherError(f"{text!r} is not a target; write it as clock=value, like steps=26000")
    return clock, position


class Stale(LauncherError):
    """A record changed after it was read; the write did not happen."""


class Provider(Record):
    """An account that supplies machines on demand. It is set once and can be changed.

    Secrets are never stored here. The account key comes from the environment
    of whoever runs the launcher.
    """

    name: str  # what a machine template's `provider` names
    kind: Literal["runpod"]
    registry: str  # the provider's id for the credential that pulls private images
    storage: str  # the storage a new pool uses unless it names another


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
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    image: Image | None = None

    _parse_image = field_validator("image", mode="before")(_image)
    data_center: str | None = None  # the provider's name for the place; None: wherever it has one


class GPU(Record):
    """One GPU of a type, on a host with at least this much CPU and memory."""

    id: str  # the provider's GPU type id, such as "NVIDIA L4"
    min_vcpus: int
    min_memory_gb: int

    def __str__(self) -> str:
        # The host's CPU and memory are only known as the minimums asked for.
        return f"{self.id} with at least {self.min_vcpus} vCPUs, {self.min_memory_gb} GB"


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

    @property
    def rentable(self) -> bool:
        """Whether a launch could rent one now."""
        ceiling = self.max_usd_per_hour
        return (
            self.offer.available
            and (ceiling is None or self.offer.usd_per_hour <= ceiling)
            and self.in_use < self.max_machines
        )


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


class OnDemand(Record):
    """What to rent and the budget for renting it: a template, machines at once, a price ceiling.

    Every launch that names it with `rent` shares its limits. Each machine it
    rents records its name, and its Rental is the machine's paid window.
    """

    name: str
    template: MachineTemplate
    max_machines: int = Field(default=1, ge=0)  # rented machines in use at once
    max_usd_per_hour: Decimal | None = None  # above this price, no machine is rented
    startup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    delivery_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    log_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    setup_seconds: int = 900  # added to a rental's life when its dollar caps allow
    max_rental_seconds: int = 43_000  # under the 12 hour storage-session ceiling
    # Where sessions on its machines keep records. Fixed when it is added:
    # None then means the provider's storage at that moment.
    storage: str | None = None


# Where machines come from: owned ones in a pool, or rented ones from an on-demand.
Supply = Pool | OnDemand


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
    requested_at: datetime | None = None
    provider_id: str | None = None
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


class Runtime(Record):
    """A preinstalled job interpreter and the versions it must already contain."""

    python: str = Field(min_length=1)
    python_version: str | None = None  # exact version or a dotted prefix, e.g. 3.12
    packages: dict[str, str] = Field(default_factory=dict)  # exact distribution versions
    cuda: str | None = None  # PyTorch's compiled CUDA version, not host driver version


class Input(Record):
    """A file a job is given before it starts, checked by its digest.

    Either an object in the bucket of the machine's storage, or, with `run`, a
    file of that run's folder (`key` relative to it, such as
    `checkpoints/steps-100.pt`). The worker fetches a bucket object through its
    storage access. A run's file comes from the bucket where the run's folder
    was carried, else from this machine's folder for that run, relayed by the
    scheduler from the machine that last ran it. Bytes that do not match are
    refused before the job starts.
    """

    name: str  # where the job finds it: ETUDE_INPUTS_DIR/NAME
    key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    run: str | None = None  # the run whose folder holds the file

    @field_validator("name")
    @classmethod
    def _plain(cls, name: str) -> str:
        if name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"input name {name!r} must be a plain file name")
        return name

    @model_validator(mode="after")
    def _within(self) -> Input:
        parts = self.key.split("/")
        if self.run is not None and (self.key.startswith("/") or {"", ".", ".."} & set(parts)):
            raise ValueError(
                f"input key {self.key!r} must be a path within run {self.run}'s folder"
            )
        return self


class Variable(Record):
    """One environment variable a job is given."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str


def _distinct(value: tuple[Input, ...]) -> tuple[Input, ...]:
    if len({given.name for given in value}) != len(value):
        raise ValueError("each input needs its own name")
    return value


class Job(Record):
    """What to run, without a seed. Launching freezes it: `revision` becomes a commit.

    What makes a job itself is what decides the result: the command, the config, the
    source, its inputs, setup/cleanup, runtime and the target. Two launches that agree on those are the same job, so
    their runs can be found together. The name, the hours, the dollars and the
    attempts are how it is launched; they bound each run and can differ between runs.
    """

    setup: str | None = None
    cleanup: str | None = None
    stop_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cleanup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    runtime: Runtime | None = None
    # Run by sh in the source checkout; reads the ETUDE_* environment. A typed job's
    # (`etude.jobs`) is Etude's own; written by hand only for work that is not Python.
    command: str
    max_hours: float
    max_usd: Decimal | None = None  # required when the run may rent
    name: str = ""
    until: str | None = None  # how far to go, as "clock=value"; absolute, also when continuing
    config: str | None = None  # the config file's text, handed back at ETUDE_CONFIG
    repo: str | None = None  # a local git repository; None runs in an empty directory
    revision: str | None = None  # None snapshots working files; a named revision is exact
    max_attempts: int = 3  # tries allowed after confirmed infrastructure failure
    inputs: tuple[Input, ...] = ()  # files at ETUDE_INPUTS_DIR/NAME when the job starts
    # Set in the job's environment; Etude's own ETUDE_* settings take precedence.
    environment: tuple[Variable, ...] = ()

    _check_inputs = field_validator("inputs")(_distinct)

    def runs(self, *seeds: int, params: dict[str, Param] | None = None) -> list[Run]:
        """This job once per seed, each recording the parameters that led to it."""
        return [Run(job=self, seed=seed, params=dict(params or {})) for seed in seeds]


def job_identity(job: Job) -> dict[str, str | int | None]:
    """The fields that make a job itself. Name and limits are left out on purpose."""
    identity: dict[str, str | int | None] = {
        "command": job.command,
        "config": job.config,
        "repo": job.repo,
        "revision": job.revision,
        "until": job.until,
    }
    for name, value in (("setup", job.setup), ("cleanup", job.cleanup)):
        if value is not None:
            identity[name] = value
    if job.runtime is not None:
        identity["runtime"] = json.dumps(job.runtime.model_dump(mode="json"), sort_keys=True)
    if job.environment:
        identity["environment"] = json.dumps(
            sorted([v.name, v.value] for v in job.environment), sort_keys=True
        )
    if job.inputs:
        identity["inputs"] = json.dumps(
            sorted(
                (given.model_dump(mode="json") for given in job.inputs), key=lambda i: i["name"]
            ),
            sort_keys=True,
        )
    return identity


class SourcePosition(Record):
    """Git history observed at submission; absent on runs recorded by older clients."""

    commit: str
    origin: str | None
    branch: str | None
    base_branch: str | None
    base_commit: str | None
    base_date: str | None
    base_subject: str | None
    ahead: int | None
    release: str | None
    snapshot: bool = False


class Run(Record):
    """A job and a seed: the thing that is launched."""

    job: Job
    seed: int | None = None
    # The values a runs() function was called with. Kept for reading; a job's
    # identity comes from what it runs, never from these.
    params: dict[str, Param] = Field(default_factory=dict)
    source: SourcePosition | None = None
    id: str | None = None  # allocated by the submitting client before git publication


class RunRecord(Record):
    id: str
    job: str  # digest of what decides the result; runs of one job share it
    spec: Job  # as launched, with this run's name and limits
    seed: int | None
    pool: str | None  # the pool whose machines it may use
    rent: str | None  # the on-demand it may rent from, after the pool's free machines
    cohort: str | None
    created_at: datetime
    cancel_requested_at: datetime | None
    version: int
    autorun: bool = False
    queued: bool = False  # historical submission metadata; never affects scheduling
    params: dict[str, Param] = Field(default_factory=dict)  # as on the Run that was sent
    source: SourcePosition | None = None

    @property
    def name(self) -> str:
        parts = [self.spec.name] if self.spec.name else []
        return "-".join(parts + ([] if self.seed is None else [f"seed{self.seed}"])) or self.id


class Cohort(Record):
    name: str
    parallel: int  # rentals at once
    max_usd: Decimal | None  # ceiling on what its rentals can cost in total
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
    claimed_at: datetime | None
    startup_deadline: datetime | None = None
    receipt: str | None
    started_at: datetime | None
    refused_at: datetime | None
    ended_at: datetime | None
    version: int
    autorun: bool = False
    hardware: Hardware | None = None


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


class Status(Record):
    """Everything `status` says about a run, computed from the stored records."""

    run: str
    phase: Phase
    reason: str | None  # why a queued run is waiting
    outcome: Outcome | None
    cause: Cause | None
    until: str | None  # the job's target, as "clock=value"
    clocks: dict[str, float]  # where the run last reported each of its clocks
    usd: Decimal
    attempts: int
    rental_ended: bool | None  # True only on a dated confirmation

    machine: str | None = None
    hardware: Hardware | None = None
    estimated: bool = False

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
        clock = parse_target(self.until)[0] if self.until else next(iter(self.clocks), None)
        at = f", {clock} {number(self.clocks[clock])}" if clock in self.clocks else ""
        return f"{head}{at}, {'~' if self.estimated else ''}${self.usd:.2f}"


class CohortSpec(Record):
    """Named limits shared by a submitted group of runs."""

    name: str
    parallel: int = 1
    max_usd: Decimal | None = None


class QuotedMachine(Record):
    """Where one run of a launch's first wave would start, and the most it could cost."""

    run: str  # the run's name
    machine: str | None  # an existing machine; None means one would be rented
    hardware: Hardware | None  # for a rental, what the provider would be asked for
    usd_per_hour: Decimal
    max_usd: Decimal  # a new rental's whole window; an existing machine's share for the run


class Quote(Record):
    """What a launch would take now, without taking it."""

    machines: tuple[QuotedMachine, ...]  # the first wave, as a launch would place it
    limit_usd: Decimal | None  # job and cohort caps on the whole launch; None: it cannot rent

    @property
    def max_usd(self) -> Decimal:
        """The most the first wave's machines can cost."""
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
