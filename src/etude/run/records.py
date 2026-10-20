"""File contracts every run shares: its declaration, attempts and final state.

A run declares its clocks once. A clock is a coordinate the run advances: a
step counts something, a time is in seconds. A run that logs no rows, such as
an evaluation, declares none. Wall time is always recorded and never declared.

These models know nothing about research, launching or providers.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Reserved read-side axis: seconds since the Unix epoch, recorded on every row.
WALL_CLOCK = "wall_seconds"

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
    """Where a file lies and, when known, the digest and size it must have."""

    uri: str
    sha256: Digest | None = None
    size_bytes: Count | None = None
    version: str | None = None
    state: Literal["unverified", "unavailable", "verified"] = "unverified"
    media_type: str = "application/octet-stream"


class Clock(Record):
    """One declared x-axis option. `meaning` is the axis label a reader sees."""

    name: Name
    kind: Literal["step", "time"]
    meaning: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def not_reserved(self) -> Self:
        if self.name == WALL_CLOCK:
            raise ValueError("clock_name_reserved")
        return self


class Continuation(Record):
    """The parent run and the clock value this run continues from.

    Readers take the parent's rows up to and including `value` on `clock`, then
    this run's. The parent's own later rows are kept, not rewritten.
    """

    parent: SourceKey
    clock: Name
    value: float


class RunDeclaration(Record):
    """A run's identity. Re-declaring the same key resumes it; clocks must match."""

    schema_version: Literal[1] = 1
    source_key: SourceKey
    clocks: tuple[Clock, ...] = Field(max_length=16)
    # The launcher's Job: what this run shares with its siblings, which differ
    # only by `seed`. Statistics are taken over a job's runs and nothing else.
    job: str | None = Field(default=None, max_length=500)
    # What the job's author called it, for display. Never used to group runs.
    job_name: str | None = Field(default=None, max_length=500)
    seed: int | None = None
    # The launcher's cohort: the runs this one was launched with.
    cohort: str | None = Field(default=None, max_length=500)
    kind: str | None = Field(default=None, max_length=200)
    # The author's exact configuration bytes; never reserialized by Etude.
    config_json: str | None = Field(default=None, max_length=1024 * 1024)
    config_sha256: Digest | None = None
    continues: Continuation | None = None

    @model_validator(mode="after")
    def distinct_clocks(self) -> Self:
        names = [clock.name for clock in self.clocks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate_clock")
        if self.continues is not None and self.continues.clock not in names:
            raise ValueError("continuation_clock_undeclared")
        if (self.config_json is None) != (self.config_sha256 is None):
            raise ValueError("config_requires_digest")
        return self


class RunCounters(Record):
    """What the client could not deliver as chartable rows, reported by the client."""

    # Rows the bounded queue refused; they exist nowhere.
    dropped: Count = 0
    # Rows missing a declared clock; charted only against the clocks they gave.
    incomplete: Count = 0


class RunFile(Record):
    """`run.json`: everything needed to declare this run somewhere else later."""

    schema_version: Literal[1] = 1
    experiment: str
    namespace: str
    declaration: RunDeclaration


class RunState(Record):
    """`state.json`: written when the run finishes or fails."""

    state: Literal["finished", "failed"]
    counters: RunCounters
    # Retained evidence may not identify its writer; it cannot establish that
    # a later attempt ended. New completions always name the writer.
    writer: Writer | None = None


class Attempt(Record):
    """One process that wrote to a run: a launch, or a relaunch after a crash.

    A run outlives the hardware it runs on. Each start gets its own identity,
    which is also the `writer` in the names of the row files it wrote, so every
    stretch of a curve can be traced to the machine that produced it. One pod
    may appear under many runs, and one run under many pods.
    """

    schema_version: Literal[1] = 1
    writer: Writer
    started_ms: int
    host: str
    # The launcher's RunAttempt, when it started this process.
    attempt: str | None = None
    pod: str | None = None
    # Checkpoint coordinates supplied at start, independent of the first logged row.
    resumed_at: dict[str, float] = Field(default_factory=dict)
