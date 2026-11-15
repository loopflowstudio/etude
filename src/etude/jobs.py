"""The author-facing experiment model: typed jobs and the state declarations read.

`TrainingJob`, `EvalJob` and `ExperimentState` are defined here. Typed jobs
lower to `spec.Job` for storage, transport and identity, deriving their command
and JSON config afresh after a copy. State is observed, never stored.
The worker calls `_hold` only when executing an evaluation; worker startup
imports neither this module nor the state reader in `etude.experiment`.
"""

from __future__ import annotations

import inspect
import os
import runpy
import shlex
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fnmatch import fnmatchcase
from functools import cached_property
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, SupportsIndex, TypedDict, cast, overload

from pydantic import BaseModel, Field, JsonValue, ValidationError, field_validator, model_validator

from etude.cache import Artifact, Content, ContentMismatch, Corrupt
from etude.call import Function, changed_config, config_type
from etude.carriage import CHECKPOINTS, OUT_PRESENT, checkpoint_file_name, checkpoint_key
from etude.checkpoints import CheckpointId, CheckpointRecord, CheckpointRef, checkpoint_id
from etude.errors import EtudeError, LauncherError
from etude.eval.match import EvalResult
from etude.eval.records import (
    Benchmark,
    CheckpointPlayer,
    Line,
    Match,
    Player,
    RoundRobin,
    TournamentRun,
    assemble,
    digest,
)
from etude.eval.run import Result, Tables
from etude.eval.tournament import evaluating
from etude.identity import stored_owner
from etude.out import MEMBERSHIP, OutFile, OutFiles, OutMember
from etude.records import ArtifactRef, Record, RunRecord, SourceKey
from etude.run import as_storage, located_runs
from etude.spec import (
    Input,
    Job,
    Preparation,
    RunRequest,
    Runtime,
    SmokeForm,
    Until,
    Variable,
    number,
)
from etude.storage import Storage, place_artifact
from etude.train.read import read
from etude.train.records import ParentRun


class Checkpoints(tuple[CheckpointRecord, ...]):
    """A training run's checkpoints in the order it saved them; a tuple, so slices work."""

    def every(self, n: int) -> Checkpoints:
        """Every nth checkpoint by ordinal: the nth, the 2nth and so on."""
        if n < 1:
            raise EtudeError("every_needs_a_positive_n", 422)
        return Checkpoints(each for each in self if each.ordinal % n == 0)

    def at(self, clock: str, *values: float) -> Checkpoints:
        """The checkpoints saved where `clock` stood at one of `values`."""
        wanted = set(values)
        return Checkpoints(each for each in self if each.at.get(clock) in wanted)

    def tagged(self, tag: str) -> Checkpoints:
        return Checkpoints(each for each in self if tag in each.tags)

    @property
    def last(self) -> CheckpointRecord | None:
        return self[-1] if self else None


class OutMembers(tuple[OutMember, ...]):
    """A run's stored out files, in membership order; selections remain OutMembers."""

    @overload
    def __getitem__(self, index: SupportsIndex) -> OutMember: ...

    @overload
    def __getitem__(self, index: slice) -> OutMembers: ...

    def __getitem__(self, index: SupportsIndex | slice) -> OutMember | OutMembers:
        if isinstance(index, slice):
            return OutMembers(super().__getitem__(index))
        return super().__getitem__(index)

    def matching(self, pattern: str) -> OutMembers:
        """Match paths case-sensitively; `*` also matches across `/`."""
        return OutMembers(member for member in self if fnmatchcase(member.path, pattern))

    def file(self, path: str) -> OutMember:
        for member in self:
            if member.path == path:
                return member
        raise EtudeError("out_file_not_found", 404, detail=path)

    def inputs(self, under: str) -> tuple[Input, ...]:
        """Give each member at `under/PATH`, preserving its path within out."""
        return tuple(member.input(under + "/" + member.path) for member in self)


@dataclass(frozen=True)
class TrainingRunSummary:
    name: str
    experiment: str
    # Its job as the report names it (`etude.train.report.job_names`): the launch name, with
    # its config digest when that name ran several configs ("ppo 1a2b3c").
    job: str
    # The name it was launched under, which covers every config launched under it.
    launch: str
    # Its learner: the first run of the chain of continuations that led to it.
    line: str
    state: Literal["queued", "running", "stopped", "finished", "failed"]
    checkpoints: Checkpoints
    # The seed it declared, if any.
    seed: int | None = None
    _read_out: Callable[[], tuple[OutMember, ...]] = field(default=tuple, repr=False, compare=False)

    @cached_property
    def out(self) -> OutMembers:
        return OutMembers(self._read_out())

    @property
    def ended(self) -> bool:
        return self.state in {"finished", "failed"}

    @property
    def final(self) -> CheckpointRecord | None:
        """The last checkpoint once the run finished; None while it may save more."""
        return self.checkpoints.last if self.state == "finished" else None


@dataclass(frozen=True)
class EvalRunSummary:
    name: str
    state: Literal["queued", "running", "stopped", "finished", "failed"]
    matches: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperimentState:
    """An experiment's training runs under a source, as a pass found it. Never stored."""

    experiment: str
    runs: tuple[TrainingRunSummary, ...] = ()
    eval_runs: tuple[EvalRunSummary, ...] = ()
    committed_usd: Decimal = Decimal(0)

    def run(self, name: str) -> TrainingRunSummary:
        """The training run named `name`; raises `EtudeError("run_not_found")` without one."""
        for each in self.runs:
            if each.name == name.removeprefix(self.experiment + "/"):
                return each
        raise EtudeError("run_not_found", 404) from ValueError(name)

    @property
    def lines(self) -> tuple[Line, ...]:
        """Each learner line, as relative seats find checkpoints in it: seeded as its first
        run declared, and ended once every run of it has."""
        found: dict[str, list[TrainingRunSummary]] = {}
        for run in self.runs:
            found.setdefault(run.line, []).append(run)
        made = (
            Line(
                name=name,
                seed=next((run.seed for run in runs if run.name == name), runs[0].seed),
                job=runs[0].job,
                launch=runs[0].launch,
                ended=all(run.ended for run in runs),
                checkpoints=tuple(c.ref for run in runs for c in run.checkpoints),
            )
            for name, runs in found.items()
        )
        return tuple(sorted(made, key=lambda line: line.order))

    def checkpoint(self, key: CheckpointId) -> CheckpointRecord | None:
        for each in self.runs:
            for saved in each.checkpoints:
                if saved.key == key:
                    return saved
        return None


def checkpoint_inputs(
    matches: Iterable[Match],
    state: ExperimentState,
    checkpoints: Sequence[CheckpointRecord] = (),
) -> dict[str, CheckpointRecord | CheckpointRef]:
    """The checkpoints seated, keeping producer records where available.

    A seat's artifact locates its bytes even when another experiment saved it.
    Missing verification metadata fails with the checkpoint's key.
    """
    declared = {each.key: each for each in checkpoints}
    found: dict[str, CheckpointRecord | CheckpointRef] = {}
    for match in matches:
        for player in match.players:
            if not isinstance(player, CheckpointPlayer) or player.checkpoint.key in found:
                continue
            ref = player.checkpoint
            record = declared.get(ref.key) or state.checkpoint(ref.key)
            # Compact checkpoint keys can coincide across experiments. The
            # seated artifact, not this experiment's matching spelling, owns the bytes.
            saved = record if record is not None and record.artifact == ref.artifact else ref
            if saved.artifact.sha256 is None or saved.artifact.size_bytes is None:
                raise EtudeError("checkpoint_not_stored", 404, detail=ref.key)
            found[ref.key] = saved
    return found


def fetch_checkpoints(
    matches: Sequence[Match],
    state: ExperimentState,
    checkpoints: Sequence[CheckpointRecord],
    folder: Path,
) -> dict[str, str]:
    """Verify seated artifacts into a caller-owned folder and return their local URIs by key."""
    here: dict[str, str] = {}
    for key, checkpoint in checkpoint_inputs(matches, state, checkpoints).items():
        name = digest(key)[:16]
        for file in checkpoint_inputs_for(checkpoint, name):
            assert file.uri is not None
            try:
                place_artifact(
                    Artifact(location=file.uri, sha256=file.sha256, size_bytes=file.size_bytes),
                    folder / file.name,
                )
            except FileNotFoundError:
                raise EtudeError("checkpoint_not_stored", 404, detail=key) from None
            except (ContentMismatch, Corrupt):
                raise EtudeError("checkpoint_content_mismatch", 409, detail=key) from None
        here[key] = (folder / name).as_uri()
    return here


class JobOptions(TypedDict, total=False):
    """How a typed job is launched: every `Job` field except those it derives.

    It derives the command and config, the experiment (an evaluation's own,
    else the one `etude launch` reads from the experiment file's name) and the
    smoke form: a training job's `smoke` config; an evaluation's is Etude's.
    """

    max_hours: float | None
    max_usd: Decimal | None
    name: str
    seeds: tuple[int, ...]
    until: Until | None
    # A local git repository; None runs in an empty directory. Omitted: the
    # checkout the function's file is in.
    repo: str | None
    revision: str | None
    inputs: tuple[Input, ...]
    environment: tuple[Variable, ...]
    runtime: Runtime | None
    prepare: Preparation | None
    setup: str | None
    cleanup: str | None
    stop_seconds: float | None
    cleanup_seconds: float | None
    max_attempts: int


# The job input that carries a benchmark's `CheckpointRecord`, before a member's suffix.
_BENCHMARK_CHECKPOINT = "checkpoint"


class Evaluation(Record):
    """An evaluation job's config: what `_hold` holds, with what, on the machine."""

    tournament: Benchmark | RoundRobin
    # None in a job an experiment's eval returns: `etude eval` names it for the file.
    experiment: str | None
    play: Function
    # A benchmark's checkpoint; released single-file inputs remain readable.
    checkpoint: CheckpointRecord | Input | None = None
    # Checkpoints an experiment's eval chose to hold the benchmark for.
    checkpoints: tuple[CheckpointRecord, ...] = ()
    # A round robin's entrants.
    entrants: tuple[Player, ...] = ()
    workers: int = Field(default=1, ge=1)
    # The matches a pass found new (`etude.controller.advance`), and the
    # job input holding each checkpoint they seat, by its key encoded.
    matches: tuple[Match, ...] | None = None
    files: dict[str, str] = {}
    # The experiment's training lines that pass saw, where relative seats find checkpoints.
    lines: tuple[Line, ...] = ()


class TrainingJob[C: BaseModel](Job):
    """One training function and config, repeated over the declared seeds."""

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}
    name: str = Field(min_length=1)
    train: Callable[[C], None] = Field(exclude=True)
    config: C  # type: ignore[assignment]  # Lowered to JSON text only by plain().
    smoke: C | None = Field(default=None, exclude=True)  # type: ignore[assignment]
    start: CheckpointRecord | None = Field(default=None, exclude=True)
    extras: tuple[str, ...] = Field(default=(), exclude=True)
    command: str = ""

    @model_validator(mode="before")
    @classmethod
    def _named(cls, value: object) -> object:
        if isinstance(value, dict) and not value.get("name"):
            raise LauncherError("TrainingJob requires name=")
        return value

    @model_validator(mode="after")
    def _validate_training(self) -> TrainingJob[C]:
        if "command" in self.model_fields_set:
            raise LauncherError(
                "typed jobs derive command; use extras, environment, runtime or prepare"
            )
        _refuse_unused_extras(self.extras, self.prepare)
        self.plain()
        return self

    def plain(self) -> Job:
        if not self.name:
            raise LauncherError("TrainingJob requires name=")
        options = _options(self)
        if self.start is not None:
            options["inputs"] = (*self.inputs, *checkpoint_inputs_for(self.start, "start"))
            options["environment"] = (
                *self.environment,
                Variable(name="ETUDE_START", value=self.start.key),
            )
        job = _job(
            self.train,
            self.config,
            origin=self.train,
            options=options,
            experiment=self.experiment,
            extras=self.extras,
        )
        if self.smoke is not None:
            job = job.model_copy(
                update={"smoke": SmokeForm(config=_config_text(self.train, self.smoke))}
            )
        return job


class EvalJob[M: BaseModel | Mapping[str, JsonValue]](Job):
    """A play function, a tournament and the checkpoints or entrants it seats."""

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}
    play: Callable[[Match[M]], Tables | Result] = Field(exclude=True)
    tournament: Benchmark | RoundRobin = Field(exclude=True)
    checkpoint: CheckpointRecord | Input | None = Field(default=None, exclude=True)
    checkpoints: Sequence[CheckpointRecord] = Field(default=(), exclude=True)
    enter: Sequence[Player] = Field(default=(), exclude=True)
    workers: int = Field(default=1, ge=1, exclude=True)
    extras: tuple[str, ...] = Field(default=(), exclude=True)
    command: str = ""

    @field_validator("tournament", mode="before")
    @classmethod
    def _ready_tournament(cls, value: object) -> object:
        if callable(value):
            raise LauncherError(
                "tournament must be a Benchmark or RoundRobin; call the function first"
            )
        return value

    @model_validator(mode="after")
    def _validate_evaluation(self) -> EvalJob[M]:
        if self.name and self.name != self.tournament.name:
            raise LauncherError("an EvalJob is named by its tournament")
        object.__setattr__(self, "name", self.tournament.name)
        if "command" in self.model_fields_set:
            raise LauncherError(
                "typed jobs derive command; use extras, environment, runtime or prepare"
            )
        _refuse_unused_extras(self.extras, self.prepare)
        self.plain()
        return self

    def plain(self) -> Job:
        options = _options(self)
        _require_hours(options)
        options["name"] = self.tournament.name
        if isinstance(self.tournament, Benchmark) and self.enter:
            raise LauncherError(
                f"{self.tournament.name} is a benchmark: give it checkpoints, no entrants"
            )
        if self.checkpoint is not None and self.checkpoints:
            raise LauncherError("give one checkpoint input, or the checkpoints an experiment chose")
        if isinstance(self.tournament, RoundRobin) and (
            self.checkpoint is not None or self.checkpoints or not self.enter
        ):
            raise LauncherError(
                f"{self.tournament.name} is a round robin: give it entrants, no checkpoint"
            )
        if self.checkpoint is not None:
            inputs = (
                checkpoint_inputs_for(self.checkpoint, _BENCHMARK_CHECKPOINT)
                if isinstance(self.checkpoint, CheckpointRecord)
                else (self.checkpoint,)
            )
            options["inputs"] = (*self.inputs, *inputs)
        spec = Evaluation(
            tournament=self.tournament,
            experiment=self.experiment,
            play=_function(self.play, _repo(self.play, options)),
            checkpoint=self.checkpoint,
            checkpoints=tuple(self.checkpoints),
            entrants=tuple(self.enter),
            workers=self.workers,
        )
        assemble(
            [
                TournamentRun(
                    tournament=self.tournament,
                    checkpoints=tuple(c.ref for c in spec.checkpoints),
                    entrants=spec.entrants,
                )
            ]
        )
        return _job(
            _hold,
            spec,
            origin=self.play,
            options=options,
            experiment=self.experiment,
            extras=self.extras,
        )


def _options(job: Job) -> JobOptions:
    """Only authored launch fields; derived defaults are recomputed after a copy."""
    return cast(
        JobOptions,
        {
            name: getattr(job, name)
            for name in JobOptions.__annotations__
            if name in job.model_fields_set
        },
    )


def evaluation_spec(job: Job) -> Evaluation:
    """What an evaluation job holds; raises `EtudeError` for any other job.

    An evaluation job is one whose command calls `_hold` (`_called`).
    """
    job = job.plain()
    if job.config is None or _called(_function(_hold, None)) not in job.command:
        raise EtudeError("not_an_evaluation_job", 422)
    return Evaluation.model_validate_json(job.config)


def with_experiment(job: Job, experiment: str) -> Job:
    """The evaluation job an experiment's `eval_jobs(state)` returned, as one of `experiment`.

    Raises `EtudeError("eval_job_of_another_experiment")` for a job naming
    another experiment, and `EtudeError("not_an_evaluation_job")` for one that
    is not an evaluation.
    """
    job = job.plain()
    spec = evaluation_spec(job)
    if spec.experiment not in (None, experiment):
        raise EtudeError("eval_job_of_another_experiment", 422) from ValueError(spec.experiment)
    spec = spec.model_copy(update={"experiment": experiment})
    return job.model_copy(update={"config": spec.model_dump_json(), "experiment": experiment})


def hold_evaluation(
    spec: Evaluation,
    *,
    checkout: str | None = None,
    checkpoint: CheckpointRef | None = None,
    files: Mapping[str, str] = {},
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
) -> EvalResult:
    """Hold what an evaluation job's config describes (`etude.eval.tournament.hold`).

    `play` is imported from `checkout`, the job's source, or the working
    directory as on the job's machine. `files` maps checkpoint keys to their
    verified local URIs for explicit matches. `checkpoint` is a benchmark's one
    checkpoint where it lies here. Raises `EtudeError("evaluation_names_no_experiment")`
    for a config whose experiment is not yet named (`with_experiment`).
    """
    if spec.experiment is None:
        raise EtudeError("evaluation_names_no_experiment", 422)
    if spec.matches is not None:
        spec = spec.model_copy(
            update={"matches": tuple(_relocated(m, files) for m in spec.matches)}
        )
    # Loaded by import path: the function `evaluation` was given, typed there.
    play = cast(Callable[[Match], Tables | Result], spec.play.load(checkout))
    with evaluating(
        spec.tournament,
        experiment=spec.experiment,
        checkpoint=checkpoint,
        checkpoints=tuple(each.ref for each in spec.checkpoints),
        enter=spec.entrants,
        matches=spec.matches,
        workers=spec.workers,
        directory=directory,
        storages=storages,
        lines=spec.lines,
        resume=True,
    ) as active:
        return active.play(play)


def with_matches(job: Job, matches: Sequence[Match], state: ExperimentState) -> Job:
    """The job playing exactly `matches`, with each checkpoint they seat as an input.

    A checkpoint is a file of the training run that saved it, so the launcher
    fetches it from that run's bucket folder or relays it from its last
    machine, verified by digest.
    """
    job = job.plain()
    spec = evaluation_spec(job)
    saved = checkpoint_inputs(matches, state, spec.checkpoints)
    # One input per checkpoint key, even where two keys hold the same bytes.
    files = {
        key: f"checkpoint-{digest(key)[:16]}{PurePosixPath(checkpoint_file_name(each.artifact.uri)).suffix}"
        for key, each in saved.items()
    }
    inputs = tuple(
        given
        for key, each in sorted(saved.items())
        for given in checkpoint_inputs_for(each, files[key])
    )
    spec = spec.model_copy(update={"matches": tuple(matches), "files": files, "lines": state.lines})
    return job.model_copy(
        update={"config": spec.model_dump_json(), "inputs": (*job.inputs, *inputs)}
    )


def checkpoint_inputs_for(
    checkpoint: CheckpointRecord | CheckpointRef, name: str
) -> tuple[Input, ...]:
    """The individually verified files of one checkpoint, under one input name."""
    artifact = checkpoint.artifact
    key = (checkpoint.file if isinstance(checkpoint, CheckpointRecord) else None) or checkpoint_key(
        artifact.uri
    )
    if artifact.members:
        files = {"/" + member: content for member, content in sorted(artifact.members.items())}
    elif artifact.sha256 is None or artifact.size_bytes is None:
        raise LauncherError("a checkpoint input requires its digest and byte length")
    else:
        files = {"": Content(sha256=artifact.sha256, size_bytes=artifact.size_bytes)}
    owner = (
        stored_owner(checkpoint.run, checkpoint.experiment, checkpoint.folder)
        if isinstance(checkpoint, CheckpointRecord) and checkpoint.run
        else None
    )
    return tuple(
        Input(
            name=name + member,
            key=key + member,
            sha256=content.sha256,
            size_bytes=content.size_bytes,
            run=owner,
            uri=artifact.uri + member,
        )
        for member, content in files.items()
    )


# The job input that carries a continuation's checkpoint file, before its suffix.
_PARENT_CHECKPOINT = "parent-checkpoint"


def continue_from(
    parent: RunRecord,
    until: str,
    *,
    source: Storage | Path | str,
    ordinal: int | None = None,
    config: BaseModel | None = None,
) -> RunRequest:
    """A new run seeded with out and the available latest checkpoint, or an ordinal.

    File inputs freeze the bytes chosen here by digest; a parent changing them
    before delivery is refused. Config is validated against the typed job's
    function before queueing. The target is absolute and must advance its clock.
    Empty out without a latest copied checkpoint is refused before queueing.
    """
    storage = as_storage(source)
    keys = storage.keys("")
    name = parent.id.rsplit("/", 1)[-1]
    candidates = [
        item
        for item in located_runs(storage, keys, run=name)
        if not parent.experiment or item.file.experiment == parent.experiment
    ]
    if len(candidates) != 1:
        raise LauncherError(f"run {parent.id} has no unambiguous tracked folder in {storage.name}")
    declaration, folders = candidates[0].file, candidates[0].folders
    source_run = folders[0].rstrip("/")
    data = read(storage, declaration.experiment, name)
    target = Until.parse(until)
    previous = next(
        (v.value for v in parent.launch.spec.environment if v.name == "ETUDE_CONTINUES"), None
    )
    inherited_input = None if previous is None else ParentRun.model_validate_json(previous).input
    inputs = {
        i.name: i
        for i in parent.launch.spec.inputs
        if i.name != inherited_input
        and (inherited_input is None or not i.name.startswith(inherited_input + "/"))
        and i.out is None
    }
    parent_key = SourceKey(namespace=declaration.namespace, kind="run", source_id=name)
    # An explicit checkpoint needs no out reads, even when the parent has out.
    out = None if ordinal is not None else _parent_out(storage, folders, keys)
    checkpoint = next((r for r in reversed(data.checkpoints) if ordinal in (None, r.ordinal)), None)
    key = None
    if checkpoint is not None:
        file = PurePosixPath(checkpoint.artifact.uri)
        key = checkpoint.file
        if key is None and file.parent.parts[-2:] == (name, CHECKPOINTS):
            # Addressed within the parent's folder, so where its records were
            # read from does not make the same continuation a different job.
            key = checkpoint_key(checkpoint.artifact.uri)
    copied = key is not None and key.startswith(f"{CHECKPOINTS}/")
    members = () if out is None else out.files
    # Empty out still resumes a copied checkpoint. A removed out checkpoint
    # must never select an older record or resurrect its retained bytes.
    if out is not None and not members and not copied:
        if checkpoint is None:
            raise LauncherError(
                f"run {parent.id} has no saved state: out is empty and no checkpoint was recorded"
            )
        raise LauncherError(
            f"run {parent.id} has empty out and its latest checkpoint's out file was removed; "
            "use --ordinal N to select a recorded checkpoint explicitly"
        )
    given = None
    if members:
        # A recipe may save a checkpoint without logging scalar rows.
        at = {**(checkpoint.at if checkpoint is not None else {}), **data.reached}
        for index, member in enumerate(members):
            name = _input_name(f"parent-out-{index}", inputs)
            inputs[name] = Input(
                name=name,
                out=member.key.removeprefix("out/"),
                key=member.key,
                sha256=member.sha256,
                size_bytes=member.size_bytes,
                run=parent.id,
                uri=storage.uri(source_run + "/" + member.key),
            )
        if not copied and not any(
            member.key == key or key is not None and member.key.startswith(key + "/")
            for member in members
        ):
            checkpoint = None
        origin = "parent"
    else:
        origin = "checkpoint"
        at = checkpoint.at if checkpoint is not None else {}
    if not members or copied:
        if (
            checkpoint is None
            or checkpoint.artifact.sha256 is None
            or checkpoint.artifact.size_bytes is None
        ):
            raise LauncherError(f"run {parent.id} recorded no stored checkpoint {ordinal or ''}")
        if key is not None:
            given = _input_name(_PARENT_CHECKPOINT + PurePosixPath(key).suffix, inputs)
            located = checkpoint.model_copy(update={"run": name, "file": key, "folder": source_run})
            for file_input in checkpoint_inputs_for(located, given):
                inputs[file_input.name] = file_input.model_copy(update={"run": parent.id})
    if target.clock not in at:
        raise LauncherError(
            f"a continuation needs a target on a clock its {origin} recorded: {', '.join(sorted(at))}"
        )
    if target.value <= at[target.clock]:
        raise LauncherError(
            f"the target {target} is not beyond the {origin}'s "
            f"{target.clock}={number(at[target.clock])}"
        )
    if checkpoint is not None:
        artifact = checkpoint.artifact
        if key is not None:
            artifact = artifact.model_copy(update={"uri": key})
        # Mutable parent tags do not change the child's job identity.
        checkpoint = checkpoint.model_copy(update={"artifact": artifact, "tags": ()})
    handed = ParentRun(
        parent=parent_key, at=at, clocks=declaration.clocks, checkpoint=checkpoint, input=given
    )
    environment = (
        *(v for v in parent.launch.spec.environment if v.name != "ETUDE_CONTINUES"),
        Variable(name="ETUDE_CONTINUES", value=handed.model_dump_json()),
    )
    text = (
        parent.launch.spec.config
        if config is None
        else _config_text(load_job_function(parent.launch.spec), config)
    )
    job = parent.launch.spec.model_copy(
        update={
            "until": target,
            "inputs": tuple(inputs.values()),
            "environment": environment,
            "config": text,
        }
    )
    return RunRequest(
        job=job, seed=parent.seed, params=dict(parent.launch.params), source=parent.launch.source
    )


def _parent_out(storage: Storage, folders: Sequence[str], keys: Sequence[str]) -> OutFiles | None:
    """Choose one physical out; absent membership requires reading and hashing its files.

    None means no out exists. Empty membership excludes retained out bytes;
    copied checkpoints live separately under checkpoints/.
    """
    for folder in folders:
        if folder + MEMBERSHIP in keys:
            return OutFiles.model_validate_json(storage.get(folder + MEMBERSHIP))
        out_keys = sorted(key for key in keys if key.startswith(folder + "out/"))
        if not out_keys and folder + OUT_PRESENT not in keys:
            continue
        files: list[OutFile] = []
        for full_key in out_keys:
            key = full_key[len(folder) :]
            if any(part.startswith(".") for part in key.split("/")) or key.endswith(
                (".partial", ".open")
            ):
                continue
            body = storage.get(full_key)
            files.append(OutFile(key=key, sha256=sha256(body).hexdigest(), size_bytes=len(body)))
        return OutFiles(files=tuple(files))
    return None


def _input_name(name: str, inputs: Mapping[str, Input]) -> str:
    """Choose a transport name without taking any name the job already uses."""
    while name in inputs:
        name += "-"
    return name


def load_job_function(job: Job, *, current: bool = False) -> Callable[..., object]:
    """Load a typed job's function; `current` executes checkout source afresh.

    Config inspection uses the imported model type, preserving Python class
    identity. Local attempts execute current source even in a persistent client.
    """
    command = shlex.split(job.command)
    if "etude.call" not in command or "--function" not in command:
        raise LauncherError("config changes require a typed job")
    function = command[command.index("--function") + 1]
    path = command[command.index("--path") + 1] if "--path" in command else ""
    module, _, name = function.partition(":")
    source = Path(job.repo or ".") / path / (module.replace(".", "/") + ".py")
    if current and source.is_file():
        # A local attempt runs current source, even when this process imported it before.
        folder = str((Path(job.repo or ".") / path).resolve())
        sys.path.insert(0, folder)
        try:
            parts = name.split(".")
            found: object = runpy.run_path(str(source)).get(parts[0])
            for part in parts[1:]:
                found = getattr(found, part)
        finally:
            sys.path.remove(folder)
        if not callable(found):
            raise LauncherError(f"{module}:{name} is not a function")
        return found
    return Function(module=module, name=name, path=path).load(job.repo)


def continued_config(parent: RunRecord, changes: str) -> BaseModel:
    """Validate a JSON object of changes against the parent's typed config."""
    kind = config_type(load_job_function(parent.launch.spec))
    try:
        return changed_config(kind, changes, stored=parent.launch.spec.config or "{}")
    except ValueError as error:
        raise LauncherError(str(error)) from None


def _hold(evaluation: Evaluation) -> None:
    """On the machine: hold the tournament, with the checkpoint inputs in their seats.

    Eval job commands name it as `etude.jobs:_hold`, so its name and module stay.
    """
    if evaluation.experiment is None or (evaluation.checkpoints and evaluation.matches is None):
        raise SystemExit("etude: an experiment's evaluation job is launched by `etude eval`")
    folder = os.environ.get("ETUDE_INPUTS_DIR")
    if (evaluation.checkpoint is not None or evaluation.files) and not folder:
        raise SystemExit("etude: an evaluation job's checkpoints arrive as inputs")
    checkpoint = None
    given = evaluation.checkpoint
    if isinstance(given, CheckpointRecord):
        # The record keeps its own key; only its bytes moved, to this input.
        uri = (Path(str(folder)) / _BENCHMARK_CHECKPOINT).as_uri()
        checkpoint = given.ref.model_copy(
            update={"artifact": given.artifact.model_copy(update={"uri": uri})}
        )
    elif given is not None:
        # A training run's file is keyed by its run and name; any other, by where it lies.
        key = (
            checkpoint_id(
                run=given.run,
                name=given.key if given.key.startswith("out/") else checkpoint_file_name(given.key),
            )
            if given.run is not None
            else checkpoint_id(address=given.key)
        )
        artifact = ArtifactRef(
            uri=(Path(str(folder)) / given.name).as_uri(),
            sha256=given.sha256,
            size_bytes=given.size_bytes,
        )
        checkpoint = CheckpointRef(key=key, artifact=artifact)
    if evaluation.matches is not None:
        here = {key: (Path(str(folder)) / name).as_uri() for key, name in evaluation.files.items()}
    else:
        here = {}
    hold_evaluation(evaluation, checkpoint=checkpoint, files=here)


def _relocated(match: Match, here: Mapping[str, str]) -> Match:
    """The match with each model's checkpoint file where it lies on this machine."""
    players: list[Player] = []
    for player in match.players:
        if not isinstance(player, CheckpointPlayer) or str(player.checkpoint.key) not in here:
            players.append(player)
            continue
        checkpoint = player.checkpoint
        moved = checkpoint.artifact.model_copy(update={"uri": here[str(checkpoint.key)]})
        players.append(
            player.model_copy(
                update={"checkpoint": checkpoint.model_copy(update={"artifact": moved})}
            )
        )
    return match.model_copy(update={"players": tuple(players)})


def _require_hours(options: JobOptions) -> None:
    """Refuse a job with no `max_hours` in words, before `Job` refuses it as a missing field."""
    if "max_hours" not in options:
        raise LauncherError("a job needs max_hours: the most hours it may hold a machine")


def _refuse_unused_extras(extras: tuple[str, ...], prepare: Preparation | None) -> None:
    """A preparation naming its own interpreter owns its install, so extras would go unused."""
    if extras and prepare is not None and prepare.python is not None:
        raise LauncherError(
            "extras= is not used when prepare= names its own python; "
            "install the extras in that preparation's command"
        )


def _job(
    function: Callable[..., object],
    config: BaseModel,
    *,
    origin: Callable[..., object],
    options: JobOptions,
    experiment: str | None = None,
    extras: tuple[str, ...] = (),
) -> Job:
    """The job that calls `function(config)`; `origin` is the author's function it brings."""
    text = _config_text(function, config)
    repo = _repo(origin, options)
    called = _function(function, repo)
    sourced: JobOptions = {**options, "repo": None if repo is None else str(repo)}
    if "prepare" not in options and options.get("runtime") is None and repo is not None:
        derived = _uv_preparation(repo, extras)
        if derived is not None:
            sourced["prepare"] = derived
    prepared = sourced.get("prepare")
    # A named interpreter is selected as ETUDE_PYTHON; otherwise the checkout is a uv project.
    named = options.get("runtime") is not None or (
        prepared is not None and prepared.python is not None
    )
    python = '"$ETUDE_PYTHON"' if named else "uv run python"
    if extras and (prepared is None or prepared.python is None):
        flags = " ".join(f"--extra {shlex.quote(extra)}" for extra in extras)
        python = f"uv run {flags}" + (' --python "$ETUDE_PYTHON"' if named else "") + " python"
    command = f"{python} -m etude.call {_called(called)}"
    if called.path:
        command += f" --path {shlex.quote(called.path)}"
    return Job(**sourced, command=command, config=text, experiment=experiment)


def _called(function: Function) -> str:
    """The part of a job's command that names the function it calls."""
    return f"--function {shlex.quote(str(function))}"


def _config_text(function: Callable[..., object], config: BaseModel) -> str:
    """`config` as the job's text, refused unless `function` takes it and it validates back."""
    try:
        kind = config_type(function)
    except TypeError as error:
        raise LauncherError(str(error)) from None
    if not isinstance(config, kind):
        raise LauncherError(
            f"{function.__qualname__} takes a {kind.__name__} config, not {type(config).__name__}"
        )
    text = config.model_dump_json()
    try:
        kind.model_validate_json(text)
    except ValidationError as error:
        raise LauncherError(
            f"the {kind.__name__} config would not validate on the machine: {error}"
        ) from None
    return text


def _uv_preparation(repo: Path, extras: tuple[str, ...]) -> Preparation | None:
    """The dependency environment of an unpackaged uv project, prepared once per lock.

    uv installs only the dependencies of a project it does not package (no build
    system, or `tool.uv.package = false`), so a venv keyed by `pyproject.toml`
    and `uv.lock` (and `.python-version`) is exactly what `uv run` would use,
    and the job imports its own code from the checkout as before. A packaged
    project, a workspace or a path source needs more of the source, so it keeps
    `uv run` unless the job declares its own `prepare`. Reads the working tree's
    `pyproject.toml`; the key covers the launched commit's content.
    """
    project, lock = repo / "pyproject.toml", repo / "uv.lock"
    if not (project.is_file() and lock.is_file()):
        return None
    data = tomllib.loads(project.read_text())
    uv = data.get("tool", {}).get("uv", {})
    package = uv.get("package")
    if (package if package is not None else "build-system" in data) or "workspace" in uv:
        return None
    for source in uv.get("sources", {}).values():
        given = source if isinstance(source, list) else [source]
        if any("path" in entry or "workspace" in entry for entry in given):
            return None
    return Preparation(
        command=(
            'UV_PROJECT_ENVIRONMENT="$ETUDE_PREPARED_DIR/.venv"'
            " uv sync --frozen --no-install-project"
            + "".join(f" --extra {shlex.quote(extra)}" for extra in extras)
        ),
        # uv chooses the interpreter from .python-version, so it decides the result too.
        paths=("pyproject.toml", "uv.lock", *_present(repo, ".python-version")),
        python=".venv/bin/python",
    )


def _present(repo: Path, *names: str) -> tuple[str, ...]:
    return tuple(name for name in names if (repo / name).is_file())


def _repo(origin: Callable[..., object], options: JobOptions) -> Path | None:
    """The explicit source, the function's git checkout, or its local import root."""
    if "repo" in options:
        given = options["repo"]
        return None if given is None else Path(given).resolve()
    file = Path(inspect.getfile(origin)).resolve()
    for folder in file.parents:
        if (folder / ".git").exists():
            return folder
    # Without git, local training still needs the root from which Python imports it.
    if origin.__module__ not in ("__main__", "<run_path>"):
        parts = origin.__module__.split(".")
        return file.parents[len(parts) if file.name == "__init__.py" else len(parts) - 1]
    return file.parent


def _function(function: Callable[..., object], repo: Path | None) -> Function:
    """Name `function` by import path, from where its module sits in `repo`.

    A module installed in this environment is imported by name alone, as in
    the job's; one in the checkout is imported from the folder its name starts in.
    """
    module, name = function.__module__, function.__qualname__
    if module == "<run_path>" and "<" not in name:
        # CLI files are executed by runpy. Their top-level functions are still
        # importable from the source checkout under the actual file's name.
        where = Path(inspect.getfile(function)).resolve()
        if repo is not None and where.is_relative_to(repo) and where.stem.isidentifier():
            return Function(
                module=where.stem,
                name=name,
                path=where.parent.relative_to(repo).as_posix().removesuffix("."),
            )
    if module in ("__main__", "<run_path>") or "<" in name:
        raise LauncherError(
            f"{name} must be defined at the top of an importable module, so the job can import it"
        )
    file = getattr(sys.modules.get(module), "__file__", None)
    if repo is None or file is None:
        return Function(module=module, name=name)
    where = Path(file).resolve()
    if where.is_relative_to(Path(sys.prefix).resolve()) or not where.is_relative_to(repo):
        return Function(module=module, name=name)
    relative = where.relative_to(repo).parts
    named = (*module.split("."), "__init__.py") if where.name == "__init__.py" else None
    expected = named or (*module.split(".")[:-1], module.split(".")[-1] + ".py")
    if relative[-len(expected) :] != expected:
        raise LauncherError(
            f"{module} is at {'/'.join(relative)} in {repo}, where the job cannot import it"
            f" as {module}"
        )
    return Function(module=module, name=name, path="/".join(relative[: -len(expected)]))
