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
from typing import Literal

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
    eval_runs = [
        jobs.EvalRunSummary(
            name=d.name,
            state=d.stored_state,
            last_write=d.last_write,
        )
        for d in every
        if d.file is not None and d.experiment == experiment and d.file.kind == "evaluation"
    ]
    committed = Decimal(0)
    for item in retained:
        record, status = item.record, item.status
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
            eval_runs.append(
                jobs.EvalRunSummary(
                    name=name,
                    state=_launcher_state(
                        prior.state if prior else "running",
                        item,
                    ),
                    matches=matches,
                    status=status,
                    last_write=prior.last_write if prior else None,
                )
            )
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
                runs.append(
                    jobs.TrainingRunSummary(
                        name=name,
                        experiment=experiment,
                        job=record.launch.spec.name,
                        launch=record.launch.spec.name,
                        line=name,
                        seed=record.seed,
                        state=_launcher_state("running", item),
                        checkpoints=jobs.Checkpoints(),
                        status=status,
                    )
                )
            else:
                runs[runs.index(prior_training)] = replace(
                    prior_training, state=_launcher_state(prior_training.state, item), status=status
                )
    return jobs.ExperimentState(
        experiment=experiment, runs=tuple(runs), eval_runs=tuple(eval_runs), committed_usd=committed
    )


def _launcher_state(
    state: Literal["queued", "running", "stopped", "finished", "failed"], item: Standing
) -> Literal["queued", "running", "stopped", "finished", "failed"]:
    """Stored terminal outcomes win; the launcher owns continuation after a stop."""
    if state in {"finished", "failed"}:
        return state
    if item.status.done:
        if item.status.outcome == "succeeded":
            return "stopped" if state == "stopped" else "finished"
        return "failed"
    return item.placement
