"""Read an experiment's stored and scheduler state without keeping pass memory.

`observe` reads a source once and merges the launcher's public `standing`;
`state_of` does the same for a page's already-read data, using only local records.
Author-facing types are defined in `etude.jobs`.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

import etude.jobs as jobs
from etude.checkpoints import CheckpointRecord
from etude.eval.records import Match
from etude.launch import client
from etude.launch.records import Standing
from etude.storage import Storage
from etude.train.read import RunData, lines, read_all
from etude.train.report import job_names

# ---------------------------------------------------------------------------
# What eval(state) chooses from


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
    so (`_closed_without_success`), and so ends at its last stored checkpoint.
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
    statuses = {r.record.id: r.status for r in retained}
    lost = {
        name.removeprefix(experiment + "/")
        for name, status in statuses.items()
        if status.done and status.outcome != "succeeded"
    }
    runs = []
    for data in ours:
        saved = jobs.Checkpoints(
            record.model_copy(
                update={"run": data.name, "experiment": data.experiment, "line": line[data.name]}
            )
            for record in data.checkpoints
            if record.artifact.sha256 is not None and record.artifact.size_bytes is not None
        )
        # A planned stop is not an end: the launcher calls the recipe again.
        state: Literal["queued", "running", "finished", "failed"] = (
            data.outcome.state
            if data.outcome is not None and data.outcome.state != "stopped"
            else "failed"
            if data.name in lost
            else "running"
        )
        runs.append(
            jobs.TrainingRunSummary(
                name=data.name,
                experiment=data.experiment,
                job=job[data.name].name,
                launch=job[data.name].launch,
                line=line[data.name],
                state=state,
                checkpoints=saved,
                seed=data.file.seed if data.file else None,
            )
        )
    eval_runs = [
        jobs.EvalRunSummary(
            name=d.name,
            state="running"
            if d.outcome is None or d.outcome.state == "stopped"
            else d.outcome.state,
        )
        for d in every
        if d.file is not None and d.experiment == experiment and d.file.kind == "evaluation"
    ]
    committed = Decimal(0)
    for item in retained:
        record, status = item.record, item.status
        committed += status.usd if status.done else record.launch.spec.max_usd or Decimal(0)
        state = (
            "queued"
            if status.phase in {"queued", "renting"}
            else "failed"
            if status.done and status.outcome != "succeeded"
            else "running"
        )
        # Evaluation matches are part of the executable job's public config.
        text = record.launch.spec.config
        config = json.loads(text) if text else {}
        if "tournament" in config and "matches" in config:
            matches = tuple(Match.model_validate(m).key for m in config.get("matches") or ())
            prior = next(
                (r for r in eval_runs if r.name == record.id.removeprefix(experiment + "/")), None
            )
            if prior is not None:
                eval_runs.remove(prior)
                if prior.state in {"finished", "failed"}:
                    state = prior.state
            eval_runs.append(jobs.EvalRunSummary(name=record.id, state=state, matches=matches))
        else:
            prior_training = next(
                (
                    r
                    for r in runs
                    if r.name in {record.id, record.id.removeprefix(experiment + "/")}
                    or (r.launch == record.launch.spec.name and r.seed == record.seed)
                ),
                None,
            )
            if prior_training is None:
                runs.append(
                    jobs.TrainingRunSummary(
                        name=record.id,
                        experiment=experiment,
                        job=record.launch.spec.name,
                        launch=record.launch.spec.name,
                        line=record.id,
                        seed=record.seed,
                        state=state,
                        checkpoints=jobs.Checkpoints(),
                    )
                )
    return jobs.ExperimentState(
        experiment=experiment, runs=tuple(runs), eval_runs=tuple(eval_runs), committed_usd=committed
    )


# ---------------------------------------------------------------------------


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
