"""Small test panels executed through the public EvalRun lifetime."""

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from etude.checkpoints import CheckpointRef
from etude.eval.records import (
    Benchmark,
    Environment,
    Line,
    Match,
    Player,
    Rollup,
    RoundRobin,
)
from etude.eval.result import EvalResult, Result, Tables
from etude.eval.tournament import _open_evaluation, evaluating
from etude.jobs import Agents
from etude.storage import Storage


def play_matches(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    play: Callable[..., Awaitable[Tables | Result]],
    agents: Agents | None = None,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
) -> EvalResult:
    # Rollups belong to the low-level run when the fixture has no generator.
    with _open_evaluation(
        environment,
        experiment=experiment,
        matches=matches,
        run=run,
        directory=directory,
        storages=storages,
        rollups=rollups,
        resume=True,
    ) as active:
        return active.play(play, agents=agents)


def play_tournament(
    tournament: Benchmark | RoundRobin,
    *,
    experiment: str,
    play: Callable[..., Awaitable[Tables | Result]],
    agents: Agents | None = None,
    checkpoint: CheckpointRef | None = None,
    checkpoints: Sequence[CheckpointRef] = (),
    enter: Sequence[Player] = (),
    matches: Sequence[Match] | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
    lines: Sequence[Line] = (),
) -> EvalResult:
    if rollups:
        tournament = tournament.model_copy(update={"rollups": tuple(rollups)})
    with evaluating(
        tournament,
        experiment=experiment,
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        enter=enter,
        matches=matches,
        directory=directory,
        storages=storages,
        lines=lines,
        resume=True,
    ) as active:
        return active.play(play, agents=agents)
