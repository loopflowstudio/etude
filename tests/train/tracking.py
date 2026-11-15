"""Construct tracked-run records for controlled storage and continuation fixtures.

Product training enters through track under an Etude-assigned run; fixtures
construct its declaration and live resource directly to inject bounded inputs.
"""

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel

from etude.records import Clock, Continuation
from etude.run import declare, destinations
from etude.storage import Storage
from etude.train.tracking import TrainingRun, _inherited


def open_run(
    *,
    experiment: str | None = None,
    clocks: Sequence[str | Clock] | None = None,
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    kind: str | None = None,
    config: BaseModel | Mapping[str, object] | str | None = None,
    continues: Continuation | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    namespace: str = "local",
    queue_size: int = 10_000,
    segment_seconds: float = 2.0,
    segment_rows: int = 5_000,
    strict: bool = False,
    metric_name_limit: int | None = 1000,
    resume: bool = False,
) -> TrainingRun:
    """A fixture declaration and the real training writer, without another public entry."""
    experiment = experiment or os.environ.get("ETUDE_EXPERIMENT") or "fixture"
    handed = _inherited()
    if handed is not None and continues is None:
        at = handed.at
        until = os.environ.get("ETUDE_UNTIL_CLOCK")
        clock = until if until in at else next(iter(at))
        continues = Continuation(parent=handed.parent, clock=clock, value=at[clock])
    declared = declare(
        experiment=experiment,
        clocks=None
        if clocks is None
        else tuple(
            Clock(name=clock, kind="step", meaning=clock) if isinstance(clock, str) else clock
            for clock in clocks
        ),
        run=run,
        job=job,
        seed=seed,
        kind=kind,
        config=config.model_dump(mode="json") if isinstance(config, BaseModel) else config,
        continues=continues,
        inherited_clocks=() if handed is None else handed.clocks,
        directory=directory,
        namespace=namespace,
        resume=resume
        or os.environ.get("ETUDE_RESUME") == "1"
        or bool(os.environ.get("ETUDE_ATTEMPT")),
    )
    return TrainingRun(
        experiment=experiment,
        name=declared.name,
        root=declared.root,
        clocks=declared.record.clocks,
        destinations=destinations(declared.base, storages),
        queue_size=queue_size,
        segment_seconds=segment_seconds,
        segment_rows=segment_rows,
        strict=strict,
        closed_clocks=clocks is not None,
        metric_name_limit=metric_name_limit,
        inherited=None if handed is None else handed.checkpoint,
        seed=declared.record.seed or 0,
    )
