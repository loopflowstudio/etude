"""File contracts every run shares: its declaration, writers and outcome.

A run records clocks explicitly or as they are first used. A clock is a
coordinate the run advances: a step counts something, a time is a duration
(seconds on an author's clocks).
A run that logs no rows, such as an evaluation, declares none. Wall time is
always recorded and never declared, and so are Etude's two clocks in hours
(`BUILT_IN_CLOCKS`), which a training run gives on every row and checkpoint.

RunRecord holds the shared identity. Its optional submission belongs to the
launcher database. Declarations omit it from run.json; providers are not represented here.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, model_validator

from etude.cache import Content
from etude.carriage import within
from etude.spec import Job, Param, Smoke, SourcePosition

Name = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^\S+$")]
Writer = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(ge=0)]


class Record(BaseModel):
    model_config = ConfigDict(defer_build=True, frozen=True, extra="forbid", allow_inf_nan=False)


class SourceKey(Record):
    """An identity as its author named it, scoped so two authors cannot collide."""

    namespace: str = Field(min_length=1, max_length=100, pattern=r"^[^:]+$")
    kind: str = Field(min_length=1, max_length=100, pattern=r"^[^:]+$")
    source_id: str = Field(min_length=1, max_length=1000)

    def key(self) -> str:
        return f"{self.namespace}:{self.kind}:{self.source_id}"


class ArtifactRef(Record):
    """A file or checkpoint folder, with its digest, size and individually verified members."""

    uri: str
    sha256: Digest | None = None
    size_bytes: Count | None = None
    version: str | None = None
    state: Literal["unverified", "unavailable", "verified"] = "unverified"
    media_type: str = "application/octet-stream"
    # Folder checkpoints carry a manifest of individually verifiable objects.
    members: dict[str, Content] = Field(
        default_factory=dict, exclude_if=lambda members: not members
    )

    @model_validator(mode="after")
    def _portable_members(self) -> Self:
        for key in self.members:
            if not within(key):
                raise ValueError("artifact members must be portable relative paths")
        return self


class Clock(Record):
    """One declared x-axis option. `meaning` is the axis label a reader sees."""

    name: Name
    kind: Literal["step", "time"]
    meaning: str = Field(min_length=1, max_length=200)


# Etude's clocks, which no author declares. `running_hours` is the learner line's
# running time: absolute across resume and continuation like a step clock, it
# counts from `track(...)` and drops what a crashed attempt ran past its last
# checkpoint. `hours_since_start` is wall time since the line's first attempt
# began, gaps included.
RUNNING_HOURS = "running_hours"
HOURS_SINCE_START = "hours_since_start"
BUILT_IN_CLOCKS = (
    Clock(name=RUNNING_HOURS, kind="time", meaning="hours the learner line has run"),
    Clock(
        name=HOURS_SINCE_START, kind="time", meaning="hours since the line's first attempt started"
    ),
)
BUILT_IN_NAMES = (RUNNING_HOURS, HOURS_SINCE_START)


class Continuation(Record):
    """The parent run and the clock value this run continues from.

    Readers take the parent's rows up to and including `value` on `clock`, then
    this run's. The parent's own later rows are kept, not rewritten.
    """

    parent: SourceKey
    clock: Name
    value: float


class RunSubmission(Record):
    """Submission and scheduling facts owned by the launcher database."""

    spec: Job
    pool: str | None = None
    rent: str | None = None
    created_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    version: int = 1
    autorun: bool = False
    queued: bool = False
    resume_after: int = 0
    params: dict[str, Param] = Field(default_factory=dict)
    source: SourcePosition | None = None
    smoke: Smoke = Smoke()


class RunRecord(Record):
    """A run's stored identity, optionally with its launcher submission metadata.

    Standalone writers have no execution specification. A launched run's config
    lives in its specification; its digest is derived from those exact bytes.
    Historical nested run.json declarations decode here and are never rewritten.
    """

    schema_version: Literal[2] = 2
    id: str
    experiment: str = ""
    namespace: str = "local"
    source_kind: str = "run"
    clocks: tuple[Clock, ...] = Field(default=(), max_length=16)
    job: str | None = Field(default=None, max_length=500)
    job_name: str | None = Field(default=None, max_length=500)
    seed: int | None = None
    cohort: str | None = Field(default=None, max_length=500)
    kind: str | None = Field(default=None, max_length=200)
    config: str | None = Field(default=None, max_length=1024 * 1024)
    continues: Continuation | None = None
    start: str | None = None
    submission: RunSubmission | None = Field(
        default=None, exclude_if=lambda submission: submission is None
    )

    @model_validator(mode="before")
    @classmethod
    def _historical(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        declaration = data.pop("declaration", None)
        if isinstance(declaration, dict):
            data.update(declaration)
            if "source_key" not in data or "experiment" not in data:
                raise ValueError("historical run declaration requires source_key and experiment")
            source = SourceKey.model_validate(data.pop("source_key"))
            data.update(
                id=f"{data['experiment']}/{source.source_id}",
                source_kind=source.kind,
                namespace=source.namespace,
                schema_version=2,
            )
            data["config"] = data.pop("config_json", None)
            data.pop("config_sha256", None)
        # Stored flat launcher records and default-filled run.json files predate
        # the submission boundary. Only a real specification establishes one.
        submitted = {key: data.pop(key) for key in RunSubmission.model_fields if key in data}
        if submitted.get("spec") is not None:
            data["submission"] = submitted
        return data

    @model_validator(mode="after")
    def _identity(self) -> Self:
        names = [clock.name for clock in self.clocks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate_clock")
        if set(BUILT_IN_NAMES) & set(names):
            raise ValueError("clock_name_reserved")
        if self.continues is not None and self.continues.clock not in (*names, *BUILT_IN_NAMES):
            raise ValueError("continuation_clock_undeclared")
        if self.submission is not None and self.config is not None:
            raise ValueError("launched_config_belongs_to_spec")
        return self

    @property
    def source_key(self) -> SourceKey:
        return SourceKey(
            namespace=self.namespace,
            kind=self.source_kind,
            source_id=self.id.removeprefix(self.experiment + "/"),
        )

    @property
    def config_json(self) -> str | None:
        return self.submission.spec.config if self.submission is not None else self.config

    @property
    def config_sha256(self) -> str | None:
        text = self.config_json
        return sha256(text.encode()).hexdigest() if text is not None else None

    @property
    def launch(self) -> RunSubmission:
        """Required launcher facts; standalone declarations have none."""
        if self.submission is None:
            raise ValueError(f"run {self.id!r} has no launcher specification")
        return self.submission

    @property
    def name(self) -> str:
        return self.source_key.source_id


class RunCounters(Record):
    """What the client could not deliver as chartable rows, reported by the client."""

    # Rows the bounded queue refused; they exist nowhere.
    dropped: Count = 0
    # Rows missing a declared clock; charted only against the clocks they gave.
    incomplete: Count = 0


class RunOutcome(Record):
    """`outcome.json`: how a tracking attempt ended, including a planned stop."""

    state: Literal["finished", "failed", "stopped"]
    counters: RunCounters
    # Retained evidence may not identify its writer; it cannot establish that
    # a later attempt ended. New completions always name the writer.
    writer: Writer | None = None


class RunWriter(Record):
    """The lifetime of one tracking context, with its own row-replacement boundary.

    Many writers may share a launcher allocation, or write without one.
    Historical writer files retain their fields and schema version.
    """

    schema_version: Literal[1] = 1
    writer: Writer
    run: str | None = None
    started_ms: int
    host: str
    # The launcher's RunAttempt, when it started this process.
    attempt: str | None = None
    pod: str | None = None
    commit: str | None = None
    until: str | None = None  # absolute launch target, retained across manual resumes
    # Historical records supplied checkpoint coordinates at start. New writers
    # fill this from their first durable row and supersede that coordinate too.
    resumed_at: dict[str, float] = Field(default_factory=dict)
    first_row: bool = False


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


class RunProgress(Record):
    """The job-owned progress report shared by training, evaluation and the worker."""

    clocks: dict[str, FiniteFloat] = Field(default_factory=dict)
    tally: Tally | None = None
    writers: tuple[RunWriter, ...] = ()
