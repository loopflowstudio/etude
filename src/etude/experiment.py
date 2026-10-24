"""Experiments: an eval function that names what to evaluate, and the pass that plays what is new.

An experiment file defines `eval(state)`. `state` (`ExperimentState`, from
`observe`) is every training run under a source, with the checkpoints each
recorded and whether it has ended. `eval` returns the evaluation jobs it wants
now (`etude.jobs.evaluation`): a benchmark held for the checkpoints it chose,
and matchups of chosen checkpoints against each other. It is called again on
every pass, so a design that depends on what exists, such as each checkpoint
against the one before it, grows as checkpoints arrive.

`advance` is one pass: for each job, the matches its benchmark calls for that
no eval run delivered (`etude.eval.tournament.unplayed`), played here or
launched as a job that plays exactly those. A launched pass names its run for
the matches it plays and remembers it in a ledger beside the launcher's
records, so a later pass neither launches it again nor plays its matches while
it runs. A pass that finds one of its launched runs failed stops: rerun it
after looking, rather than spend again unattended.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import Field

from etude.carriage import CHECKPOINTS
from etude.eval.match import Tables
from etude.eval.records import Agent, Match, Model, digest
from etude.eval.tournament import hold, unplayed
from etude.launch import client
from etude.launch.records import Job, Run
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Digest, Record
from etude.run.storage import Storage
from etude.train.read import read_all

# ---------------------------------------------------------------------------
# What eval(state) chooses from


class Checkpoint(Record):
    """A checkpoint a training run recorded, with a stored file to evaluate."""

    run: str
    experiment: str
    ordinal: int = Field(ge=1)
    artifact: ArtifactRef
    # Where each of the run's clocks stood when it was saved.
    clocks: dict[str, float]
    tags: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The file's name, as a model seat names it: `updates-200.pt`."""
        return PurePosixPath(self.artifact.uri).name

    def agent(self, model: Model) -> Agent:
        """This checkpoint in `model`'s seat, for a matchup of chosen checkpoints."""
        return model.agent(self.artifact)


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
        return Checkpoints(each for each in self if each.clocks.get(clock) in wanted)

    def tagged(self, tag: str) -> Checkpoints:
        return Checkpoints(each for each in self if tag in each.tags)

    @property
    def last(self) -> Checkpoint | None:
        return self[-1] if self else None


@dataclass(frozen=True)
class TrainingRun:
    name: str
    experiment: str
    state: Literal["running", "finished", "failed"]
    checkpoints: Checkpoints

    @property
    def ended(self) -> bool:
        return self.state != "running"

    @property
    def final(self) -> Checkpoint | None:
        """The last checkpoint once the run finished; None while it may save more."""
        return self.checkpoints.last if self.state == "finished" else None


@dataclass(frozen=True)
class ExperimentState:
    """Every training run under a source, as a pass found it. Never stored."""

    runs: tuple[TrainingRun, ...] = ()

    def run(self, name: str) -> TrainingRun:
        """The training run named `name`; raises `EtudeError("run_not_found")` without one."""
        for each in self.runs:
            if each.name == name:
                return each
        raise EtudeError("run_not_found", 404) from ValueError(name)

    def experiment(self, name: str) -> tuple[TrainingRun, ...]:
        """The training runs of one experiment, by run name."""
        return tuple(each for each in self.runs if each.experiment == name)

    def checkpoint(self, sha256: Digest) -> Checkpoint | None:
        for each in self.runs:
            for saved in each.checkpoints:
                if saved.artifact.sha256 == sha256:
                    return saved
        return None


def observe(source: Storage | Path | str) -> ExperimentState:
    """The training runs under a directory or bucket and their stored checkpoints.

    A checkpoint whose file Etude did not store, so no digest, is left out:
    nothing can be evaluated without its bytes.
    """
    runs = []
    for data in read_all(source):
        if data.file is None or data.file.declaration.kind == "evaluation":
            continue
        saved = Checkpoints(
            Checkpoint(
                run=data.name,
                experiment=data.experiment,
                ordinal=record.ordinal,
                artifact=record.artifact,
                clocks=record.at,
                tags=record.tags,
            )
            for record in sorted(data.checkpoints, key=lambda one: one.ordinal)
            if record.artifact.sha256 is not None and record.artifact.size_bytes is not None
        )
        state = "running" if data.state is None else data.state.state
        runs.append(
            TrainingRun(name=data.name, experiment=data.experiment, state=state, checkpoints=saved)
        )
    return ExperimentState(runs=tuple(runs))


# ---------------------------------------------------------------------------
# The experiment file


type EvalFunction = Callable[[ExperimentState], object]


def read_experiment(path: Path) -> EvalFunction:
    """The file's `eval(state)`. The file may import its neighbours, as a job file may."""
    if path.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422) from ValueError(path)
    folder = str(path.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    found = runpy.run_path(str(path.resolve())).get("eval")
    if not callable(found):
        raise EtudeError("experiment_file_defines_no_eval", 422) from ValueError(path)
    return cast(EvalFunction, found)


# ---------------------------------------------------------------------------
# One pass


class Pass(Record):
    """What one pass did for one evaluation job."""

    tournament: str
    # Matches it called for that no eval run delivered, before this pass.
    new: int
    # Of those, already being played by a run an earlier pass launched.
    running: int
    played: int = 0
    launched: str | None = None


def advance(
    eval_function: EvalFunction,
    *,
    source: Storage,
    pool: str | None = None,
    rent: str | None = None,
    directory: Path | str | None = None,
) -> list[Pass]:
    """One pass: observe `source`, ask `eval_function`, and play or launch what is new.

    Without `pool` or `rent` the matches are played here, as one eval run per
    job delivered to `source`. With one, each job with new matches is launched
    to play exactly those; the machine's storage must deliver to `source`, or
    the next pass will find them still new. Raises `EtudeError("launched_pass_failed")`
    when a run an earlier pass launched failed.
    """
    # Imported here: typed jobs import this module for `Checkpoint`.
    from etude.jobs import Evaluation, evaluation_spec, with_matches  # noqa: PLC0415

    state = observe(source)
    returned = eval_function(state)
    given: list[object] = list(returned) if isinstance(returned, list | tuple) else [returned]
    jobs = [each for each in given if isinstance(each, Job)]
    if len(jobs) != len(given):
        raise EtudeError("eval_returns_evaluation_jobs", 422)
    ledger = _Ledger(_ledger_root(directory)) if pool or rent else None
    running = ledger.running() if ledger is not None else set()
    passes = []
    for job in jobs:
        spec: Evaluation = evaluation_spec(job)
        checkpoints = tuple(each.artifact for each in spec.checkpoints)
        new = unplayed(
            spec.tournament,
            experiment=spec.experiment,
            checkpoints=checkpoints,
            directory=directory,
            storages=[source],
        )
        pending = [match for match in new if match.key not in running]
        done = Pass(tournament=spec.tournament.name, new=len(new), running=len(new) - len(pending))
        if not pending:
            passes.append(done)
            continue
        if ledger is None:
            play = cast(Callable[[Match], Tables], spec.play.load(job.repo))
            held = hold(
                spec.tournament,
                experiment=spec.experiment,
                play=play,
                checkpoints=checkpoints,
                matches=pending,
                workers=spec.workers,
                directory=directory,
                storages=[source],
            )
            passes.append(done.model_copy(update={"played": held.played}))
            continue
        run = f"{spec.tournament.name}-{digest(sorted(m.key for m in pending))[:12]}"
        launched = with_matches(job, pending, state)
        ledger.launch(run, launched, pending, pool=pool, rent=rent)
        passes.append(done.model_copy(update={"launched": run}))
    return passes


def checkpoint_inputs(matches: Iterable[Match], state: ExperimentState) -> dict[Digest, Checkpoint]:
    """Each checkpoint the matches seat, by digest, as `state` found it stored."""
    found: dict[Digest, Checkpoint] = {}
    for match in matches:
        for seated in match.agents:
            if seated.kind != "checkpoint" or seated.artifact in found:
                continue
            saved = state.checkpoint(seated.artifact)
            if saved is None:
                raise EtudeError("checkpoint_not_stored", 404) from ValueError(seated.artifact)
            found[seated.artifact] = saved
    return found


def checkpoint_file(saved: Checkpoint) -> str:
    """The checkpoint's key within its run's folder, where a job input finds it."""
    return f"{CHECKPOINTS}/{saved.name}"


class _Launched(Record):
    run: str
    matches: tuple[Digest, ...]


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

    def running(self) -> set[Digest]:
        """Matches of launched runs not yet done. Raises when one failed."""
        keys: set[Digest] = set()
        for each in self._read():
            found = client.status(each.run)
            if not found.done:
                keys.update(each.matches)
            elif found.outcome != "succeeded":
                raise EtudeError("launched_pass_failed", 409) from ValueError(
                    f"{each.run}: {found}"
                )
        return keys

    def launch(
        self,
        run: str,
        job: Job,
        matches: Sequence[Match],
        *,
        pool: str | None,
        rent: str | None,
    ) -> None:
        client.launch(Run(id=run, job=job), pool, rent=rent)
        launched = [*self._read(), _Launched(run=run, matches=tuple(m.key for m in matches))]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staged = self.path.with_suffix(".tmp")
        staged.write_text(json.dumps([each.model_dump(mode="json") for each in launched]))
        staged.replace(self.path)


def _ledger_root(directory: Path | str | None) -> Path:
    if directory is not None:
        return Path(directory) / "experiments"
    return Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser() / "experiments"
