"""File contracts for runs, clocks, logged rows and evaluation records.

A run declares its clocks once. A clock is a coordinate the run advances: a
step counts something, a time is in seconds. Every logged row gives a value for
every declared clock; wall time is always recorded and never declared. There is
no run-wide counter and no ordering rule: a repeated coordinate is kept, and
readers take the latest write.

These models know nothing about research, launching or providers.
"""

from __future__ import annotations

import math
import re
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


def Step(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock that counts something, such as updates or learner transitions."""
    return Clock(name=name, kind="step", meaning=meaning)


def Time(name: str, meaning: str) -> Clock:  # noqa: N802 - reads as a declaration
    """A clock in seconds, such as active training time. Not wall time."""
    return Clock(name=name, kind="time", meaning=meaning)


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


# Two-sided 95% Student's t quantiles for 1 to 30 degrees of freedom.
_T95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def interval95(mean: float, std: float, n: int) -> tuple[float, float] | None:
    """The 95% interval for a mean of `n` seeds with sample deviation `std`.

    One seed has no interval. The seeds run are a sample of those that could
    have been, so the deviation divides by n - 1 and the width uses Student's t.
    """
    if n < 2:
        return None
    half = (_T95[n - 2] if n - 2 < len(_T95) else 1.960) * std / math.sqrt(n)
    return mean - half, mean + half


class Estimate(Record):
    """A statistic with its interval, as computed and described by the author."""

    name: Name
    mean: float
    lower: float
    upper: float
    n: Count
    method: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.lower <= self.mean <= self.upper:
            raise ValueError("estimate_interval_unordered")
        return self


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


class CheckpointRecord(Record):
    """A recorded model: what a run saved and where every one of its clocks stood.

    `ordinal` counts a run's checkpoints from one and is itself a clock: the
    axis evaluations and continuation are naturally read against. A checkpoint
    is matched to evaluations by its artifact's digest, or its URI when the
    author gave no digest. When Etude stored the file, the artifact carries its
    digest and size and the file sits beside this record, in the author's
    directory or bucket. Everything here is fixed except `tags`.
    """

    schema_version: Literal[1] = 1
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    at: dict[Name, float]
    wall_ms: int
    # The author's labels, such as "best". They may be added and removed later.
    tags: tuple[Name, ...] = Field(default=(), max_length=32)


class DefinitionRef(Record):
    name: Name
    sha256: Digest


class EvaluationDefinition(Record):
    """How an evaluation is done, as a file in the author's repository.

    The name and the digest of the exact text are its identity: editing the
    file makes a new version without anyone numbering it. Etude describes and
    compares by it; it does not run it. Shared across the organisation.
    """

    name: Name
    text: str = Field(max_length=256 * 1024)
    sha256: Digest

    def ref(self) -> DefinitionRef:
        return DefinitionRef(name=self.name, sha256=self.sha256)


class EvaluationRecord(Record):
    """One application of a definition to one model: a peer of metrics.

    The subject is a checkpoint. If a run recorded that checkpoint, readers
    learn the run and clock coordinate from the match; if none did, the record
    stands alone. Any run may write it, during training or long after. It grows
    while running and then settles or fails; a failed record is kept.
    `revision` increases with every change, so a late or repeated delivery can
    never move a record backwards.
    """

    schema_version: Literal[1] = 1
    key: Name
    definition: DefinitionRef
    subject: ArtifactRef
    revision: Count
    state: Literal["running", "settled", "failed"]
    episodes: Count = 0
    expected_episodes: Count | None = None
    # Where the episodes themselves are stored, relative to the run.
    episodes_uri: str | None = None
    estimates: tuple[Estimate, ...] = ()
    failure: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.state == "failed") != (self.failure is not None):
            raise ValueError("failure_requires_failed_state")
        if len({estimate.name for estimate in self.estimates}) != len(self.estimates):
            raise ValueError("duplicate_estimate")
        return self


def checkpoint_name(at: dict[str, float]) -> str:
    """A checkpoint's file name under its run: where the first declared clock stood.

    Safe in any bucket and shell: the clock's name keeps letters, digits, `.`,
    `_` and `-`, anything else becomes `_`, and the value is a plain number. A
    second checkpoint at the same coordinate, such as one saved again after a
    resume, takes the same name. The exact clocks are in the record itself.
    """
    clock, value = next(iter(at.items()))
    number = str(int(value)) if float(value).is_integer() else repr(float(value))
    return re.sub(r"[^A-Za-z0-9._-]", "_", clock) + "-" + number


def subject_identity(artifact: ArtifactRef) -> str:
    """What makes two references the same model: the digest, else the URI."""
    return artifact.sha256 or artifact.uri
