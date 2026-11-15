"""Environment descriptions and the deprecated one-call evaluation spelling.

Execution and storage belong to EvalRun; evaluating opens its lifetime.
The evaluate wrapper warns until 0.5 and delegates to that same resource.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from etude.eval.records import (
    Environment,
    Match,
    Rollup,
    as_config,
)
from etude.eval.result import EvalResult as EvalResult
from etude.eval.result import Result as Result
from etude.eval.result import Tables as Tables
from etude.eval.run import ETUDE_TABLES as ETUDE_TABLES
from etude.eval.run import _evaluating_matches
from etude.eval.source import digest_files
from etude.storage import Storage

KIND = "evaluation"


def environment(
    name: str,
    *,
    files: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Environment:
    """What matches are played in: its defining files, hashed like code, and its config."""
    return Environment(name=name, files=digest_files(files), config=as_config(config))


def evaluate(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    play: Callable[..., Tables | Result],
    agents: Agents | None = None,
    workers: int = 1,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
) -> EvalResult:
    """Play every match not already stored in this eval run.

    The deprecated workers option is ignored; Etude sizes the game pool.

    Matches start in the order `etude.eval.schedule.ordered` gives, so
    consecutive matches reuse the checkpoints the author's runtime keeps loaded.

    The eval run is named `run` (see `etude.run.declare` for its
    defaults); resuming it requires the same environment and `rollups`
    (`EtudeError("eval_run_rollups_conflict")`): a sealed block's partial
    totals are never taken again. Prints one line to
    stderr as each match is stored: how many of the pending are, and since
    when. Raises
    `EtudeError("match_failed")` when a match raised, returned no rows or
    returned rows Etude refuses, after retaining all in-flight outcomes.
    Delivery failures raise RetryRequested; files stay in the run's directory
    for `etude sync`.
    """
    warnings.warn(
        "evaluate is now evaluating(...), then run.play(play); removed in 0.5",
        DeprecationWarning,
        stacklevel=2,
    )
    with _evaluating_matches(
        experiment=experiment,
        environment=environment,
        matches=matches,
        run=run,
        directory=directory,
        storages=storages,
        rollups=rollups,
        resume=True,
    ) as active:
        return active.play(play, agents=agents)


def __getattr__(name: str) -> object:
    if name == "EvalRun":
        warnings.warn(
            "etude.eval.match.EvalRun is now EvalResult; removed in 0.5. "
            "The live run is etude.eval.run.EvalRun",
            DeprecationWarning,
            stacklevel=2,
        )
        return EvalResult
    raise AttributeError(name)


if TYPE_CHECKING:
    from etude.jobs import Agents
