"""Experiments: an eval function that names what to evaluate, and the pass that plays what is new.

An experiment file defines `eval(state)`; the experiment is named for the
file, so `ppo.py` is experiment `ppo`. `state` (`ExperimentState`, from
`observe`) is the experiment's training runs under a source, with the
checkpoints each recorded and whether it has ended. Its evaluations are the
experiment's too, so `etude report` finds them beside its training runs. `eval` returns the evaluation jobs it wants
now (`etude.jobs.evaluation`): a benchmark held for the checkpoints it chose,
whose seatings may seat checkpoints relative to each held one or chosen
checkpoints against each other. It is called again on
every pass, so a design that depends on what exists, such as each checkpoint
against the one before it, grows as checkpoints arrive.

`advance` is one pass: for each job, the matches its benchmark calls for that
no eval run delivered (`etude.eval.tournament.played`), played here or
launched as a job that plays exactly those. A launched pass names its run for
the matches it plays and remembers it in a ledger beside the launcher's
records, so a later pass neither launches it again nor plays its matches while
it runs. A pass that would launch matches a failed run was given stops: rerun
them after looking, rather than spend again unattended. What a failed run
delivered is not wanted again, and another experiment's run is none of its
business.
"""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from etude.carriage import checkpoint_file_name
from etude.eval.records import (
    CheckpointKey,
    CheckpointRef,
    Inference,
    Line,
    Match,
    Model,
    digest,
    folder_name,
)
from etude.eval.tournament import played
from etude.launch import client
from etude.launch.database import Database
from etude.launch.errors import LauncherError
from etude.launch.records import Job, Run, Smoke
from etude.launch.scheduler import Scheduler
from etude.launch.source import home
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Digest, Record
from etude.run.storage import Storage
from etude.train.read import RunData, lines, read_all
from etude.train.report import job_names

# ---------------------------------------------------------------------------
# What eval(state) chooses from


class Checkpoint(Record):
    """A checkpoint a training run recorded, with a stored file to evaluate."""

    run: str
    experiment: str
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    # Where each of the run's clocks stood when it was saved.
    at: dict[str, float]
    # When it was saved, in milliseconds since the epoch.
    # None only in a checkpoint serialized before this field existed.
    wall_ms: int | None = None
    # The learner it belongs to: the first run of the chain of continuations that led to its run.
    line: str
    tags: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The file's name, as a model seat names it: `updates-200.pt`."""
        return checkpoint_file_name(self.artifact.uri)

    @property
    def key(self) -> CheckpointKey:
        """What identifies it: its run and file, never its bytes."""
        return CheckpointKey(run=self.run, name=self.name)

    @property
    def ref(self) -> CheckpointRef:
        """Its key, stored file, where it stood and its line, as a benchmark is held for it."""
        return CheckpointRef(key=self.key, artifact=self.artifact, at=self.at, line=self.line)

    def model(self, inference: Inference) -> Model:
        """This checkpoint playing as `inference`, for a seating of chosen checkpoints."""
        return inference.model(self.ref)


class Checkpoints(tuple[Checkpoint, ...]):
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
    def last(self) -> Checkpoint | None:
        return self[-1] if self else None


@dataclass(frozen=True)
class TrainingRun:
    name: str
    experiment: str
    # Its job as the report names it (`etude.train.report.job_names`): the launch name, with
    # its config digest when that name ran several configs ("ppo 1a2b3c").
    job: str
    # The name it was launched under, which covers every config launched under it.
    launch: str
    # Its learner: the first run of the chain of continuations that led to it.
    line: str
    state: Literal["running", "finished", "failed"]
    checkpoints: Checkpoints
    # The seed it declared, if any.
    seed: int | None = None

    @property
    def ended(self) -> bool:
        return self.state != "running"

    @property
    def final(self) -> Checkpoint | None:
        """The last checkpoint once the run finished; None while it may save more."""
        return self.checkpoints.last if self.state == "finished" else None


@dataclass(frozen=True)
class ExperimentState:
    """An experiment's training runs under a source, as a pass found it. Never stored."""

    experiment: str
    runs: tuple[TrainingRun, ...] = ()

    def run(self, name: str) -> TrainingRun:
        """The training run named `name`; raises `EtudeError("run_not_found")` without one."""
        for each in self.runs:
            if each.name == name:
                return each
        raise EtudeError("run_not_found", 404) from ValueError(name)

    @property
    def lines(self) -> tuple[Line, ...]:
        """Each learner line, as relative seats find checkpoints in it: seeded as its first
        run declared, and ended once every run of it has."""
        found: dict[str, list[TrainingRun]] = {}
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

    def checkpoint(self, key: CheckpointKey) -> Checkpoint | None:
        for each in self.runs:
            for saved in each.checkpoints:
                if saved.key == key:
                    return saved
        return None


def observe(source: Storage | Path | str, experiment: str) -> ExperimentState:
    """The experiment's training runs under a directory or bucket and their stored checkpoints.

    A checkpoint whose file Etude did not store, so no digest, is left out:
    nothing can be evaluated without its bytes. Runs' records alone are read:
    no metric block or index.
    """
    return state_of(read_all(source), experiment)


def state_of(every: Sequence[RunData], experiment: str) -> ExperimentState:
    """The experiment's training runs among runs already read, as `observe` finds them.

    A run that recorded no end has failed once this computer's launcher says
    so (`_closed_without_success`), and so ends at its last stored checkpoint.
    """
    line = lines(every)
    ours = [
        data
        for data in every
        if data.file is not None
        and data.experiment == experiment
        and data.file.declaration.kind != "evaluation"
    ]
    job = job_names(ours)
    lost = _closed_without_success([data.name for data in ours if data.state is None])
    runs = []
    for data in ours:
        saved = Checkpoints(
            Checkpoint(
                run=data.name,
                experiment=data.experiment,
                ordinal=record.ordinal,
                artifact=record.artifact,
                at=record.at,
                wall_ms=record.wall_ms,
                line=line[data.name],
                tags=record.tags,
            )
            for record in data.checkpoints
            if record.artifact.sha256 is not None and record.artifact.size_bytes is not None
        )
        state: Literal["running", "finished", "failed"] = (
            data.state.state
            if data.state is not None
            else "failed"
            if data.name in lost
            else "running"
        )
        runs.append(
            TrainingRun(
                name=data.name,
                experiment=data.experiment,
                job=job[data.name].name,
                launch=job[data.name].launch,
                line=line[data.name],
                state=state,
                checkpoints=saved,
                seed=data.file.declaration.seed if data.file else None,
            )
        )
    return ExperimentState(experiment=experiment, runs=tuple(runs))


def _closed_without_success(runs: Sequence[str]) -> set[str]:
    """Of runs that recorded no end, those this computer's launcher closed without success.

    Only a run's own process writes its end (`state.json`), so one whose
    machine was lost or deleted reads as running in storage for good. The
    launcher knows its last attempt is over. A run it calls succeeded is
    still delivering its end, and one it never launched stays running. The
    launcher's database is read as it stands: no provider or controller is
    asked, so a loss this computer's launcher has not seen yet is not known.
    """
    database = home() / "launcher.db"
    if not runs or not database.exists():
        return set()
    # Its own connection, for this thread: a served page is built on whichever thread asks,
    # and with no services, since a read reaches no machine.
    store = Database(database)
    try:
        launcher = Scheduler(store, {})
        lost = set()
        for run in runs:
            try:
                found = launcher.read(run)
            except LauncherError:
                continue  # a run this launcher never launched
            if found.done and found.outcome != "succeeded":
                lost.add(run)
        return lost
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The experiment file


type EvalFunction = Callable[[ExperimentState], object]


@dataclass(frozen=True)
class Experiment:
    """An experiment file: the experiment named for it, and its `eval(state)`."""

    name: str
    eval: EvalFunction


def read_experiment(path: Path) -> Experiment:
    """The file's experiment. The file may import its neighbours, as a job file may."""
    if path.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422) from ValueError(path)
    folder = str(path.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    found = runpy.run_path(str(path.resolve())).get("eval")
    if not callable(found):
        raise EtudeError("experiment_file_defines_no_eval", 422) from ValueError(path)
    return Experiment(name=path.stem, eval=cast(EvalFunction, found))


def asked(function: EvalFunction, state: ExperimentState) -> list[Job]:
    """The jobs `eval(state)` returns now: one, or a list or tuple of them.

    Raises `EtudeError("eval_returns_evaluation_jobs")` for anything else;
    which jobs evaluate is `etude.jobs.evaluation_spec`'s to say.
    """
    returned = function(state)
    given: list[object] = list(returned) if isinstance(returned, list | tuple) else [returned]
    jobs = [each for each in given if isinstance(each, Job)]
    if len(jobs) != len(given):
        raise EtudeError("eval_returns_evaluation_jobs", 422)
    return jobs


# ---------------------------------------------------------------------------
# One pass


class Pass(Record):
    """What one pass did for one evaluation job."""

    tournament: str
    # Matches it called for that no eval run attempted, before this pass.
    new: int
    # Of those, already being played by a run an earlier pass launched.
    running: int
    # Matches it called for that failed, with no done record since: shown, not replayed.
    failed: int = 0
    played: int = 0
    launched: str | None = None


def advance(
    experiment: Experiment,
    *,
    source: Storage,
    pool: str | None = None,
    rent: str | None = None,
    directory: Path | str | None = None,
    retry_failed: bool = False,
    smoke: Smoke = Smoke(),
    queue: bool = False,
) -> list[Pass]:
    """One pass: observe `source`, ask `eval`, and play, launch or queue what is new.

    What is wanted is what `eval` returns now (`etude.eval.tournament.played`):
    a seating it no longer returns is no longer played. Each job's eval run is
    the experiment's; a job naming another experiment is refused with
    `EtudeError("eval_job_of_another_experiment")`. A failed match is counted
    and left alone, so a result is never quietly replaced; `retry_failed`
    plays it again, and launches again what a failed launched run left unplayed.

    Without `pool` or `rent` the matches are played here, as one eval run per
    job delivered to `source`. With one, each job with new matches is launched
    to play exactly those; the machine's storage must deliver to `source`, or
    the next pass will find them still new. Raises `EtudeError("launched_pass_failed")`
    when a run an earlier pass launched failed with matches this pass wants,
    unless `retry_failed`. Each launched run records `smoke`, what was smoked
    before this pass.

    With `queue=True`, record jobs even when the chosen pool or on-demand is
    busy. Queued matches stay in flight and are not submitted again. A launcher
    controller or `etude wait` moves them when capacity becomes available.
    """
    if queue and pool is None and rent is None:
        raise EtudeError("queued_evals_need_a_pool_or_rent", 422)
    # Imported here: typed jobs import this module for `Checkpoint`.
    from etude.jobs import (  # noqa: PLC0415
        evaluation_spec,
        hold_evaluation,
        with_experiment,
        with_matches,
    )

    state = observe(source, experiment.name)
    jobs = asked(experiment.eval, state)
    ledger = _Ledger(_ledger_root(directory)) if pool or rent else None
    standing = ledger.standing() if ledger is not None else _Standing()
    passes = []
    for given_job in jobs:
        job = with_experiment(given_job, experiment.name)
        spec = evaluation_spec(job)
        checkpoints = tuple(each.ref for each in spec.checkpoints)
        found = played(
            spec.tournament,
            experiment=experiment.name,
            checkpoints=checkpoints,
            directory=directory,
            storages=[source],
            lines=state.lines,
        )
        new = [*found.new, *(found.failed if retry_failed else ())]
        pending = [match for match in new if match.key not in standing.running]
        done = Pass(
            tournament=spec.tournament.name,
            new=len(new),
            running=len(new) - len(pending),
            failed=0 if retry_failed else len(found.failed),
        )
        if not pending:
            passes.append(done)
            continue
        if ledger is None:
            held = hold_evaluation(
                spec.model_copy(update={"matches": tuple(pending), "lines": state.lines}),
                checkout=job.repo,
                directory=directory,
                storages=[source],
            )
            passes.append(done.model_copy(update={"played": held.played}))
            continue
        stopped = {standing.failed[m.key] for m in pending if m.key in standing.failed}
        if stopped and not retry_failed:
            raise EtudeError("launched_pass_failed", 409) from ValueError(sorted(stopped))
        matches = digest(sorted(m.key for m in pending))[:12]
        run = ledger.unused(f"{folder_name(spec.tournament.name)}-{matches}")
        launched = with_matches(job, pending, state)
        ledger.launch(run, launched, pending, pool=pool, rent=rent, smoke=smoke, queue=queue)
        passes.append(done.model_copy(update={"launched": run}))
    return passes


def checkpoint_inputs(matches: Iterable[Match], state: ExperimentState) -> dict[str, Checkpoint]:
    """Each checkpoint the matches seat, by its key encoded, as `state` found it stored.

    Raises `EtudeError("checkpoint_not_stored")` for a seated checkpoint no
    training run of the experiment stored, such as one by an outside address.
    """
    found: dict[str, Checkpoint] = {}
    for match in matches:
        for player in match.players:
            if not isinstance(player, Model) or str(player.checkpoint.key) in found:
                continue
            key = player.checkpoint.key
            saved = state.checkpoint(key)
            if saved is None:
                raise EtudeError("checkpoint_not_stored", 404) from ValueError(str(key))
            found[str(key)] = saved
    return found


class _Launched(Record):
    run: str
    matches: tuple[Digest, ...]


@dataclass(frozen=True)
class _Standing:
    """What the ledger's runs mean for this pass."""

    # Matches of runs not yet done.
    running: set[Digest] = field(default_factory=set)
    # Matches of runs that ended without succeeding, to what `status` said of the run.
    failed: dict[Digest, str] = field(default_factory=dict)


class _Ledger:
    """The runs passes launched, kept in one JSON file per computer.

    It only says which matches are in flight; delivered results are read from
    the source, never from here.
    """

    def __init__(self, root: Path) -> None:
        self.path = root / "launched.json"

    def _read(self) -> list[_Launched]:
        if not self.path.is_file():
            return []
        return [_Launched.model_validate(each) for each in json.loads(self.path.read_text())]

    def standing(self) -> _Standing:
        """Where every launched run stands. One ledger serves all of a computer's experiments."""
        standing = _Standing()
        for each in self._read():
            found = client.status(each.run)
            if not found.done:
                standing.running.update(each.matches)
            elif found.outcome != "succeeded":
                standing.failed.update(dict.fromkeys(each.matches, f"{each.run}: {found}"))
        return standing

    def unused(self, run: str) -> str:
        """`run`, or the next `run-N` no pass launched: a retry may be given the same matches."""
        taken = {each.run for each in self._read()}
        if run not in taken:
            return run
        return next(f"{run}-{n}" for n in count(2) if f"{run}-{n}" not in taken)

    def launch(
        self,
        run: str,
        job: Job,
        matches: Sequence[Match],
        *,
        pool: str | None,
        rent: str | None,
        smoke: Smoke,
        queue: bool,
    ) -> None:
        submit = client.queue if queue else client.launch
        submit(Run(id=run, job=job, smoke=smoke), pool, rent=rent)
        launched = [*self._read(), _Launched(run=run, matches=tuple(m.key for m in matches))]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.path.with_suffix(".tmp")
        staged.write_text(json.dumps([each.model_dump(mode="json") for each in launched]))
        staged.replace(self.path)


def _ledger_root(directory: Path | str | None) -> Path:
    return (home() if directory is None else Path(directory)) / "experiments"
