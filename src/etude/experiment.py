"""Read an experiment's stored and scheduler state without keeping pass memory.

`observe` reads a source once and merges the launcher's public `standing`;
`state_of` does the same for a page's already-read data, using only local records.
Author-facing types are defined in `etude.jobs`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from graphlib import TopologicalSorter
from pathlib import Path

import etude.jobs as jobs
from etude.checkpoints import PolicyKey
from etude.errors import EtudeError
from etude.eval.records import Benchmark, Line, Match, TournamentRun
from etude.launch import client
from etude.launch.records import Standing
from etude.run import LocatedRun, RunListing
from etude.storage import Storage
from etude.train.read import RunData, lines, read_all
from etude.train.report import job_names

__all__ = ("observe",)


def observe(
    source: Storage | Path | str,
    experiment: str,
    *,
    keys: Sequence[str] | None = None,
    declarations: Sequence[LocatedRun] | None = None,
) -> jobs.ExperimentState:
    """The experiment's training runs under a directory or bucket and their stored checkpoints.

    A checkpoint whose file Etude did not store, so no digest, is left out:
    nothing can be evaluated without its bytes. Runs' records alone are read:
    no metric block or index.
    """
    return state_of(
        read_all(source, experiment, metrics=frozenset(), keys=keys, declarations=declarations),
        experiment,
        retained=client.standing(experiment),
    )


def read_tournament_lines(
    source: Storage,
    state: jobs.ExperimentState,
    tournaments: Sequence[TournamentRun],
    *,
    listing: RunListing,
) -> tuple[Line, ...]:
    """Read the lines reachable by these tournaments, sharing one source discovery."""
    named: set[str] = set()
    for tournament in tournaments:
        benchmark = tournament.tournament
        if not isinstance(benchmark, Benchmark) or not any(s.relative for s in benchmark.seatings):
            continue
        named.update(benchmark.experiments)
        named.update(
            c.policy.experiment
            for c in tournament.checkpoints
            if c.policy is not None and c.policy.experiment is not None
        )
    found = list(state.lines)
    for name in sorted(named - {state.experiment}):
        found.extend(
            line.seen_from(state.experiment)
            for line in observe(
                source, name, keys=listing.keys, declarations=listing.declarations
            ).lines
        )
    return tuple(found)


def find_training_run(
    runs: Iterable[jobs.TrainingRunSummary], address: str | None, job: str, seed: int | None
) -> jobs.TrainingRunSummary | None:
    """Match the run name, or its recorded job and seed for compact spellings."""
    for run in runs:
        if address is not None and run.name == address.removeprefix(run.experiment + "/"):
            return run
        if run.launch == job and run.seed == seed:
            return run
    return None


def state_of(
    every: Sequence[RunData], experiment: str, *, retained: tuple[Standing, ...] | None = None
) -> jobs.ExperimentState:
    """The experiment's training runs among runs already read, as `observe` finds them.

    A run that recorded no end has failed once this computer's launcher says
    so, and so ends at its last stored checkpoint.
    """
    ours = [
        data
        for data in every
        if data.file is not None
        and data.experiment == experiment
        and data.file.kind != "evaluation"
    ]
    line = lines(ours)
    job = job_names(ours)
    by_name = {data.name: data for data in ours}
    retained = client.standing(experiment, local=True) if retained is None else retained
    runs: dict[str, jobs.TrainingRunSummary] = {}
    # Parent records precede continuations even when their names sort the other way.
    parents = {
        data.name: (data.file.continues.parent.source_id,)
        if data.file is not None
        and data.file.continues is not None
        and data.file.continues.parent.source_id in by_name
        else ()
        for data in ours
    }
    for name in TopologicalSorter(parents).static_order():
        data = by_name[name]
        first = by_name.get(line[data.name], data)
        saved = jobs.Checkpoints(
            record.model_copy(
                update={
                    "run": data.name,
                    "experiment": data.experiment,
                    "line": line[data.name],
                    "policy": PolicyKey(
                        experiment=data.experiment,
                        job=job[first.name].name,
                        seed=first.file.seed if first.file else None,
                        role=record.role,
                    ),
                }
            )
            for record in data.checkpoints
            if record.artifact.sha256 is not None and record.artifact.size_bytes is not None
        )
        runs[name] = jobs.TrainingRunSummary(
            name=data.name,
            experiment=data.experiment,
            job=job[data.name].name,
            launch=job[data.name].launch,
            line=line[data.name],
            state=data.stored_state,
            checkpoints=saved,
            seed=data.file.seed if data.file else None,
            _data=data,
            last_write=data.last_write,
        )
    evaluations = {
        d.name: d
        for d in every
        if d.file and d.experiment == experiment and d.file.kind == "evaluation"
    }
    launched_evals: list[jobs.EvalRunSummary] = []
    committed = Decimal(0)
    for item in retained:
        record = item.record
        committed += item.committed_usd
        name = record.id.removeprefix(experiment + "/")
        # Evaluation matches are part of the executable job's public config.
        try:
            group = jobs.evaluation_group(record.launch.spec)
        except EtudeError as error:
            if error.code != "not_an_evaluation_job":
                raise
            group = None
        if group is not None:
            matches = tuple(
                Match.model_validate(m.model_dump(), context={"experiment": experiment}).key
                for spec in group.evaluations
                for m in spec.matches
            )
            stored_eval = evaluations.pop(name, None)
            summary = jobs.EvalRunSummary(
                name=name,
                state="queued",
                matches=matches,
                last_write=stored_eval.last_write if stored_eval else None,
            )
            launched_evals.append(_join_allocation(summary, stored_eval, item))
        else:
            launch = record.launch.spec.name
            training = find_training_run(runs.values(), record.id, launch, record.seed)
            if training is None:
                training = jobs.TrainingRunSummary(
                    name=name,
                    experiment=experiment,
                    job=launch,
                    launch=launch,
                    line=name,
                    seed=record.seed,
                    state="queued",
                    checkpoints=jobs.Checkpoints(),
                )
            runs[training.name] = _join_allocation(training, training._data, item)
    file_evals = (
        jobs.EvalRunSummary(name=d.name, state=d.stored_state, last_write=d.last_write)
        for d in evaluations.values()
    )
    return jobs.ExperimentState(
        experiment=experiment,
        runs=tuple(runs.values()),
        eval_runs=(*file_evals, *launched_evals),
        committed_usd=committed,
    )


def _join_allocation[Run: (jobs.TrainingRunSummary, jobs.EvalRunSummary)](
    run: Run, data: RunData | None, item: Standing
) -> Run:
    """Join the accepted file outcome to the allocation that could have written it."""
    status = item.status
    writers = data.writers if data else ()
    latest = writers[-1] if writers else None
    outcome = data.outcome if data else None
    # read_located accepts the latest writer's end, or an anonymous legacy end.
    # After an explicit resume, only an end attributed to the new allocation counts.
    ended_here = False
    if outcome and latest and status.attempt is not None:
        if latest.attempt == status.attempt:
            ended_here = outcome.writer == latest.writer
        elif latest.attempt and status.attempt:
            outcome = None
    if item.record.launch.resume_after and not ended_here:
        outcome = None

    phase = status.phase
    state: jobs.RunState
    allocation_phase: jobs.AllocationPhase | None = None
    if outcome and (outcome.state != "stopped" or (status.done and status.outcome == "succeeded")):
        state = outcome.state
    elif status.done:
        state = "finished" if status.outcome == "succeeded" else "failed"
    elif ended_here and phase != "queued":
        # The writer stopped, but the launcher still owns continuation.
        state, allocation_phase = item.placement, "stopped"
    elif (
        phase in ("renting", "setting_up")
        and status.attempt is not None
        and any(w.attempt == status.attempt for w in writers)
    ):
        state = "running"
    else:
        state = item.placement
        allocation_phase = phase if phase in ("renting", "setting_up", "stopping") else None
    return replace(run, state=state, status=status, allocation_phase=allocation_phase)
