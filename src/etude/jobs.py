"""The author-facing experiment model: typed jobs and the state declarations read.

`TrainingJob`, `EvalJob`, `Agents` and `ExperimentState` are defined here. Typed jobs
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
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from functools import cached_property
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, SupportsIndex, TypedDict, cast, overload

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from etude.cache import Artifact, Content, ContentMismatch, Corrupt
from etude.call import Function, changed_config, config_type
from etude.carriage import CHECKPOINTS, OUT_PRESENT, checkpoint_file_name, checkpoint_key
from etude.checkpoints import (
    CheckpointId,
    CheckpointRecord,
    CheckpointRef,
    PolicyKey,
    _checkpoint_id,
    in_role,
    qualified,
)
from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval.game import Play, PlayMany, _match_type
from etude.eval.loader import AgentLoader, LoaderDeclaration
from etude.eval.records import (
    Benchmark,
    CheckpointPlayer,
    Code,
    Composition,
    Line,
    Match,
    Player,
    RoundRobin,
    TournamentRun,
    assemble,
    digest,
    folder_name,
    player_checkpoints,
    relocate_player,
    select_policy,
)
from etude.eval.result import EvalResult
from etude.eval.run import (
    Playing,
    _group_progress,
    _terminable,
    play_together,
)
from etude.eval.tournament import _open_evaluation
from etude.identity import run_address, stored_owner
from etude.launch.records import Standing, Status
from etude.out import MEMBERSHIP, OutFile, OutFiles, OutMember
from etude.records import ArtifactRef, Continuation, Record, RunRecord
from etude.run import RunListing, as_storage, declare, destinations, under
from etude.spec import (
    CONTINUATION_ENV,
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
from etude.storage import Storage, default_root, place_artifact
from etude.train.read import RunData, read_located
from etude.train.records import ParentRun

__all__ = (
    "TrainingJob",
    "EvalJob",
    "Agents",
    "ExperimentState",
    "continue_from",
    "TrainingRunSummary",
    "EvalRunSummary",
    "Policy",
    "Checkpoints",
    "OutMembers",
)


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
        runs = ", ".join(dict.fromkeys(member.run for member in self)) or "a run with no stored out"
        raise EtudeError("out_file_not_found", 404, detail=f"{path} is not stored in {runs}")

    def inputs(self, under: str) -> tuple[Input, ...]:
        """Give each member at `under/PATH`, preserving its path within out."""
        return tuple(member.input(under + "/" + member.path) for member in self)


type RunState = Literal["queued", "running", "stopped", "finished", "failed"]


# A refinement while the launcher still holds work, including a writer's
# confirmed stop before the launcher schedules its continuation.
type AllocationPhase = Literal["renting", "setting_up", "stopping", "stopped"]


@dataclass(frozen=True, kw_only=True)
class _RunObservation:
    # Keep the same launcher observation for status, placement and budget decisions.
    standing: Standing | None = field(default=None, repr=False, hash=False)
    last_write: datetime | None = None
    allocation_phase: AllocationPhase | None = None
    # The file observation carries the reader's selected data and may be absent before delivery.
    stored: RunData | None = field(default=None, repr=False, compare=False)

    @property
    def status(self) -> Status | None:
        return self.standing.status if self.standing else None

    @property
    def stored_address(self) -> str | None:
        """The address in the file declaration, including historical compact spellings."""
        return self.stored.address if self.stored else None


@dataclass(frozen=True)
class TrainingRunSummary(_RunObservation):
    name: str
    experiment: str
    # Its job as the report names it (`etude.train.report.job_names`): the launch name, with
    # its config digest when that name ran several configs ("ppo 1a2b3c").
    job: str
    # The name it was launched under, which covers every config launched under it.
    launch: str
    # Its learner: the first run of the chain of continuations that led to it.
    line: str
    state: RunState
    checkpoints: Checkpoints
    # The seed it declared, if any.
    seed: int | None = None

    continues: Continuation | None = None
    # Visible parent of the recorded handoff, under its stored spelling when available.
    parent_address: str | None = None

    @classmethod
    def queued(cls, name: str, experiment: str, job: str, seed: int | None) -> TrainingRunSummary:
        return cls(
            name=name,
            experiment=experiment,
            job=job,
            launch=job,
            line=name,
            state="queued",
            checkpoints=Checkpoints(),
            seed=seed,
        )

    @property
    def address(self) -> str:
        """The full spelling held by the launcher or written in the run declaration."""
        if self.status:
            return self.status.run
        return self.stored_address or run_address(self.name, self.experiment)

    @cached_property
    def out(self) -> OutMembers:
        return OutMembers(self.stored.out if self.stored is not None else ())

    @property
    def ended(self) -> bool:
        return self.state in {"finished", "failed"}

    @property
    def final(self) -> CheckpointRecord | None:
        """The last checkpoint once the run finished; None while it may save more."""
        return self.checkpoints.last if self.state == "finished" else None


@dataclass(frozen=True)
class EvalRunSummary(_RunObservation):
    name: str
    state: RunState
    matches: tuple[str, ...] = ()
    file_state: RunState | None = None

    @property
    def address(self) -> str:
        """The submitted address, or the file declaration's address for a standalone run."""
        if self.status:
            return self.status.run
        return self.stored_address or self.name


@dataclass(frozen=True)
class Policy:
    """One resolved policy and its published checkpoints in save order."""

    key: PolicyKey
    checkpoints: Checkpoints


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
            if name in (each.address, each.stored_address):
                return each
        for each in self.runs:
            if each.name == name.removeprefix(self.experiment + "/"):
                return each
        raise EtudeError("run_not_found", 404) from ValueError(name)

    def policy(self, key: PolicyKey) -> Policy:
        if key.experiment is not None and key.experiment != self.experiment:
            raise EtudeError(
                "policy_not_found",
                422,
                detail=f"Use observe(source, {key.experiment!r}).policy(key) for another experiment",
            )
        line = select_policy(self.lines, key, experiment=self.experiment)
        keys = {checkpoint.key for checkpoint in line.checkpoints}
        return Policy(
            key=line.policy,
            checkpoints=Checkpoints(
                checkpoint
                for run in self.runs
                for checkpoint in run.checkpoints
                if checkpoint.key in keys
            ),
        )

    @property
    def lines(self) -> tuple[Line, ...]:
        """Each learner line, as relative seats find checkpoints in it: seeded as its first
        run declared, and ended once every run of it has."""
        found: dict[str, list[TrainingRunSummary]] = {}
        for run in self.runs:
            found.setdefault(run.line, []).append(run)
        made = []
        for name, runs in found.items():
            first = next((run for run in runs if run.name == name), runs[0])
            roles = {c.role for run in runs for c in run.checkpoints} or {"policy"}
            for role in sorted(roles):
                made.append(
                    Line(
                        name=in_role(name, role),
                        experiment=self.experiment,
                        role=role,
                        seed=first.seed,
                        job=first.job,
                        launch=first.launch,
                        ended=all(run.ended for run in runs),
                        checkpoints=tuple(
                            c.ref for run in runs for c in run.checkpoints if c.role == role
                        ),
                    )
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
        for ref in (c for player in match.players for c in player_checkpoints(player)):
            if ref.key in found:
                continue
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


class Agents(LoaderDeclaration[Callable[..., object]]):
    """Checkpoint forwards and optional game-side player construction."""

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _validate_loading(self) -> Agents:
        if self.load is not None and len(inspect.signature(self.load).parameters) != 1:
            raise TypeError("load must take one checkpoint")
        if self.build is not None and len(inspect.signature(self.build).parameters) != 2:
            raise TypeError("build must take player and leaves")
        # Unused settings would still split the group key.
        if self.load is None:
            unused = sorted(self.model_fields_set & {"devices", "max_batch", "memory_gb"})
            if unused:
                raise ValueError(f"{', '.join(unused)} requires a checkpoint loader")
        return self

    def plain(self, repo: Path | None) -> AgentLoader:
        build = None if self.build is None else _function(self.build, repo)
        if self.load is None:
            return AgentLoader(build=build)
        return AgentLoader(
            load=_function(self.load, repo),
            build=build,
            devices=self.devices,
            max_batch=self.max_batch,
            memory_gb=self.memory_gb,
        )


class Evaluation(Record):
    """Resolved matches, their tournament record and portable artifact inputs.

    Author selection lives in EvalJob. Released queued declarations are decoded
    once here; execution never interprets checkpoint/entrant selection variants.
    """

    tournament: TournamentRun
    experiment: str | None
    play: Function
    streaming: bool = Field(default=False, exclude_if=lambda value: not value)
    matches: tuple[Match, ...]
    files: dict[str, str] = {}
    run: str | None = None
    agents: AgentLoader = Field(default_factory=AgentLoader)

    @model_validator(mode="before")
    @classmethod
    def _released(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        agents = value.get("agents")
        if isinstance(agents, dict) and "max_delay_seconds" in agents:
            value = {
                **value,
                "agents": {k: v for k, v in agents.items() if k != "max_delay_seconds"},
            }
        definition = value.get("tournament")
        if (
            isinstance(definition, dict)
            and "tournament" in definition
            or isinstance(definition, TournamentRun)
        ):
            return value
        # Released queued payloads named a generator and checkpoint/entrants.
        # Normalize those at the JSON boundary, retaining original match keys.
        tournament = TypeAdapter(Benchmark | RoundRobin).validate_python(definition)
        experiment = value.get("experiment")
        checkpoints = TypeAdapter(tuple[CheckpointRecord, ...]).validate_python(
            value.get("checkpoints", ())
        )
        given = TypeAdapter(CheckpointRecord | CheckpointRef | Input | None).validate_python(
            value.get("checkpoint")
        )
        refs = tuple(each.ref for each in checkpoints)
        files = TypeAdapter(dict[str, str]).validate_python(value.get("files", {}))
        if given is not None:
            if isinstance(given, Input):
                ref = _input_checkpoint(given)
                files[ref.key] = given.name
            else:
                ref = given.ref if isinstance(given, CheckpointRecord) else given
                files[ref.key] = _BENCHMARK_CHECKPOINT
            refs = (ref,)
        old_matches = TypeAdapter(tuple[Match, ...] | None).validate_python(value.get("matches"))
        compact = {
            qualified(ref.key, experiment or ""): ref
            for match in old_matches or ()
            for player in match.players
            for ref in player_checkpoints(player)
            if qualified(ref.key, experiment or "") != ref.key
        }
        if compact:
            refs = tuple(
                compact.get(ref.key, ref).model_copy(update={"artifact": ref.artifact})
                for ref in refs
            )
        recorded = TournamentRun(
            tournament=tournament,
            checkpoints=refs,
            entrants=TypeAdapter(tuple[Player, ...]).validate_python(value.get("entrants", ())),
        )
        lines = TypeAdapter(tuple[Line, ...]).validate_python(value.get("lines", ()))
        if experiment is not None:
            recorded = recorded.bind(lines, experiment=experiment)
        resolved, _ = assemble([recorded]).resolve(lines, experiment=experiment)
        recorded = recorded.model_copy(update={"seated": resolved.seated})
        matches = value.get("matches")
        return dict(
            tournament=recorded,
            experiment=experiment,
            play=value["play"],
            matches=tuple(each.match for each in resolved.schedule())
            if matches is None
            else matches,
            files=files,
            run=(
                os.environ.get("ETUDE_RUN")
                or f"{folder_name(tournament.name)}.{digest(recorded.model_dump(mode='json'))[:12]}"
            )
            if compact
            else None,
            agents=value.get("agents") or {},
        )

    @property
    def definition(self) -> Benchmark | RoundRobin:
        return self.tournament.tournament

    @property
    def address(self) -> str:
        if self.experiment is None:
            raise EtudeError("evaluation_names_no_experiment", 422)
        if self.run:
            return run_address(self.run, self.experiment)
        identity = digest(sorted(match.key for match in self.matches))
        return f"{self.experiment}/{folder_name(self.definition.name)}-{identity[:12]}"

    def relocate(self, files: Mapping[str, str]) -> Evaluation:
        """Locate checkpoint leaves without changing scientific match identity."""
        return self.model_copy(
            update={
                "matches": tuple(
                    match.model_copy(
                        update={"players": tuple(relocate_player(p, files) for p in match.players)}
                    )
                    for match in self.matches
                )
            }
        )


class EvalGroup(Record):
    """The tournaments one run plays, in declaration order."""

    evaluations: tuple[Evaluation, ...]

    @model_validator(mode="before")
    @classmethod
    def _retained(cls, value: object) -> object:
        # Released launcher records stored one Evaluation, including queued work.
        if isinstance(value, dict) and "tournament" in value:
            return {"evaluations": (value,)}
        return value


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


class EvalJob(Job):
    """Declare matches through a generator, with their play, loading and machine needs."""

    model_config = {"arbitrary_types_allowed": True, "frozen": True, "extra": "forbid"}
    play: Play | None = Field(default=None, exclude=True)
    play_many: PlayMany | None = Field(default=None, exclude=True)
    tournament: Benchmark | RoundRobin = Field(exclude=True)
    checkpoint: CheckpointRecord | Input | None = Field(default=None, exclude=True)
    checkpoints: Sequence[CheckpointRecord] = Field(default=(), exclude=True)
    enter: Sequence[Player] = Field(default=(), exclude=True)
    agents: Agents = Field(default_factory=Agents, exclude=True)
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
    def _validate_evaluation(self) -> EvalJob:
        if self.experiment is not None:
            raise LauncherError(
                "EvalJob belongs to its file's experiment; use experiment= or policy= on a seat"
            )
        if self.name and self.name != self.tournament.name:
            raise LauncherError("an EvalJob is named by its tournament")
        object.__setattr__(self, "name", self.tournament.name)
        if "command" in self.model_fields_set:
            raise LauncherError(
                "typed jobs derive command; use extras, environment, runtime or prepare"
            )
        _refuse_unused_extras(self.extras, self.prepare)
        _require_hours(_options(self))
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
        try:
            _match_type(self._game(), streaming=self.play_many is not None)
        except TypeError as error:
            raise LauncherError(str(error)) from None
        players = list(self.enter)
        if isinstance(self.tournament, Benchmark):
            players.extend(
                p
                for seating in self.tournament.seatings
                for p in seating.players
                if isinstance(p, (CheckpointPlayer, Code, Composition))
            )
        if (
            self.agents.load is not None
            and self.agents.build is None
            and any(
                isinstance(p, (Code, Composition)) or isinstance(p, CheckpointPlayer) and p.config
                for p in players
            )
        ):
            raise LauncherError("composed and code players need Agents(build=)")
        return self

    def _game(self) -> Play | PlayMany:
        if self.play is not None and self.play_many is None:
            return self.play
        if self.play_many is not None and self.play is None:
            return self.play_many
        raise LauncherError("give either async def play or async def play_many")

    def recorded(self) -> TournamentRun:
        """The author's selection; no scheduling, source lookup or executable payload."""
        refs = tuple(each.ref for each in self.checkpoints)
        if isinstance(self.checkpoint, CheckpointRecord):
            refs = (self.checkpoint.ref,)
        elif isinstance(self.checkpoint, Input):
            refs = (_input_checkpoint(self.checkpoint),)
        return TournamentRun(
            tournament=self.tournament, checkpoints=refs, entrants=tuple(self.enter)
        )

    def plain(self) -> Job:
        return self.lower(ExperimentState(experiment=self.experiment or ""))

    def lower(
        self,
        state: ExperimentState,
        matches: Sequence[Match] | None = None,
        lines: Sequence[Line] = (),
    ) -> Job:
        """Freeze selected matches and verified inputs once, before grouping."""
        if state.experiment and any(not checkpoint.experiment for checkpoint in self.checkpoints):
            raise EtudeError(
                "checkpoint_names_no_experiment",
                422,
                detail="Use checkpoints from a named experiment",
            )
        options = _options(self)
        options["name"] = self.tournament.name
        game = self._game()
        repo = _repo(game, options)
        recorded = self.recorded()
        experiment = state.experiment or None
        bound = recorded.bind(lines, experiment=experiment) if experiment is not None else recorded
        resolved, _ = assemble([bound]).resolve(lines, experiment=experiment)
        selected = (
            tuple(each.match for each in resolved.schedule()) if matches is None else tuple(matches)
        )
        given: dict[str, tuple[Input, ...]] = {}
        if self.checkpoint is not None:
            ref = recorded.checkpoints[0]
            name = f"checkpoint-{digest(ref.key)[:16]}{PurePosixPath(checkpoint_file_name(ref.artifact.uri)).suffix}"
            given[ref.key] = (
                checkpoint_inputs_for(self.checkpoint, name)
                if isinstance(self.checkpoint, CheckpointRecord)
                else (self.checkpoint.model_copy(update={"name": name}),)
            )
        inputs: dict[str, Input] = {item.name: item for item in self.inputs}
        files: dict[str, str] = {}
        for key, checkpoint in checkpoint_inputs(selected, state, self.checkpoints).items():
            name = f"checkpoint-{digest(key)[:16]}{PurePosixPath(checkpoint_file_name(checkpoint.artifact.uri)).suffix}"
            for item in given.get(key, ()) or checkpoint_inputs_for(checkpoint, name):
                if item.name in inputs and inputs[item.name] != item:
                    raise EtudeError("eval_input_conflict", 422, detail=item.name)
                inputs[item.name] = item
            files[key] = name
        spec = Evaluation(
            tournament=bound.model_copy(update={"seated": resolved.seated}),
            experiment=experiment,
            play=_function(game, repo),
            streaming=self.play_many is not None,
            matches=selected,
            files=files,
            agents=self.agents.plain(repo),
        )
        options["inputs"] = tuple(inputs.values())
        return _job(
            _hold,
            EvalGroup(evaluations=(spec,)),
            origin=game,
            options=options,
            experiment=experiment,
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


def evaluation_group(job: Job) -> EvalGroup:
    """Read an eval command, including retained single-tournament launcher records."""
    job = job.plain()
    if job.config is None or _called(_function(_hold, None)) not in job.command:
        raise EtudeError("not_an_evaluation_job", 422)
    return EvalGroup.model_validate_json(job.config)


def _group_key(job: Job, spec: Evaluation) -> str:
    """Launch compatibility, independent of scientific match identity and job limits."""
    shared = {
        "repo",
        "revision",
        "runtime",
        "prepare",
        "setup",
        "cleanup",
        "environment",
        "stop_seconds",
        "cleanup_seconds",
        "max_attempts",
    }
    return job.model_dump_json(include=shared) + spec.agents.model_dump_json()


def hold_group(
    group: EvalGroup,
    *,
    checkout: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
) -> tuple[EvalResult, ...]:
    """Start agents once, play every tournament, then raise any match failure.

    A launch owns the aggregate tally and closes its root after every tournament
    and shared resource closes. Each tournament keeps its scientific result files.
    """
    results: list[EvalResult] = []
    failures: list[EtudeError | RetryRequested] = []
    # A launched attempt's courier collects only below its assigned track folder.
    launched = os.environ.get("ETUDE_RUN_ADDRESS") or os.environ.get("ETUDE_RUN")
    base = Path(directory) if directory is not None else default_root()
    with _terminable(), ExitStack() as lifetime:
        progress = None
        if not group.evaluations:
            return ()
        experiment = group.evaluations[0].experiment
        if experiment is None:
            raise EtudeError("evaluation_names_no_experiment", 422)
        if launched:
            declared = declare(
                experiment=experiment,
                run=launched,
                directory=base,
                kind="evaluation",
                clocks=(),
                members=tuple(spec.address for spec in group.evaluations),
                resume=True,
            )
            progress = lifetime.enter_context(
                _group_progress(
                    declared,
                    destinations(base, storages),
                    total=sum(len(spec.matches) for spec in group.evaluations),
                )
            )
            base = declared.root
        artifacts = (base if launched else base / experiment) / "artifacts" / "agents"
        plays: list[Playing] = []
        members: list[ExitStack] = []
        for spec in group.evaluations:
            if spec.experiment is None:
                raise EtudeError("evaluation_names_no_experiment", 422)
            member = lifetime.enter_context(ExitStack())
            active = member.enter_context(
                _open_evaluation(
                    spec.tournament,
                    experiment=spec.experiment,
                    matches=spec.matches,
                    run=spec.address,
                    directory=base,
                    storages=() if launched else storages,
                    progress=progress,
                    resume=True,
                )
            )
            members.append(member)
            plays.append(
                Playing(
                    run=active,
                    play=spec.play,
                    streaming=spec.streaming,
                    tournament=spec.definition.name,
                    agent_loader=spec.agents,
                )
            )
        report = progress or plays[0].run._report
        report.total = sum(len(each.run.matches) for each in plays)
        report.done = sum(each.skipped for each in plays)
        for each in plays:
            each.run._report = report
        report.write()
        outcomes = play_together(plays, artifacts=artifacts, checkout=checkout)
        for member, outcome in zip(members, outcomes, strict=True):
            if isinstance(outcome, (EtudeError, RetryRequested)):
                member.__exit__(type(outcome), outcome, outcome.__traceback__)
                failures.append(outcome)
            else:
                member.close()
                results.append(outcome)
        if failures:
            raise failures[0]
    return tuple(results)


def group_evaluations(jobs: Sequence[Job], evaluations: Sequence[Evaluation]) -> Job:
    """One compatible machine job, with checkpoint inputs deduplicated by key."""
    first = jobs[0].plain()
    inputs: dict[str, Input] = {}
    for job in jobs:
        for given in job.inputs:
            if given.name in inputs and inputs[given.name] != given:
                raise EtudeError("eval_input_conflict", 422, detail=given.name)
            inputs[given.name] = given
    return first.model_copy(
        update={
            "name": "evals",
            "config": EvalGroup(evaluations=tuple(evaluations)).model_dump_json(),
            "inputs": tuple(inputs.values()),
            "max_hours": sum(job.require_hours() for job in jobs),
            "max_usd": None
            if any(job.max_usd is None for job in jobs)
            else sum((job.max_usd or Decimal(0) for job in jobs), Decimal(0)),
        }
    )


def _input_checkpoint(given: Input) -> CheckpointRef:
    """Name an input's checkpoint independently of its current byte location."""
    key = (
        _checkpoint_id(
            experiment="",
            run=given.run,
            name=given.key if given.key.startswith("out/") else checkpoint_file_name(given.key),
        )
        if given.run is not None
        else given.key
    )
    return CheckpointRef(
        address=key,
        artifact=ArtifactRef(
            uri=given.uri or given.key, sha256=given.sha256, size_bytes=given.size_bytes
        ),
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
    source: RunListing | Storage | Path | str,
    ordinal: int | None = None,
    config: BaseModel | None = None,
) -> RunRequest:
    """A new run seeded with out and the available latest checkpoint, or an ordinal.

    File inputs freeze the bytes chosen here by digest; a parent changing them
    before delivery is refused. Config is validated against the typed job's
    function before queueing. The target is absolute and must advance its clock.
    A RunListing reuses a discovery of this source. Empty out
    without a latest copied checkpoint is refused before queueing.
    """
    name = parent.id.rsplit("/", 1)[-1]
    listing = (
        source if isinstance(source, RunListing) else RunListing.read(as_storage(source), run=name)
    )
    storage = listing.storage
    candidates = [
        item
        for item in listing.declarations
        if item.name == name
        and (not parent.experiment or item.file.experiment == parent.experiment)
    ]
    if len(candidates) != 1:
        raise LauncherError(f"run {parent.id} has no unambiguous tracked folder in {storage.name}")
    (located,) = candidates
    declaration = located.file
    data = read_located(storage, located, listing.keys)
    target = Until.parse(until)
    spec = parent.launch.spec
    previous = spec.continuation_json
    inherited_input = None if previous is None else ParentRun.model_validate_json(previous).input
    inputs = {
        i.name: i
        for i in spec.inputs
        if i.name != inherited_input
        and (inherited_input is None or not i.name.startswith(inherited_input + "/"))
        and i.out is None
    }
    # An explicit checkpoint needs no out reads, even when the parent has out.
    out = _parent_out(listing, parent.id, located.folders) if ordinal is None else None
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
    # Empty out still resumes a copied checkpoint. A removed out checkpoint
    # must never select an older record or resurrect its retained bytes.
    if out == () and not copied:
        if checkpoint is None:
            raise LauncherError(
                f"run {parent.id} has no saved state: out is empty and no checkpoint was recorded"
            )
        raise LauncherError(
            f"run {parent.id} has empty out and its latest checkpoint's out file was removed; "
            "use --ordinal N to select a recorded checkpoint explicitly"
        )
    given = None
    if out:
        # A recipe may save a checkpoint without logging scalar rows.
        at = {**(checkpoint.at if checkpoint is not None else {}), **data.reached}
        for index, member in enumerate(out):
            input_name = _input_name(f"parent-out-{index}", inputs)
            inputs[input_name] = member.input(input_name, out=member.path)
        if not copied and not any(
            member.key == key or key is not None and member.key.startswith(key + "/")
            for member in out
        ):
            checkpoint = None
        origin = "parent"
    else:
        origin = "checkpoint"
        at = checkpoint.at if checkpoint is not None else {}
    if not out or copied:
        if (
            checkpoint is None
            or checkpoint.artifact.sha256 is None
            or checkpoint.artifact.size_bytes is None
        ):
            raise LauncherError(f"run {parent.id} recorded no stored checkpoint {ordinal or ''}")
        if key is not None:
            given = _input_name(_PARENT_CHECKPOINT + PurePosixPath(key).suffix, inputs)
            saved = checkpoint.model_copy(update={"file": key})
            for file_input in checkpoint_inputs_for(saved, given):
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
        parent=declaration.source_key,
        at=at,
        clocks=declaration.clocks,
        checkpoint=checkpoint,
        input=given,
    )
    environment = (
        *(v for v in spec.environment if v.name != CONTINUATION_ENV),
        Variable(name=CONTINUATION_ENV, value=handed.model_dump_json()),
    )
    text = spec.config if config is None else _config_text(load_job_function(spec), config)
    job = spec.model_copy(
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


def _parent_out(listing: RunListing, run: str, folders: Sequence[str]) -> OutMembers | None:
    """Choose one physical out; absent membership requires reading and hashing its files.

    None means no out exists. Empty membership excludes retained out bytes;
    copied checkpoints live separately under checkpoints/.
    """
    storage = listing.storage
    for folder in folders:
        if folder + MEMBERSHIP in listing.objects:
            members = OutFiles.model_validate_json(storage.get(folder + MEMBERSHIP)).files
        else:
            out_keys = list(under(listing.names_in(folder), "out/"))
            if not out_keys and folder + OUT_PRESENT not in listing.objects:
                continue
            files = []
            for key in out_keys:
                if any(part.startswith(".") for part in key.split("/")) or key.endswith(
                    (".partial", ".open")
                ):
                    continue
                body = storage.get(folder + key)
                files.append(
                    OutFile(key=key, sha256=sha256(body).hexdigest(), size_bytes=len(body))
                )
            members = tuple(files)
        # A declaration-only copy may precede the folder that supplied these bytes.
        return OutMembers(
            member.locate(run, storage.uri(folder + member.key)) for member in members
        )
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


def _hold(group: EvalGroup) -> None:
    """On the machine, relocate inputs and hold all tournaments below its track folder."""
    hold_group(EvalGroup(evaluations=tuple(_received(spec) for spec in group.evaluations)))


def _received(evaluation: Evaluation) -> Evaluation:
    if evaluation.experiment is None:
        raise SystemExit(
            "etude: evaluation jobs need their experiment; use etude eval FILE or etude launch FILE"
        )
    folder = os.environ.get("ETUDE_INPUTS_DIR")
    if evaluation.files and folder is None:
        raise SystemExit("etude: an evaluation job's checkpoints arrive as inputs")
    return evaluation.relocate(
        {key: (Path(str(folder)) / name).as_uri() for key, name in evaluation.files.items()}
    )


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
    file = Path(inspect.getfile(inspect.unwrap(origin))).resolve()
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
        where = Path(inspect.getfile(inspect.unwrap(function))).resolve()
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
