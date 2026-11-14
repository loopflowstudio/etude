"""A job's execution specification and the seed requests made from it.

These records describe Python and command jobs without importing a launcher,
training loop or evaluation runtime. Limits are launch metadata, not identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from etude.carriage import within
from etude.errors import LauncherError
from etude.identity import run_address


class Record(BaseModel):
    """Frozen execution records, with the launcher's historical validation policy."""

    model_config = ConfigDict(defer_build=True, frozen=True)


type Param = str | int | float | bool


def number(value: float) -> str:
    """A clock's value as written: whole numbers without a decimal point."""
    return str(int(value)) if float(value).is_integer() else str(value)


class Until(Record):
    """How far a job goes: a value on one of its clocks, absolute also when continuing.

    Written and stored as "clock=value", like `steps=26000`; a whole number is
    kept as an int, so `steps=26000.0` is the same target.
    """

    clock: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    value: int | float = Field(ge=0, allow_inf_nan=False)

    @classmethod
    def parse(cls, text: str) -> Until:
        """Read "clock=value"; raises LauncherError for anything else."""
        return cls.model_validate(text)

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        clock, equals, text = value.partition("=")
        try:
            parsed = float(text) if "." in text or "e" in text.lower() else int(text)
        except ValueError:
            parsed = None
        if not equals or not clock.replace("_", "").isalnum() or parsed is None or parsed < 0:
            raise LauncherError(
                f"{value!r} is not a target; write it as clock=value, like steps=26000"
            )
        return {"clock": clock, "value": parsed}

    @field_validator("value")
    @classmethod
    def _whole(cls, value: int | float) -> int | float:
        return int(value) if isinstance(value, float) and value.is_integer() else value

    @model_serializer
    def _as_text(self) -> str:
        return str(self)

    def __str__(self) -> str:
        return f"{self.clock}={number(self.value)}"


class Runtime(Record):
    """A job interpreter and the versions it must contain.

    `python` names a preinstalled interpreter. Leave it unset when the job's
    `Preparation.python` provides one; the pins are then checked against that.
    """

    python: str | None = Field(default=None, min_length=1)
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
    out: str | None = None  # seed this path within the run's out on its first attempt
    key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    run: str | None = None  # the launcher run whose machine can relay the file
    uri: str | None = None  # unsigned source locator; delivery may relay it elsewhere

    @field_validator("name")
    @classmethod
    def _plain(cls, name: str) -> str:
        if not within(name):
            raise ValueError(f"input name {name!r} must be a portable relative path")
        return name

    @model_validator(mode="after")
    def _within(self) -> Input:
        if self.out is not None and not within(self.out):
            raise ValueError("an input destination must be within the run's out")
        if self.run is not None and not within(self.key):
            raise ValueError(
                f"input key {self.key!r} must be a path within run {self.run}'s folder"
            )
        return self


class Preparation(Record):
    """Work a machine keeps and reuses while its key is unchanged, such as a job's venv.

    `command` runs by sh after the claim and before the runtime check, and
    writes `$ETUDE_PREPARED_DIR`. It sees exactly what its key covers: it runs
    in a copy of the source holding only `paths`, and finds only the inputs it
    names, at `$ETUDE_PREPARED_INPUTS/NAME`, so reading anything else fails the
    build rather than going stale. Its key is the content of `paths`, the
    command, the named inputs' digests, the job environment and the machine's
    platform; while that is unchanged a later attempt on the machine reuses
    the verified directory and the command does not run. `python` names the
    job's interpreter inside the directory, such as `.venv/bin/python`.
    """

    command: str = Field(min_length=1)
    paths: tuple[str, ...] = Field(min_length=1)  # source paths whose content decides the result
    inputs: tuple[str, ...] = ()  # names of the job's inputs the command reads
    python: str | None = None  # the job's interpreter, relative to $ETUDE_PREPARED_DIR

    @field_validator("paths")
    @classmethod
    def _within_source(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            if path.startswith("/") or {"", ".", ".."} & set(path.rstrip("/").split("/")):
                raise ValueError(f"prepare path {path!r} must be a path within the source")
        return paths

    @field_validator("python")
    @classmethod
    def _within_prepared(cls, python: str | None) -> str | None:
        if python is not None and (
            python.startswith("/") or {"", ".", ".."} & set(python.split("/"))
        ):
            raise ValueError(f"prepared python {python!r} must be a path within the directory")
        return python


class Variable(Record):
    """One environment variable a job is given."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: str


def _distinct(value: tuple[Input, ...]) -> tuple[Input, ...]:
    if len({given.name for given in value}) != len(value):
        raise ValueError("each input needs its own name")
    names = {given.name for given in value}
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:n]) in names for n in range(1, len(parts))):
            raise ValueError("an input cannot be both a file and a folder")
    return value


class SmokeForm(Record):
    """How to run a job tiny for `etude smoke`: overrides of the job's own fields.

    Each one given replaces the job's for the smoke run; any left unset keeps
    the job's. Etude never looks inside the work to shrink it.
    """

    command: str | None = None  # a command job's tiny command, such as "manabot train --smoke"
    config: str | None = None  # a typed job's tiny config text (`training(..., smoke=...)`)
    until: Until | None = None  # a tiny target


class Job(Record):
    """What to run, without a seed. Launching freezes it: `revision` becomes a commit.

    What makes a job itself is what decides the result: the command, the config, the
    source, its inputs, setup/cleanup, preparation, runtime and the target. Two launches that agree on those are the same job, so
    their runs can be found together. The name, the hours, the dollars and the
    attempts are how it is launched; they bound each run and can differ between runs.
    """

    setup: str | None = None
    cleanup: str | None = None
    stop_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cleanup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    runtime: Runtime | None = None
    prepare: Preparation | None = None
    # Run by sh in the source checkout; reads the ETUDE_* environment. A typed job's
    # (`etude.jobs`) is Etude's own; written by hand only for work that is not Python.
    command: str
    max_hours: float
    max_usd: Decimal | None = None  # required when the run may rent
    name: str = ""
    seeds: tuple[int, ...] = (0,)  # launch repetitions; excluded from job identity
    until: Until | None = None  # how far to go; absolute, also when continuing
    config: str | None = None  # the config file's text, handed back at ETUDE_CONFIG
    repo: str | None = None  # a local git repository; None runs in an empty directory
    revision: str | None = None  # None snapshots working files; a named revision is exact
    max_attempts: int = 3  # tries allowed, including infrastructure loss and planned stops
    inputs: tuple[Input, ...] = ()  # files at ETUDE_INPUTS_DIR/NAME when the job starts
    # Set in the job's environment; Etude's own ETUDE_* settings take precedence.
    environment: tuple[Variable, ...] = ()
    # The experiment its runs record under, handed to them as ETUDE_EXPERIMENT:
    # the experiment file `etude launch` read it from names it for its stem.
    experiment: str | None = None
    # How to run it tiny before spending (`etude smoke`). Not part of its identity:
    # changing it does not make a new job.
    smoke: SmokeForm | None = None

    _check_inputs = field_validator("inputs")(_distinct)

    @field_validator("seeds")
    @classmethod
    def _seeds(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("seeds must be nonempty and distinct")
        return value

    @model_validator(mode="after")
    def _prepared_from_inputs(self) -> Job:
        if self.prepare is not None:
            unknown = set(self.prepare.inputs) - {given.name for given in self.inputs}
            if unknown:
                raise ValueError(f"prepare reads inputs the job is not given: {sorted(unknown)}")
        prepared = self.prepare is not None and self.prepare.python is not None
        if self.runtime is not None and (self.runtime.python is None) != prepared:
            raise ValueError(
                "name the job interpreter once: in runtime.python, or in prepare.python"
            )
        return self

    def runs(self, *seeds: int, params: dict[str, Param] | None = None) -> list[RunRequest]:
        """This job once per given seed, or once per declared seed when none is given.

        Declared seeds use compact addresses, omitting a sole job or seed.
        Given seeds are a caller's own repetitions: they take ids at admission,
        so the same job and seed can be submitted again.
        """
        if seeds:
            return [RunRequest(job=self, seed=seed, params=dict(params or {})) for seed in seeds]
        return expand_runs([self], params=params)


def expand_runs(
    given: Sequence[Job | RunRequest], *, params: dict[str, Param] | None = None
) -> list[RunRequest]:
    """Expand declared seeds, omitting a sole job or seed from its address.

    Explicit requests keep their ids and admission-time repetitions. Selection
    happens after expansion, so selecting one seed never changes its address.
    """
    jobs: list[Job] = []
    for each in given:
        job = each if isinstance(each, Job) else each.job
        if job not in jobs:
            jobs.append(job)
    sole = len(jobs) == 1
    requests: list[RunRequest] = []
    for each in given:
        if isinstance(each, RunRequest):
            requests.append(each)
            continue
        name = each.experiment if sole and each.experiment else each.name
        for seed in each.seeds:
            address = None
            if name:
                local = name + (f"@{seed}" if len(each.seeds) > 1 else "")
                # Among several jobs, one named for its experiment keeps both levels.
                address = (
                    run_address(local, each.experiment)
                    if sole or not each.experiment
                    else f"{each.experiment}/{local}"
                )
            requests.append(RunRequest(job=each, seed=seed, params=dict(params or {}), id=address))
    return requests


def job_id(job: Job) -> str:
    """What runs of one job share: a digest of the fields that decide the result.

    Name, limits, attempts and the smoke form are left out on purpose. Order
    does not matter among environment variables, inputs or runtime packages.
    """
    identity: dict[str, object] = {
        "command": job.command,
        "config": job.config,
        "repo": job.repo,
        "revision": job.revision,
        "until": None if job.until is None else str(job.until),
    }
    optional = {"setup": job.setup, "cleanup": job.cleanup, "experiment": job.experiment}
    identity |= {name: value for name, value in optional.items() if value is not None}
    if job.runtime is not None:
        identity["runtime"] = job.runtime.model_dump(mode="json")
    if job.prepare is not None:
        identity["prepare"] = job.prepare.model_dump(mode="json")
    if job.environment:
        identity["environment"] = sorted([v.name, v.value] for v in job.environment)
    if job.inputs:
        identity["inputs"] = sorted(
            (given.model_dump(mode="json", exclude={"uri"}) for given in job.inputs),
            key=lambda i: i["name"],
        )
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


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


class SmokeStep(Record):
    """One part of an experiment file run in smoke form before its runs were submitted."""

    step: Literal["train", "eval", "report"]
    name: str  # what ran: the job's or tournament's name, or "report"
    # skipped: the file has no such step (no eval(state), not an experiment file).
    # not_smoked: the file has one that the smoke did not run (no smoke form, no checkpoints).
    outcome: Literal["passed", "not_smoked", "skipped"]
    seconds: float | None = None  # on the submitting machine; None when it did not run
    detail: str = ""  # what it made, such as "1 checkpoint", or why it did not run


class Smoke(Record):
    """What the smoke before a run's submission did; empty when none was asked for.

    Smoking at submission is opt-in (`--smoke`, `smoke=True`). Its steps are its
    own job's train step and its file's eval and report, never another job's. A
    failing smoke submits nothing.
    """

    steps: tuple[SmokeStep, ...] = ()

    @property
    def outcome(self) -> Literal["passed", "not_smoked"]:
        """Passed only when a step passed and none was `not_smoked`.

        `not_smoked` covers a submission without `--smoke`, a declared step the
        smoke did not run (a job with no smoke form, an eval with no
        checkpoints) and a smoke whose every step was skipped.
        """
        ran = any(each.outcome == "passed" for each in self.steps)
        missed = any(each.outcome == "not_smoked" for each in self.steps)
        return "passed" if ran and not missed else "not_smoked"

    def __str__(self) -> str:
        if self.outcome == "not_smoked":
            missed = [each for each in self.steps if each.outcome == "not_smoked"]
            if not missed:
                return "not smoked"
            return f"not smoked ({missed[0].step}: {missed[0].detail})"
        ran = [each for each in self.steps if each.outcome == "passed"]
        seconds = sum(each.seconds or 0 for each in ran)
        steps = ", ".join(dict.fromkeys(each.step for each in ran))
        return f"passed in {seconds:.1f} s ({steps})"


class RunRequest(Record):
    """A job and a seed: the thing that is launched."""

    job: Job
    seed: int | None = None
    # The values a runs() function was called with. Kept for reading; a job's
    # identity comes from what it runs, never from these.
    params: dict[str, Param] = Field(default_factory=dict)
    source: SourcePosition | None = None
    id: str | None = None  # allocated by the submitting client before git publication
    smoke: Smoke = Smoke()
    resume: bool = False  # explicitly take up an existing run; id is required
