"""Read an experiment's stored and scheduler state without keeping pass memory.

`observe` reads a source once and merges the launcher's public `standing`;
`state_of` does the same for a page's already-read data, using only local records.
Author-facing types are defined in `etude.jobs`.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Literal

import etude.jobs as jobs
from etude.checkpoints import CheckpointRecord
from etude.eval.records import Match
from etude.launch import client
from etude.launch.records import Standing
from etude.out import OutMember
from etude.records import RunOutcome
from etude.storage import Storage
from etude.train.read import RunData, lines, read_all
from etude.train.report import job_names


def observe(
    source: Storage | Path | str, experiment: str, *, keys: Sequence[str] | None = None
) -> jobs.ExperimentState:
    """The experiment's training runs under a directory or bucket and their stored checkpoints.

    A checkpoint whose file Etude did not store, so no digest, is left out:
    nothing can be evaluated without its bytes. Runs' records alone are read:
    no metric block or index.
    """
    return state_of(
        read_all(source, experiment, metrics=frozenset(), keys=keys),
        experiment,
        retained=client.standing(experiment),
    )


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
    retained = client.standing(experiment, local=True) if retained is None else retained
    runs = []
    for data in ours:

        def _read_out(data: RunData = data) -> tuple[OutMember, ...]:
            return data.out

        saved = jobs.Checkpoints(
            record.model_copy(
                update={"run": data.name, "experiment": data.experiment, "line": line[data.name]}
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
                state=_stored_state(data.outcome),
                checkpoints=saved,
                seed=data.file.seed if data.file else None,
                _read_out=_read_out,
            )
        )
    eval_runs = [
        jobs.EvalRunSummary(
            name=d.name,
            state=_stored_state(d.outcome),
        )
        for d in every
        if d.file is not None and d.experiment == experiment and d.file.kind == "evaluation"
    ]
    committed = Decimal(0)
    for item in retained:
        record = item.record
        committed += item.committed_usd
        name = record.id.removeprefix(experiment + "/")
        # Evaluation matches are part of the executable job's public config.
        text = record.launch.spec.config
        config = json.loads(text) if text else {}
        if "tournament" in config and "matches" in config:
            matches = tuple(Match.model_validate(m).key for m in config.get("matches") or ())
            prior = next((r for r in eval_runs if r.name == name), None)
            if prior is not None:
                eval_runs.remove(prior)
            eval_runs.append(
                jobs.EvalRunSummary(
                    name=name,
                    state=_launcher_state(prior.state if prior else "running", item),
                    matches=matches,
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
                    )
                )
            else:
                runs[runs.index(prior_training)] = replace(
                    prior_training, state=_launcher_state(prior_training.state, item)
                )
    return jobs.ExperimentState(
        experiment=experiment, runs=tuple(runs), eval_runs=tuple(eval_runs), committed_usd=committed
    )


def _stored_state(
    outcome: RunOutcome | None,
) -> Literal["running", "stopped", "finished", "failed"]:
    return outcome.state if outcome is not None else "running"


def _launcher_state(
    state: Literal["queued", "running", "stopped", "finished", "failed"], item: Standing
) -> Literal["queued", "running", "stopped", "finished", "failed"]:
    """Stored terminal outcomes win; the launcher owns continuation after a stop."""
    if state in {"finished", "failed"}:
        return state
    if item.status.done:
        return state if item.status.outcome == "succeeded" else "failed"
    return item.placement


def __getattr__(name: str) -> object:
    if name == "TrainingRun":
        warnings.warn(
            "etude.experiment.TrainingRun is now etude.jobs.TrainingRunSummary; removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
        return jobs.TrainingRunSummary
    if name == "Checkpoint":
        warnings.warn(
            "etude.experiment.Checkpoint is now etude.checkpoints.CheckpointRecord; removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
        return CheckpointRecord
    raise AttributeError(name)
