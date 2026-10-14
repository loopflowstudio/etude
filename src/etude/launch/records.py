"""Launcher records: what a caller passes, what is stored, and what a caller reads back.

A pool is a named set of machines with a queue. Machine describes an added or
rented computer; Rental describes only its paid window. RunAttempt allocates
a run to either kind of machine. Session is internal: one start of Etude's
process on a machine, and its journal folder.
Jobs describe commands and limits; cohorts group runs.

Units: a clock is any named count of progress a job keeps, such as steps,
agent_steps or wall_time_ms. Positions on a clock are absolute. Hours and US
dollars bound machines.
Every time is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
    Its keys come from the environment of whoever runs the launcher.
    """

    name: str
    bucket: str
    prefix: str  # the folder in that bucket its sessions live under
    region: str  # the bucket's region


class MachineTemplate(Record):
    """What a pool's on-demand machines are made from: which provider, what kind, and where."""

    provider: str  # the name of the provider that supplies them
    gpu: str | None = None  # the provider's name for the GPU, such as "NVIDIA L4"
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    max_usd_per_hour: Decimal | None = None  # above this price, no machine is taken
    image: str | None = None  # the image the machines boot, pinned by digest
    data_center: str | None = None  # the provider's name for the place; None: wherever it has one


class Pool(Record):
    """A named set of machines: ones you own, ones made on demand, or both.

    Owned machines are used first, since they cost nothing more. Machine size
    belongs to the pool, not the job.
    """

    name: str
    on_demand_template: MachineTemplate | None = None  # None: the pool is only what you own
    max_on_demand: int = 0  # on-demand machines in use at once
    setup_seconds: int = 900  # added to a rental's life when its dollar caps allow
    max_rental_seconds: int = 43_000  # under the 12 hour storage-session ceiling
    off: bool = False  # takes no new runs; what it already holds finishes and stays readable
    # Where sessions on its rented machines keep records. Fixed when the pool is added:
    # None then means the provider's storage at that moment.
    storage: str | None = None


class Machine(Record):
    """One machine in a pool: a box you own, or one made on demand from the pool's template.

    Kind describes how it was acquired: this computer, an added SSH host, or
    rented. Provider names the account separately. What the provider said about
    making and deleting a rented machine is recorded on its one session and read
    here; only rented machines may be deleted.
    Unfinished RunAttempts decide whether it is busy.
    """

    name: str
    pool: str
    kind: Literal["local", "ssh", "rented"]
    provider: str | None = None  # account that supplied a rented machine
    requested_at: datetime | None = None
    provider_id: str | None = None
    refused_at: datetime | None = None
    address: str | None = None  # SSH address for an added host; never a provider receipt
    storage: str | None = None
    deleted_at: datetime | None = None  # only an on-demand machine is ever deleted


class Checkpoint(Record):
    """One output file of a parent run, named by digest, and where each clock stood."""

    run: str
    name: str
    sha256: str
    clocks: dict[str, float]


class Job(Record):
    """What to run, without a seed. Launching freezes it: `revision` becomes a commit.

    What makes a job itself is what decides the result: the command, the config, the
    source and the target. Two launches that agree on those are the same job, so
    their runs can be found together. The name, the hours, the dollars and the
    attempts are how it is launched; they bound each run and can differ between runs.
    """

    command: str  # run by sh in the source checkout; reads the ETUDE_* environment
    max_hours: float
    max_usd: Decimal | None = None  # required when its pool can spend money
    name: str = ""
    until: str | None = None  # how far to go, as "clock=value"; absolute, also when continuing
    config: str | None = None  # the config file's text, handed back at ETUDE_CONFIG
    repo: str | None = None  # a local git repository; None runs in an empty directory
    revision: str | None = None  # None snapshots working files; a named revision is exact
    max_attempts: int = 3  # tries allowed after confirmed infrastructure failure

    def runs(self, *seeds: int, params: dict[str, Param] | None = None) -> list[Run]:
        """This job once per seed, each recording the parameters that led to it."""
        return [Run(job=self, seed=seed, params=dict(params or {})) for seed in seeds]


def job_identity(job: Job) -> dict[str, str | int | None]:
    """The fields that make a job itself. Name and limits are left out on purpose."""
    return {
        "command": job.command,
        "config": job.config,
        "repo": job.repo,
        "revision": job.revision,
        "until": job.until,
    }


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
    checkpoint: Checkpoint | None = None  # set by continue_from
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
    checkpoint: Checkpoint | None
    pool: str
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
    pool: str
    machine: str
    planned_at: datetime
    claimed_at: datetime | None
    receipt: str | None
    started_at: datetime | None
    refused_at: datetime | None
    ended_at: datetime | None
    version: int
    autorun: bool = False


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
    checkpoint: str | None  # the output the run named as its checkpoint
    outputs: tuple[Output, ...]
    exit_code: int | None
    version: int
    # When it was placed. Its share of the cost starts no earlier.
    planned_at: datetime | None = None
    added: bool = False  # placed on a session that already existed
    checkpoint_clocks: dict[str, float] = Field(default_factory=dict)  # frozen with checkpoint


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


class StartPhase(Record):
    """A recorded start milestone, or an explicitly unavailable timestamp.

    The database retains no provider reply timestamp. That milestone remains
    unavailable rather than borrowing the session readiness timestamp.
    """

    name: str
    at: datetime | None
    attempt: str | None = None
    evidence: str | None = None
