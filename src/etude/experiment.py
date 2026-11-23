"""Read an experiment's stored and scheduler state without keeping pass memory.

`observe` reads a source once and merges the launcher's public `standing`;
`state_of` does the same for a page's already-read data, using only local records.
Author-facing types are defined in `etude.jobs`.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    runs = []
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
        runs.append(
            jobs.TrainingRunSummary(
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
        )
    evaluations = {
        d.name: d
        for d in every
        if d.file and d.experiment == experiment and d.file.kind == "evaluation"
    }
    eval_runs = [
        jobs.EvalRunSummary(
            name=d.name,
            state=d.stored_state,
            last_write=d.last_write,
        )
        for d in evaluations.values()
    ]
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
            prior = next((r for r in eval_runs if r.name == name), None)
            if prior is not None:
                eval_runs.remove(prior)
            summary = (
                replace(prior, matches=matches)
                if prior
                else jobs.EvalRunSummary(name=name, state="queued", matches=matches)
            )
            eval_runs.append(_join_allocation(summary, evaluations.get(name), item))
        else:
            prior_training = next(
                (
                    r
                    for r in runs
                    if r.name == name
                    or (r.launch == record.launch.spec.name and r.seed == record.seed)
                ),
                None,
            )
            if prior_training is None:
                prior_training = jobs.TrainingRunSummary(
                    name=name,
                    experiment=experiment,
                    job=record.launch.spec.name,
                    launch=record.launch.spec.name,
                    line=name,
                    seed=record.seed,
                    state="queued",
                    checkpoints=jobs.Checkpoints(),
                )
                runs.append(_join_allocation(prior_training, None, item))
            else:
                runs[runs.index(prior_training)] = _join_allocation(
                    prior_training, prior_training._data, item
                )
    return jobs.ExperimentState(
        experiment=experiment, runs=tuple(runs), eval_runs=tuple(eval_runs), committed_usd=committed
    )


def _join_allocation[Run: (jobs.TrainingRunSummary, jobs.EvalRunSummary)](
    run: Run, data: RunData | None, item: Standing
) -> Run:
    """Join the accepted file outcome to the allocation that could have written it."""
    status = item.status
    writers = data.writers if data else ()
    executing = status.attempt is not None and any(w.attempt == status.attempt for w in writers)
    outcome = data.outcome if data else None
    if outcome:
        # read_located accepts only the latest writer's named end, or a
        # legacy end with at most one writer. A resume needs positive attribution.
        writer = writers[-1] if writers else None
        ended_on = writer.attempt if writer else None
        ended_here = (
            status.attempt is not None
            and ended_on == status.attempt
            and writer is not None
            and outcome.writer == writer.writer
        )
        if ended_on and status.attempt and ended_on != status.attempt:
            outcome = None
        elif item.record.launch.resume_after and not ended_here:
            outcome = None
        elif outcome.state != "stopped":
            return replace(run, state=outcome.state, status=status)
        elif ended_here and status.phase not in ("queued", "done"):
            # The tracking context stopped, but the launcher still owns continuation.
            return replace(run, state=item.placement, allocation_phase="stopped", status=status)

    if status.done:
        if status.outcome == "succeeded":
            return replace(
                run,
                state="stopped" if outcome and outcome.state == "stopped" else "finished",
                status=status,
            )
        return replace(run, state="failed", status=status)
    phase = status.phase
    if phase in ("renting", "setting_up") and executing:
        return replace(run, state="running", status=status)
    return replace(
        run,
        state=item.placement,
        status=status,
        allocation_phase=phase if phase in ("renting", "setting_up", "stopping") else None,
    )
