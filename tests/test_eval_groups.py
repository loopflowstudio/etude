"""Grouped evals share agents and preserve tournament delivery and admission."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import nullcontext
from decimal import Decimal
from multiprocessing import active_children
from multiprocessing.connection import Connection, wait
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import cast

import duckdb
import pytest
from pydantic import ValidationError

from etude.call import Function
from etude.carriage import put_back, restore_set
from etude.checkpoints import CheckpointRef
from etude.controller.advance import advance
from etude.controller.declarations import Experiment, read_experiment
from etude.controller.smoke import Step, smoke_file
from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval import resources as eval_resources
from etude.eval.agents import (
    Answer,
    BatchRequest,
    CheckpointRequest,
    Forward,
    ForwardError,
    ModelForward,
    recv_frame,
    send_frame,
)
from etude.eval.batching import _add_socket_batch, _Batches, _encode_socket
from etude.eval.bus import ForwardOutcome
from etude.eval.game import GameReport, Matches
from etude.eval.match import environment
from etude.eval.query import _kept_path, statsdb
from etude.eval.records import Code, Config, Match, RoundRobin, checkpoint_player
from etude.eval.resources import (
    Placement,
    _has_gpu,
    available_cpus,
    memory_bytes,
    place,
    resources,
)
from etude.eval.result import Result, Tables
from etude.eval.run import Terminated
from etude.eval.tournament import evaluating, played
from etude.experiment import observe, state_of
from etude.jobs import (
    Agents,
    EvalGroup,
    EvalJob,
    Evaluation,
    ExperimentState,
    evaluation_group,
    hold_group,
)
from etude.launch import client
from etude.launch.records import Pool, Standing, Status
from etude.page import build as build_page
from etude.records import (
    ArtifactRef,
    Record,
    RunCounters,
    RunOutcome,
    RunProgress,
    RunRecord,
    RunSubmission,
)
from etude.run import sync
from etude.spec import Job, RunRequest
from etude.storage import DirectoryStorage, default_root
from etude.train.read import read_all
from tests.eval.arithmetic import checkpoint, load, play
from tests.eval.thread_budget import load_array_checkpoint, load_checkpoint
from tests.launch.counting import Counting, job_text
from tests.launch.fake import bench
from tests.run.storage import MeasuredStorage
from tests.test_jobs import checkout
from tests.train.tracking import open_run


@pytest.fixture(autouse=True)
def shared_agents(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Exercise shared transport with bounded, injected hardware; CPU tests opt out."""
    shared = getattr(request, "param", True)
    monkeypatch.setattr(eval_resources, "_has_gpu", lambda: shared)


async def plain(match: Match, agents: list[object]) -> Tables:
    return {"scores": [{"seat": 0, "score": 7}]}


def tournament(name: str) -> RoundRobin:
    return RoundRobin(
        name=name, environment=environment("grouped"), configs=({"lane": name},), games=1
    )


def job(
    name: str,
    agents: Agents | None,
    playing: Callable[..., Awaitable[Tables]] | None = None,
) -> EvalJob:
    return EvalJob(
        tournament=tournament(name),
        enter=(checkpoint("left"), checkpoint("right")),
        play=playing or (plain if agents is None else play),
        agents=agents or Agents(),
        repo=None,
        max_hours=0.1,
        max_usd=Decimal(1),
    )


def group(*jobs: EvalJob) -> EvalGroup:
    return EvalGroup(
        evaluations=tuple(
            spec
            for job in jobs
            for spec in evaluation_group(
                job.lower(ExperimentState(experiment="grouped"))
            ).evaluations
        )
    )


def test_a_pass_groups_by_agents_and_keeps_group_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    agents = Agents(load=load)
    jobs = [job("one", agents), job("two", agents), job("plain", None)]

    def declare(state: ExperimentState) -> list[EvalJob]:
        return jobs

    decisions = advance(
        Experiment(name="grouped", eval=declare),
        source=DirectoryStorage(tmp_path / "source"),
        pool="waiting",
        mode="queue",
    )
    records = b.scheduler.runs()
    assert len(records) == 2
    assert decisions[0].launched == decisions[1].launched != decisions[2].launched
    assert sorted(r.launch.spec.require_hours() for r in records) == [0.1, 0.2]
    assert sorted(r.launch.spec.max_usd or Decimal(0) for r in records) == [Decimal(1), Decimal(2)]
    assert not (tmp_path / "events").exists()


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("budget", ["1", "2"])
def test_pending_group_queues_during_training_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history: bool, budget: str
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")

    def declare(state: ExperimentState) -> list[EvalJob]:
        return [job("one", None), job("two", None)]

    if history:
        # A tiny recorded pace must not postpone new matches during training.
        for name in ("one", "two"):
            with evaluating(
                tournament(name),
                experiment="grouped",
                enter=(Code(name="earlier"), checkpoint("right")),
                directory=source.root,
            ) as active:
                for match in active.matches:
                    active.store(match, asyncio.run(plain(match, [])), seconds=0.01)
    definition = Experiment(name="grouped", eval=declare)
    measured = MeasuredStorage(source)
    counts = Counting(b.store, b.service)
    with open_run(experiment="grouped", run="train@0", directory=source.root):
        with counts.record():
            decisions = advance(
                definition,
                source=measured,
                pool="waiting",
                rent="l4",
                mode="queue",
                max_usd=Decimal(budget),
            )
        assert (measured.listings, measured.requests) == (1, 10 if history else 2)
        assert all(measured.got.count(key) == 1 for key in measured.got)
        observed = counts.snapshot()
        assert observed.provider == observed.bucket == observed.ssh == observed.git == 0
        if budget == "1":
            assert all(
                d.state == "not queued" and "--max-usd" in (d.reason or "") for d in decisions
            )
            assert b.scheduler.runs() == ()
        else:
            assert all(d.new == 1 and d.state == "queued" for d in decisions)
            assert decisions[0].launched == decisions[1].launched
            assert len(b.scheduler.runs()) == 1
            again = advance(
                definition,
                source=source,
                pool="waiting",
                rent="l4",
                mode="queue",
                max_usd=Decimal(budget),
            )
            assert all(d.new == 0 and d.running == 1 and d.launched is None for d in again)
            assert len(b.scheduler.runs()) == 1


@pytest.mark.parametrize("capacity", ["new", "free", "occupied", "unavailable"])
def test_pending_group_uses_existing_room_or_one_rental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capacity: str
) -> None:
    b = bench(tmp_path, slots=2 if capacity == "free" else 1)
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    if capacity in {"free", "occupied"}:
        b.scheduler.launch(
            RunRequest(
                id="before", job=Job(command="true", repo=None, max_hours=1, max_usd=Decimal(1))
            ),
            rent="l4",
        )
        assert b.service.creates == 1

    def declare(state: ExperimentState) -> list[EvalJob]:
        return [job("one", None), job("two", None)]

    source = DirectoryStorage(tmp_path / "source")
    definition = Experiment(name="grouped", eval=declare)
    with open_run(experiment="grouped", run="training", directory=source.root):
        decisions = advance(
            definition,
            source=source,
            rent="l4",
            mode="queue" if capacity == "unavailable" else "launch",
            max_usd=Decimal(2),
        )
        assert decisions[0].launched == decisions[1].launched
        if capacity == "unavailable":
            assert b.service.creates == 0
            b.service.refuse = 1
            b.scheduler.tick()
            assert b.scheduler.read(str(decisions[0].launched)).phase == "queued"
        assert b.service.creates == (2 if capacity == "occupied" else 1)
        records = b.scheduler.runs()
        assert len([r for r in records if r.experiment == "grouped"]) == 1
        creates = b.service.creates
        again = advance(definition, source=source, rent="l4", mode="queue", max_usd=Decimal(2))
        assert all(d.new == 0 and d.running == 1 and d.launched is None for d in again)
        assert b.scheduler.runs() == records
        assert b.service.creates == creates


@pytest.mark.parametrize("transient", [False, True])
def test_group_admission_skips_deliveries_and_preserves_retry_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")
    jobs = [
        job(name, None).model_copy(
            update={
                "enter": (
                    checkpoint("left"),
                    checkpoint("right"),
                    checkpoint("third"),
                )
            }
        )
        for name in ("one", "two")
    ]
    failed: set[str] = set()
    delivered: set[str] = set()
    for selected in jobs:
        # Retain a partial delivery, then resume the remaining match and fail it.
        with evaluating(
            selected.tournament,
            experiment="grouped",
            enter=selected.enter,
            directory=source.root,
        ) as active:
            *successes, last = active.matches
            for match in successes:
                active.store(match, asyncio.run(plain(match, [])), seconds=0.01)
                delivered.add(match.key)
            failed.add(last.key)
        with (
            pytest.raises(RetryRequested if transient else EtudeError),
            evaluating(
                selected.tournament,
                experiment="grouped",
                enter=selected.enter,
                directory=source.root,
                resume=True,
            ) as active,
        ):
            if transient:
                error = RetryRequested("fixture interrupted")
                active.fail_pending(error)
                raise error
            active.fail(last, ValueError("fixture ordinary failure"))

    def declare(state: ExperimentState) -> list[EvalJob]:
        return jobs

    definition = Experiment(name="grouped", eval=declare)
    decisions = advance(definition, source=source, pool="waiting", mode="queue")
    if not transient:
        assert all(d.new == 0 and d.failed == 1 for d in decisions)
        assert b.scheduler.runs() == ()
        decisions = advance(
            definition, source=source, pool="waiting", mode="queue", retry_failed=True
        )
    assert all(d.new == 1 and d.state == "queued" for d in decisions)
    (record,) = b.scheduler.runs()
    pending = {
        match.key
        for spec in evaluation_group(record.launch.spec).evaluations
        for match in spec.matches or ()
    }
    assert pending == failed
    assert pending.isdisjoint(delivered)


def test_retained_single_eval_records_are_read_and_finished_groups_stop_being_in_flight(
    tmp_path: Path,
) -> None:
    spec = evaluation_group(job("one", None)).evaluations[0]
    old = job("one", None).plain().model_copy(update={"config": spec.model_dump_json()})
    assert evaluation_group(old).evaluations == (spec,)
    assert EvalGroup.model_validate_json(spec.model_dump_json()).evaluations == (spec,)
    # The launched config names all member matches, independent of stored member names.
    source = DirectoryStorage(tmp_path)
    selected = played(
        spec.definition, experiment="grouped", enter=spec.tournament.entrants, storages=[source]
    ).new
    spec = spec.model_copy(update={"matches": tuple(selected)})
    grouped = old.model_copy(update={"config": EvalGroup(evaluations=(spec,)).model_dump_json()})
    status = Status(
        run="grouped/evals-test",
        phase="done",
        reason=None,
        outcome="succeeded",
        cause=None,
        until=None,
        clocks={},
        usd=Decimal(0),
        attempts=1,
        rental_ended=True,
    )
    retained = Standing(
        record=RunRecord(
            id=status.run,
            experiment="grouped",
            job="eval",
            submission=RunSubmission(spec=grouped, pool="local"),
        ),
        status=status,
    )
    state = state_of((), "grouped", retained=(retained,))
    assert not state.runs
    assert state.eval_runs[0].state == "finished"
    assert state.eval_runs[0].matches == tuple(m.key for m in selected)


def test_unknown_durations_rent_instead_of_stranding_new_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")

    def declare(state: ExperimentState) -> list[EvalJob]:
        return [job("new", None)]

    with open_run(experiment="grouped", run="training", directory=source.root):
        decisions = advance(
            Experiment(name="grouped", eval=declare), source=source, pool="waiting", mode="queue"
        )
        assert decisions[0].launched is not None
        assert len(b.scheduler.runs()) == 1


def test_an_execution_deadline_is_a_retryable_failure_not_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_DEADLINE", "2000-01-01T00:00:00+00:00")
    active = job("deadline", None)
    with pytest.raises(RetryRequested, match="execution deadline"):
        hold_group(group(active), directory=tmp_path)
    result = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(result.new) == len(result.failed) == len(result.retry) == 1
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state, retry from matches").fetchall() == [("failed", True)]
        assert db.sql("select state from runs").fetchall() == [("failed",)]
    finally:
        db.close()


def test_statsdb_rebuilds_the_last_released_schema_with_retry_flags(tmp_path: Path) -> None:
    hold_group(group(job("prior", None)), directory=tmp_path)
    statsdb("grouped", tmp_path).close()
    with duckdb.connect(_kept_path("grouped", DirectoryStorage(tmp_path))) as old:
        old.sql("drop index matches_identity")
        old.sql("alter table matches drop column retry")
        old.sql("update kept.statsdb set schema = 14")
    upgraded = statsdb("grouped", tmp_path)
    try:
        assert upgraded.sql("select state, retry from matches").fetchall() == [("done", False)]
        assert upgraded.sql("select schema from kept.statsdb").fetchone() == (16,)
    finally:
        upgraded.close()


def test_delivery_failure_preserves_local_results_and_fails_the_run_for_retry(
    tmp_path: Path,
) -> None:
    active = job("delivery", None)
    active = active.model_copy(
        update={
            "tournament": active.tournament.model_copy(
                update={"configs": ({"case": 0}, {"case": 1})}
            )
        }
    )
    with pytest.raises(RetryRequested, match="delivery failed"):
        hold_group(
            group(active),
            directory=tmp_path / "local",
            storages=[OfflineStorage(tmp_path / "offline")],
        )
    db = statsdb("grouped", tmp_path / "local")
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)]
        assert set(db.sql("select state, retry from matches").fetchall()) == {
            ("done", False),
            ("failed", True),
        }
    finally:
        db.close()


def test_a_wait_expiring_stops_and_reaps_games_with_retryable_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expired(handles: Sequence[object], timeout: float | None = None) -> list[object]:
        return []

    monkeypatch.setattr("etude.eval.run.wait", expired)
    monkeypatch.setenv("ETUDE_DEADLINE", "2099-01-01T00:00:00+00:00")
    active = job("expired-wait", None)
    with pytest.raises(RetryRequested, match="deadline reached"):
        hold_group(group(active), directory=tmp_path)
    result = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(result.failed) == len(result.retry) == len(result.new) == 1


class OfflineStorage(DirectoryStorage):
    def put(self, key: str, data: bytes) -> None:
        raise OSError("fixture storage is offline")

    def put_file(self, key: str, path: Path) -> None:
        raise OSError("fixture storage is offline")


def test_two_tournaments_share_loaded_agents_and_resume_without_starting_them(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=load, max_batch=2)
    selected = group(job("ladder", agents), job("gauntlet", agents))
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [1, 1]
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_text())
    assert sum(batch["rows"] for batch in batches) == 4
    assert sum(batch["loads"] for batch in batches) == 2
    assert all(
        all(
            batch[field] >= 0.0
            for field in (
                "queue_wait_seconds",
                "receive_seconds",
                "load_seconds",
                "forward_seconds",
                "encode_seconds",
                "send_seconds",
            )
        )
        for batch in batches
    )
    reports = [
        GameReport.model_validate_json(path.read_bytes())
        for path in (tmp_path / "grouped/artifacts/agents").glob("game-*.json")
    ]
    assert len(reports) == 2
    assert sum(r.matches for r in reports) == 2
    assert sum(r.bus.calls for r in reports) == 4
    assert sum(r.bus.answers for r in reports) == 4
    log = capfd.readouterr().err
    assert log.count("agents: started") == 1
    assert log.count("agents: loaded") == 2
    assert "2 checkpoints loaded, 4 rows" in log
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: started" not in capfd.readouterr().err
    assert len(list((tmp_path / "grouped/artifacts/agents").glob("game-*.json"))) == 2
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select score from scores").fetchall() == [(2,)] * 4
    finally:
        db.close()


def raises_load(checkpoint: CheckpointRef) -> ModelForward:
    raise ValueError("fixture loading error")


def exits_load(checkpoint: CheckpointRef) -> ModelForward:
    os._exit(3)


@pytest.mark.parametrize("exit_process", [False, True])
def test_loading_failure_never_records_success_and_only_process_exit_retries(
    tmp_path: Path, exit_process: bool
) -> None:
    agents = Agents(load=exits_load if exit_process else raises_load)
    active = job("failure", agents)
    with pytest.raises(RetryRequested if exit_process else EtudeError):
        hold_group(group(active), directory=tmp_path)
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.failed) == 1 and not found.delivered
    assert len(found.retry) == len(found.new) == int(exit_process)
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)]
        assert db.sql("select retry from matches").fetchall() == [(exit_process,)]
    finally:
        db.close()
    if exit_process:
        repaired = job("failure", Agents(load=load))
        assert hold_group(group(repaired), directory=tmp_path)[0].played == 1


def test_plain_games_use_the_pool_without_agents(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert hold_group(group(job("plain", None)), directory=tmp_path)[0].played == 1
    assert "agents:" not in capfd.readouterr().err
    assert not (tmp_path / "grouped/artifacts/agents/batches.json").exists()


def test_play_arity_and_removed_workers_are_refused(tmp_path: Path) -> None:
    def sync_play(match: Match, agents: list[object]) -> Tables:
        return {}

    async def one_parameter(match: Match) -> Tables:
        return {}

    for agents in (None, Agents(load=load)):
        with pytest.raises(LauncherError, match="async def"):
            EvalJob(
                tournament=tournament("bad"),
                enter=(checkpoint("left"), checkpoint("right")),
                play=sync_play,  # type: ignore[arg-type]
                agents=agents or Agents(),
                repo=None,
                max_hours=0.1,
            )
        with pytest.raises(LauncherError, match="match and agents"):
            job("bad", agents, one_parameter)
    with evaluating(
        tournament("bad"),
        experiment="grouped",
        enter=(checkpoint("left"), checkpoint("right")),
        directory=tmp_path,
    ) as active:
        with pytest.raises(TypeError, match="async def"):
            active.play(sync_play)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="workers"):
        EvalJob.model_validate({**job("bad", None).model_dump(), "workers": 2})
    with pytest.raises(ValidationError, match="max_delay_seconds"):
        Agents.model_validate({"max_delay_seconds": 0.05})


@pytest.mark.parametrize(
    (
        "cpus",
        "play",
        "streaming",
        "agents",
        "max_batch",
        "memory_gb",
        "expected",
    ),
    [
        # Saturated play CPU: 64 pending, max_batch=32 -> 16/32 processes.
        (32, (64,), 0, True, 32, None, Placement("local", 32, 1, 0, 1, False)),
        (16, (64,), 0, True, 32, None, Placement("local", 16, 1, 0, 2, False)),
        # Medium play CPU: 16 pending, max_batch=32, 16 cores -> 8 processes (2 games each).
        (16, (16,), 0, True, 32, None, Placement("local", 8, 1, 0, 2, False)),
        # Mixed CPU/Mac: 500 play_many (1 streaming) + 10 play, max_batch=256, 16 cores -> 2 processes.
        (16, (10,), 1, True, 256, None, Placement("local", 2, 1, 0, 10, False)),
        (3, (20,), 0, True, 32, None, Placement("local", 2, 1, 0, 11, False)),
        (2, (20,), 0, True, 32, None, Placement("local", 2, 1, 0, 16, False)),
        (8, (20,), 0, False, 32, None, Placement(None, 8, 1, 0, 1, False)),
        # Code-only mixed: 3 play + 2 streaming -> 5 processes.
        (8, (3,), 2, False, 32, None, Placement(None, 5, 1, 0, 1, False)),
        (8, (20,), 0, True, 32, None, Placement("local", 3, 1, 0, 8, False)),
        (8, (20,), 0, True, 32, 1.5, Placement("local", 2, 1, 0, 16, False)),
        (8, (1,), 0, True, 32, None, Placement("local", 1, 1, 0, 1, False)),
        (1, (20,), 0, True, 32, None, Placement("local", 1, 1, 0, 20, False)),
        (1, (20,), 0, False, 32, None, Placement(None, 1, 1, 0, 1, False)),
        (8, (), 0, True, 32, None, Placement(None, 0, 1, 0, 0, False)),
        (8, (0,), 0, True, 32, None, Placement(None, 0, 1, 0, 0, False)),
        (8, (20,), 0, True, 32, 2.0, Placement("local", 1, 1, 0, 20, False)),
    ],
)
def test_place_respects_cpu_pending_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int,
    play: tuple[int, ...],
    streaming: int,
    agents: bool,
    max_batch: int,
    memory_gb: float | None,
    expected: Placement,
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    monkeypatch.setattr(eval_resources, "_has_gpu", lambda: False)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr(
        "etude.eval.resources.memory_bytes", lambda: 16 * 1024**3 if cpus >= 16 else 2 * 1024**3
    )
    chosen = place(play, streaming, agents=agents, max_batch=max_batch, memory_gb=memory_gb)
    assert chosen == expected


@pytest.mark.parametrize(
    (
        "cpus",
        "play",
        "streaming",
        "max_batch",
        "memory_gb",
        "agents",
        "visible",
        "expected",
    ),
    [
        # Saturated play GPU (4 cores).
        (4, (64,), 0, 32, None, True, "0", Placement("socket", 3, 1, 1, 11, False)),
        # Small play GPU (3 pending, max_batch=32, 4 cores) -> 1 process, 3 games, 3 agent threads.
        (4, (3,), 0, 32, None, True, "0", Placement("socket", 1, 1, 3, 3, False)),
        # Small play GPU (5 pending, max_batch=32, 16 cores) -> 1 process, 5 games, 15 agent threads.
        (16, (5,), 0, 32, None, True, "0", Placement("socket", 1, 1, 15, 5, False)),
        # Mixed GPU (500 play_many + 10 play, max_batch=256, 16 cores) -> 2 processes, 14 agent threads.
        (16, (10,), 1, 256, None, True, "0", Placement("socket", 2, 1, 14, 10, False)),
        # Three 1-match play tournaments on 4 cores -> 3 processes, 1 game each, 1 agent thread.
        (4, (1, 1, 1), 0, 32, None, True, "0", Placement("socket", 3, 1, 1, 1, False)),
        (4, (64,), 0, 8, None, True, "0", Placement("socket", 3, 1, 1, 3, False)),
        (4, (64,), 0, 32, 15.5, True, "0", Placement("socket", 2, 1, 2, 16, False)),
        # All-play_many routes direct with grouped=True on GPU and non-GPU.
        (4, (), 2, 32, None, True, "0", Placement("local", 1, 3, 0, 0, True)),
        (4, (64,), 0, 32, None, False, "0", Placement(None, 4, 1, 0, 1, False)),
        (4, (), 2, 32, None, False, "0", Placement(None, 1, 1, 0, 0, True)),
        (4, (64,), 0, 32, None, True, "", Placement("local", 4, 1, 0, 8, False)),
        (4, (), 2, 32, None, True, "", Placement("local", 1, 3, 0, 0, True)),
    ],
)
def test_gpu_placement_fills_batches_and_routes_streaming_direct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cpus: int,
    play: tuple[int, ...],
    streaming: int,
    max_batch: int,
    memory_gb: float | None,
    agents: bool,
    visible: str,
    expected: Placement,
) -> None:
    monkeypatch.setattr(eval_resources, "_has_gpu", _has_gpu)
    # Controlled hardware output, without importing or executing a model library.
    probe = tmp_path / "nvidia-smi"
    probe.write_text("#!/bin/sh\nprintf '0\\n'\n")
    probe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    chosen = place(play, streaming, agents=agents, max_batch=max_batch, memory_gb=memory_gb)
    assert chosen == expected


def test_code_only_and_streaming_placement_do_not_probe_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe() -> bool:
        raise AssertionError("code-only and all-play_many placement must not probe GPU hardware")

    monkeypatch.setattr(eval_resources, "_has_gpu", fail_probe)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    assert place((6,), 0, agents=False) == Placement(
        bus=None, processes=4, game_threads=1, agent_threads=0, games_per_process=1, grouped=False
    )
    assert place((), 2, agents=False) == Placement(
        bus=None, processes=1, game_threads=1, agent_threads=0, games_per_process=0, grouped=True
    )
    assert place((), 2, agents=True) == Placement(
        bus="local",
        processes=1,
        game_threads=3,
        agent_threads=0,
        games_per_process=0,
        grouped=True,
    )


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_temporary_files_work_after_game_scratch_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool, failed: bool
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("ETUDE_SCRATCH_DIR", "parent scratch")
    monkeypatch.setenv("ETUDE_AGENTS_DIR", "parent artifacts")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path) if cached else None)
    with pytest.raises(RuntimeError, match="game failed") if failed else nullcontext():
        with resources(tmp_path / "artifacts") as owned:
            scratch = owned.scratch
            with tempfile.TemporaryDirectory() as during:
                assert Path(during).parent == tmp_path
            assert os.environ["ETUDE_SCRATCH_DIR"] == "parent scratch"
            assert os.environ["ETUDE_AGENTS_DIR"] == "parent artifacts"
            if failed:
                raise RuntimeError("game failed")
    assert not scratch.exists()
    with tempfile.TemporaryDirectory() as after:
        assert Path(after).parent == tmp_path
        assert Path(after).is_dir()


@pytest.mark.parametrize(
    ("cpus", "memory_gb", "expected_processes", "expected_games"),
    [(1, None, 1, 1), (4, None, 3, 1), (4, 1.75, 1, 1), (8, None, 3, 1)],
)
def test_group_execution_respects_declared_agent_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    cpus: int,
    memory_gb: float | None,
    expected_processes: int,
    expected_games: int,
) -> None:
    measurements = iter([cpus])
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: next(measurements))
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    outstanding: list[int] = []

    def count_processes(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> list[object]:
        # Each live game process contributes pipe + sentinel; agent contributes sentinel.
        outstanding.append((len(handles) - 1) // 2)
        return list(wait(handles, timeout=timeout))

    monkeypatch.setattr("etude.eval.run.wait", count_processes)
    agents = Agents(load=load_checkpoint, memory_gb=memory_gb)
    selected = group(*(job(name, agents) for name in ("one", "two", "three")))
    assert [result.played for result in hold_group(selected, directory=tmp_path)] == [1, 1, 1]
    assert max(outstanding) == expected_processes
    expected_threads = max(1, cpus - expected_processes)
    assert (
        f"{expected_processes} processes, {expected_games} games each; {expected_threads} inference threads"
        in capfd.readouterr().err
    )
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_bytes())
    assert sum(batch["loads"] for batch in batches) == 2
    budgets = json.loads((tmp_path / "grouped/artifacts/agents/threads.json").read_text())
    assert set(budgets.values()) == {str(expected_threads)}
    for spec in selected.evaluations:
        found = played(
            spec.definition,
            experiment="grouped",
            enter=spec.tournament.entrants,
            directory=tmp_path,
        )
        assert len(found.delivered) == 1 and not found.new and not found.failed


def test_released_queued_evaluation_discards_workers_and_max_delay() -> None:
    spec = evaluation_group(job("old", None)).evaluations[0]
    text = {
        "tournament": spec.definition.model_dump(mode="json"),
        "experiment": spec.experiment,
        "play": spec.play.model_dump(mode="json"),
        "entrants": [player.model_dump(mode="json") for player in spec.tournament.entrants],
        "workers": 28,
        "agents": {"max_batch": 16, "max_delay_seconds": 0.005},
    }
    restored = EvalGroup.model_validate(text).evaluations[0]
    assert restored.definition == spec.definition
    assert restored.matches == spec.matches
    assert restored.agents.max_batch == 16
    # 0.4.7 Evaluation JSON carrying max_delay_seconds also decodes cleanly.
    v047 = {
        **spec.model_dump(mode="json"),
        "agents": {**spec.agents.model_dump(mode="json"), "max_delay_seconds": 0.005},
    }
    assert Evaluation.model_validate(v047) == spec


class Echo:
    def __call__(self, inputs: list[object]) -> list[object]:
        return inputs


def echo(checkpoint: CheckpointRef) -> ModelForward:
    return Echo()


def test_batch_merges_rows_up_to_max_batch_and_runs_oversized_alone() -> None:
    first_answers: list[tuple[int, ForwardOutcome]] = []
    second_answers: list[tuple[int, ForwardOutcome]] = []
    player = checkpoint("one")
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(echo, 4, 1024)
    batches.add(first_answers.append, 1, player.checkpoint, [("a", "b")], 2)
    batches.add(second_answers.append, 2, player.checkpoint, [("c", "d")], 2)
    batches.add(second_answers.append, 3, player.checkpoint, ["e"], 1)
    first = batches.flush()
    assert first_answers.pop(0) == (1, ForwardOutcome("one", [("a", "b")]))
    assert second_answers.pop(0) == (2, ForwardOutcome("one", [("c", "d")]))
    assert [b.rows for b in first] == [4]
    assert [b.requests for b in first] == [2]
    assert batches.loads == 1
    second = batches.flush()
    assert second_answers.pop(0) == (3, ForwardOutcome("one", ["e"]))
    assert [b.rows for b in second] == [1]
    # A single pre-collated call above max_batch runs alone and reports real rows.
    batches.add(first_answers.append, 4, player.checkpoint, ["big"], 6)
    oversized = batches.flush()
    assert first_answers.pop(0) == (4, ForwardOutcome("one", ["big"]))
    assert [b.rows for b in oversized] == [6]


def test_lru_charges_checkpoint_bytes_and_keeps_players_with_waiting_inputs() -> None:
    players = [
        checkpoint_player(
            CheckpointRef(
                address=f"{name}/checkpoints/model",
                artifact=ArtifactRef(uri=f"file:///unused/{name}", sha256="0" * 64, size_bytes=1),
            )
        )
        for name in ("a", "b", "c")
    ]
    a, b, c = players
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(echo, 1, 2)
    answers: list[tuple[int, ForwardOutcome]] = []
    for index, player in enumerate((a, b), start=1):
        batches.add(answers.append, index, player.checkpoint, [player.name], 1)
        batches.flush()
        assert answers.pop(0) == (index, ForwardOutcome(player.checkpoint.key, [player.name]))
    # While a still has waiting rows in pending, loading c evicts b instead of a.
    batches.add(answers.append, 3, c.checkpoint, ["c"], 1)
    batches.add(answers.append, 4, a.checkpoint, ["a"], 1)
    batches.flush()
    assert answers.pop(0) == (3, ForwardOutcome(c.checkpoint.key, ["c"]))
    assert answers.pop(0) == (4, ForwardOutcome(a.checkpoint.key, ["a"]))
    assert batches.loads == 3
    batches.add(answers.append, 5, b.checkpoint, ["b"], 1)
    batches.flush()
    assert answers.pop(0) == (5, ForwardOutcome(b.checkpoint.key, ["b"]))
    assert batches.loads == 4  # b was evicted to make room for c.


class WrongAnswers:
    def __call__(self, inputs: list[object]) -> list[object]:
        return []


def wrong_answers(checkpoint: CheckpointRef) -> ModelForward:
    return WrongAnswers()


class UnpicklableAnswers:
    def __call__(self, inputs: list[object]) -> list[object]:
        return [lambda: None for _ in inputs]


def unpicklable_answers(checkpoint: CheckpointRef) -> ModelForward:
    return UnpicklableAnswers()


@pytest.mark.parametrize("loader", [wrong_answers, unpicklable_answers])
def test_bad_batch_answers_fail_all_asking_tournaments(
    tmp_path: Path, loader: Callable[[CheckpointRef], ModelForward]
) -> None:
    agents = Agents(load=loader)
    selected = group(job("one", agents), job("two", agents))
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(selected, directory=tmp_path)
    reports = [
        GameReport.model_validate_json(path.read_bytes())
        for path in (tmp_path / "grouped/artifacts/agents").glob("game-*.json")
    ]
    assert len(reports) == 2
    assert [r.bus.calls for r in reports] == [1, 1]
    assert [r.bus.answers for r in reports] == [1, 1]
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state, retry from matches").fetchall() == [("failed", False)] * 2
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
    finally:
        db.close()


def test_launched_group_delivers_members_then_the_next_pass_plays_nothing(
    tmp_path: Path, command_python: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout(
        tmp_path / "repo",
        {
            "launched_agents.py": f"""
from etude.jobs import Agents, EvalJob, ExperimentState
from etude.eval.records import Code, RoundRobin, checkpoint_player
from etude.eval.match import environment
from etude.spec import Runtime, Variable
from tests.eval.arithmetic import load
from tests.eval.arithmetic import checkpoint
from tests.eval.arithmetic import play

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    agents = Agents(load=load)
    return [EvalJob(tournament=RoundRobin(name=name, environment=environment("agents"),
                   configs=({{"lane": name}},), games=1),
                   enter=(checkpoint("left"), checkpoint("right")), play=play, agents=agents,
                   repo=None, runtime=Runtime(python={command_python!r}), environment=(Variable(name="CUDA_VISIBLE_DEVICES", value=""),), max_hours=0.05)
            for name in ("one", "two")]
"""
        },
    )
    scheduler = client.default()
    monkeypatch.setattr(client, "_client", lambda: scheduler)
    monkeypatch.setattr(client, "standing", scheduler.standing)
    source = DirectoryStorage(default_root())
    definition = read_experiment(repo / "launched_agents.py")
    try:
        decisions = advance(definition, source=source, pool="local", mode="launch")
        assert decisions[0].launched == decisions[1].launched
        (status,) = scheduler.wait(str(decisions[0].launched), every=0.05, timeout=30)
        assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=status.run))
        assert len([key for key in source.keys("") if key.endswith("tournament.json")]) == 2
        assert any("/artifacts/agents/game-" in key for key in source.keys(""))
        assert not any(key.endswith("/batches.json") for key in source.keys(""))
        log = job_text(scheduler.logs(refresh=True, run=status.run))
        assert log.count("agents: started") == log.count("agents: stopped") == 0
        again = advance(definition, source=source, pool="local", mode="launch")
        assert all(d.new == 0 and d.launched is None for d in again)
        assert len(scheduler.runs(done=True)) == 1
    finally:
        for active in scheduler.runs():
            scheduler.cancel(active.id)
        scheduler.wait(every=0.05, timeout=30)


def test_game_imports_only_play_module_with_one_send_and_receive_per_decision(
    tmp_path: Path,
) -> None:
    (tmp_path / "light_agent_game.py").write_text("""
import sys
from collections.abc import Sequence
from etude.eval.agents import Forward
from etude.eval.records import Match
from etude.eval.result import Tables

async def play(match: Match, agents: Sequence[Forward]) -> Tables:
    assert "separate_loading" not in sys.modules
    assert "etude.eval.batching" not in sys.modules
    assert await agents[0]([]) == []
    return {"scores": [{"seat": 0, "score": int(str((await agents[0]([1]))[0]))}]}
""")
    (tmp_path / "separate_loading.py").write_text(
        "raise RuntimeError('model module imported by game')\n"
    )
    program = """
import json
import multiprocessing
import socket
import sys
import threading
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from tempfile import TemporaryDirectory
from etude.call import Function
from etude.eval.agents import Answer, recv_frame, send_frame
from etude.eval.game import Done, Grant, Outcome, Stream, StreamEnd, Take, run_game_process
from etude.eval.records import Match
from etude.eval.resources import Resources
from tests.eval.arithmetic import checkpoint


counts = {"sent": 0, "received": 0}
with TemporaryDirectory(dir="/tmp", prefix="etude-test-") as scratch, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    address = scratch + "/a.sock"
    listener.bind(address)
    listener.listen()
    def answer() -> None:
        peer, _ = listener.accept()
        with peer:
            batch = ForkingPickler.loads(recv_frame(peer))
            counts["received"] += 1
            for req in batch.requests:
                send_frame(peer, ForkingPickler.dumps(Answer(req.id, req.checkpoint.key, req.inputs)))
                counts["sent"] += 1
    thread = threading.Thread(target=answer)
    thread.start()
    parent, child = multiprocessing.Pipe()
    match = Match(players=(checkpoint("one"),))
    def drive() -> None:
        req = parent.recv()
        assert isinstance(req, Take)
        parent.send(Grant((match,)))
        outcome = parent.recv()
        assert isinstance(outcome, Outcome) and outcome.error is None
        req2 = parent.recv()
        assert isinstance(req2, Take)
        parent.send(Grant(()))
        end = parent.recv()
        assert isinstance(end, StreamEnd) and end.error is None and end.matches == 1
        done = parent.recv()
        assert isinstance(done, Done)
        assert done.report.bus.calls == done.report.bus.answers == 1
        assert done.report.matches == 1
        print(done.report.imports.model_dump_json())
    driver = threading.Thread(target=drive)
    driver.start()
    run_game_process(
        child,
        Resources(scratch=Path(scratch), artifacts=Path(scratch)),
        (Stream("light", Function(module="light_agent_game", name="play", path=sys.argv[1]), 1),),
        agents=address,
    )
    driver.join()
    thread.join()
    parent.close()
assert counts == {"sent": 1, "received": 1}
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout)["count"] == 1


def test_eval_only_smoke_starts_one_agent_process(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    steps: tuple[Step, ...] = ("eval",)
    result = smoke_file(Path("examples/agents.py"), steps=steps, source=DirectoryStorage(tmp_path))
    assert len(result.rest) == 2
    assert all(step.outcome == "passed" for step in result.rest)
    log = capfd.readouterr().err
    assert log.count("agents: started") == log.count("agents: stopped") == 1


async def stop_parent(match: Match, agents: Sequence[Forward]) -> Tables:
    await agents[0]([1])
    os.kill(os.getppid(), signal.SIGTERM)
    return {"scores": [{"seat": 0, "score": 1}]}


def test_sigterm_reaps_agents_and_games_and_records_retry(tmp_path: Path) -> None:
    before = {p.pid for p in active_children()}
    active = job("stop", Agents(load=load)).model_copy(update={"play": stop_parent})
    with pytest.raises(Terminated):
        hold_group(group(active), directory=tmp_path)
    assert {p.pid for p in active_children()} <= before
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.new) == len(found.retry) == len(found.failed) == 1


def test_a_failed_load_is_not_attempted_again_in_the_run() -> None:
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(raises_load, 1, 10)
    answers: list[tuple[int, ForwardOutcome]] = []
    player = checkpoint("one")
    batches.add(answers.append, 1, player.checkpoint, [1], 1)
    batches.flush()
    _, first = answers.pop(0)
    assert first.error and "fixture loading error" in first.error and first.loading
    # If loading is attempted a second time this function would now succeed.
    batches.forwards.load = echo
    batches.add(answers.append, 2, player.checkpoint, [2], 1)
    batches.flush()
    _, second = answers.pop(0)
    assert second.error == first.error and second.loading
    assert batches.loads == 0


@pytest.mark.parametrize("version", [1, 2])
def test_container_limits_include_ancestor_cpu_and_leaf_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    root = tmp_path / "cgroup"
    membership = tmp_path / "membership"
    if version == 2:
        (root / "tenant").mkdir(parents=True)
        (root / "tenant/cpu.max").write_text("max 100000")
        (root / "cpu.max").write_text("200000 100000")
        (root / "tenant/memory.max").write_text(str(1024**3))
        (root / "memory.max").write_text(str(2 * 1024**3))
        membership.write_text("0::/tenant\n")
    else:
        (root / "cpu/tenant").mkdir(parents=True)
        (root / "memory/tenant").mkdir(parents=True)
        (root / "cpu/cpu.cfs_quota_us").write_text("200000")
        (root / "cpu/cpu.cfs_period_us").write_text("100000")
        (root / "memory/tenant/memory.limit_in_bytes").write_text(str(1024**3))
        (root / "memory/memory.limit_in_bytes").write_text(str(2 * 1024**3))
        membership.write_text("2:cpu,cpuacct:/tenant\n3:memory:/tenant\n")

    def fixture_path(value: str) -> Path:
        return (
            root
            if value == "/sys/fs/cgroup"
            else membership
            if value == "/proc/self/cgroup"
            else Path(value)
        )

    def affinity(pid: int) -> set[int]:
        return set(range(32))

    monkeypatch.setattr("etude.eval.resources.Path", fixture_path)
    monkeypatch.setattr(os, "sched_getaffinity", affinity, raising=False)
    assert available_cpus() == 2
    assert memory_bytes() == 1024**3


async def fails_game(match: Match, agents: Sequence[Forward]) -> Tables:
    raise ValueError("fixture game error")


async def mixed_outcomes(match: Match[Config], agents: list[object]) -> Tables:
    if match.config["outcome"] == "failed":
        raise ValueError("fixture game error")
    if match.config["outcome"] == "retry":
        raise RetryRequested("fixture transient error")
    return await plain(match, list(agents))


@pytest.mark.parametrize("retry", [False, True])
def test_a_game_failure_retains_in_flight_outcomes_and_resume_skips_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    # Keep acceptance order deterministic: failure, success, then another failure.
    monkeypatch.setattr("etude.eval.tournament.ordered", lambda matches: iter(matches))
    pipes_seen: list[object] = []

    def all_pipes(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> list[object]:
        # Wait until every active pipe has a message so all 3 processes advance in lockstep.
        pipes: list[Connection] = [h for h in handles if isinstance(h, Connection)]
        if len(pipes) == 3 and len(pipes_seen) < 2:
            pipes_seen.append(len(pipes))
            ready: list[object] = []
            while len(ready) < len(pipes):
                for item in wait([p for p in pipes if p not in ready], timeout=timeout):
                    ready.append(item)
            return ready
        return list(wait(handles, timeout=timeout))

    monkeypatch.setattr("etude.eval.run.wait", all_pipes)
    matches = [
        Match(players=(checkpoint("one"),), config={"outcome": outcome, "index": seed}, games=1)
        for seed, outcome in enumerate(("failed", "done", "retry" if retry else "failed", "done"))
    ]
    world = environment("grouped")
    with pytest.raises(RetryRequested if retry else EtudeError):
        with evaluating(
            world, experiment="grouped", matches=matches, run="mixed", directory=tmp_path
        ) as active:
            active.play(mixed_outcomes)
    db = statsdb("grouped", tmp_path)
    try:
        records = dict(
            (key, (state, again))
            for key, state, again in db.sql("select match, state, retry from matches").fetchall()
        )
        expected = {
            matches[0].key: ("failed", False),
            matches[1].key: ("done", False),
            matches[2].key: ("failed", retry),
        }
        if retry:
            expected[matches[3].key] = ("failed", True)
        assert records == expected
        assert db.sql("select count(*) from scores").fetchone() == (1,)
        assert db.sql("select state from runs").fetchall() == [("failed",)]
    finally:
        db.close()
    with evaluating(
        world,
        experiment="grouped",
        matches=matches,
        run="mixed",
        directory=tmp_path,
        resume=True,
    ) as active:
        repaired = active.play(plain)
    assert (repaired.played, repaired.skipped) == (3, 1)


def test_a_game_failure_stops_only_its_tournament(tmp_path: Path) -> None:
    agents = Agents(load=load)
    bad = job("bad", agents).model_copy(update={"play": fails_game})
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(group(bad, job("good", agents)), directory=tmp_path)
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from scores").fetchone() == (2,)
        assert sorted(row[0] for row in db.sql("select state from runs").fetchall()) == [
            "failed",
            "finished",
        ]
    finally:
        db.close()


@pytest.mark.parametrize("with_agents", [False, True])
@pytest.mark.parametrize("gpu", [False, True])
def test_heavy_import_warning_is_once_and_only_for_games_with_agents(
    tmp_path: Path, with_agents: bool, gpu: bool
) -> None:
    (tmp_path / "torch.py").write_text("# Controlled framework fixture.\n")
    (tmp_path / "heavy_fixture_game.py").write_text(
        "import torch\nfrom collections.abc import Sequence\nfrom etude.eval.records import Match\n"
        "from etude.eval.agents import Forward\nfrom etude.eval.result import Tables\n"
        "async def play(match: Match" + ", agents: Sequence[Forward]" + ") -> Tables:\n"
        "    return {'scores': [{'seat': 0, 'score': 1}]}\n"
    )
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from heavy_fixture_game import play
from etude.eval.match import environment
from etude.eval.records import Code, RoundRobin, checkpoint_player
from etude.jobs import Agents, EvalGroup, EvalJob, evaluation_group, hold_group, ExperimentState
from tests.eval.arithmetic import load
from tests.eval.arithmetic import checkpoint
import etude.eval.resources
etude.eval.resources._has_gpu = lambda: sys.argv[3] == "True"
agents = Agents(load=load) if sys.argv[2] == "True" else Agents()
jobs = [EvalJob(tournament=RoundRobin(name=name, environment=environment("heavy"), configs=({},), games=1),
               enter=(checkpoint("one"), checkpoint("two")), play=play,
               agents=agents or Agents(), repo=None, max_hours=0.1) for name in ("first", "second")]
hold_group(EvalGroup(evaluations=tuple(evaluation_group(job.lower(ExperimentState(experiment="heavy"))).evaluations[0] for job in jobs)),
           directory=sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(with_agents), str(gpu)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("game module heavy_fixture_game: imported torch") == int(with_agents)
    if with_agents:
        assert "move the import to load's module" in result.stderr


def test_agent_startup_failure_reports_retryable_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    monkeypatch.setenv("ETUDE_FIXTURE_TOKEN", "synthetic-fixture-credential")
    # Put the secret in the artifacts path and block batches.json with a directory
    # so the agent process fails during startup before control.send(None).
    root = tmp_path / "synthetic-fixture-credential"
    (root / "grouped/artifacts/agents/batches.json").mkdir(parents=True)
    selected = group(job("startup", Agents(load=load)))
    spec = selected.evaluations[0]
    with pytest.raises(RetryRequested, match="startup"):
        hold_group(EvalGroup(evaluations=(spec,)), directory=root)
    log = capfd.readouterr().err
    assert "[redacted]" in log and "synthetic-fixture-credential" not in log
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 1
    found = played(
        spec.definition, experiment="grouped", enter=spec.tournament.entrants, directory=root
    )
    assert len(found.new) == len(found.retry) == len(found.failed) == 1


@pytest.mark.parametrize(
    ("kind", "module_source", "expected_error"),
    [
        ("missing_module", None, "ModuleNotFoundError"),
        ("syntax_error", "def load(\n", "SyntaxError"),
        (
            "runtime_secret",
            "import os\nraise RuntimeError('import boom: ' + os.environ['ETUDE_FIXTURE_TOKEN'])\n",
            "import boom: [redacted]",
        ),
    ],
)
@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_broken_load_module_fails_permanently_on_both_transports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    module_source: str | None,
    expected_error: str,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    monkeypatch.setenv("ETUDE_FIXTURE_TOKEN", "synthetic-fixture-credential")
    module_name = f"broken_loader_{kind}_fixture"
    if module_source is not None:
        (tmp_path / f"{module_name}.py").write_text(module_source)
    selected = group(job("broken_loader", Agents(load=load)))
    spec = selected.evaluations[0]
    assert spec.agents is not None
    spec = spec.model_copy(
        update={
            "agents": spec.agents.model_copy(
                update={"load": Function(module=module_name, name="load", path=str(tmp_path))}
            )
        }
    )
    with pytest.raises(EtudeError, match="match_failed") as info:
        hold_group(EvalGroup(evaluations=(spec,)), directory=tmp_path)
    assert expected_error in str(info.value)
    assert "synthetic-fixture-credential" not in str(info.value)
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 1
    found = played(
        spec.definition, experiment="grouped", enter=spec.tournament.entrants, directory=tmp_path
    )
    assert len(found.failed) == 1 and not found.retry and not found.new
    db = statsdb("grouped", tmp_path)
    try:
        rows = db.sql("select state, retry, error from matches").fetchall()
        assert len(rows) == 1
        state, retry, error = rows[0]
        assert state == "failed" and retry is False
        assert expected_error in error
        assert "synthetic-fixture-credential" not in error
    finally:
        db.close()


async def catches_agent_error(match: Match, agents: Sequence[Forward]) -> Tables:
    try:
        await agents[0]([1])
    except ForwardError as error:
        assert error.checkpoint == match.players[0].name
    return {"scores": [{"seat": 0, "score": 99}]}


class RaisingForward:
    def __call__(self, inputs: list[object]) -> list[object]:
        raise ValueError("fixture acting error")


def raises_forward(checkpoint: CheckpointRef) -> ModelForward:
    return RaisingForward()


def retry_load(checkpoint: CheckpointRef) -> ModelForward:
    raise RetryRequested("fixture transient loading error")


@pytest.mark.parametrize("loader", [raises_load, raises_forward, wrong_answers, retry_load])
@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_caught_agent_errors_fail_matches_without_publishing_returned_rows(
    tmp_path: Path, loader: Callable[[CheckpointRef], ModelForward]
) -> None:
    agents = Agents(load=loader)
    jobs = [
        job(name, agents).model_copy(update={"play": catches_agent_error})
        for name in ("one", "two")
    ]
    with pytest.raises(RetryRequested if loader is retry_load else EtudeError):
        hold_group(group(*jobs), directory=tmp_path)
    for active in jobs:
        found = played(
            active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
        )
        assert len(found.failed) == 1 and not found.delivered
        assert len(found.retry) == int(loader is retry_load)
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
        assert (
            db.sql("select state, retry from matches").fetchall()
            == [("failed", loader is retry_load)] * 2
        )
        assert "scores" not in {row[0] for row in db.sql("show tables").fetchall()}
    finally:
        db.close()


def selective_load(checkpoint: CheckpointRef) -> ModelForward:
    if str(checkpoint.key) == "broken":
        raise ValueError("fixture broken player")
    return load(checkpoint)


def selective_retry_load(checkpoint: CheckpointRef) -> ModelForward:
    if str(checkpoint.key) == "broken":
        raise RetryRequested("fixture player unavailable")
    return load(checkpoint)


async def asks_or_returns(match: Match[dict[str, str]], agents: Sequence[Forward]) -> Tables:
    # The second tournament never asks for an agent, even when it seats the broken player.
    if match.config["lane"] == "two":
        return await plain(match, list(agents))
    try:
        return await play(match, agents)
    except Exception:
        return await plain(match, list(agents))


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("in_flight", [False, True])
@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_loader_failure_reaches_undispatched_matches_across_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool, in_flight: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4 if in_flight else 1)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=selective_retry_load if retry else selective_load)
    jobs = [
        job(name, agents).model_copy(
            update={
                "enter": (checkpoint("broken"), checkpoint("left"), checkpoint("right")),
                "play": catches_agent_error if in_flight else asks_or_returns,
            }
        )
        for name in ("one", "two")
    ]
    selected = group(*jobs)
    # Attempt the broken leaf before a no-forward game can deliver success.
    # The separate earlier-delivery test pins the opposite ordering.
    selected = selected.model_copy(
        update={
            "evaluations": tuple(
                spec.model_copy(
                    update={
                        "matches": tuple(
                            sorted(
                                spec.matches,
                                key=lambda match: (
                                    {p.name for p in match.players} != {"broken", "right"}
                                ),
                            )
                        )
                    }
                )
                for spec in selected.evaluations
            )
        }
    )
    with pytest.raises(RetryRequested if retry else EtudeError):
        hold_group(selected, directory=tmp_path)
    for active in jobs:
        found = played(
            active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
        )
        assert len(found.failed) == 2
        assert len(found.delivered) == 1
        assert len(found.retry) == len(found.new) == (2 if retry else 0)
        assert all(any(p.name == "broken" for p in m.players) for m in found.failed)
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
        assert (
            db.sql("select retry from matches where state = 'failed'").fetchall() == [(retry,)] * 4
        )
        assert all(
            "fixture" in row[0]
            for row in db.sql("select error from matches where state = 'failed'").fetchall()
        )
        assert db.sql("select count(distinct match) from scores").fetchone() == (2,)
    finally:
        db.close()


async def returns_before_loading(match: Match, agents: Sequence[Forward]) -> Tables:
    if any(player.name == "left" for player in match.players):
        return await plain(match, list(agents))
    return await play(match, agents)


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_loader_failure_preserves_previously_delivered_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=selective_load)
    active = job("one", agents).model_copy(
        update={
            "enter": (checkpoint("broken"), checkpoint("left"), checkpoint("right")),
            "play": returns_before_loading,
        }
    )
    selected = group(active)
    matches = played(
        active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
    ).new
    # Deliver the broken player's match with left before attempting its load with right.
    first = next(m for m in matches if {p.name for p in m.players} == {"broken", "left"})
    spec = selected.evaluations[0].model_copy(
        update={"matches": (first, *(m for m in matches if m != first))}
    )
    selected = EvalGroup(evaluations=(spec,))
    with pytest.raises(EtudeError):
        hold_group(selected, directory=tmp_path)
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.failed) == 1
    assert len(found.delivered) == 2
    assert not found.new
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)]
        assert db.sql("select count(distinct match) from scores").fetchone() == (2,)
    finally:
        db.close()


class WireValue:
    serializations = 0

    def __init__(self, value: int) -> None:
        self.value = value

    def __reduce__(self) -> tuple[type[str], tuple[str]]:
        type(self).serializations += 1
        return str, (str(self.value),)


class WireAgent:
    def __call__(self, inputs: list[object]) -> list[object]:
        return [WireValue(int(str(row))) for row in inputs]


def wire_agent(checkpoint: CheckpointRef) -> ModelForward:
    return WireAgent()


def test_protocol_counts_one_serialization_each_way_and_batches_shared_checkpoint() -> None:
    # Small real sockets retain the wire bytes; no load or clock threshold.
    cp = checkpoint("wire")
    batches: _Batches[memoryview] = _Batches(wire_agent, 2, 1024, encode=_encode_socket)
    sockets = [socket.socketpair() for _ in range(2)]
    clients = [parent for parent, _ in sockets]
    receivers = [child for _, child in sockets]
    answer_size = len(ForkingPickler.dumps(Answer(1, "wire", [WireValue(0)])))
    WireValue.serializations = 0
    request_bytes = 0
    observations = []

    def send_to(sock: socket.socket) -> Callable[[memoryview], None]:
        return lambda payload: send_frame(sock, payload)

    try:
        for turn in range(2):
            for index, (peer_client, receiver) in enumerate(zip(clients, receivers, strict=True)):
                payload = ForkingPickler.dumps(
                    BatchRequest(
                        (
                            CheckpointRequest(
                                turn * 2 + index + 1, cp.checkpoint, [WireValue(turn)], 1
                            ),
                        )
                    )
                )
                send_frame(peer_client, payload)
                received = recv_frame(receiver)
                request_bytes += len(received)
                _add_socket_batch(batches, send_to(receiver), ForkingPickler.loads(received))
            observations.extend(batches.flush())
            for index, peer_client in enumerate(clients):
                answer_payload = recv_frame(peer_client)
                assert len(answer_payload) == answer_size
                assert ForkingPickler.loads(answer_payload) == Answer(
                    turn * 2 + index + 1, "wire", [str(turn)]
                )
        assert WireValue.serializations == 8  # Four decisions, one encoding each way.
        assert [batch.rows for batch in observations] == [2, 2]
        assert [batch.requests for batch in observations] == [2, 2]
        assert [batch.forward_calls for batch in observations] == [1, 1]
        assert [batch.loads for batch in observations] == [1, 0]
        assert batches.loads == 1
    finally:
        for peer_client in clients:
            peer_client.close()
        for receiver in receivers:
            receiver.close()


def test_agentless_game_reports_have_no_inference_wait(tmp_path: Path) -> None:
    selected = group(job("plain", None))
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [1]
    paths = list((tmp_path / "grouped/artifacts/agents").glob("game-*.json"))
    assert len(paths) == 1
    report = GameReport.model_validate_json(paths[0].read_bytes())
    assert report.matches == 1
    assert report.wall_seconds >= 0.0
    assert report.bus.calls == report.bus.answers == 0
    assert report.bus.round_trip_seconds == report.bus.handoff_seconds == 0.0
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0]
    assert len(list((tmp_path / "grouped/artifacts/agents").glob("game-*.json"))) == 1


def test_standalone_play_retains_game_report_without_a_group(tmp_path: Path) -> None:
    match = Match(players=(checkpoint("left"),), games=1)
    bucket = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    with evaluating(
        environment("grouped"),
        experiment="grouped",
        matches=(match,),
        run="standalone",
        directory=tmp_path,
        storages=(bucket,),
    ) as active:
        assert active.play(plain).played == 1
    paths = list((tmp_path / "grouped/standalone/artifacts/agents").glob("game-*.json"))
    assert len(paths) == 1
    report = GameReport.model_validate_json(paths[0].read_bytes())
    assert report.matches == 1
    assert report.wall_seconds >= 0.0
    assert bucket.uploads == 6
    assert len([key for key in bucket.keys("") if "/game-" in key]) == 1


async def multi_round_play(match: Match[Config], agents: Sequence[Forward]) -> Tables:
    rounds = int(str(match.config["rounds"]))
    total = 0
    for step in range(rounds):
        [value] = await agents[0]([step])
        total += int(str(value))
    return {"scores": [{"seat": 0, "score": total}]}


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_barrier_batches_games_into_one_message_per_process_per_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    # 6 games of unequal length (2, 2, 3, 3, 4, 4 decisions) in 1 game process:
    # zero-delay barrier batches all waiting games every round, shrinking as games finish.
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    cp = checkpoint("one")
    lengths = (2, 2, 3, 3, 4, 4)
    matches = [
        Match(players=(cp,), config={"rounds": rounds, "slot": index}, games=1)
        for index, rounds in enumerate(lengths)
    ]
    shared = bool(request.node.callspec.params["shared_agents"])
    # Cap to 1 game process so all 6 games run on one event loop.
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2 if shared else 1)
    with evaluating(
        environment("barrier"),
        experiment="barrier",
        matches=matches,
        run="unequal",
        directory=tmp_path,
    ) as active:
        result = active.play(multi_round_play, agents=Agents(load=load, max_batch=6))
    assert result.played == 6
    artifacts = tmp_path / "barrier/unequal/artifacts/agents"
    [report] = [
        GameReport.model_validate_json(p.read_bytes()) for p in artifacts.glob("game-*.json")
    ]
    assert report.matches == 6
    assert report.bus.calls == report.bus.answers == 4
    if shared:
        # 4 decision rounds -> exactly 4 socket messages for all 6 games (not 18).
        assert report.bus.wire_bytes > 0
        batches = json.loads((artifacts / "batches.json").read_text())
    else:
        assert report.bus.wire_bytes == 0
        assert not (artifacts / "batches.json").exists()
        batches = json.loads((artifacts / f"batches-{report.pid}.json").read_text())
    assert [batch["rows"] for batch in batches] == [6, 6, 4, 2]
    assert [batch["requests"] for batch in batches] == [1, 1, 1, 1]
    assert sum(batch["loads"] for batch in batches) == 1


def test_barrier_batches_across_multiple_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 8 games of 3 rounds each across 2 game processes (4 games each, max_batch=8):
    # each process sends 3 socket messages (P=2 -> 6 messages total, not N*3=24).
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    cp = checkpoint("one")
    matches = [
        Match(players=(cp,), config={"rounds": 3, "slot": index}, games=1) for index in range(8)
    ]
    with evaluating(
        environment("barrier"),
        experiment="barrier",
        matches=matches,
        run="multi-proc",
        directory=tmp_path,
    ) as active:
        result = active.play(multi_round_play, agents=Agents(load=load, max_batch=8))
    assert result.played == 8
    artifacts = tmp_path / "barrier/multi-proc/artifacts/agents"
    reports = [
        GameReport.model_validate_json(p.read_bytes()) for p in artifacts.glob("game-*.json")
    ]
    assert len(reports) == 2
    assert sum(r.matches for r in reports) == 8
    assert [r.bus.calls for r in reports] == [3, 3]
    assert [r.bus.answers for r in reports] == [3, 3]
    batches = json.loads((artifacts / "batches.json").read_text())
    assert sum(batch["rows"] for batch in batches) == 24
    assert all(batch["rows"] <= 8 for batch in batches)


class VectorRules(Record):
    turns: int = 1
    error: str = ""


class VectorForward:
    def __call__(self, inputs: list[object]) -> list[object]:
        return [
            [int(str(row)) + 1 for row in batch]
            if isinstance(batch, tuple)
            else int(str(batch)) + 1
            for batch in inputs
        ]


def load_vector(checkpoint: CheckpointRef) -> ModelForward:
    return VectorForward()


async def vector_slots(
    matches: Matches[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result | Exception]]:
    active: list[tuple[Match[VectorRules], Forward, int]] = []
    while True:
        for match, agents in await matches.take(2 - len(active)):
            forward = cast(Forward, agents[0])
            active.append((match, forward, match.config.turns))
        if not active:
            return
        forward = active[0][1]
        assert all(agent is forward for _, agent, _ in active)
        [outputs] = await forward([tuple(left for _, _, left in active)], rows=len(active))
        assert isinstance(outputs, list)
        ready = active
        active = []
        for (match, agent, left), score in zip(ready, outputs, strict=True):
            if left > 1:
                active.append((match, agent, left - 1))
            elif match.config.error == "retry":
                yield match, RetryRequested("fixture slot retry")
            elif match.config.error:
                yield match, ValueError("fixture slot failure")
            else:
                yield match, Result(tables={"vector_scores": [{"seat": 0, "score": score}]})


@pytest.mark.parametrize("retry", [False, True])
def test_play_many_backfills_after_a_yielded_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    cp = checkpoint("one")
    # Empty configs acquire the typed defaults inside the process. Retention
    # must still use the scheduled keys, and a failed first slot must backfill.
    configs: tuple[Config, ...] = (
        {"error": "retry" if retry else "fail"},
        {"turns": 4},
        {},
        {},
        {},
    )
    matches = [
        Match(players=(cp,), config=config, index=index) for index, config in enumerate(configs)
    ]
    with (
        pytest.raises(RetryRequested if retry else EtudeError, match="fixture slot"),
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=matches,
            run="slots",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(vector_slots, agents=Agents(load=load_vector, max_batch=1))
    artifacts = tmp_path / "vector/slots/artifacts/agents"
    assert not (artifacts / "batches.json").exists()
    [batch_file] = list(artifacts.glob("batches-*.json"))
    batches = json.loads(batch_file.read_text())
    assert [batch["rows"] for batch in batches] == [2, 2, 2, 2]
    db = statsdb("vector", tmp_path)
    try:
        assert db.sql("select match, retry from matches where state = 'failed'").fetchall() == [
            (matches[0].key, retry)
        ]
        assert set(db.sql("select match, score from vector_scores").fetchall()) == {
            (match.key, 2) for match in matches[1:]
        }
    finally:
        db.close()


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_mixed_play_and_play_many_group_caps_each_play_many_at_one_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    shared = bool(request.node.callspec.params["shared_agents"])
    # Budget exceeds total tournaments to verify each play_many tournament spawns
    # exactly one process with zero empty game-*.json reports.
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 4 * 1024**3)
    agents = Agents(load=load_vector, max_batch=3)
    jobs = [
        EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(),
                games=2,
                base_seed=seed,
            ),
            enter=(checkpoint("left"), checkpoint("right")),
            play_many=vector_slots,
            agents=agents,
            repo=None,
            max_hours=0.1,
        )
        for seed, name in enumerate(("first", "second"), start=1)
    ]
    selected = group(*jobs, job("ordinary", agents))
    assert [spec.streaming for spec in selected.evaluations] == [True, True, False]
    results = hold_group(selected, directory=tmp_path)
    assert [result.played for result in results] == [2, 2, 1]
    artifacts = tmp_path / "grouped/artifacts/agents"
    reports = [
        GameReport.model_validate_json(path.read_bytes()) for path in artifacts.glob("game-*.json")
    ]
    assert len(reports) == 3
    assert sorted((report.tournaments, report.matches) for report in reports) == [
        (("first",), 2),
        (("ordinary",), 1),
        (("second",), 2),
    ]
    if shared:
        assert all(report.bus.wire_bytes > 0 for report in reports)
        batches = json.loads((artifacts / "batches.json").read_text())
        assert sum(batch["rows"] for batch in batches) == 6
        assert max(batch["rows"] for batch in batches) <= 3
        assert sum(batch["requests"] for batch in batches) == 4
    else:
        assert all(report.bus.wire_bytes == 0 for report in reports)
        assert not (artifacts / "batches.json").exists()
        batch_files = list(artifacts.glob("batches-*.json"))
        assert len(batch_files) == 3
        all_batches = [batch for path in batch_files for batch in json.loads(path.read_text())]
        assert sum(batch["rows"] for batch in all_batches) == 6
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (4,)
        assert db.sql("select count(*) from scores").fetchone() == (2,)
    finally:
        db.close()


async def array_vector_slots(
    matches: Matches[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result | Exception]]:
    import numpy as np  # noqa: PLC0415  # Test fixture keeps NumPy local to array tests.

    active: list[tuple[Match[VectorRules], Forward, Forward, int]] = []
    while True:
        for match, agents in await matches.take(2 - len(active)):
            active.append(
                (match, cast(Forward, agents[0]), cast(Forward, agents[1]), match.config.turns)
            )
        if not active:
            return
        rows = np.array([left for _, _, _, left in active], dtype=np.int64)
        pending_first = active[0][1]([rows], rows=len(active))
        pending_second = active[0][2]([rows], rows=len(active))
        [first_out] = await pending_first
        [second_out] = await pending_second
        totals = cast(np.ndarray, first_out) + cast(np.ndarray, second_out)
        ready = active
        active = []
        for (match, first, second, left), score in zip(ready, totals.tolist(), strict=True):
            if left > 1:
                active.append((match, first, second, left - 1))
            else:
                yield match, Result(tables={"vector_scores": [{"seat": 0, "score": score}]})


async def failing_stream(
    matches: Matches[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result]]:
    taken = await matches.take(2)
    assert taken
    raise ValueError("first tournament failed")
    yield taken[0][0], Result(tables={"vector_scores": []})


async def code_only_vector_slots(
    matches: Matches[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result]]:
    while True:
        taken = await matches.take(2)
        if not taken:
            return
        for match, _ in taken:
            yield (
                match,
                Result(tables={"vector_scores": [{"seat": 0, "score": match.config.turns}]}),
            )


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_play_many_group_runs_in_one_direct_process_across_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 16)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    agents = Agents(load=load_array_checkpoint, max_batch=4)
    jobs = [
        EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(),
                games=2,
                base_seed=seed,
            ),
            enter=(checkpoint("left"), checkpoint("right")),
            play_many=array_vector_slots,
            agents=agents,
            repo=None,
            max_hours=0.1,
        )
        for seed, name in enumerate(("first", "second"), start=1)
    ]
    results = hold_group(group(*jobs), directory=tmp_path)
    assert [result.played for result in results] == [2, 2]
    stderr = capfd.readouterr().err
    assert (
        "etude: agents: direct inference in 1 game process, slots requested by play_many; 15 threads"
        in stderr
    )
    assert "agents: started" not in stderr
    artifacts = tmp_path / "grouped/artifacts/agents"
    assert not (artifacts / "batches.json").exists()
    [report_file] = list(artifacts.glob("game-*.json"))
    report = GameReport.model_validate_json(report_file.read_bytes())
    assert report.tournaments == ("first", "second")
    assert report.matches == 4
    assert report.bus.wire_bytes == 0
    assert report.bus.arrays > 0
    assert report.bus.arrays_copied == 0
    assert report.bus.copied_bytes == 0
    [batch_file] = list(artifacts.glob("batches-*.json"))
    batches = json.loads(batch_file.read_text())
    # Two checkpoints shared across two tournaments are each loaded once in the one process.
    assert sum(batch["loads"] for batch in batches) == 2
    budgets = json.loads((artifacts / "threads.json").read_text())
    assert set(budgets.values()) == {"15"}
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (4,)
    finally:
        db.close()


def test_code_only_play_many_group_runs_in_one_process_without_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def fail_probe() -> bool:
        raise AssertionError("code-only play_many group must not probe GPU hardware")

    monkeypatch.setattr(eval_resources, "_has_gpu", fail_probe)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    jobs = [
        EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(),
                games=2,
                base_seed=seed,
            ),
            enter=(Code(name="left"), Code(name="right")),
            play_many=code_only_vector_slots,
            agents=Agents(),
            repo=None,
            max_hours=0.1,
        )
        for seed, name in enumerate(("first", "second"), start=1)
    ]
    results = hold_group(group(*jobs), directory=tmp_path)
    assert [result.played for result in results] == [2, 2]
    stderr = capfd.readouterr().err
    assert "etude: 1 game process, slots requested by play_many" in stderr
    assert "agents:" not in stderr
    artifacts = tmp_path / "grouped/artifacts/agents"
    assert not list(artifacts.glob("batches*.json"))
    [report_file] = list(artifacts.glob("game-*.json"))
    report = GameReport.model_validate_json(report_file.read_bytes())
    assert report.tournaments == ("first", "second")
    assert report.matches == 4
    assert report.bus.calls == 0


def test_direct_play_many_group_isolates_stream_failure_between_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    agents = Agents(load=load_array_checkpoint, max_batch=4)
    broken = EvalJob(
        tournament=RoundRobin(
            name="broken",
            environment=environment("grouped"),
            config=VectorRules(),
            games=2,
            base_seed=1,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=failing_stream,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    healthy = EvalJob(
        tournament=RoundRobin(
            name="healthy",
            environment=environment("grouped"),
            config=VectorRules(),
            games=2,
            base_seed=2,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=array_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    with pytest.raises(EtudeError, match="first tournament failed"):
        hold_group(group(broken, healthy), directory=tmp_path)
    artifacts = tmp_path / "grouped/artifacts/agents"
    [report_file] = list(artifacts.glob("game-*.json"))
    assert GameReport.model_validate_json(report_file.read_bytes()).tournaments == (
        "broken",
        "healthy",
    )
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (2,)
        assert db.sql("select count(*) from matches where state = 'failed'").fetchone() == (2,)
        assert sorted(row[0] for row in db.sql("select state from runs").fetchall()) == [
            "failed",
            "finished",
        ]
    finally:
        db.close()


def test_direct_play_many_group_isolates_stream_import_error_before_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    agents = Agents(load=load_array_checkpoint, max_batch=4)
    first = EvalJob(
        tournament=RoundRobin(
            name="bad-import",
            environment=environment("grouped"),
            config=VectorRules(),
            games=2,
            base_seed=1,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=array_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    healthy = EvalJob(
        tournament=RoundRobin(
            name="healthy",
            environment=environment("grouped"),
            config=VectorRules(),
            games=2,
            base_seed=2,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=array_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    selected = group(first, healthy)
    broken_spec = selected.evaluations[0].model_copy(
        update={"play": Function(module="nonexistent_stream_module_xyz", name="play_many")}
    )
    with pytest.raises(EtudeError, match="nonexistent_stream_module_xyz"):
        hold_group(
            EvalGroup(evaluations=(broken_spec, selected.evaluations[1])), directory=tmp_path
        )
    artifacts = tmp_path / "grouped/artifacts/agents"
    assert len(list(artifacts.glob("game-*.json"))) == 1
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (2,)
        assert sorted(row[0] for row in db.sql("select state from runs").fetchall()) == [
            "failed",
            "finished",
        ]
    finally:
        db.close()


def test_direct_imports_snapshot_excludes_loader_and_detects_mlx_in_game_module(
    tmp_path: Path,
) -> None:
    (tmp_path / "torch.py").write_text("# Synthetic torch fixture.\n")
    (tmp_path / "mlx.py").write_text("# Synthetic mlx fixture.\n")
    (tmp_path / "heavy_loader_fixture.py").write_text(
        "import mlx\nimport torch\n"
        "from etude.checkpoints import CheckpointRef\n"
        "from etude.eval.agents import ModelForward\n"
        "def load(checkpoint: CheckpointRef) -> ModelForward:\n"
        "    return lambda inputs: [1 for _ in inputs]\n"
    )
    (tmp_path / "light_play_many_fixture.py").write_text(
        "from collections.abc import AsyncIterator\n"
        "from typing import cast\n"
        "from etude.eval.agents import Forward\n"
        "from etude.eval.game import Matches\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "async def play_many(matches: Matches) -> AsyncIterator[tuple[Match, Tables]]:\n"
        "    while True:\n"
        "        taken = await matches.take(2)\n"
        "        if not taken:\n"
        "            return\n"
        "        for match, agents in taken:\n"
        "            [score] = await cast(Forward, agents[0])([0])\n"
        "            yield match, {'scores': [{'seat': 0, 'score': int(str(score))}]}\n"
    )
    (tmp_path / "mlx_game_fixture.py").write_text(
        "import mlx\n"
        "from collections.abc import Sequence\n"
        "from etude.eval.agents import Forward\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "async def play(match: Match, agents: Sequence[Forward]) -> Tables:\n"
        "    return {'scores': [{'seat': 0, 'score': 1}]}\n"
    )
    program = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import etude.eval.resources
from etude.eval.game import GameReport
from etude.eval.match import environment
from etude.eval.records import RoundRobin
from etude.jobs import Agents, EvalGroup, EvalJob, ExperimentState, evaluation_group, hold_group
from tests.eval.arithmetic import checkpoint
from heavy_loader_fixture import load
from light_play_many_fixture import play_many
from mlx_game_fixture import play

# 1. Direct play_many with heavy loader: game imports snapshot must stay clean.
direct_root = Path(sys.argv[1]) / "direct"
direct_job = EvalJob(
    tournament=RoundRobin(name="clean", environment=environment("imp"), configs=({},), games=1),
    enter=(checkpoint("one"), checkpoint("two")),
    play_many=play_many,
    agents=Agents(load=load),
    repo=None,
    max_hours=0.1,
)
hold_group(
    evaluation_group(direct_job.lower(ExperimentState(experiment="imp"))),
    directory=direct_root,
)
[report_path] = list((direct_root / "imp/artifacts/agents").glob("game-*.json"))
direct_report = GameReport.model_validate_json(report_path.read_bytes())
assert direct_report.tournaments == ("clean",)
assert direct_report.imports.heavy == ()
assert direct_report.imports.warning is None

# 2. Shared-agent play importing mlx in the game module warns on mlx.
etude.eval.resources._has_gpu = lambda: True
shared_root = Path(sys.argv[1]) / "shared"
shared_job = EvalJob(
    tournament=RoundRobin(name="mlx-warn", environment=environment("imp2"), configs=({},), games=1),
    enter=(checkpoint("one"), checkpoint("two")),
    play=play,
    agents=Agents(load=load),
    repo=None,
    max_hours=0.1,
)
hold_group(
    evaluation_group(shared_job.lower(ExperimentState(experiment="imp2"))),
    directory=shared_root,
)
[shared_path] = list((shared_root / "imp2/artifacts/agents").glob("game-*.json"))
shared_report = GameReport.model_validate_json(shared_path.read_bytes())
assert shared_report.imports.heavy == ("mlx",)
assert shared_report.imports.warning is not None
assert "imported mlx" in shared_report.imports.warning
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("game module mlx_game_fixture: imported mlx") == 1


@pytest.mark.parametrize("gpu", [False, True])
def test_import_hygiene_catches_wrapper_and_lazy_play_imports_on_both_transports(
    tmp_path: Path, gpu: bool
) -> None:
    (tmp_path / "torch.py").write_text("# Synthetic torch fixture.\n")
    (tmp_path / "mlx.py").write_text("# Synthetic mlx fixture.\n")
    (tmp_path / "clean_loader_fixture.py").write_text(
        "import mlx\nimport torch\n"
        "from etude.checkpoints import CheckpointRef\n"
        "from etude.eval.agents import ModelForward\n"
        "def load(checkpoint: CheckpointRef) -> ModelForward:\n"
        "    return lambda inputs: [1 for _ in inputs]\n"
    )
    (tmp_path / "heavy_wrapper_fixture.py").write_text(
        "import torch\n"
        "from etude.eval.records import Seat\n"
        "from etude.records import Record\n"
        "class HeavyWrapper(Record):\n"
        "    over: Seat\n"
    )
    (tmp_path / "wrapper_game_fixture.py").write_text(
        "from collections.abc import Mapping, Sequence\n"
        "from etude.eval.agents import Forward\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "def build(player: object, leaves: Mapping[str, Forward]) -> object:\n"
        "    return player\n"
        "async def play(match: Match, agents: Sequence[object]) -> Tables:\n"
        "    return {'scores': [{'seat': 0, 'score': 1}]}\n"
    )
    (tmp_path / "lazy_mlx_game_fixture.py").write_text(
        "from collections.abc import Sequence\n"
        "from etude.eval.agents import Forward\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "async def play(match: Match, agents: Sequence[Forward]) -> Tables:\n"
        "    # Call forward first so direct mode has already imported mlx on the inference thread.\n"
        "    [score] = await agents[0]([0])\n"
        "    import mlx\n"
        "    assert mlx is not None\n"
        "    return {'scores': [{'seat': 0, 'score': int(str(score))}]}\n"
    )
    program = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import etude.eval.resources
from etude.eval.game import GameReport
from etude.eval.match import environment
from etude.eval.records import RoundRobin, compose
from etude.jobs import Agents, EvalJob, ExperimentState, evaluation_group, hold_group
from tests.eval.arithmetic import checkpoint
from clean_loader_fixture import load
from heavy_wrapper_fixture import HeavyWrapper
from wrapper_game_fixture import build as wrapper_build
from wrapper_game_fixture import play as wrapper_play
from lazy_mlx_game_fixture import play as lazy_play

etude.eval.resources._has_gpu = lambda: sys.argv[2] == "True"

# 1. Player wrapper module importing torch is caught during build_agents on both transports.
wrap_root = Path(sys.argv[1]) / "wrap"
wrap_job = EvalJob(
    tournament=RoundRobin(name="wrap", environment=environment("w"), configs=({},), games=1),
    enter=(compose(HeavyWrapper(over=checkpoint("one"))), checkpoint("two")),
    play=wrapper_play,
    agents=Agents(load=load, build=wrapper_build),
    repo=None,
    max_hours=0.1,
)
hold_group(evaluation_group(wrap_job.lower(ExperimentState(experiment="w"))), directory=wrap_root)
[wrap_path] = list((wrap_root / "w/artifacts/agents").glob("game-*.json"))
wrap_report = GameReport.model_validate_json(wrap_path.read_bytes())
assert wrap_report.imports.heavy == ("torch",)
assert wrap_report.imports.importer == "heavy_wrapper_fixture"
assert wrap_report.imports.warning is not None

# 2. Lazy import inside play() after the inference thread already loaded mlx is caught too.
lazy_root = Path(sys.argv[1]) / "lazy"
lazy_job = EvalJob(
    tournament=RoundRobin(name="lazy", environment=environment("l"), configs=({},), games=1),
    enter=(checkpoint("one"), checkpoint("two")),
    play=lazy_play,
    agents=Agents(load=load),
    repo=None,
    max_hours=0.1,
)
hold_group(evaluation_group(lazy_job.lower(ExperimentState(experiment="l"))), directory=lazy_root)
[lazy_path] = list((lazy_root / "l/artifacts/agents").glob("game-*.json"))
lazy_report = GameReport.model_validate_json(lazy_path.read_bytes())
assert lazy_report.imports.heavy == ("mlx",)
assert lazy_report.imports.importer == "lazy_mlx_game_fixture"
assert lazy_report.imports.warning is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(gpu)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("game module wrapper_game_fixture: imported torch") == 1
    assert result.stderr.count("game module lazy_mlx_game_fixture: imported mlx") == 1


async def scalar_vector_slots(
    matches: Matches[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result]]:
    while True:
        taken = await matches.take(4)
        if not taken:
            return
        forward = cast(Forward, taken[0][1][0])
        outputs = await forward([match.config.turns for match, _ in taken])
        for (match, _), score in zip(taken, outputs, strict=True):
            yield match, Result(tables={"vector_scores": [{"seat": 0, "score": int(str(score))}]})


def test_mixed_and_small_gpu_groups_match_exact_spawned_processes_and_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 16)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    # 1. Small play GPU (5 pending, max_batch=32, 16 cores): 1 process, 5 games, 15 agent threads.
    small_root = tmp_path / "small"
    agents = Agents(load=load_checkpoint, max_batch=32)
    small_job = EvalJob(
        tournament=RoundRobin(
            name="small",
            environment=environment("grouped"),
            config={"lane": "small"},
            games=5,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play=play,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    assert [r.played for r in hold_group(group(small_job), directory=small_root)] == [5]
    small_err = capfd.readouterr().err
    assert "1 processes, 5 games each; 15 inference threads" in small_err
    small_reports = list((small_root / "grouped/artifacts/agents").glob("game-*.json"))
    assert len(small_reports) == 1
    small_threads = json.loads((small_root / "grouped/artifacts/agents/threads.json").read_text())
    assert set(small_threads.values()) == {"15"}

    # 2. Mixed GPU (play_many + play, max_batch=256, 16 cores): 2 processes, 14 agent threads.
    mixed_root = tmp_path / "mixed"
    mixed_agents = Agents(load=load_checkpoint, max_batch=256)
    stream_job = EvalJob(
        tournament=RoundRobin(
            name="stream",
            environment=environment("grouped"),
            config=VectorRules(),
            games=2,
            base_seed=1,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=scalar_vector_slots,
        agents=mixed_agents,
        repo=None,
        max_hours=0.1,
    )
    play_job = EvalJob(
        tournament=RoundRobin(
            name="ordinary",
            environment=environment("grouped"),
            config={"lane": "ordinary"},
            games=3,
            base_seed=2,
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play=play,
        agents=mixed_agents,
        repo=None,
        max_hours=0.1,
    )
    assert [r.played for r in hold_group(group(stream_job, play_job), directory=mixed_root)] == [
        2,
        3,
    ]
    mixed_err = capfd.readouterr().err
    assert "14 inference threads" in mixed_err
    mixed_reports = list((mixed_root / "grouped/artifacts/agents").glob("game-*.json"))
    assert len(mixed_reports) == 2
    mixed_threads = json.loads((mixed_root / "grouped/artifacts/agents/threads.json").read_text())
    assert set(mixed_threads.values()) == {"14"}


async def invalid_stream(matches: Matches[Config]) -> AsyncIterator[tuple[Match, Tables]]:
    taken = await matches.take(2)
    match, agents = taken[0]
    outcome = match.config["outcome"]
    if outcome == "abandoned":
        return
    if outcome == "unknown":
        yield match.model_copy(), await plain(match, list(agents))
    if outcome == "escaped":
        raise ValueError("fixture escaped the generator")
    result = await plain(match, list(agents))
    yield match, result
    yield match, result


@pytest.mark.parametrize(
    ("outcome", "detail", "failed"),
    [
        ("duplicate", "unknown or already-yielded match", 1),
        ("unknown", "unknown or already-yielded match", 2),
        ("abandoned", "without yielding its held matches", 2),
        ("escaped", "fixture escaped the generator", 2),
    ],
)
def test_play_many_retains_held_matches_on_a_broken_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, detail: str, failed: int
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    matches = [
        Match(players=(Code(name="one"),), config={"outcome": outcome}, index=index)
        for index in range(3)
    ]
    with (
        pytest.raises(EtudeError, match=detail),
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=matches,
            run="broken",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(invalid_stream)
    db = statsdb("vector", tmp_path)
    try:
        records = db.sql("select match, state, error from matches").fetchall()
        assert {key for key, _, _ in records} == {match.key for match in matches[:2]}
        errors = [error for _, state, error in records if state == "failed"]
        assert len(errors) == failed
        assert all(detail in error for error in errors)
    finally:
        db.close()


def test_play_many_requires_one_async_generator_with_a_match_source(tmp_path: Path) -> None:
    async def wrong_source(match: Match) -> AsyncIterator[tuple[Match, Tables]]:
        yield match, {}

    with pytest.raises(LauncherError, match="either async def play or async def play_many"):
        EvalJob(
            tournament=tournament("both"),
            enter=(checkpoint("left"), checkpoint("right")),
            play=plain,
            play_many=vector_slots,
            max_hours=0.1,
        )
    with pytest.raises(LauncherError, match="either async def play or async def play_many"):
        EvalJob(
            tournament=tournament("neither"),
            enter=(checkpoint("left"), checkpoint("right")),
            max_hours=0.1,
        )
    with evaluating(
        environment("vector"), experiment="vector", matches=[], run="signature", directory=tmp_path
    ) as active:
        with pytest.raises(TypeError, match="async def generator"):
            active.play_many(plain)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"Matches\[YourConfig\]"):
            active.play_many(wrong_source)


def test_launched_group_recovers_both_tournaments_with_their_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = job("ladder", None)
    second = job("gauntlet", None)
    ladder, gauntlet = group(first, second).evaluations
    # An explicit member name can even match the assigned launch name.
    address = "grouped/evals-test"
    selected = EvalGroup(evaluations=(ladder.model_copy(update={"run": address}), gauntlet))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    monkeypatch.setenv("ETUDE_RUN_ADDRESS", address)
    monkeypatch.setenv("ETUDE_RUN", "evals-test")
    monkeypatch.setenv("ETUDE_ATTEMPT", "first")
    source = tmp_path / "first"
    root = source / address
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    broken = gauntlet.model_copy(
        update={"play": Function(module=__name__, name=fails_game.__name__)}
    )
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(EvalGroup(evaluations=(selected.evaluations[0], broken)), directory=source)
    (failed,) = observe(source, "grouped").eval_runs
    assert (failed.address, failed.state) == (address, "failed")
    next_host = tmp_path / "replacement"
    put_back(next_host / address, restore_set(root))
    source, root = next_host, next_host / address
    monkeypatch.setenv("ETUDE_OUT", str(root / "out"))
    monkeypatch.setenv("ETUDE_ATTEMPT", "replacement")
    outputs = tmp_path / "replacement-outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    assert [r.played for r in hold_group(selected, directory=source)] == [0, 1]
    declaration = RunRecord.model_validate_json((root / "run.json").read_bytes())
    assert declaration.members == (address, gauntlet.address)
    progress = RunProgress.model_validate_json((outputs / "etude-result.json").read_bytes())
    assert progress.tally and progress.tally.done == progress.tally.total == 2
    (writer,) = progress.writers
    assert (writer.run, writer.attempt) == (address, "replacement")
    assert (
        RunOutcome.model_validate_json((root / "outcome.json").read_bytes()).writer == writer.writer
    )

    copied = DirectoryStorage(tmp_path / "archive" / "copy")
    assert sync(source, storages=[copied]) == {}
    assert all(key.startswith(address + "/") for key in copied.keys(""))
    (recovered,) = observe(tmp_path / "archive", "grouped").eval_runs
    assert (recovered.address, recovered.state, recovered.status) == (address, "finished", None)
    report = build_page(tmp_path / "archive", None, experiment="grouped")
    assert {name for selection in report.drawn for name in selection.tournaments} == {
        "ladder",
        "gauntlet",
    }
    db = statsdb("grouped", tmp_path / "archive")
    try:
        assert db.sql("select run, score from scores order by run").fetchall() == [
            (run, 7) for run in sorted(("evals-test", gauntlet.address.split("/")[-1]))
        ]
    finally:
        db.close()
    next_host = tmp_path / "next"
    restored = next_host / address
    put_back(restored, restore_set(copied.root / address))
    monkeypatch.setenv("ETUDE_ATTEMPT", "second")
    monkeypatch.setenv("ETUDE_OUT", str(restored / "out"))
    assert [r.skipped for r in hold_group(selected, directory=next_host)] == [1, 1]


def test_legacy_group_ownership_requires_the_retained_address_and_member_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = job("ladder", None)
    selected = group(definition)
    (spec,) = selected.evaluations
    spec = spec.model_copy(update={"run": "original"})
    source = DirectoryStorage(tmp_path / "source")
    addresses = ("grouped/first", "grouped/retry")
    member = RunRecord(id=spec.address, experiment="grouped", kind="evaluation")
    for prefix in ("", *(f"copy/{address}/" for address in addresses)):
        source.put(prefix + spec.address + "/run.json", member.model_dump_json().encode())
        source.put(
            prefix + spec.address + "/outcome.json",
            RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
        )
    submitted = definition.lower(ExperimentState(experiment="grouped")).model_copy(
        update={"config": EvalGroup(evaluations=(spec,)).model_dump_json()}
    )
    backend = bench(tmp_path / "launcher")
    backend.scheduler.queue(
        [RunRequest(id=address, job=submitted) for address in addresses], rent="l4"
    )
    monkeypatch.setattr(client, "standing", backend.scheduler.standing)
    copied = DirectoryStorage(tmp_path / "copy")
    assert sync(source.root, storages=[copied]) == {}
    rows = {s.address: s for s in observe(copied, "grouped").eval_runs}
    assert set(rows) == {*addresses, spec.address}
    for address in addresses:
        row = rows[address]
        assert row.state == "queued" and row.last_write and row.status
        assert row.matches == tuple(match.key for match in spec.matches)
    assert rows[spec.address].state == "finished" and rows[spec.address].status is None
    detached = state_of(read_all(copied), "grouped", retained=())
    assert [(r.address, r.state) for r in detached.eval_runs] == [(spec.address, "finished")]
    # A declaration is authoritative even if old launcher evidence names a
    # different tournament at the same physical location.
    parent = "copy/" + addresses[0] + "/run.json"
    copied.put(
        parent,
        RunRecord(
            id=addresses[0], experiment="grouped", kind="evaluation", members=("grouped/different",)
        )
        .model_dump_json()
        .encode(),
    )
    os.utime(copied.root / parent, (1000, 1000))
    updated = {s.address: s for s in observe(copied, "grouped").eval_runs}
    written = updated[addresses[0]].last_write
    assert written and written.timestamp() == 1000
    backend.store.close()
