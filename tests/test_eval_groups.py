"""Grouped evals share agents and preserve tournament delivery and admission."""

import asyncio
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext
from decimal import Decimal
from hashlib import sha256
from importlib import import_module
from multiprocessing import active_children
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import NoReturn, cast

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
    CheckpointRequest,
    Forward,
    ForwardError,
    ModelForward,
    SocketForwardBus,
    decode_answer,
    decode_requests,
    encode_answer,
    encode_request,
    recv_frame,
    send_frame,
)
from etude.eval.batching import Batch, _add_socket_batch, _Batches, _send_peer
from etude.eval.bus import ForwardCall, ForwardOutcome
from etude.eval.game import GameReport, MatchStream, StreamEnd, Take
from etude.eval.loader import AgentLoader
from etude.eval.match import environment
from etude.eval.query import _kept_path, statsdb
from etude.eval.records import Code, Config, Match, RoundRobin, checkpoint_player
from etude.eval.resources import (
    Accelerator,
    Device,
    Inference,
    Placement,
    TournamentQueue,
    _detect_accelerator,
    available_cpus,
    memory_bytes,
    place,
    resources,
)
from etude.eval.result import Result, Tables
from etude.eval.run import EvalRun, Playing, Terminated
from etude.eval.tournament import evaluating, played
from etude.experiment import observe, state_of
from etude.jobs import (
    Agents,
    EvalGroup,
    EvalJob,
    Evaluation,
    ExperimentState,
    _group_key,
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
from etude.run import Run, sync
from etude.spec import Job, RunRequest
from etude.storage import DirectoryStorage, default_root
from etude.train.read import read_all
from tests.eval.arithmetic import checkpoint, load, play
from tests.eval.playing import identify_outcome, wait_for_games
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
    monkeypatch.setattr(
        eval_resources,
        "_detect_accelerator",
        lambda: "cuda" if shared else "no accelerator detected",
    )
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 4)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 2 * 1024**3)


async def plain(match: Match, agents: list[object]) -> Tables:
    return {"scores": [{"seat": 0, "score": 7}]}


async def request_stop(match: Match, agents: list[object]) -> Tables:
    Path(os.environ["ETUDE_STOP_FILE"]).touch()
    return await plain(match, agents)


def tournament(name: str, *, games: int | None = None) -> RoundRobin:
    if games is not None:
        return RoundRobin(
            name=name, environment=environment("grouped"), config={"lane": name}, games=games
        )
    return RoundRobin(
        name=name, environment=environment("grouped"), configs=({"lane": name},), games=1
    )


def job(
    name: str,
    agents: Agents | None,
    playing: Callable[..., Awaitable[Tables]] | None = None,
    *,
    games: int | None = None,
) -> EvalJob:
    return EvalJob(
        tournament=tournament(name, games=games),
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


def _game_reports(directory: Path) -> list[GameReport]:
    return [
        GameReport.model_validate_json(path.read_bytes()) for path in directory.glob("game-*.json")
    ]


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


def test_a_deadline_before_play_stops_the_tournament_with_retryable_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_DEADLINE", "2000-01-01T00:00:00+00:00")
    active = job("deadline", None)
    with pytest.raises(Terminated, match="execution deadline"):
        hold_group(group(active), directory=tmp_path)
    result = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(result.new) == len(result.failed) == len(result.retry) == 1
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state, retry from matches").fetchall() == [("failed", True)]
        assert db.sql("select state from runs").fetchall() == [("stopped",)]
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    first, second = job("first", None), job("second", None)
    with pytest.raises(RetryRequested, match="delivery failed"):
        hold_group(
            group(first, second),
            directory=tmp_path / "local",
            storages=[OfflineStorage(tmp_path / "offline")],
        )
    db = statsdb("grouped", tmp_path / "local")
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
        assert db.sql("select state, retry from matches order by state").fetchall() == [
            ("done", False),
            ("failed", True),
        ]
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
    reports = _game_reports(tmp_path / "grouped/artifacts/agents")
    assert len(reports) == 2
    assert sum(r.matches for r in reports) == 2
    assert sum(r.bus.calls for r in reports) == 4
    assert sum(r.bus.answers for r in reports) == 4
    log = capfd.readouterr().err
    assert log.count("agents: shared inference") == 1
    assert log.count("agents: loaded") == 2
    assert "2 checkpoints loaded, 4 rows" in log
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: shared inference" not in capfd.readouterr().err
    assert len(_game_reports(tmp_path / "grouped/artifacts/agents")) == 2
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
        "pending",
        "agents",
        "max_batch",
        "memory_gb",
        "expected",
    ),
    [
        # Saturated play CPU: 64 pending, max_batch=32 -> 16/32 processes.
        (32, (64,), True, 32, None, ("local", 32, 1)),
        (16, (64,), True, 32, None, ("local", 16, 1)),
        (16, (5,), True, 32, None, ("local", 5, 2)),
        (16, (30,), True, 256, None, ("local", 16, 1)),
        (16, (16,), True, 32, None, ("local", 16, 1)),
        # Direct processes share queues, so the largest determines their count.
        (8, (4, 3, 2), True, 8, None, ("local", 4, 1)),
        (3, (20,), True, 32, None, ("local", 3, 1)),
        (2, (20,), True, 32, None, ("local", 2, 1)),
        (8, (20,), False, 32, None, (None, 8, 0)),
        (8, (20,), True, 32, None, ("local", 4, 1)),
        (8, (20,), True, 32, 1.5, ("local", 2, 3)),
        (8, (1,), True, 32, None, ("local", 1, 7)),
        (1, (20,), True, 32, None, ("local", 1, 1)),
        (1, (20,), False, 32, None, (None, 1, 0)),
        (8, (), True, 32, None, (None, 0, 0)),
        (8, (0,), True, 32, None, (None, 0, 0)),
        (8, (20,), True, 32, 2.0, ("local", 1, 7)),
    ],
)
def test_place_respects_cpu_pending_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int,
    pending: tuple[int, ...],
    agents: bool,
    max_batch: int,
    memory_gb: float | None,
    expected: tuple[str | None, int, int],
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: "no accelerator detected")
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr(
        "etude.eval.resources.memory_bytes", lambda: 16 * 1024**3 if cpus >= 16 else 2 * 1024**3
    )
    chosen = place(
        [TournamentQueue(count) for count in pending],
        loader=Agents(load=load, max_batch=max_batch, memory_gb=memory_gb).plain(None),
        checkpoints=agents,
    )
    assert (
        chosen.bus,
        chosen.processes,
        chosen.inference_threads,
    ) == expected


@pytest.mark.parametrize(
    ("match_mib", "processes", "threads"),
    [(200, 10, 1), (300, 6, 1), (2100, 1, 15), (1, 16, 1)],
)
@pytest.mark.parametrize("shared_agents", [False], indirect=True)
def test_place_sizes_direct_caches_for_a_match(
    monkeypatch: pytest.MonkeyPatch, match_mib: int, processes: int, threads: int
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 16)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 16 * 1024**3)
    placement = place(
        (TournamentQueue(64),),
        loader=Agents(load=load, memory_gb=2).plain(None),
        checkpoints=True,
        match_bytes=match_mib * 1024**2,
    )
    assert placement.bus == "local"
    assert placement.processes == processes
    assert placement.inference_threads == threads
    assert placement.checkpoint_bytes == 2 * 1024**3 // processes


@pytest.mark.parametrize(
    (
        "cpus",
        "pending",
        "streaming",
        "max_batch",
        "memory_gb",
        "agents",
        "visible",
        "expected",
    ),
    [
        # Saturated play GPU (4 cores).
        (4, (64,), False, 32, None, True, "0", ("socket", 3, 1)),
        # A single game process keeps inference local even on a declared accelerator.
        (4, (1,), False, 32, None, True, "0", ("local", 1, 3)),
        (16, (16,), False, 32, None, True, "0", ("socket", 15, 1)),
        # Three one-match tournaments leave one core for inference.
        (4, (1, 1, 1), False, 32, None, True, "0", ("socket", 3, 1)),
        (4, (64,), False, 8, None, True, "0", ("socket", 3, 1)),
        (16, (64,), False, 4, None, True, "0", ("socket", 4, 12)),
        (4, (64,), False, 32, 15.5, True, "0", ("socket", 2, 2)),
        (4, (2, 3), True, 32, None, True, "0", ("socket", 3, 1)),
        (4, (64,), False, 32, None, False, "0", (None, 4, 0)),
        (4, (2, 3), True, 32, None, False, "0", (None, 4, 0)),
        (4, (64,), False, 32, None, True, "", ("local", 4, 1)),
        (4, (2, 3), True, 32, None, True, "", ("local", 1, 3)),
    ],
)
def test_cuda_placement_routes_both_play_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cpus: int,
    pending: tuple[int, ...],
    streaming: bool,
    max_batch: int,
    memory_gb: float | None,
    agents: bool,
    visible: str,
    expected: tuple[str | None, int, int],
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", _detect_accelerator)
    monkeypatch.setattr("etude.eval.resources.sys.platform", "linux")
    # Controlled hardware output, without importing or executing a model library.
    probe = tmp_path / "nvidia-smi"
    probe.write_text("#!/bin/sh\nprintf 'ran' > \"$0.ran\"\nprintf '0\\n'\n")
    probe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    chosen = place(
        [TournamentQueue(count, streaming=streaming) for count in pending],
        loader=Agents(load=load, max_batch=max_batch, memory_gb=memory_gb).plain(None),
        checkpoints=agents,
    )
    assert (
        chosen.bus,
        chosen.processes,
        chosen.inference_threads,
    ) == expected
    assert (tmp_path / "nvidia-smi.ran").exists() is (agents and visible == "0")
    if agents:
        assert chosen.reason == ("cuda" if visible == "0" else "CUDA_VISIBLE_DEVICES hides GPUs")


@pytest.mark.parametrize("accelerator", ["cuda", "metal"])
def test_place_allocates_the_remaining_accelerator_processes_to_streaming(
    monkeypatch: pytest.MonkeyPatch, accelerator: Accelerator
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: accelerator)
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 16)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 16 * 1024**3)
    queues = (TournamentQueue(10), TournamentQueue(128, streaming=True))
    plan = place(queues, loader=Agents(load=load, max_batch=32).plain(None), checkpoints=True)
    assert plan.bus == "socket"
    assert plan.processes == 15
    assert plan.process_shares(queues, 32, available=plan.processes) == (8, 7)


def test_code_only_and_cpu_streaming_do_not_probe_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe() -> NoReturn:
        raise AssertionError("GPU probe should not run without a loader or accelerator declaration")

    monkeypatch.setattr(eval_resources, "_detect_accelerator", fail_probe)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    assert place((TournamentQueue(6),), loader=AgentLoader(), checkpoints=False) == Placement(
        bus=None,
        processes=4,
        inference_threads=0,
        checkpoint_bytes=0,
        reason="no loader",
    )
    assert place(
        (TournamentQueue(2, streaming=True), TournamentQueue(3, streaming=True)),
        loader=AgentLoader(),
        checkpoints=False,
    ) == Placement(
        bus=None,
        processes=4,
        inference_threads=0,
        checkpoint_bytes=0,
        reason="no loader",
    )
    assert place(
        (TournamentQueue(2, streaming=True), TournamentQueue(3, streaming=True)),
        loader=Agents(load=load, devices=("cpu",)).plain(None),
        checkpoints=True,
        match_bytes=16 * 1024**3,
    ) == Placement(
        bus="local",
        processes=1,
        inference_threads=3,
        checkpoint_bytes=8 * 1024**3,
        reason="cpu-only loader",
    )


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
@pytest.mark.parametrize("memory_gb", [None, 1.5])
def test_place_reserves_checkpoint_memory_once(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, memory_gb: float | None
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    samples = iter([2 * 1024**3])
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: next(samples))
    shared = bool(request.node.callspec.params["shared_agents"])
    placement = place(
        (TournamentQueue(6),),
        loader=Agents(load=load, max_batch=2, memory_gb=memory_gb).plain(None),
        checkpoints=True,
        match_bytes=2 * 1024**3 if shared else 1,
    )
    assert placement.bus == ("socket" if shared else "local")
    assert placement.processes > 1
    reserved = (1024 if memory_gb is None else 1536) * 1024**2
    owners = 1 if shared else placement.processes
    assert placement.checkpoint_bytes == reserved // owners


def test_a_failing_nvidia_driver_plays_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", _detect_accelerator)
    monkeypatch.setattr("etude.eval.resources.sys.platform", "linux")
    probe = tmp_path / "nvidia-smi"
    probe.write_text("#!/bin/sh\necho 'driver/library version mismatch' >&2\nexit 9\n")
    probe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    plan = place((TournamentQueue(64),), loader=Agents(load=load).plain(None), checkpoints=True)
    assert (plan.reason, plan.processes) == ("nvidia-smi failed", 4)


@pytest.mark.parametrize(
    ("plat", "machine", "cpus", "memory_gb", "batch_inputs", "processes", "threads", "accelerator"),
    [
        ("darwin", "arm64", 8, None, 32, 7, 1, "metal"),
        ("darwin", "arm64", 16, None, 8, 8, 8, "metal"),
        ("darwin", "arm64", 1, None, 32, 1, 1, "metal"),
        ("darwin", "arm64", 8, 15.5, 32, 2, 6, "metal"),
        ("darwin", "x86_64", 8, None, 32, 8, 1, None),
        ("linux", "arm64", 8, None, 32, 8, 1, None),
    ],
)
def test_apple_silicon_placement_bounds_games_by_cores_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    plat: str,
    machine: str,
    cpus: int,
    memory_gb: float | None,
    batch_inputs: int,
    processes: int,
    threads: int,
    accelerator: Accelerator | None,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", _detect_accelerator)
    monkeypatch.setattr("etude.eval.resources.sys.platform", plat)
    monkeypatch.setattr("etude.eval.resources.platform.machine", lambda: machine)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr("etude.eval.resources.shutil.which", lambda _: None)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    loader = Agents(load=load, memory_gb=memory_gb, max_batch=batch_inputs).plain(None)
    plan = place((TournamentQueue(64),), loader=loader, checkpoints=True)
    shared = accelerator is not None and processes > 1
    assert (plan.processes, plan.inference_threads, plan.reason) == (
        processes,
        threads,
        accelerator or "no accelerator detected",
    )
    held = int(memory_gb * 1024**3) if memory_gb is not None else 8 * 1024**3
    assert plan.bus == ("socket" if shared else "local")
    assert plan.checkpoint_bytes == (held if shared else held // processes)


@pytest.mark.parametrize(
    ("detected", "devices", "inference"),
    [
        ("metal", ("cuda",), "loader does not declare metal"),
        ("metal", ("metal",), "metal"),
        ("cuda", ("metal",), "loader does not declare cuda"),
        ("cuda", ("cuda",), "cuda"),
        ("no accelerator detected", ("cuda",), "no accelerator detected"),
        ("no accelerator detected", ("metal",), "no accelerator detected"),
    ],
)
def test_agent_devices_share_only_on_declared_accelerators(
    monkeypatch: pytest.MonkeyPatch,
    detected: Inference,
    devices: tuple[Device, ...],
    inference: Inference,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: detected)
    loader = Agents(load=load, devices=devices).plain(None)
    assert place((TournamentQueue(1),), loader=loader, checkpoints=True).reason == inference


def test_probe_skipped_without_checkpoint_loading_or_accelerator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_probe() -> NoReturn:
        raise AssertionError("GPU probe should not run")

    monkeypatch.setattr(eval_resources, "_detect_accelerator", fail_probe)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    for loader, checkpoints, reason in (
        (AgentLoader(), True, "no loader"),
        (Agents(load=load).plain(None), False, "no checkpoints"),
    ):
        plan = place((TournamentQueue(8),), loader=loader, checkpoints=checkpoints)
        assert (plan.reason, plan.processes, plan.checkpoint_bytes) == (
            reason,
            8,
            0,
        )
    loader = Agents(load=load, devices=("cpu",), max_batch=16).plain(None)
    cpu = place((TournamentQueue(8),), loader=loader, checkpoints=True)
    assert (cpu.reason, cpu.processes, cpu.inference_threads) == (
        "cpu-only loader",
        8,
        1,
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
    ("device", "cpus", "memory_gb", "expected_processes"),
    [
        ("cuda", 1, None, 1),
        ("cuda", 4, None, 3),
        ("cuda", 4, 1.75, 1),
        ("cuda", 8, None, 3),
        ("metal", 3, None, 2),
    ],
)
def test_group_execution_respects_declared_agent_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    cpus: int,
    memory_gb: float | None,
    expected_processes: int,
    device: Accelerator,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: device)
    measurements = iter([cpus])
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: next(measurements))
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    outstanding: list[int] = []
    shared = expected_processes > 1

    def count_processes(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> list[object]:
        # Each live game process contributes pipe + sentinel; agent contributes sentinel.
        outstanding.append((len(handles) - int(shared)) // 2)
        return list(wait(handles, timeout=timeout))

    monkeypatch.setattr("etude.eval.run.wait", count_processes)
    agents = Agents(load=load_checkpoint, memory_gb=memory_gb)
    selected = group(*(job(name, agents) for name in ("one", "two", "three")))
    assert [result.played for result in hold_group(selected, directory=tmp_path)] == [1, 1, 1]
    assert max(outstanding) == expected_processes
    expected_threads = max(1, cpus - expected_processes)
    process = "game process" if expected_processes == 1 else "game processes"
    thread = "thread" if expected_threads == 1 else "threads"
    mode = "shared" if shared else "direct"
    assert (
        f"{mode} inference for one, two, three ({device}); "
        f"{expected_processes} {process}, 1 game each; "
        f"{expected_threads} inference {thread}" in capfd.readouterr().err
    )
    artifacts = tmp_path / "grouped/artifacts/agents"
    [batch_file] = artifacts.glob("batches.json" if shared else "batches-*.json")
    batches = json.loads(batch_file.read_bytes())
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


def test_agent_device_declarations_validate_and_round_trip() -> None:
    loader = Agents(load=load).plain(None)
    for model, load_function in ((Agents, load), (AgentLoader, loader.load)):
        for invalid in ([], ["mps"], "cuda", None):
            with pytest.raises(ValidationError):
                model.model_validate({"load": load_function, "devices": invalid})
        with pytest.raises(ValidationError) as misspelled:
            model.model_validate({"load": load_function, "devices": ["mps"]})
        assert misspelled.value.error_count() == 1
        assert "'cuda', 'metal' or 'cpu'" in str(misspelled.value)
        with pytest.raises(ValidationError, match="devices requires a checkpoint loader"):
            model.model_validate({"devices": ["cuda", "metal"]})
    for unused in ({"max_batch": 8}, {"memory_gb": 1.0}):
        with pytest.raises(ValidationError, match="requires a checkpoint loader"):
            Agents.model_validate(unused)

    declaration = Agents(load=load, devices=("metal", "cpu", "cuda", "metal"))
    assert declaration.devices == ("cuda", "metal")
    record = declaration.plain(None)
    mixed = record.model_dump() | {"devices": ("cpu", "metal", "cuda")}
    assert AgentLoader.model_validate(mixed) == record
    assert AgentLoader.model_validate_json(record.model_dump_json()) == record

    released = loader.model_dump_json(exclude={"devices"})
    assert AgentLoader.model_validate_json(released).devices == ("cuda", "metal")
    empty = Agents().plain(None)
    assert AgentLoader.model_validate_json(empty.model_dump_json()) == empty


def test_agent_devices_control_grouping_and_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    state = ExperimentState(experiment="grouped")
    auto_job = job("auto", Agents(load=load))
    auto_peer = job("auto_peer", Agents(load=load, devices=("metal", "cpu", "cuda", "metal")))
    direct_job = job("direct", Agents(load=load, devices=("cpu",)))
    tuned = Agents(load=load, devices=("cpu",), memory_gb=1.0)
    tuned_job = job("tuned", tuned)
    tuned_peer = job("tuned_peer", tuned)
    metal_job = job("metal", Agents(load=load, devices=("metal",)))

    def group_key(item: EvalJob) -> str:
        lowered = item.lower(state)
        return _group_key(lowered, evaluation_group(lowered).evaluations[0])

    assert group_key(auto_job) == group_key(auto_peer)
    assert group_key(tuned_job) == group_key(tuned_peer)
    assert len({group_key(j) for j in (auto_job, direct_job, tuned_job, metal_job)}) == 4

    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: "metal")
    with pytest.raises(EtudeError, match="incompatible_eval_agents"):
        hold_group(group(auto_job, direct_job), directory=tmp_path / "incompatible")
    direct_dir = tmp_path / "direct"
    assert [
        result.played for result in hold_group(group(tuned_job, tuned_peer), directory=direct_dir)
    ] == [1, 1]
    assert (
        "etude: agents: direct inference for tuned, tuned_peer (cpu-only loader); "
        "1 game process, 1 game each; 1 inference thread" in capfd.readouterr().err
    )
    assert not (direct_dir / "grouped/artifacts/agents/batches.json").exists()


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
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(echo, 4, 1024, encode=identify_outcome)
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


def test_oversized_game_request_uses_bounded_forwards_and_one_ordered_reply() -> None:
    game, agent = socket.socketpair()
    player = checkpoint("one")
    batches = _Batches(echo, 2, 1024, encode=encode_answer)
    inputs: list[object] = [1, 2, 3, 4, 5]
    with game, agent:
        _add_socket_batch(
            batches,
            _send_peer(agent),
            [CheckpointRequest(1, player.checkpoint, inputs, len(inputs))],
        )
        observed = batches.flush()
        assert decode_answer(recv_frame(game)) == Answer(1, ForwardOutcome("one", inputs))
        assert not wait([game], timeout=0)
        assert [(batch.rows, batch.forward_calls) for batch in observed] == [
            (2, 1),
            (2, 1),
            (1, 1),
        ]
        assert [batch.requests for batch in observed] == [1, 0, 0]
        assert batches.loads == 1


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
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(echo, 1, 2, encode=identify_outcome)
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
    reports = _game_reports(tmp_path / "grouped/artifacts/agents")
    assert len(reports) == 2
    assert [r.bus.calls for r in reports] == [1, 1]
    assert [r.bus.answers for r in reports] == [1, 1]
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state, retry from matches").fetchall() == [("failed", False)] * 2
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
    finally:
        db.close()


def restore_unreadable_value() -> NoReturn:
    raise ModuleNotFoundError("fixture value class is unavailable")


class UnreadableValue:
    def __reduce__(self) -> tuple[Callable[[], NoReturn], tuple[()]]:
        return restore_unreadable_value, ()


class UnencodableValue:
    def __reduce__(self) -> NoReturn:
        raise RetryRequested("fixture value cannot be encoded")


class WireFailures:
    def __call__(self, inputs: list[object]) -> list[object]:
        return [
            UnencodableValue()
            if row == "encode_output"
            else UnreadableValue()
            if row == "decode_output"
            else row
            for row in inputs
        ]


def wire_failures(checkpoint: CheckpointRef) -> ModelForward:
    return WireFailures()


async def sends_bad_payload(match: Match[Config], agents: Sequence[Forward]) -> Tables:
    stage = match.config["lane"]
    value: object = stage
    if stage == "encode_input":
        value = UnencodableValue()
    elif stage == "decode_input":
        value = UnreadableValue()
    bad, good = await asyncio.gather(agents[0]([value]), agents[1]([7]), return_exceptions=True)
    assert good == [7]
    assert await agents[0]([8]) == [8]
    assert isinstance(bad, ForwardError)
    assert bad.checkpoint == match.players[0].name
    assert not bad.retry and not bad.loading
    raise bad


@pytest.mark.parametrize(
    ("stage", "message", "answers"),
    [
        ("encode_input", "could not encode inputs", 2),
        ("decode_input", "agent request failed to decode", 3),
        ("encode_output", "could not encode outputs", 3),
        ("decode_output", "agent reply failed to decode", 3),
    ],
)
def test_bad_wire_payload_fails_only_its_request(
    tmp_path: Path, stage: str, message: str, answers: int
) -> None:
    agents = Agents(load=wire_failures)
    selected = group(job(stage, agents, sends_bad_payload), job("healthy", agents))
    broken, healthy = [spec.address.split("/")[-1] for spec in selected.evaluations]
    with pytest.raises(EtudeError, match=message):
        hold_group(selected, directory=tmp_path)
    reports = {r.tournaments: r for r in _game_reports(tmp_path / "grouped/artifacts/agents")}
    bus = reports[(stage,)].bus
    assert (bus.calls, bus.answers) == (answers, answers)
    db = statsdb("grouped", tmp_path)
    try:
        assert set(db.sql("select run, state, retry from matches").fetchall()) == {
            (broken, "failed", False),
            (healthy, "done", False),
        }
        assert db.sql("select distinct run from scores").fetchall() == [(healthy,)]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("encode_output", "could not encode outputs"),
        ("decode_output", "agent reply failed to decode"),
    ],
)
def test_bad_output_preserves_the_other_reply_in_a_merged_forward(
    payload: str, message: str
) -> None:
    ref = checkpoint("one").checkpoint
    batches = _Batches(wire_failures, 2, 1024, encode=encode_answer)
    game, agent = socket.socketpair()
    with game, agent:
        _add_socket_batch(
            batches,
            _send_peer(agent),
            [
                CheckpointRequest(1, ref, [payload], 1),
                CheckpointRequest(2, ref, [7], 1),
            ],
        )
        [batch] = batches.flush()
        assert (batch.rows, batch.requests, batch.forward_calls) == (2, 2, 1)
        bad = decode_answer(recv_frame(game))
        assert (bad.id, bad.outcome.checkpoint) == (1, ref.key)
        assert bad.outcome.error is not None and message in bad.outcome.error
        assert not bad.outcome.retry
        assert decode_answer(recv_frame(game)) == Answer(2, ForwardOutcome(ref.key, [7]))


@pytest.mark.parametrize("truncated", [False, True])
def test_agent_disconnection_retries_unanswered_requests(tmp_path: Path, truncated: bool) -> None:
    async def exchange(address: str, listener: socket.socket) -> None:
        ref = checkpoint("one").checkpoint
        bus = await SocketForwardBus.connect(address)
        peer, _ = listener.accept()
        with peer:
            try:
                bus.bind(ref)
                futures = bus.submit([ForwardCall(ref.key, [1], 1), ForwardCall(ref.key, [2], 1)])
                if truncated:
                    header, body = encode_answer(1, ForwardOutcome(ref.key, [1]))
                    send_frame(peer, header, body[:-1])
                peer.shutdown(socket.SHUT_WR)
                async with asyncio.timeout(5):
                    answers = await asyncio.gather(*futures, return_exceptions=True)
                assert all(isinstance(answer, RetryRequested) for answer in answers)
            finally:
                await bus.close()

    with resources(tmp_path) as owned, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        address = str(owned.scratch / "agent.sock")
        server.bind(address)
        server.listen()
        asyncio.run(exchange(address, server))


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
from etude.spec import Runtime
from tests.eval.arithmetic import load
from tests.eval.arithmetic import checkpoint
from tests.eval.arithmetic import play

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    agents = Agents(load=load, devices=("cpu",))
    return [EvalJob(tournament=RoundRobin(name=name, environment=environment("agents"),
                   configs=({{"lane": name}},), games=1),
                   enter=(checkpoint("left"), checkpoint("right")), play=play, agents=agents,
                   repo=None, runtime=Runtime(python={command_python!r}), max_hours=0.05)
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
        assert log.count("agents: shared inference") == log.count("agents: stopped") == 0
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
from pathlib import Path
from tempfile import TemporaryDirectory
from etude.call import Function
from etude.eval.agents import CheckpointRequest, decode_requests, encode_answer, recv_frame, send_frame
from etude.eval.bus import ForwardOutcome
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
            batch = list(decode_requests(recv_frame(peer)))
            counts["received"] += 1
            for req in batch:
                assert isinstance(req, CheckpointRequest)
                send_frame(peer, *encode_answer(req.id, ForwardOutcome(req.checkpoint.key, req.inputs)))
                counts["sent"] += 1
    thread = threading.Thread(target=answer)
    thread.start()
    parent, child = multiprocessing.Pipe()
    match = Match(players=(checkpoint("one"),))
    def drive() -> None:
        req = parent.recv()
        assert isinstance(req, Take)
        parent.send(Grant(matches=(match,), drained=True))
        outcome = parent.recv()
        assert isinstance(outcome, Outcome) and outcome.error is None
        end = parent.recv()
        assert isinstance(end, StreamEnd) and end.error is None
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
        (Stream("light", Function(module="light_agent_game", name="play", path=sys.argv[1]), False, 1, True),),
        checkpoint_bytes=0,
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
    assert log.count("agents: shared inference") == log.count("agents: stopped") == 1


async def stop_parent(match: Match, agents: Sequence[Forward]) -> Tables:
    await agents[0]([1])
    os.kill(os.getppid(), signal.SIGTERM)
    return {"scores": [{"seat": 0, "score": 1}]}


def test_sigterm_reaps_agents_and_games_and_records_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {p.pid for p in active_children()}
    active = job("stop", Agents(load=load)).model_copy(update={"play": stop_parent})
    selected = group(active)
    address = "grouped/interrupted"
    monkeypatch.setenv("ETUDE_RUN_ADDRESS", address)
    with pytest.raises(Terminated):
        hold_group(selected, directory=tmp_path)
    assert {p.pid for p in active_children()} <= before
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.new) == len(found.retry) == len(found.failed) == 1
    root = tmp_path / address
    member = root / selected.evaluations[0].address
    for folder in (root, member):
        assert RunOutcome.model_validate_json((folder / "outcome.json").read_bytes()).state == (
            "stopped"
        )


def test_a_failed_load_is_not_attempted_again_in_the_run() -> None:
    batches: _Batches[tuple[int, ForwardOutcome]] = _Batches(
        raises_load, 1, 10, encode=identify_outcome
    )
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
    wait_for_games(monkeypatch, processes=3, rounds=2)
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
        assert records == {
            matches[0].key: ("failed", False),
            matches[1].key: ("done", False),
            matches[2].key: ("failed", retry),
            matches[3].key: ("failed", True) if retry else ("done", False),
        }
        assert db.sql("select count(*) from scores").fetchone() == (1 if retry else 2,)
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
    assert (repaired.played, repaired.skipped) == ((3, 1) if retry else (2, 2))


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_a_game_failure_does_not_stop_other_tournaments(tmp_path: Path) -> None:
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


@pytest.mark.parametrize("library", ["torch", "mlx"])
@pytest.mark.parametrize("with_agents", [False, True])
@pytest.mark.parametrize("gpu", [False, True])
def test_heavy_import_warning_is_once_and_only_for_games_with_agents(
    tmp_path: Path, library: str, with_agents: bool, gpu: bool
) -> None:
    (tmp_path / f"{library}.py").write_text("# Controlled framework fixture.\n")
    (tmp_path / "heavy_fixture_game.py").write_text(
        f"import {library}\nfrom collections.abc import Sequence\nfrom etude.eval.records import Match\n"
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
etude.eval.resources._detect_accelerator = lambda: "cuda" if sys.argv[3] == "True" else "no accelerator detected"
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
    assert result.stderr.count(f"game module heavy_fixture_game: imported {library}") == int(
        with_agents
    )
    if with_agents:
        assert "move the import to load's module" in result.stderr


def test_agent_startup_failure_reports_retryable_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    monkeypatch.setenv("ETUDE_FIXTURE_TOKEN", "synthetic-fixture-credential")
    # Put the secret in the artifacts path and block batches.json with a directory
    # so the agent process fails during startup before control.send(None).
    root = tmp_path / "synthetic-fixture-credential"
    (root / "grouped/artifacts/agents/batches.json").mkdir(parents=True)
    agents = Agents(load=load)
    selected = group(job("startup", agents), job("another", agents))
    with pytest.raises(RetryRequested, match="startup"):
        hold_group(selected, directory=root)
    log = capfd.readouterr().err
    assert "[redacted]" in log and "synthetic-fixture-credential" not in log
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 2
    spec = selected.evaluations[0]
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
def test_broken_loader_import_leaves_matches_pending_on_both_transports(
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
    selected = group(job("broken_loader", Agents(load=load), games=2))
    spec = selected.evaluations[0]
    assert spec.agents is not None
    spec = spec.model_copy(
        update={
            "agents": spec.agents.model_copy(
                update={"load": Function(module=module_name, name="load", path=str(tmp_path))}
            )
        }
    )
    with pytest.raises(EtudeError, match="eval_startup_failed") as info:
        hold_group(EvalGroup(evaluations=(spec,)), directory=tmp_path)
    assert expected_error in str(info.value)
    assert "synthetic-fixture-credential" not in str(info.value)
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 0
    found = played(
        spec.definition, experiment="grouped", enter=spec.tournament.entrants, directory=tmp_path
    )
    assert len(found.new) == 2 and not found.failed and not found.retry


def build_loader_fixture(player: object, leaves: Mapping[str, Forward]) -> object:
    return player if isinstance(player, Code) else next(iter(leaves.values()))


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_code_only_tournament_bypasses_inference_in_a_mixed_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    shared = request.node.callspec.params["shared_agents"]
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 3)
    module = f"code_only_mixed_fixture_{shared}"
    (tmp_path / f"{module}.py").write_text(
        "from multiprocessing import parent_process\n"
        "from typing import NoReturn\n"
        "from etude.eval.agents import SocketForwardBus\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "async def reject_connection(address: str) -> NoReturn:\n"
        "    raise AssertionError('code-only game tried to connect to the shared agent')\n"
        "if parent_process() is not None:\n"
        "    SocketForwardBus.connect = reject_connection\n"
        "async def play(match: Match, agents: list[object]) -> Tables:\n"
        "    return {'scores': [{'seat': 0, 'score': 7}]}\n"
        "def load(checkpoint: object) -> NoReturn:\n"
        "    raise AssertionError('loader should fail at import')\n"
        + (
            ""
            if shared
            else "if parent_process() is not None:\n    raise RuntimeError('fixture loader import')\n"
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    code_play = import_module(module).play if shared else plain
    loader = load if shared else import_module(module).load
    agents = Agents(load=loader, build=build_loader_fixture)
    code_only = EvalJob(
        tournament=tournament("code-only", games=3),
        enter=(Code(name="one"), Code(name="two")),
        play=code_play,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    model = job("models", agents)
    selected = group(code_only, model)
    if shared:
        assert [result.played for result in hold_group(selected, directory=tmp_path)] == [3, 1]
    else:
        with pytest.raises(EtudeError, match="eval_startup_failed: .*fixture loader import"):
            hold_group(selected, directory=tmp_path)
        kept = played(
            code_only.tournament, experiment="grouped", enter=code_only.enter, directory=tmp_path
        )
        assert len(kept.delivered) == 3 and not kept.failed


@pytest.mark.parametrize("streaming", [False, True], ids=["play", "play_many"])
def test_game_import_failure_fails_its_tournament_and_leaves_siblings_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    monkeypatch.setenv("ETUDE_FAIL_FIXTURE_GAME_IMPORT", "1")
    module = f"bad_game_import_fixture_{'stream' if streaming else 'play'}"
    callback = "play_many" if streaming else "play"
    (tmp_path / f"{module}.py").write_text(
        "import os\n"
        "from multiprocessing import parent_process\n"
        "if parent_process() is not None and os.getenv('ETUDE_FAIL_FIXTURE_GAME_IMPORT'):\n"
        "    raise RuntimeError('fixture game import failed')\n"
        f"async def {callback}({'matches' if streaming else 'match, agents'}):\n"
        + (
            "    while taken := await matches.take(1):\n"
            "        for match, _ in taken:\n"
            "            yield match, {'scores': [{'seat': 0, 'score': 7}]}\n"
            if streaming
            else "    return {'scores': [{'seat': 0, 'score': 7}]}\n"
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    game = getattr(import_module(module), callback)
    selected = group(
        EvalJob(
            tournament=tournament("broken"),
            enter=(Code(name="one"), Code(name="two")),
            play_many=game if streaming else None,
            play=None if streaming else game,
            max_hours=0.1,
        ),
        job("healthy", None),
    )
    broken, healthy = [spec.address.split("/")[-1] for spec in selected.evaluations]
    with pytest.raises(EtudeError, match="fixture game import failed") as caught:
        hold_group(selected, directory=tmp_path)
    assert caught.value.code == "eval_startup_failed"
    db = statsdb("grouped", tmp_path)
    try:
        assert set(db.sql("select run, state from runs").fetchall()) == {
            (broken, "failed"),
            (healthy, "finished"),
        }
        assert db.sql("select run, state, retry from matches").fetchall() == [
            (healthy, "done", False)
        ]
        assert db.sql("select distinct run from scores").fetchall() == [(healthy,)]
    finally:
        db.close()

    monkeypatch.delenv("ETUDE_FAIL_FIXTURE_GAME_IMPORT")
    repaired, sibling = hold_group(selected, directory=tmp_path)
    assert (repaired.played, repaired.skipped) == (1, 0)
    assert (sibling.played, sibling.skipped) == (0, 1)


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
    agents = Agents(
        load=selective_retry_load if retry else selective_load, max_batch=4 if in_flight else 1
    )
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


async def asks_last_seat_first(match: Match, agents: Sequence[Forward]) -> Tables:
    # The first forwards ask "transient", "transient" and "left": the retryable leaf fails first.
    for agent in reversed(agents):
        try:
            await agent([1])
        except ForwardError:
            pass
    return {"scores": [{"seat": 0, "score": 99}]}


def mixed_retry_load(checkpoint: CheckpointRef) -> ModelForward:
    if str(checkpoint.key) == "transient":
        raise RetryRequested("fixture player unavailable")
    if str(checkpoint.key) == "broken":
        raise ValueError("fixture broken player")
    return load(checkpoint)


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_later_permanent_loader_failure_keeps_tournament_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    active = job("one", Agents(load=mixed_retry_load)).model_copy(
        update={
            "enter": (checkpoint("transient"), checkpoint("broken"), checkpoint("left")),
            "play": asks_last_seat_first,
        }
    )
    with pytest.raises(RetryRequested):
        hold_group(group(active), directory=tmp_path)
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.retry) >= 1
    assert len(found.failed) == 3


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_earlier_permanent_tournament_keeps_group_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    agents = Agents(load=mixed_retry_load)
    broken = job("first", agents).model_copy(
        update={"enter": (checkpoint("broken"), checkpoint("left"))}
    )
    transient = job("second", agents).model_copy(
        update={"enter": (checkpoint("transient"), checkpoint("right"))}
    )
    with pytest.raises(RetryRequested):
        hold_group(group(broken, transient), directory=tmp_path)
    first = played(broken.tournament, experiment="grouped", enter=broken.enter, directory=tmp_path)
    second = played(
        transient.tournament, experiment="grouped", enter=transient.enter, directory=tmp_path
    )
    assert len(first.failed) == 1 and not first.retry
    assert len(second.retry) == 1


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
    agents = Agents(load=selective_load, max_batch=1)
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
    batches = _Batches(wire_agent, 2, 1024, encode=encode_answer)
    sockets = [socket.socketpair() for _ in range(2)]
    clients = [parent for parent, _ in sockets]
    receivers = [child for _, child in sockets]
    answer_size = sum(map(len, encode_answer(1, ForwardOutcome("wire", [WireValue(0)]))))
    WireValue.serializations = 0
    observations = []

    try:
        for turn in range(2):
            for index, (peer_client, receiver) in enumerate(zip(clients, receivers, strict=True)):
                request = CheckpointRequest(
                    turn * 2 + index + 1, cp.checkpoint, [WireValue(turn)], 1
                )
                send_frame(peer_client, *encode_request(request))
                decoded = list(decode_requests(recv_frame(receiver)))
                _add_socket_batch(batches, _send_peer(receiver), decoded)
            observations.extend(batches.flush())
            for index, peer_client in enumerate(clients):
                answer_payload = recv_frame(peer_client)
                assert len(answer_payload) == answer_size
                assert decode_answer(answer_payload) == Answer(
                    turn * 2 + index + 1, ForwardOutcome("wire", [str(turn)])
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
    [report] = _game_reports(tmp_path / "grouped/artifacts/agents")
    assert report.matches == 1
    assert report.wall_seconds >= 0.0
    assert report.bus.calls == report.bus.answers == 0
    assert report.bus.round_trip_seconds == report.bus.handoff_seconds == 0.0
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0]
    assert len(_game_reports(tmp_path / "grouped/artifacts/agents")) == 1


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
    [report] = _game_reports(tmp_path / "grouped/standalone/artifacts/agents")
    assert report.matches == 1
    assert report.wall_seconds >= 0.0
    assert bucket.uploads == 6
    assert len([key for key in bucket.keys("") if "/game-" in key]) == 1


async def multi_round_play(match: Match[Config], agents: Sequence[Forward]) -> Tables:
    rounds = int(str(match.config["rounds"]))
    total = 0
    for step in range(rounds):
        [value] = await agents[step % len(agents)]([step])
        total += int(str(value))
    return {"scores": [{"seat": 0, "score": total}]}


def test_direct_play_batches_eight_rows_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 2)
    wait_for_games(monkeypatch, processes=2)
    panel = [Match(players=(checkpoint("one"),), config={"rounds": 1}, index=i) for i in range(16)]
    with evaluating(
        environment("sizing"), experiment="sizing", matches=panel, run="direct", directory=tmp_path
    ) as active:
        agents = Agents(load=load, devices=("cpu",), max_batch=8)
        assert active.play(multi_round_play, agents=agents).played == 16
    artifacts = tmp_path / "sizing/direct/artifacts/agents"
    assert sorted(report.matches for report in _game_reports(artifacts)) == [8, 8]
    assert [
        [batch["rows"] for batch in json.loads(path.read_bytes())]
        for path in artifacts.glob("batches-*.json")
    ] == [[8], [8]]


def terminate_on_second_forward(checkpoint: CheckpointRef) -> ModelForward:
    def forward(inputs: list[object]) -> list[object]:
        if 1 in inputs:
            os.kill(os.getpid(), signal.SIGTERM)
        return inputs

    return forward


def test_agent_sigterm_keeps_completed_batches_in_valid_json(tmp_path: Path) -> None:
    panel = [Match(players=(checkpoint("one"),), config={"rounds": 2}, index=i) for i in range(2)]
    with (
        pytest.raises(RetryRequested),
        evaluating(
            environment("signal"),
            experiment="signal",
            matches=panel,
            run="term",
            directory=tmp_path,
        ) as active,
    ):
        active.play(multi_round_play, agents=Agents(load=terminate_on_second_forward))
    batches = json.loads((tmp_path / "signal/term/artifacts/agents/batches.json").read_text())
    assert 1 <= sum(batch["rows"] for batch in batches) <= 2


@pytest.mark.parametrize(
    ("sizes", "seats", "processes", "loads"),
    [
        pytest.param((80, 80), (0, 1), 1, 2, id="two-checkpoints"),
        pytest.param((300,), (0,), 1, 1, id="above-budget"),
        pytest.param((80,), (0, 0), 2, 2, id="self-play"),
        pytest.param((1, 1), (0, 1), 2, 3, id="tiny-checkpoints"),
    ],
)
@pytest.mark.parametrize("shared_agents", [False], indirect=True)
def test_direct_play_retains_match_checkpoints_within_its_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sizes: tuple[int, ...],
    seats: tuple[int, ...],
    processes: int,
    loads: int,
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 2)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 2 * 1024**3)
    wait_for_games(monkeypatch, processes=processes)
    players = []
    for index, size in enumerate(sizes):
        name = f"checkpoint-{index}"
        path = tmp_path / name
        data = b"1".rjust(size)
        path.write_bytes(data)
        artifact = ArtifactRef(uri=path.as_uri(), sha256=sha256(data).hexdigest(), size_bytes=size)
        players.append(checkpoint_player(CheckpointRef(address=name, artifact=artifact)))
    matches = (
        Match(players=(players[0],), config={"rounds": 6}, index=0),
        Match(players=tuple(players[seat] for seat in seats), config={"rounds": 6}, index=1),
    )
    with evaluating(
        environment("sizing"),
        experiment="sizing",
        matches=matches,
        run="direct",
        directory=tmp_path,
    ) as active:
        result = active.play(
            multi_round_play, agents=Agents(load=load, max_batch=2, memory_gb=256 / 1024**3)
        )
    assert result.played == 2
    artifacts = tmp_path / "sizing/direct/artifacts/agents"
    reports = _game_reports(artifacts)
    assert len(reports) == processes
    assert sum(report.matches for report in reports) == 2
    assert (
        sum(
            batch["loads"]
            for path in artifacts.glob("batches-*.json")
            for batch in json.loads(path.read_bytes())
        )
        == loads
    )
    with statsdb("sizing", tmp_path) as db:
        assert db.sql("select score from scores").fetchall() == [(21,), (21,)]


def test_barrier_batches_games_into_one_message_per_process_per_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6 games of unequal length (2, 2, 3, 3, 4, 4 decisions) in 1 game process:
    # zero-delay barrier batches all waiting games every round, shrinking as games finish.
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    cp = checkpoint("one")
    lengths = (2, 2, 3, 3, 4, 4)
    matches = [
        Match(players=(cp,), config={"rounds": rounds, "slot": index}, games=1)
        for index, rounds in enumerate(lengths)
    ]
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
    [report] = _game_reports(artifacts)
    assert report.matches == 6
    assert report.bus.calls == report.bus.answers == 4
    assert report.bus.wire_bytes == 0
    assert not (artifacts / "batches.json").exists()
    batches = json.loads((artifacts / f"batches-{report.pid}.json").read_text())
    assert [batch["rows"] for batch in batches] == [6, 6, 4, 2]
    assert [batch["requests"] for batch in batches] == [1, 1, 1, 1]
    assert sum(batch["loads"] for batch in batches) == 1


@pytest.mark.parametrize("accelerator", ["cuda", "metal"])
def test_barrier_batches_across_multiple_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accelerator: Accelerator
) -> None:
    # 8 games of 3 rounds each across 2 game processes (4 games each, max_batch=8):
    # each process sends 3 socket messages (P=2 -> 6 messages total, not N*3=24).
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: accelerator)
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
    reports = _game_reports(artifacts)
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


async def oversized_rows(match: Match, agents: Sequence[Forward]) -> Tables:
    assert await agents[0]([], rows=0) == []
    with pytest.raises(ValueError, match="empty inputs"):
        await agents[0]([], rows=1)
    assert await agents[0]([1, 2, 3, 4, 5]) == [1, 2, 3, 4, 5]
    return {"scores": [{"seat": 0, "score": 7}]}


def test_direct_forward_splits_oversized_rows_into_ordered_pieces(tmp_path: Path) -> None:
    with evaluating(
        environment("vector"),
        experiment="vector",
        matches=[Match(players=(checkpoint("one"),))],
        run="direct",
        directory=tmp_path,
    ) as active:
        assert (
            active.play(
                oversized_rows, agents=Agents(load=echo, devices=("cpu",), max_batch=2)
            ).played
            == 1
        )
    [report] = _game_reports(tmp_path / "vector/direct/artifacts/agents")
    batches = [
        Batch.model_validate(batch)
        for batch in json.loads(
            (tmp_path / f"vector/direct/artifacts/agents/batches-{report.pid}.json").read_bytes()
        )
    ]
    assert [batch.rows for batch in batches] == [2, 2, 1]
    assert sum(batch.loads for batch in batches) == 1


async def vector_slots(
    matches: MatchStream[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result | Exception]]:
    active: list[tuple[Match[VectorRules], Forward, int]] = []
    while True:
        for match, agents in await matches.take(min(matches.target_matches, 2) - len(active)):
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
                yield (
                    match.model_copy(),
                    Result(tables={"vector_scores": [{"seat": 0, "score": score}]}),
                )


def test_play_many_grants_spread_a_128_match_panel_across_12_processes(tmp_path: Path) -> None:
    matches = [Match(players=(Code(name="one"),), index=index) for index in range(128)]
    with evaluating(
        environment("vector"), experiment="vector", matches=matches, run="slots", directory=tmp_path
    ) as active:
        playing = Playing(
            run=active,
            play=Function(module=__name__, name=vector_slots.__name__),
            tournament="slots",
            agent_loader=AgentLoader(),
            streaming=True,
        )
        grants: list[tuple[Match, ...]] = []
        for unfed in range(12, 0, -1):
            total_held = sum(map(len, grants))
            grants.append(playing.grant(64, processes=12, unfed=unfed, total_held=total_held))
        assert [len(grant) for grant in grants] == [11] * 8 + [10] * 4
        assert [match for grant in grants for match in grant] == matches
        assert playing.grant(64) == ()

        playing.pending.extend(matches)
        first = playing.grant(64, processes=12, unfed=12)
        refill = playing.grant(64, processes=12, unfed=11, held=len(first), total_held=len(first))
        assert len(refill) == 1
        second = playing.grant(64, processes=12, unfed=11, total_held=len(first) + len(refill))
        assert len(second) == 11


def test_play_many_refills_four_slots_and_caps_an_empty_peers_tail_grant(tmp_path: Path) -> None:
    panel = [Match(players=(Code(name="one"),), index=i) for i in range(300)]
    with evaluating(
        environment("vector"), experiment="vector", matches=panel, run="slots", directory=tmp_path
    ) as active:
        playing = Playing(
            run=active,
            play=Function(module=__name__, name=vector_slots.__name__),
            tournament="slots",
            agent_loader=AgentLoader(),
            streaming=True,
        )
        for started in range(12):
            grant = playing.grant(16, processes=12, unfed=12 - started, total_held=16 * started)
            assert grant == tuple(panel[started * 16 : (started + 1) * 16])

        # One process repeatedly finishes four of its sixteen matches while its peers play.
        for next_match in range(192, 268, 4):
            refill = playing.grant(4, processes=12, unfed=0, held=12, total_held=188)
            assert refill == tuple(panel[next_match : next_match + 4])

        # The same process empties all its slots with 32 pending and eleven peers still playing.
        grant = playing.grant(64, processes=12, unfed=1, total_held=176)
        assert grant == tuple(panel[268:286])
        assert list(playing.pending) == panel[286:]


async def records_target_matches(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables]]:
    while taken := await matches.take(matches.target_matches):
        for match, _ in taken:
            yield match, {"scores": [{"seat": 0, "score": matches.target_matches}]}


@pytest.mark.parametrize(("cpus", "max_batch"), [(3, None), (1, 2)], ids=["share", "batch-cap"])
def test_play_many_targets_its_initial_share_bounded_by_batch_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    cpus: int,
    max_batch: int | None,
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: cpus)
    player = Code(name="one") if max_batch is None else checkpoint("one")
    agents = (
        None
        if max_batch is None
        else Agents(load=load_vector, devices=("cpu",), max_batch=max_batch)
    )
    panel = [Match(players=(player,), index=i) for i in range(5)]
    with evaluating(
        environment("vector"), experiment="vector", matches=panel, run="slots", directory=tmp_path
    ) as active:
        assert active.play_many(records_target_matches, agents=agents).played == 5
    assert "play_many (initial target_matches=2)" in capfd.readouterr().err
    db = statsdb("vector", tmp_path)
    try:
        assert db.sql("select score, count(*) from scores group by score").fetchall() == [(2, 5)]
    finally:
        db.close()


@pytest.mark.parametrize("retry", [False, True])
def test_play_many_backfills_after_an_author_error_and_stops_on_retry(
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
        active.play_many(vector_slots, agents=Agents(load=load_vector, max_batch=2))
    artifacts = tmp_path / "vector/slots/artifacts/agents"
    if not retry:
        assert not (artifacts / "batches.json").exists()
        [batch_file] = list(artifacts.glob("batches-*.json"))
        batches = json.loads(batch_file.read_text())
        assert [batch["rows"] for batch in batches] == [2, 2, 2, 2]
    db = statsdb("vector", tmp_path)
    try:
        failures = db.sql("select match, retry from matches where state = 'failed'").fetchall()
        if retry:
            assert set(failures) == {(match.key, True) for match in matches}
            assert "vector_scores" not in {name for (name,) in db.sql("show tables").fetchall()}
        else:
            assert failures == [(matches[0].key, False)]
            assert set(db.sql("select match, score from vector_scores").fetchall()) == {
                (match.key, 2) for match in matches[1:]
            }
    finally:
        db.close()


@pytest.mark.parametrize("accelerator", ["no accelerator detected", "cuda", "metal"])
def test_mixed_play_functions_share_checkpoint_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accelerator: Inference,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: accelerator)
    shared = accelerator in ("cuda", "metal")
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 4 * 1024**3)
    if shared:
        wait_for_games(monkeypatch, processes=3)
    players = (checkpoint("left"), checkpoint("right"))
    # A cache big enough for these two two-byte checkpoints retains both across streams.
    agents = Agents(load=load_array_checkpoint, max_batch=3, memory_gb=4 / 1024**3)
    jobs = [
        EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(),
                games=2,
                base_seed=seed,
            ),
            enter=players,
            play_many=array_vector_slots,
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
    stderr = capfd.readouterr().err
    startup = [line for line in stderr.splitlines() if "inference for " in line]
    mode = "shared" if shared else "direct"
    processes = "3 game processes" if shared else "1 game process"
    threads = "1 inference thread" if shared else "3 inference threads"
    assert startup == [
        f"etude: agents: {mode} inference for first, second, ordinary ({accelerator}); "
        f"{processes}, 1 game each, slots requested by play_many (initial target_matches=2); {threads}",
    ]
    artifacts = tmp_path / "grouped/artifacts/agents"
    reports = _game_reports(artifacts)
    if shared:
        for name in ("first", "second"):
            [stream] = [report for report in reports if report.tournaments == (name,)]
            assert (stream.matches, stream.bus.calls) == (2, 2)
        [ordinary] = [report for report in reports if report.tournaments == ("ordinary",)]
        assert ordinary.matches == 1
        assert all(report.bus.wire_bytes > 0 for report in reports if report.matches)
        [batch_file] = artifacts.glob("batches.json")
        assert not list(artifacts.glob("batches-*.json"))
    else:
        [report] = reports
        assert (report.tournaments, report.matches) == (("first", "second", "ordinary"), 5)
        assert report.bus.wire_bytes == 0
        assert report.bus.arrays > 0 and report.bus.arrays_copied == 0
        [batch_file] = artifacts.glob("batches-*.json")
        assert not (artifacts / "batches.json").exists()
    batches = [Batch.model_validate(batch) for batch in json.loads(batch_file.read_bytes())]
    assert sum(batch.rows for batch in batches) == 10
    loads = sorted((batch.checkpoint, batch.loads) for batch in batches if batch.loads)
    assert loads == sorted((player.checkpoint.key, 1) for player in players)
    budgets = json.loads((artifacts / "threads.json").read_bytes())
    assert set(budgets.values()) == {"1" if shared else "3"}
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (4,)
        assert db.sql("select count(*) from scores").fetchone() == (2,)
    finally:
        db.close()


_mixed_forward_started = threading.Event()
_mixed_forward_release = threading.Event()


def load_mixed_stream_probe(ref: CheckpointRef) -> ModelForward:
    def forward(inputs: list[object]) -> list[object]:
        if isinstance(inputs[0], dict):
            _mixed_forward_started.set()
            if not _mixed_forward_release.wait(timeout=5):
                raise TimeoutError("fixture game did not finish stepping")
            return [cast(dict[str, list[int]], value)["step"][0] for value in inputs]
        return [len(inputs)] * len(inputs)

    return forward


async def overlapping_stream(
    matches: MatchStream,
) -> AsyncIterator[tuple[Match, Tables]]:
    [(match, agents)] = await matches.take(1)
    forward = cast(Forward, agents[0])
    inputs = {"step": [1]}
    pending = forward([inputs])
    try:
        assert _mixed_forward_started.wait(timeout=5), "stream deferred its forward"
        inputs["step"][0] = 2
        following = forward([inputs])
        inputs["step"][0] = 3
    finally:
        _mixed_forward_release.set()
    [old] = await pending
    [new] = await following
    yield match, {"snapshots": [{"seat": 0, "first": cast(int, old), "second": cast(int, new)}]}


async def mixed_barrier_play(match: Match, agents: Sequence[object]) -> Tables:
    [rows] = await cast(Forward, agents[0])([0])
    return {"scores": [{"seat": 0, "score": cast(int, rows)}]}


def test_direct_mixed_group_switches_submission_mode_between_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    agents = Agents(load=load_mixed_stream_probe, max_batch=4)
    players = (checkpoint("left"), checkpoint("right"))
    vector = EvalJob(
        tournament=tournament("vector"),
        enter=players,
        play_many=overlapping_stream,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    ordinary = EvalJob(
        tournament=RoundRobin(
            name="ordinary", environment=environment("grouped"), games=2, base_seed=2
        ),
        enter=players,
        play=mixed_barrier_play,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    assert [
        result.played for result in hold_group(group(vector, ordinary), directory=tmp_path)
    ] == [
        1,
        2,
    ]
    [report] = (tmp_path / "grouped/artifacts/agents").glob("game-*.json")
    assert GameReport.model_validate_json(report.read_bytes()).tournaments == ("vector", "ordinary")
    with statsdb("grouped", tmp_path) as db:
        assert db.sql("select first, second from snapshots").fetchall() == [(1, 2)]
        assert db.sql("select score from scores").fetchall() == [(2,), (2,)]


async def array_vector_slots(
    matches: MatchStream[VectorRules],
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
    matches: MatchStream[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result]]:
    taken = await matches.take(2)
    assert taken
    raise ValueError("first tournament failed")
    yield taken[0][0], Result(tables={"vector_scores": []})


async def code_only_vector_slots(
    matches: MatchStream[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Result | ValueError]]:
    while True:
        taken = await matches.take(2)
        if not taken:
            return
        for match, _ in taken:
            if match.config.error == "exit":
                os._exit(3)
            if match.config.error == "fatal":
                raise RetryRequested("fixture stream interrupted")
            if match.config.error == "stop":
                os.kill(os.getppid(), signal.SIGTERM)
                signal.pause()
                return
            if match.config.error == "ordinary-error":
                yield match, ValueError("fixture slot error")
            else:
                yield (
                    match,
                    Result(tables={"vector_scores": [{"seat": 0, "score": match.config.turns}]}),
                )


@pytest.mark.parametrize(
    ("interruption", "share_streams"),
    [("exit", False), ("stop", False), ("teardown", False), ("exit", True), ("fatal", True)],
)
def test_interrupted_tournament_keeps_completed_tournament_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str, share_streams: bool
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    agents = Agents(load=load) if share_streams else None
    ordinary = job("ordinary", agents)

    def streaming_job(name: str, error: str, games: int) -> EvalJob:
        return EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(error=error),
                games=games,
            ),
            enter=ordinary.enter,
            play_many=code_only_vector_slots,
            agents=ordinary.agents,
            repo=None,
            max_hours=0.1,
        )

    stop = interruption == "stop"
    stream = streaming_job("stream", "" if interruption == "teardown" else interruption, 3)
    members = [ordinary, stream]
    if stop:
        # An earlier match failure must not mask the group's stop.
        members.insert(1, streaming_job("earlier-error", "ordinary-error", 1))
    if interruption == "teardown":
        # Every stream match is stored before the final report fails to write.
        put = DirectoryStorage.put

        def fail_stream_report(storage: DirectoryStorage, key: str, data: bytes) -> None:
            if key.startswith("game-") and GameReport.model_validate_json(data).tournaments == (
                "stream",
            ):
                raise OSError("fixture could not write the final report")
            put(storage, key, data)

        monkeypatch.setattr(DirectoryStorage, "put", fail_stream_report)
    selected = group(*members)
    address = "grouped/mixed"
    monkeypatch.setenv("ETUDE_RUN_ADDRESS", address)
    error, detail = {
        "exit": (RetryRequested, "game process exited"),
        "fatal": (RetryRequested, "fixture stream interrupted"),
        "stop": (Terminated, "received SIGTERM"),
        "teardown": (RetryRequested, "game report .* could not be retained"),
    }[interruption]
    with pytest.raises(error, match=detail):
        hold_group(selected, directory=tmp_path)

    root = tmp_path / address
    ordinary_run, *_, stream_run = selected.evaluations
    interrupted_state = "stopped" if stop else "failed"
    for folder, state in (
        (root, interrupted_state),
        (root / stream_run.address, interrupted_state),
        (root / ordinary_run.address, "finished"),
    ):
        assert RunOutcome.model_validate_json((folder / "outcome.json").read_bytes()).state == state
    completed = played(
        ordinary.tournament, experiment="grouped", enter=ordinary.enter, directory=tmp_path
    )
    assert len(completed.delivered) == 1 and not completed.failed
    interrupted = played(
        stream.tournament, experiment="grouped", enter=stream.enter, directory=tmp_path
    )
    if interruption == "teardown":
        assert len(interrupted.delivered) == 3 and not interrupted.failed
    else:
        assert len(interrupted.new) == len(interrupted.failed) == len(interrupted.retry) == 3


async def hold_stream_match(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables]]:
    [(match, _)] = await matches.take(1)
    signal.pause()
    yield match, {"scores": [{"seat": 0, "score": 1}]}


def test_report_failure_fails_its_owner_while_a_sibling_is_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 2)
    monkeypatch.setenv("ETUDE_RUN_ADDRESS", "grouped/mixed")
    wait_for_games(monkeypatch, processes=2)
    put = DirectoryStorage.put

    def fail_report(storage: DirectoryStorage, key: str, data: bytes) -> None:
        if key.startswith("game-") and GameReport.model_validate_json(data).tournaments == (
            "ordinary",
        ):
            raise OSError("fixture report failure")
        put(storage, key, data)

    monkeypatch.setattr(DirectoryStorage, "put", fail_report)
    ordinary = job("ordinary", None)
    sibling = EvalJob(
        tournament=RoundRobin(
            name="sibling", environment=environment("grouped"), games=3, base_seed=2
        ),
        enter=ordinary.enter,
        play_many=hold_stream_match,
        repo=None,
        max_hours=0.1,
    )
    selected = group(ordinary, sibling)
    with pytest.raises(RetryRequested, match="game report .* could not be retained"):
        hold_group(selected, directory=tmp_path)
    for spec in selected.evaluations:
        outcome = tmp_path / "grouped/mixed" / spec.address / "outcome.json"
        assert RunOutcome.model_validate_json(outcome.read_bytes()).state == "failed"
    delivered = played(
        ordinary.tournament, experiment="grouped", enter=ordinary.enter, directory=tmp_path
    )
    assert len(delivered.delivered) == 1 and not delivered.failed
    interrupted = played(
        sibling.tournament, experiment="grouped", enter=sibling.enter, directory=tmp_path
    )
    assert len(interrupted.failed) == len(interrupted.retry) == 3


async def fail_first_stream_match(
    matches: MatchStream[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Exception]]:
    for match, _ in await matches.take(1):
        yield match, ValueError("fixture stream error")
        raise ValueError("fixture stream error")


@pytest.mark.parametrize(
    ("first_streaming", "interruption"),
    [(False, "stop"), (False, "exit"), (True, "stop"), (True, "exit"), (False, "teardown")],
    ids=["play-stop", "play-crash", "stream-stop", "stream-crash", "teardown"],
)
def test_interruption_leaves_abandoned_matches_unplayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    first_streaming: bool,
    interruption: str,
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    monkeypatch.setenv("ETUDE_RUN_ADDRESS", "grouped/interrupted")
    players = (checkpoint("left"), checkpoint("right"))
    agents = Agents(load=load_vector) if first_streaming else Agents()
    broken = EvalJob(
        tournament=RoundRobin(
            name="broken",
            environment=environment("grouped"),
            config=VectorRules() if first_streaming else {"outcome": "schema"},
            games=3,
        ),
        enter=players,
        play=None if first_streaming else checked_result,
        play_many=fail_first_stream_match if first_streaming else None,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    later = EvalJob(
        tournament=RoundRobin(
            name="later",
            environment=environment("grouped"),
            config=VectorRules(error="" if interruption == "teardown" else interruption),
            games=3,
            base_seed=1,
        ),
        enter=players,
        play_many=code_only_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    if interruption == "teardown":
        put = DirectoryStorage.put

        def fail_later_report(storage: DirectoryStorage, key: str, data: bytes) -> None:
            if key.startswith("game-") and GameReport.model_validate_json(data).tournaments == (
                "later",
            ):
                raise OSError("fixture could not write the final report")
            put(storage, key, data)

        monkeypatch.setattr(DirectoryStorage, "put", fail_later_report)
    selected = group(broken, later)
    error, detail = {
        "stop": (Terminated, "received SIGTERM"),
        "exit": (RetryRequested, "game process exited"),
        "teardown": (RetryRequested, "game report .* could not be retained"),
    }[interruption]
    with pytest.raises(error, match=detail):
        hold_group(selected, directory=tmp_path)
    if first_streaming:
        assert "direct inference for broken, later (cuda); 1 game process" in (
            capfd.readouterr().err
        )

    root = tmp_path / "grouped/interrupted"
    for spec, state in zip(
        selected.evaluations,
        ("failed", "stopped" if interruption == "stop" else "failed"),
        strict=True,
    ):
        outcome = root / spec.address / "outcome.json"
        assert RunOutcome.model_validate_json(outcome.read_bytes()).state == state
    first = played(broken.tournament, experiment="grouped", enter=players, directory=tmp_path)
    assert [match.index for match in first.failed] == [0]
    assert {match.index for match in first.new} == {1, 2}
    assert not first.retry
    second = played(later.tournament, experiment="grouped", enter=players, directory=tmp_path)
    if interruption == "teardown":
        assert len(second.delivered) == 3
    else:
        assert len(second.failed) == len(second.retry) == 3


def test_a_stop_on_the_final_take_leaves_the_tournament_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stop_after_last_match(run: EvalRun) -> bool:
        return len(run.done) == len(run.matches)

    monkeypatch.setattr(Run, "should_stop", property(stop_after_last_match))
    selected = group(job("finished", None))
    (result,) = hold_group(selected, directory=tmp_path)
    assert result.played == 1
    outcome = tmp_path / selected.evaluations[0].address / "outcome.json"
    assert RunOutcome.model_validate_json(outcome.read_bytes()).state == "finished"


@pytest.mark.parametrize("crash", [False, True])
def test_mixed_eval_group_attributes_checkpoint_failures_to_both_play_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 2)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=exits_load if crash else selective_load)
    broken, left, right = (checkpoint(name) for name in ("broken", "left", "right"))
    ordinary = job("ordinary", agents).model_copy(update={"enter": (broken, left)})
    stream = EvalJob(
        tournament=RoundRobin(
            name="stream", environment=environment("grouped"), config=VectorRules(), games=1
        ),
        enter=(broken, left, right),
        play_many=code_only_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    with pytest.raises(RetryRequested if crash else EtudeError):
        hold_group(group(ordinary, stream), directory=tmp_path)

    found = played(stream.tournament, experiment="grouped", enter=stream.enter, directory=tmp_path)
    if crash:
        assert len(found.failed) == len(found.retry) == len(found.new) == 3
        assert not found.delivered
    else:
        assert not found.retry
        assert len(found.failed) == 2
        assert all(
            any(player.name == "broken" for player in match.players) for match in found.failed
        )
        [healthy] = found.delivered
        assert {player.name for player in healthy.match.players} == {"left", "right"}


async def large_slot_request(
    matches: MatchStream[VectorRules],
) -> AsyncIterator[tuple[Match[VectorRules], Tables | Exception]]:
    while granted := await matches.take(64):
        for match, agents in granted:
            try:
                await cast(Forward, agents[0])([1])
            except ForwardError as error:
                yield match, error
            else:
                yield match, {"grants": [{"seat": 0, "size": len(granted)}]}


@pytest.mark.parametrize(
    ("accelerator", "cpus", "mode", "processes"),
    [
        ("no accelerator detected", 1, "direct", 1),
        ("no accelerator detected", 3, "direct", 2),
        ("cuda", 3, "shared", 2),
        ("metal", 3, "shared", 2),
    ],
)
def test_play_many_caps_large_grants_across_game_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    accelerator: Inference,
    cpus: int,
    mode: str,
    processes: int,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: accelerator)
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: cpus)
    slots = EvalJob(
        tournament=RoundRobin(
            name="slots", environment=environment("grouped"), config=VectorRules(), games=6
        ),
        enter=(checkpoint("left"), checkpoint("right")),
        play_many=large_slot_request,
        agents=Agents(load=load_vector, max_batch=4),
        repo=None,
        max_hours=0.1,
    )
    (result,) = hold_group(group(slots), directory=tmp_path)
    assert result.played == 6
    stderr = capfd.readouterr().err
    process = "process" if processes == 1 else "processes"
    assert (
        f"{mode} inference for slots ({accelerator}); "
        f"{processes} game {process}, slots requested by play_many" in stderr
    )
    artifacts = tmp_path / "grouped/artifacts/agents"
    reports = _game_reports(artifacts)
    assert len(reports) == processes and sum(report.matches for report in reports) == 6
    assert all(
        (report.bus.wire_bytes > 0) == (mode == "shared") for report in reports if report.matches
    )
    with statsdb("grouped", tmp_path) as db:
        assert db.sql("select count(*), max(size) from grants").fetchone() == (
            6,
            min(4, math.ceil(6 / processes)),
        )


@pytest.mark.parametrize("broken", [False, True])
def test_fast_direct_stream_takes_pending_matches_while_its_peer_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: "no accelerator detected")
    first: Connection | None = None

    def wait_for_first(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal first
        if first is None:
            first = next(handle for handle in handles if isinstance(handle, Connection))
        if not first.closed:
            assert first.poll(10), "the first stream stopped sending results"
            return (first,)
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", wait_for_first)
    healthy, broken_player = checkpoint("left"), checkpoint("broken")
    matches = [
        Match(players=(broken_player if broken and index < 5 else healthy,), index=index)
        for index in range(8)
    ]
    with (
        pytest.raises(EtudeError, match="fixture broken player") if broken else nullcontext(),
        evaluating(
            environment("vector"),
            experiment="vector",
            run="uneven",
            matches=matches,
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(large_slot_request, agents=Agents(load=selective_load, max_batch=4))
    reports = _game_reports(tmp_path / "vector/uneven/artifacts/agents")
    assert sorted(report.matches for report in reports) == [0, 7 if broken else 8]
    with statsdb("vector", tmp_path) as db:
        assert db.sql("select count(*) from grants").fetchone() == (3 if broken else 8,)
        assert set(
            db.sql("select match, retry from matches where state = 'failed'").fetchall()
        ) == ({(match.key, False) for match in matches[:5]} if broken else set())


def test_fast_direct_process_can_play_all_matches_in_its_second_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 2)
    artifacts = tmp_path / "grouped/artifacts/agents"
    first: Connection | None = None
    finished_first: GameReport | None = None

    def serve_first_process(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal first, finished_first
        if first is None:
            first, second = [handle for handle in handles if isinstance(handle, Connection)]
            assert first.poll(10) and second.poll(10), "the game processes did not request matches"
            return (first, second)
        if not first.closed:
            assert first.poll(10), "the first game process stopped responding"
            return (first,)
        if finished_first is None:
            [finished_first] = _game_reports(artifacts)
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", serve_first_process)
    agents = Agents(load=load, devices=("cpu",), max_batch=2)
    selected = group(job("first", agents, plain, games=2), job("later", agents, plain, games=3))
    assert [result.played for result in hold_group(selected, directory=tmp_path)] == [2, 3]
    assert finished_first is not None and finished_first.matches == 4


def test_stop_during_first_direct_stream_leaves_later_matches_unstarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    monkeypatch.setenv("ETUDE_STOP_FILE", str(tmp_path / "stop"))
    agents = Agents(load=load, devices=("cpu",))
    first, later = job("first", agents, request_stop), job("later", agents, plain)

    with pytest.raises(Terminated, match="evaluation stopped"):
        hold_group(group(first, later), directory=tmp_path)

    reports = _game_reports(tmp_path / "grouped/artifacts/agents")
    [report] = reports
    assert report.tournaments == ("first", "later") and report.matches == 1
    retained = played(later.tournament, experiment="grouped", enter=later.enter, directory=tmp_path)
    assert len(retained.new) == len(retained.retry) == 1
    assert not retained.delivered


def test_released_shared_slot_starts_waiting_tournament_before_expanding_active_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 3)
    artifacts = tmp_path / "grouped/artifacts/agents"
    active_process: tuple[Connection | int, ...] = ()
    first_reports: set[tuple[str, ...]] = set()

    def delay_active_process(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal active_process, first_reports
        if not active_process:
            active_process = tuple(handles[:2])
        if not first_reports:
            reports = _game_reports(artifacts)
            if len(reports) == 2:
                first_reports = {report.tournaments for report in reports}
            else:
                ready = wait(
                    [handle for handle in handles if handle not in active_process], timeout=10
                )
                assert ready, "the second game slot stopped responding"
                return ready
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", delay_active_process)
    agents = Agents(load=load, max_batch=2)
    jobs = (
        job("active", agents, plain, games=2),
        job("short", agents, plain),
        job("waiting", agents, plain),
    )
    assert [result.played for result in hold_group(group(*jobs), directory=tmp_path)] == [2, 1, 1]
    assert first_reports == {("short",), ("waiting",)}


def test_play_many_sends_no_take_after_the_last_match_is_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receive = Connection.recv
    requests: list[Take] = []

    def count_takes(pipe: Connection) -> object:
        message: object = receive(pipe)
        if isinstance(message, Take):
            requests.append(message)
        return message

    monkeypatch.setattr(Connection, "recv", count_takes)
    with evaluating(
        environment("vector"),
        experiment="vector",
        matches=(Match(players=(Code(name="one"),), config=VectorRules()),),
        directory=tmp_path,
    ) as active:
        assert active.play_many(code_only_vector_slots).played == 1
    assert len(requests) == 1


@pytest.mark.parametrize("direct", [True, False], ids=["direct", "shared"])
def test_booting_streams_cover_pending_demand_as_siblings_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direct: bool
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 4 if direct else 5)
    recv = Connection.recv
    first: Connection | None = None
    draining: set[Connection | int] = set()
    serving_stream = not direct
    initialized = False

    def receive(pipe: Connection) -> object:
        nonlocal serving_stream
        message: object = recv(pipe)
        if pipe is first and isinstance(message, StreamEnd):
            serving_stream = True
        return message

    def finish_siblings(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal first, initialized
        if not initialized:
            assert len(handles) == (8 if direct else 9)
            if direct:
                pipe = handles[0]
                assert isinstance(pipe, Connection)
                first = pipe
                draining.update(handles[2:])
            else:
                # Ordinary and streaming starts alternate while both queues need slots.
                draining.update((*handles[:2], *handles[4:6]))
            initialized = True
        if first is not None and not serving_stream:
            assert first.poll(10), "the first direct process did not finish its async stream"
            return (first,)
        if remaining := draining.intersection(handles):
            ready = wait(remaining, timeout=10)
            assert ready, "an async sibling did not exit"
            draining.difference_update(handle for handle in ready if isinstance(handle, int))
            return ready
        return wait(handles, timeout=timeout)

    monkeypatch.setattr(Connection, "recv", receive)
    monkeypatch.setattr("etude.eval.run.wait", finish_siblings)
    agents = Agents(load=load, devices=("cpu",) if direct else ("cuda",), max_batch=4)
    ordinary = job("ordinary", agents, plain, games=4 if direct else 2)
    stream_count = 4 if direct else 2
    stream = EvalJob(
        tournament=RoundRobin(
            name="stream", environment=environment("grouped"), config={}, games=stream_count
        ),
        enter=ordinary.enter,
        play_many=records_target_matches,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    assert [
        result.played for result in hold_group(group(ordinary, stream), directory=tmp_path)
    ] == [
        4 if direct else 2,
        stream_count,
    ]
    reports = _game_reports(tmp_path / "grouped/artifacts/agents")
    assert len(reports) == 4
    assert sum("stream" in report.tournaments for report in reports) == (1 if direct else 2)


async def records_initial_grant(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables]]:
    initial = True
    while taken := await matches.take(64):
        for match, _ in taken:
            yield (
                match,
                {
                    "grants": [
                        {
                            "seat": 0,
                            "target": matches.target_matches,
                            "size": len(taken),
                            "first": initial,
                        }
                    ]
                },
            )
        initial = False


def test_expanded_stream_targets_its_first_grant_while_peers_hold_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial: tuple[Connection | int, ...] = ()
    first_granted = short_exited = expanded_granted = False

    def prioritize_expansion(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal initial, first_granted, short_exited, expanded_granted
        if not initial:
            assert len(handles) == 7
            initial = tuple(handles)
        if not first_granted:
            ready = wait(initial[:1], timeout=10)
            assert ready, "the first stream did not request matches"
            first_granted = True
            return ready
        if not short_exited:
            ready = wait([handle for handle in initial[2:4] if handle in handles], timeout=10)
            assert ready, "the short tournament did not release its slot"
            short_exited = initial[3] in ready
            return ready
        if not expanded_granted:
            (expanded,) = [
                handle
                for handle in handles
                if isinstance(handle, Connection) and handle not in initial
            ]
            ready = wait([expanded], timeout=10)
            assert ready, "the expanded stream did not request matches"
            expanded_granted = True
            return ready
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", prioritize_expansion)
    agents = Agents(load=load, max_batch=16)
    short = job("short", agents, plain)
    stream = EvalJob(
        tournament=RoundRobin(
            name="stream", environment=environment("grouped"), config={}, games=12
        ),
        enter=short.enter,
        play_many=records_initial_grant,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    assert [result.played for result in hold_group(group(stream, short), directory=tmp_path)] == [
        12,
        1,
    ]
    with statsdb("grouped", tmp_path) as db:
        assert db.sql(
            "select target, size, count(*) from grants where first and target < 6 group by all"
        ).fetchall() == [(3, 3, 3)]


def test_drained_async_queue_releases_process_while_peer_still_holds_games(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 5)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 4 * 1024**3)
    first: tuple[Connection, int] | None = None
    exited = False
    replaced = False

    def wait_for_first(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal first, exited, replaced
        if first is None:
            pipe, sentinel = handles[0], handles[1]
            assert isinstance(pipe, Connection) and isinstance(sentinel, int)
            first = (pipe, sentinel)
            second = handles[4]
            assert isinstance(second, Connection)
            assert pipe.poll(10) and second.poll(10), "the async processes did not request matches"
            return (pipe, second)
        pipe, sentinel = first
        if not pipe.closed:
            assert pipe.poll(10), "the first async process stopped sending results"
            return (pipe,)
        if not exited:
            ready = wait([sentinel], timeout=10)
            assert ready, "the first async process did not exit"
            exited = True
            return ready
        if not replaced:
            # The other async process still holds its startup matches. A new
            # stream must be scheduled before we let any other child report.
            assert sum(isinstance(handle, Connection) for handle in handles) == 4
            replaced = True
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", wait_for_first)
    agents = Agents(load=load, max_batch=16)
    ordinary = job("ordinary", agents, plain).model_copy(
        update={
            "tournament": RoundRobin(
                name="ordinary", environment=environment("grouped"), config={}, games=8
            )
        }
    )
    stream = EvalJob(
        tournament=RoundRobin(
            name="stream", environment=environment("grouped"), config=VectorRules(), games=8
        ),
        enter=ordinary.enter,
        play_many=code_only_vector_slots,
        agents=agents,
        repo=None,
        max_hours=0.1,
    )
    assert [
        result.played for result in hold_group(group(ordinary, stream), directory=tmp_path)
    ] == [
        8,
        8,
    ]
    assert replaced


def test_streaming_tournament_uses_processes_released_by_its_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 16)
    monkeypatch.setattr(eval_resources, "memory_bytes", lambda: 16 * 1024**3)
    small_pipes: set[Connection] = set()
    expanded = False

    def wait_for_small_queues(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal expanded
        if expanded:
            return wait(handles, timeout=timeout)
        pipes = [handle for handle in handles if isinstance(handle, Connection)]
        if not small_pipes:
            assert len(pipes) == 15
            # Initial slots rotate through three small queues and the large one.
            small_pipes.update(pipe for index, pipe in enumerate(pipes) if index not in (3, 7, 11))
        if len(pipes) == 15 and all(pipe.closed for pipe in small_pipes):
            expanded = True
            return wait(handles, timeout=timeout)
        # Leave large-queue requests waiting for grants until its siblings exit.
        ready = wait(
            [handle for handle in handles if isinstance(handle, int) or handle in small_pipes],
            timeout=10 if timeout is None else min(timeout, 10),
        )
        assert ready, "released slots did not go to the remaining tournament"
        return ready

    monkeypatch.setattr("etude.eval.run.wait", wait_for_small_queues)
    agents = Agents(load=load, max_batch=32)
    players = (checkpoint("left"), checkpoint("right"))
    jobs = [
        EvalJob(
            tournament=RoundRobin(
                name=name,
                environment=environment("grouped"),
                config=VectorRules(),
                games=games,
                base_seed=seed,
            ),
            enter=players,
            play_many=code_only_vector_slots,
            agents=agents,
            repo=None,
            max_hours=0.1,
        )
        for seed, (name, games) in enumerate(zip(("one", "two", "three", "large"), (4, 4, 4, 200)))
    ]
    assert [result.played for result in hold_group(group(*jobs), directory=tmp_path)] == [
        4,
        4,
        4,
        200,
    ]
    assert expanded
    reports = _game_reports(tmp_path / "grouped/artifacts/agents")
    assert sum(report.tournaments == ("large",) and report.matches > 0 for report in reports) == 15


def test_code_only_play_many_group_runs_without_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def fail_probe() -> bool:
        raise AssertionError("code-only play_many group must not probe GPU hardware")

    monkeypatch.setattr(eval_resources, "_detect_accelerator", fail_probe)
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
    assert "etude: 4 game processes, slots requested by play_many" in stderr
    assert "agents:" not in stderr
    artifacts = tmp_path / "grouped/artifacts/agents"
    assert not list(artifacts.glob("batches*.json"))
    reports = _game_reports(artifacts)
    assert len(reports) == 4
    assert {report.tournaments for report in reports} == {("first",), ("second",)}
    assert sum(report.matches for report in reports) == 4
    assert all(report.bus.calls == 0 for report in reports)


@pytest.mark.parametrize("shared_agents", [False], indirect=True)
def test_direct_mixed_group_isolates_stream_failure_between_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
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
        hold_group(group(broken, healthy, job("ordinary", agents)), directory=tmp_path)
    artifacts = tmp_path / "grouped/artifacts/agents"
    reports = _game_reports(artifacts)
    [report] = reports
    assert report.tournaments == ("broken", "healthy", "ordinary")
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from vector_scores").fetchone() == (2,)
        assert db.sql("select count(*) from scores").fetchone() == (2,)
        assert db.sql("select count(*) from matches where state = 'failed'").fetchone() == (2,)
        assert sorted(row[0] for row in db.sql("select state from runs").fetchall()) == [
            "failed",
            "finished",
            "finished",
        ]
    finally:
        db.close()


def test_direct_play_many_group_isolates_stream_import_error_before_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
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
        healthy_name = selected.evaluations[1].address.split("/")[-1]
        assert db.sql("select run, state from matches").fetchall() == [(healthy_name, "done")] * 2
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
        "from etude.eval.game import MatchStream\n"
        "from etude.eval.records import Match\n"
        "from etude.eval.result import Tables\n"
        "async def play_many(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables]]:\n"
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
assert direct_report.imports.warning() is None

# 2. Shared-agent play importing mlx in the game module warns on mlx.
etude.eval.resources._detect_accelerator = lambda: "cuda"
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
shared_warning = shared_report.imports.warning()
assert shared_warning is not None
assert "imported mlx" in shared_warning
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

etude.eval.resources._detect_accelerator = lambda: "cuda" if sys.argv[2] == "True" else "no accelerator detected"

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
assert wrap_report.imports.warning() is not None

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
assert lazy_report.imports.warning() is not None
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


@pytest.mark.parametrize(("gpu", "streaming"), [(False, False), (True, False), (True, True)])
def test_only_socket_play_warns_about_module_count(
    tmp_path: Path, gpu: bool, streaming: bool
) -> None:
    package = tmp_path / "many_mods_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    imports = []
    for index in range(270):
        (package / f"m{index}.py").write_text(f"VALUE = {index}\n")
        imports.append(f"import many_mods_pkg.m{index}")
    (tmp_path / "many_mods_loader.py").write_text(
        "from etude.checkpoints import CheckpointRef\n"
        "from etude.eval.agents import ModelForward\n"
        "def load(checkpoint: CheckpointRef) -> ModelForward:\n"
        "    return lambda inputs: [1 for _ in inputs]\n"
    )
    (tmp_path / "many_mods_game.py").write_text(
        "\n".join(imports) + "\nfrom collections.abc import AsyncIterator, Sequence\n"
        "from etude.eval.agents import Forward\n"
        "from etude.eval.game import MatchStream\n"
        "from etude.eval.records import Config, Match\n"
        "from etude.eval.result import Tables\n"
        "async def play(match: Match, agents: Sequence[Forward]) -> Tables:\n"
        "    [score] = await agents[0]([0])\n"
        "    return {'scores': [{'seat': 0, 'score': int(str(score))}]}\n"
        "async def play_many(matches: MatchStream[Config]) -> AsyncIterator[tuple[Match, Tables]]:\n"
        "    while granted := await matches.take(1):\n"
        "        for match, agents in granted:\n"
        "            yield match, await play(match, agents)\n"
    )
    program = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import etude.eval.resources
from etude.eval.game import MAX_GAME_IMPORTS, GameReport
from etude.eval.match import environment
from etude.eval.records import RoundRobin
from etude.jobs import Agents, EvalJob, ExperimentState, evaluation_group, hold_group
from tests.eval.arithmetic import checkpoint
from many_mods_game import play, play_many
from many_mods_loader import load

gpu = sys.argv[2] == "True"
streaming = sys.argv[3] == "True"
etude.eval.resources._detect_accelerator = lambda: "cuda" if gpu else "no accelerator detected"
etude.eval.resources.available_cpus = lambda: 3 if gpu else 1
etude.eval.resources.memory_bytes = lambda: 2 * 1024**3
root = Path(sys.argv[1]) / "run"
job = EvalJob(
    tournament=RoundRobin(name="many", environment=environment("many"), config={}, games=2),
    enter=(checkpoint("one"), checkpoint("two")),
    play=play if not streaming else None,
    play_many=play_many if streaming else None,
    agents=Agents(load=load, max_batch=1 if not gpu else 2),
    repo=None,
    max_hours=0.1,
)
hold_group(evaluation_group(job.lower(ExperimentState(experiment="many"))), directory=root)
paths = list((root / "many/artifacts/agents").glob("game-*.json"))
assert len(paths) == (2 if gpu else 1)
for path in paths:
    report = GameReport.model_validate_json(path.read_bytes())
    assert report.imports.heavy == ()
    assert report.imports.count > MAX_GAME_IMPORTS
    assert report.imports.warning(count_limit=False) is None
    assert report.imports.warning(count_limit=True) is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(gpu), str(streaming)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected = 1 if gpu and not streaming else 0
    assert result.stderr.count("game module many_mods_game: loaded ") == expected


async def record_game_process(match: Match, agents: list[object]) -> Tables:
    return {"scores": [{"seat": 0, "score": os.getpid()}]}


def test_direct_async_tournaments_get_their_own_match_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    taking_small: set[Connection] = set()
    recv = Connection.recv

    def receive(pipe: Connection) -> object:
        message: object = recv(pipe)
        if isinstance(message, Take) and message.stream == 1:
            taking_small.add(pipe)
        return message

    def wait_for_small_takes(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        # Let each process take from the small queue before any can refill it.
        if 0 < len(taking_small) < 4:
            handles = [handle for handle in handles if handle not in taking_small]
            timeout = 10 if timeout is None else min(timeout, 10)
        return wait(handles, timeout=timeout)

    monkeypatch.setattr(Connection, "recv", receive)
    monkeypatch.setattr("etude.eval.run.wait", wait_for_small_takes)
    agents = Agents(load=load, devices=("cpu",), max_batch=32)
    selected = group(
        job("large", agents, plain, games=64), job("small", agents, record_game_process, games=4)
    )
    large, small = hold_group(selected, directory=tmp_path)
    assert (large.played, small.played) == (64, 4)
    assert "4 game processes, 1–8 games each" in capfd.readouterr().err
    with statsdb("grouped", tmp_path) as db:
        assert db.sql(
            "select count(distinct score) from scores where run = ?", params=[small.name]
        ).fetchone() == (4,)


def test_async_startup_grants_keep_every_shared_game_process_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 5)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 4 * 1024**3)
    wait_for_games(monkeypatch, processes=4)
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
    assert "4 game processes, up to 2 games each; 1 inference thread" in small_err
    reports = _game_reports(small_root / "grouped/artifacts/agents")
    assert sorted(report.matches for report in reports) == [1, 1, 1, 2]


async def invalid_stream(matches: MatchStream[Config]) -> AsyncIterator[tuple[Match, Tables]]:
    taken = await matches.take(2)
    match, agents = taken[0]
    outcome = match.config["outcome"]
    if outcome == "abandoned":
        return
    if outcome == "unknown":
        unknown = match.model_copy(update={"index": 1000})
        yield unknown, await plain(match, list(agents))
    if outcome == "escaped":
        raise ValueError("fixture escaped the generator")
    if outcome in ("early", "teardown"):
        for match, agents in taken:
            yield match, await plain(match, list(agents))
        if outcome == "teardown":
            raise RuntimeError("fixture teardown")
        return
    result = await plain(match, list(agents))
    yield match, result
    yield match.model_copy(), result


@pytest.mark.parametrize(
    ("outcome", "detail", "failed"),
    [
        ("duplicate", "unknown, ambiguous or already-yielded match", 1),
        ("unknown", "unknown, ambiguous or already-yielded match", 2),
        ("abandoned", "without yielding its held matches", 2),
        ("escaped", "fixture escaped the generator", 2),
        ("early", "returned before taking all pending matches", 0),
        ("teardown", "fixture teardown", 0),
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
        pytest.raises(EtudeError, match=detail) as caught,
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=matches,
            run="broken",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(invalid_stream)
    assert caught.value.code == ("match_failed" if failed else "eval_stream_failed")
    [report] = _game_reports(tmp_path / "vector/broken/artifacts/agents")
    assert report.matches == 2 - failed
    db = statsdb("vector", tmp_path)
    try:
        records = db.sql("select match, state, error from matches").fetchall()
        assert {key for key, _, _ in records} == {match.key for match in matches[:2]}
        errors = [error for _, state, error in records if state == "failed"]
        assert len(errors) == failed
        assert all(detail in error for error in errors)
    finally:
        db.close()


def test_play_many_refuses_a_copy_with_an_ambiguous_authored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    player = checkpoint("one")
    panel = [Match(players=(player,), config=config) for config in ({}, {"turns": 1})]
    assert panel[0].key != panel[1].key  # Both acquire the same defaults in VectorRules.
    with (
        pytest.raises(EtudeError, match="ambiguous"),
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=panel,
            run="ambiguous",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(vector_slots, agents=Agents(load=load_vector))
    db = statsdb("vector", tmp_path)
    try:
        assert set(db.sql("select match, state from matches").fetchall()) == {
            (match.key, "failed") for match in panel
        }
    finally:
        db.close()


async def checked_result(match: Match[Config], agents: Sequence[object]) -> Tables:
    outcome = match.config["outcome"]
    if outcome == "author":
        raise ValueError("fixture author error")
    if outcome == "retry":
        raise RetryRequested("fixture driver retry")
    if outcome == "schema":
        return {}
    if outcome == "return":
        return cast(Tables, ["invalid return"])
    if outcome == "return-exception":
        return cast(Tables, ValueError("returned exception"))
    return await plain(match, list(agents))


async def checked_pair(
    matches: MatchStream[Config],
) -> AsyncIterator[tuple[Match, Tables | Exception]]:
    while taken := await matches.take(2):
        for match, agents in taken:
            yield match, await checked_result(match, agents)


@pytest.mark.parametrize(
    ("streaming", "invalid", "kind"),
    [
        (False, "return", "list"),
        (True, "return", "list"),
        (False, "return-exception", "ValueError"),
    ],
)
def test_invalid_outcome_fails_one_match_stops_grants_and_retains_held_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool, invalid: str, kind: str
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    panel = [
        Match(players=(checkpoint("one"),), config={"outcome": outcome}, index=i)
        for i, outcome in enumerate((invalid, "in-flight", "pending"))
    ]
    with (
        pytest.raises(EtudeError, match=f"game outcomes must be tables or a Result, got {kind}"),
        evaluating(
            environment("invalid_return"),
            experiment="invalid_return",
            matches=panel,
            run="policy",
            directory=tmp_path,
        ) as active,
    ):
        agents = Agents(load=echo, devices=("cpu",), max_batch=2)
        if streaming:
            active.play_many(checked_pair, agents=agents)
        else:
            active.play(checked_result, agents=agents)
    db = statsdb("invalid_return", tmp_path)
    try:
        assert set(db.sql("select match, state, retry from matches").fetchall()) == {
            (panel[0].key, "failed", False),
            (panel[1].key, "done", False),
        }
        assert set(db.sql("select match from scores").fetchall()) == {
            (panel[1].key,),
        }
    finally:
        db.close()
    reports = _game_reports(tmp_path / "invalid_return/policy/artifacts/agents")
    assert [report.matches for report in reports] == [2]


async def checked_stream(
    matches: MatchStream[Config],
) -> AsyncIterator[tuple[Match, Tables | Exception]]:
    while taken := await matches.take(1):
        match, agents = taken[0]
        if match.config["outcome"] == "author":
            yield match, ValueError("fixture author error")
        else:
            yield match, await checked_result(match, agents)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("terminal", ["schema", "retry"])
def test_failure_policy_keeps_author_errors_local_and_stops_on_terminal_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool, terminal: str
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    panel = [
        Match(players=(Code(name="one"),), config={"outcome": outcome}, index=i)
        for i, outcome in enumerate(("author", "done", terminal, "pending"))
    ]
    with (
        pytest.raises(
            RetryRequested if terminal == "retry" else EtudeError,
            match="fixture driver retry" if terminal == "retry" else "match_has_no_rows",
        ),
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=panel,
            run="policy",
            directory=tmp_path,
        ) as active,
    ):
        if streaming:
            active.play_many(checked_stream)
        else:
            active.play(checked_result)
    db = statsdb("vector", tmp_path)
    try:
        got = {
            key: (state, retry)
            for key, state, retry in db.sql("select match, state, retry from matches").fetchall()
        }
        expected = {
            panel[0].key: ("failed", False),
            panel[1].key: ("done", False),
            panel[2].key: ("failed", terminal == "retry"),
        }
        if terminal == "retry":
            expected[panel[3].key] = ("failed", True)
        assert got == expected
    finally:
        db.close()


async def catches_stream_load(
    matches: MatchStream[Config],
) -> AsyncIterator[tuple[Match, Tables | Exception]]:
    while taken := await matches.take(1):
        match, agents = taken[0]
        mode = match.config["outcome"]
        if mode == "pruned":
            raise AssertionError("a failed checkpoint was granted again")
        forward = cast(Forward, agents[0])
        try:
            await forward([1])
        except ForwardError:
            if mode == "wrapped":
                yield match, ValueError("author wrapped a load error")
                continue
        yield match, await plain(match, list(agents))


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
@pytest.mark.parametrize("mode", ["fallback", "wrapped"])
def test_streaming_load_failure_blocks_fallback_rows_and_future_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setattr(eval_resources, "available_cpus", lambda: 1)
    broken = checkpoint("broken")
    panel = [
        Match(players=(broken,), config={"outcome": mode}, index=0),
        Match(players=(broken,), config={"outcome": "pruned"}, index=1),
        Match(players=(checkpoint("healthy"),), config={"outcome": "healthy"}, index=2),
    ]
    with (
        pytest.raises(EtudeError, match="fixture broken player"),
        evaluating(
            environment("vector"),
            experiment="vector",
            matches=panel,
            run="load",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(catches_stream_load, agents=Agents(load=selective_load))
    db = statsdb("vector", tmp_path)
    try:
        failures = db.sql("select match, error from matches where state = 'failed'").fetchall()
        assert {key for key, _ in failures} == {match.key for match in panel[:2]}
        assert all("fixture broken player" in error for _, error in failures)
        assert db.sql("select match from scores").fetchall() == [(panel[2].key,)]
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
        with pytest.raises(TypeError, match=r"MatchStream\[YourConfig\]"):
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
