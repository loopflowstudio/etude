"""Small test panels executed through the public EvalRun lifetime."""

from collections.abc import Awaitable, Callable, Sequence
from multiprocessing.connection import Connection, wait
from pathlib import Path

import pytest

from etude.checkpoints import CheckpointRef
from etude.eval.bus import ForwardOutcome
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


def identify_outcome(request_id: int, outcome: ForwardOutcome) -> tuple[int, ForwardOutcome]:
    return request_id, outcome


def wait_for_games(monkeypatch: pytest.MonkeyPatch, *, processes: int, rounds: int = 1) -> None:
    """Synchronize fixture grants or outcomes without relying on child startup order."""

    def ready_games(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal rounds
        pipes = [handle for handle in handles if isinstance(handle, Connection)]
        if not rounds:
            return wait(handles, timeout=timeout)
        assert len(pipes) == processes
        rounds -= 1
        for pipe in pipes:
            assert pipe.poll(10), "a fixture game did not send its message"
        return pipes

    monkeypatch.setattr("etude.eval.run.wait", ready_games)


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
