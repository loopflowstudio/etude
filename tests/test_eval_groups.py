"""Grouped evals share agents and preserve tournament delivery and admission."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, wait
from contextlib import nullcontext
from decimal import Decimal
from importlib import import_module
from multiprocessing import active_children, get_context
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import NoReturn

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
from etude.eval.agents import AgentConnection, Answer, Forward, Request
from etude.eval.batching import _Batches
from etude.eval.game import Execution, Performance, _performance
from etude.eval.loader import AgentLoader
from etude.eval.match import environment
from etude.eval.query import _kept_path, statsdb
from etude.eval.records import Code, Config, Match, RoundRobin, checkpoint_player
from etude.eval.resources import (
    Accelerator,
    Device,
    Inference,
    _detect_accelerator,
    available_cpus,
    memory_bytes,
    plan_execution,
    resources,
)
from etude.eval.result import Tables
from etude.eval.run import Terminated
from etude.eval.tournament import evaluating, played
from etude.experiment import observe, state_of
from etude.jobs import (
    Agents,
    EvalGroup,
    EvalJob,
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
from tests.eval.thread_budget import load_checkpoint
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


def plain(match: Match, agents: list[object]) -> Tables:
    return {"scores": [{"seat": 0, "score": 7}]}


def tournament(name: str) -> RoundRobin:
    return RoundRobin(
        name=name, environment=environment("grouped"), configs=({"lane": name},), games=1
    )


def job(name: str, agents: Agents | None, playing: Callable[..., Tables] | None = None) -> EvalJob:
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
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    b.scheduler.add_pool(Pool(name="waiting"))
    b.scheduler.set_limit("grouped", Decimal(budget))
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
                    active.store(match, plain(match, []), seconds=0.01)
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
            )
        assert (measured.listings, measured.requests) == (1, 10 if history else 2)
        assert all(measured.got.count(key) == 1 for key in measured.got)
        observed = counts.snapshot()
        assert observed.provider == observed.bucket == observed.ssh == observed.git == 0
        if budget == "1":
            assert all(
                d.state == "not queued" and d.reason == "limit $1.00 ($0.00 committed)"
                for d in decisions
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
        again = advance(definition, source=source, rent="l4", mode="queue")
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
                active.store(match, plain(match, []), seconds=0.01)
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
    def expired(
        fs: Iterable[Future[Execution]],
        timeout: float | None = None,
        return_when: str = "ALL_COMPLETED",
    ) -> tuple[set[Future[Execution]], set[Future[Execution]]]:
        return set(), set(fs)

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
    # Each game asks one row per seat in turn; two concurrent games fill every
    # two-row batch, so batches flush on size, never on the delay.
    agents = Agents(load=load, max_batch=2, max_delay_seconds=60)
    selected = group(job("ladder", agents), job("gauntlet", agents))
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [1, 1]
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_text())
    assert [{row["tournament"] for row in batch["inputs"]} for batch in batches] == [
        {"ladder", "gauntlet"}
    ] * 2
    protocols = [
        json.loads(path.read_text())
        for path in (tmp_path / "grouped/artifacts/agents").glob("protocol-*.json")
    ]
    assert len(protocols) == 2
    assert sum(p["requests"] for p in protocols) == 4
    assert sum(p["answers"] for p in protocols) == 4
    assert sum(sum(p["latency_buckets"]) for p in protocols) == 4
    assert sum(batch["forward_calls"] for batch in batches) == 2
    assert sum(batch["loads"] for batch in batches) == 2
    log = capfd.readouterr().err
    assert log.count("agents: shared inference") == 1
    assert log.count("agents: loaded") == 2
    assert "2 checkpoints loaded, 4 inputs" in log
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: shared inference" not in capfd.readouterr().err
    assert len(list((tmp_path / "grouped/artifacts/agents").glob("protocol-*.json"))) == 2
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select score from scores").fetchall() == [(2,)] * 4
    finally:
        db.close()


def raises_load(checkpoint: CheckpointRef) -> Forward:
    raise ValueError("fixture loading error")


def exits_load(checkpoint: CheckpointRef) -> Forward:
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


def test_play_arity_and_removed_workers_are_refused() -> None:
    def one_parameter(match: Match) -> Tables:
        return {}

    for agents in (None, Agents(load=load)):
        with pytest.raises(LauncherError, match="match and agents"):
            job("bad", agents, one_parameter)
    with pytest.raises(ValidationError, match="workers"):
        EvalJob.model_validate({**job("bad", None).model_dump(), "workers": 2})


@pytest.mark.parametrize(
    ("cpus", "pending", "agents", "memory_gb", "games"),
    [
        (32, 64, True, None, 32),
        (16, 64, True, None, 16),
        (3, 20, True, None, 3),
        (2, 20, True, None, 2),
        (8, 20, False, None, 8),
        (8, 20, True, None, 4),
        (8, 20, True, 1.5, 2),
        (8, 1, True, None, 1),
        (1, 20, True, None, 1),
        (1, 20, False, None, 1),
        (8, 20, True, 2.0, 1),
    ],
)
def test_plan_execution_respects_cpu_pending_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int,
    pending: int,
    agents: bool,
    memory_gb: float | None,
    games: int,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: "no accelerator detected")
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: cpus)
    monkeypatch.setattr(
        "etude.eval.resources.memory_bytes", lambda: 16 * 1024**3 if cpus >= 16 else 2 * 1024**3
    )
    loader = Agents(load=load, memory_gb=memory_gb).plain(None)
    plan = plan_execution(pending, loader, checkpoints=agents)
    reason = "no accelerator detected" if agents else "no checkpoints"
    assert (plan.games, plan.threads, plan.inference) == (games, 1, reason)


@pytest.mark.parametrize(
    ("pending", "batch_inputs", "memory_gb", "agents", "visible", "games"),
    [
        (64, 32, None, True, "0", 32),
        (3, 32, None, True, "0", 3),
        (64, 8, None, True, "0", 8),
        (64, 32, 15.5, True, "0", 2),
        (64, 32, None, False, "0", 4),
        (64, 32, None, True, "", 4),
        (64, 32, None, True, "-1", 4),
    ],
)
def test_gpu_games_fill_a_batch_with_pending_and_memory_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending: int,
    batch_inputs: int,
    memory_gb: float | None,
    agents: bool,
    visible: str,
    games: int,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", _detect_accelerator)
    monkeypatch.setattr("etude.eval.resources.sys.platform", "linux")
    # Controlled hardware output, without importing or executing a model library.
    probe = tmp_path / "nvidia-smi"
    probe.write_text("#!/bin/sh\nprintf 'ran' > \"$0.ran\"\nprintf '0\\n'\n")
    probe.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", visible)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 16 * 1024**3)
    loader = Agents(load=load, max_batch=batch_inputs, memory_gb=memory_gb).plain(None)
    plan = plan_execution(pending, loader, checkpoints=agents)
    expected_shared = agents and visible == "0"
    assert plan.games == games
    assert plan.threads == (max(1, 4 - games) if expected_shared else 1)
    assert plan.shared is expected_shared
    assert (tmp_path / "nvidia-smi.ran").exists() is expected_shared
    if expected_shared:
        assert plan.inference == "cuda"
    elif agents:
        assert plan.inference == "CUDA_VISIBLE_DEVICES hides GPUs"
        assert plan.describe(["ladder"]) == (
            "etude: agents: direct inference for ladder (CUDA_VISIBLE_DEVICES hides GPUs); "
            "4 games at a time; 1 inference thread each"
        )
    else:
        assert plan.inference == "no checkpoints"


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
    plan = plan_execution(64, Agents(load=load).plain(None), checkpoints=True)
    assert (plan.inference, plan.games) == ("nvidia-smi failed", 4)
    assert plan.describe(["ladder"]) == (
        "etude: agents: direct inference for ladder (nvidia-smi failed); "
        "4 games at a time; 1 inference thread each"
    )


@pytest.mark.parametrize(
    ("plat", "machine", "cpus", "memory_gb", "batch_inputs", "games", "threads", "accelerator"),
    [
        ("darwin", "arm64", 8, None, 32, 7, 1, "metal"),
        ("darwin", "arm64", 16, None, 8, 15, 1, "metal"),
        ("darwin", "arm64", 1, None, 32, 1, 1, "metal"),
        ("darwin", "arm64", 8, 15.5, 32, 2, 6, "metal"),
        ("darwin", "x86_64", 8, None, 32, 8, 1, None),
        ("linux", "arm64", 8, None, 32, 8, 1, None),
    ],
)
def test_apple_silicon_plan_execution_bounds_games_by_cores_and_memory(
    monkeypatch: pytest.MonkeyPatch,
    plat: str,
    machine: str,
    cpus: int,
    memory_gb: float | None,
    batch_inputs: int,
    games: int,
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
    plan = plan_execution(64, loader, checkpoints=True)
    shared = accelerator is not None
    assert (plan.games, plan.threads, plan.inference) == (
        games,
        threads,
        accelerator or "no accelerator detected",
    )
    held = int(memory_gb * 1024**3) if memory_gb is not None else 8 * 1024**3
    assert plan.checkpoint_bytes == (held if shared else held // games)


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
    assert plan_execution(1, loader, checkpoints=True).inference == inference


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
        plan = plan_execution(8, loader, checkpoints=checkpoints)
        assert (plan.inference, plan.games, plan.checkpoint_bytes) == (reason, 8, 0)
        assert plan.describe(["t"]) is None
    loader = Agents(load=load, devices=("cpu",)).plain(None)
    cpu = plan_execution(8, loader, checkpoints=True)
    assert (cpu.inference, cpu.games, cpu.threads) == ("cpu-only loader", 8, 1)


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
    ("device", "cpus", "memory_gb", "concurrent"),
    [
        ("cuda", 1, None, 3),
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
    concurrent: int,
    device: Accelerator,
) -> None:
    monkeypatch.setattr(eval_resources, "_detect_accelerator", lambda: device)
    measurements = iter([cpus])
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: next(measurements))
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    outstanding: list[int] = []

    def count_games(
        fs: Iterable[Future[object]],
        timeout: float | None = None,
        return_when: str = "ALL_COMPLETED",
    ) -> tuple[set[Future[object]], set[Future[object]]]:
        signals = list(fs)
        # One signal observes agent exit; the others are dispatched matches.
        outstanding.append(len(signals) - 1)
        return wait(signals, timeout=timeout, return_when=return_when)

    monkeypatch.setattr("etude.eval.run.wait", count_games)
    agents = Agents(load=load_checkpoint, memory_gb=memory_gb)
    selected = group(*(job(name, agents) for name in ("one", "two", "three")))
    assert [result.played for result in hold_group(selected, directory=tmp_path)] == [1, 1, 1]
    assert max(outstanding) == concurrent
    threads = max(1, cpus - concurrent)
    game_unit = "game" if concurrent == 1 else "games"
    thread_unit = "thread" if threads == 1 else "threads"
    assert (
        f"shared inference for one, two, three ({device}); {concurrent} {game_unit} at a time; {threads} inference {thread_unit}"
        in capfd.readouterr().err
    )
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_bytes())
    assert sum(batch["loads"] for batch in batches) == 2
    budgets = json.loads((tmp_path / "grouped/artifacts/agents/threads.json").read_text())
    assert set(budgets.values()) == {str(threads)}
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
    for unused in ({"max_batch": 8}, {"max_delay_seconds": 0.1}, {"memory_gb": 1.0}):
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


@pytest.mark.parametrize(("field", "value"), [("max_batch", 32), ("max_delay_seconds", 0.005)])
def test_cpu_only_loader_rejects_batching_settings(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError, match=f"{field} requires an accelerator in devices"):
        Agents.model_validate({"load": load, "devices": ["cpu", "cpu"], field: value})


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
        "2 games at a time; 1 inference thread each" in capfd.readouterr().err
    )
    assert not (direct_dir / "grouped/artifacts/agents/batches.json").exists()


def test_released_queued_evaluation_discards_workers() -> None:
    spec = evaluation_group(job("old", None)).evaluations[0]
    text = {
        "tournament": spec.definition.model_dump(mode="json"),
        "experiment": spec.experiment,
        "play": spec.play.model_dump(mode="json"),
        "entrants": [player.model_dump(mode="json") for player in spec.tournament.entrants],
        "workers": 28,
    }
    restored = EvalGroup.model_validate(text).evaluations[0]
    assert restored.definition == spec.definition
    assert restored.matches == spec.matches


class Echo:
    def __call__(self, inputs: list[object]) -> list[object]:
        return inputs


def echo(checkpoint: CheckpointRef) -> Forward:
    return Echo()


def test_batch_size_and_delay_flush_with_a_controlled_clock() -> None:
    parent, child = get_context("spawn").Pipe()
    player = checkpoint("one")
    batches = _Batches(echo, 2, 0.5, 1024)
    try:
        batches.add(child, Request(player.checkpoint, [1], "first", "a", player.key), 10)
        batches.flush(10.49)
        assert not parent.poll()
        batches.add(child, Request(player.checkpoint, [2], "second", "b", player.key), 10.49)
        full = batches.flush(10.49)
        assert parent.recv() == Answer([1])
        assert parent.recv() == Answer([2])
        assert batches.loads == 1
        batches.add(child, Request(player.checkpoint, [3], "third", "c", player.key), 11)
        delayed = batches.flush(11.5)
        assert parent.recv() == Answer([3])
        assert [len(batch.inputs) for batch in [*full, *delayed]] == [2, 1]
    finally:
        child.close()
        parent.close()


def test_oversized_game_request_uses_bounded_forwards_and_one_ordered_reply() -> None:
    parent, child = get_context("spawn").Pipe()
    player = checkpoint("one")
    batches = _Batches(echo, 2, 0.5, 1024)
    inputs: list[object] = [1, 2, 3, 4, 5]
    try:
        batches.add(child, Request(player.checkpoint, inputs, "first", "a", player.key), 10)
        observed = batches.flush(10)
        assert parent.poll()
        assert parent.recv() == Answer(inputs)
        assert not parent.poll()
        assert [(len(batch.inputs), batch.forward_calls) for batch in observed] == [
            (2, 1),
            (2, 1),
            (1, 1),
        ]
        assert batches.loads == 1
    finally:
        child.close()
        parent.close()


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
    batches = _Batches(echo, 1, 0.5, 2)
    parent, child = get_context("spawn").Pipe()
    try:
        for player in (a, b):
            batches.add(child, Request(player.checkpoint, [player.name], "one", "m", player.key), 0)
            batches.flush(0)
            assert parent.recv() == Answer([player.name])
        # A future row protects a even though it is the oldest loaded player.
        batches.max_batch = 2
        batches.add(child, Request(a.checkpoint, ["a"], "one", "m", a.key), 2)
        batches.add(child, Request(c.checkpoint, ["c"], "two", "n", c.key), 1)
        batches.flush(1.5)
        assert parent.recv() == Answer(["c"])
        batches.flush(2.5)
        assert parent.recv() == Answer(["a"])
        assert batches.loads == 3
        batches.add(child, Request(b.checkpoint, ["b"], "one", "m", b.key), 3)
        batches.flush(3.5)
        assert parent.recv() == Answer(["b"])
        assert batches.loads == 4  # b was evicted to make room for c.
    finally:
        parent.close()
        child.close()


class WrongAnswers:
    def __call__(self, inputs: list[object]) -> list[object]:
        return []


def wrong_answers(checkpoint: CheckpointRef) -> Forward:
    return WrongAnswers()


class UnpicklableAnswers:
    def __call__(self, inputs: list[object]) -> list[object]:
        return [lambda: None for _ in inputs]


def unpicklable_answers(checkpoint: CheckpointRef) -> Forward:
    return UnpicklableAnswers()


@pytest.mark.parametrize("loader", [wrong_answers, unpicklable_answers])
def test_bad_batch_answers_fail_all_asking_tournaments(
    tmp_path: Path, loader: Callable[[CheckpointRef], Forward]
) -> None:
    agents = Agents(load=loader, max_delay_seconds=0.25)
    selected = group(job("one", agents), job("two", agents))
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(selected, directory=tmp_path)
    protocols = [
        json.loads(path.read_text())
        for path in (tmp_path / "grouped/artifacts/agents").glob("protocol-*.json")
    ]
    assert len(protocols) == 2
    assert [p["requests"] for p in protocols] == [1, 1]
    assert [p["answers"] for p in protocols] == [1, 1]
    assert all(sum(p["latency_buckets"]) == 1 for p in protocols)
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
        assert any("/artifacts/agents/protocol-" in key for key in source.keys(""))
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
from etude.eval.records import Match, checkpoint_player
from etude.eval.result import Tables

def play(match: Match, agents: Sequence[Forward]) -> Tables:
    assert "separate_loading" not in sys.modules
    assert "etude.eval.batching" not in sys.modules
    assert agents[0]([]) == []
    return {"scores": [{"seat": 0, "score": int(str(agents[0]([1])[0]))}]}
""")
    (tmp_path / "separate_loading.py").write_text(
        "raise RuntimeError('model module imported by game')\n"
    )
    program = """
import json
import socket
import sys
import threading
from multiprocessing.connection import Connection
from tempfile import TemporaryDirectory
from etude.call import Function
from etude.eval.agents import Answer
from etude.eval.game import initialize_game, play_match
from etude.eval.resources import Resources
from etude.eval.records import Match, checkpoint_player
from pathlib import Path
from tests.eval.arithmetic import checkpoint


counts = {"sent": 0, "received": 0}
with TemporaryDirectory(dir="/tmp", prefix="etude-test-") as scratch, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    initialize_game(Resources(scratch=Path(scratch), artifacts=Path(scratch)))
    address = scratch + "/a.sock"
    listener.bind(address)
    listener.listen()
    def answer() -> None:
        peer, _ = listener.accept()
        with Connection(peer.detach()) as connection:
            request = connection.recv()
            counts["received"] += 1
            connection.send(Answer(request.inputs))
            counts["sent"] += 1
    thread = threading.Thread(target=answer)
    thread.start()
    result = play_match(Function(module="light_agent_game", name="play", path=sys.argv[1]),
                        Match(players=(checkpoint("one"),)), address, memory=0)
    thread.join()
assert counts == {"sent": 1, "received": 1}
assert result.performance.requests == result.performance.answers == 1
assert sum(result.performance.latency_buckets) == 1
assert result.performance.round_trip_p50_seconds == result.performance.round_trip_p95_seconds
print(result.imports.model_dump_json())
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


def stop_parent(match: Match, agents: Sequence[Forward]) -> Tables:
    agents[0]([1])
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
    batches = _Batches(raises_load, 1, 0, 10)
    parent, child = get_context("spawn").Pipe()
    try:
        player = checkpoint("one")
        batches.add(child, Request(player.checkpoint, [1], "one", "a", player.key), 0)
        batches.flush(0)
        first = parent.recv()
        assert first.error and "fixture loading error" in first.error
        # If loading is attempted a second time this function would now succeed.
        batches.forwards.load = echo
        batches.add(child, Request(player.checkpoint, [2], "two", "b", player.key), 1)
        batches.flush(1)
        assert parent.recv() == first
        assert batches.loads == 0
    finally:
        parent.close()
        child.close()


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


def fails_game(match: Match, agents: Sequence[Forward]) -> Tables:
    raise ValueError("fixture game error")


def mixed_outcomes(match: Match[Config], agents: list[object]) -> Tables:
    if match.config["outcome"] == "failed":
        raise ValueError("fixture game error")
    if match.config["outcome"] == "retry":
        raise RetryRequested("fixture transient error")
    return plain(match, list(agents))


@pytest.mark.parametrize("retry", [False, True])
def test_a_game_failure_retains_in_flight_outcomes_and_resume_skips_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    # Keep acceptance order deterministic: failure, success, then another failure.
    monkeypatch.setattr("etude.eval.tournament.ordered", lambda matches: iter(matches))

    def completed(
        fs: Iterable[Future[object]],
        timeout: float | None = None,
        return_when: str = "ALL_COMPLETED",
    ) -> tuple[set[Future[object]], set[Future[object]]]:
        return wait(fs, timeout=timeout, return_when="ALL_COMPLETED")

    monkeypatch.setattr("etude.eval.run.wait", completed)
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


@pytest.mark.parametrize("library", ["torch", "mlx"])
@pytest.mark.parametrize("with_agents", [False, True])
def test_heavy_import_warning_is_once_and_only_for_games_with_agents(
    tmp_path: Path, library: str, with_agents: bool
) -> None:
    (tmp_path / f"{library}.py").write_text("# Controlled framework fixture.\n")
    (tmp_path / "heavy_fixture_game.py").write_text(
        f"import {library}\nfrom collections.abc import Sequence\nfrom etude.eval.records import Match\n"
        "from etude.eval.agents import Forward\nfrom etude.eval.result import Tables\n"
        "def play(match: Match" + ", agents: Sequence[Forward]" + ") -> Tables:\n"
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
etude.eval.resources._detect_accelerator = lambda: "cuda"
agents = Agents(load=load) if sys.argv[2] == "True" else Agents()
jobs = [EvalJob(tournament=RoundRobin(name=name, environment=environment("heavy"), configs=({},), games=1),
               enter=(checkpoint("one"), checkpoint("two")), play=play,
               agents=agents or Agents(), repo=None, max_hours=0.1) for name in ("first", "second")]
hold_group(EvalGroup(evaluations=tuple(evaluation_group(job.lower(ExperimentState(experiment="heavy"))).evaluations[0] for job in jobs)),
           directory=sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(with_agents)],
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


@pytest.mark.parametrize("grouped", [False, True], ids=["standalone", "group"])
def test_agent_startup_failure_reports_retryable_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    grouped: bool,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(outputs))
    monkeypatch.setenv("ETUDE_FIXTURE_TOKEN", "synthetic-fixture-credential")
    module = f"bad_agent_import_fixture_{'group' if grouped else 'standalone'}"
    (tmp_path / f"{module}.py").write_text(
        "import os\n"
        "from multiprocessing import parent_process\n"
        "from etude.checkpoints import CheckpointRef\n"
        "from etude.eval.agents import Forward\n"
        "if parent_process() is not None:\n"
        "    raise RuntimeError('startup: ' + os.environ['ETUDE_FIXTURE_TOKEN'])\n"
        "def load(checkpoint: CheckpointRef) -> Forward:\n"
        "    raise AssertionError('the agent should fail before loading')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    agents = Agents(load=import_module(module).load)
    selected = job("startup", agents)
    with pytest.raises(RetryRequested, match="startup"):
        if grouped:
            hold_group(group(selected), directory=tmp_path)
        else:
            with evaluating(
                selected.tournament, experiment="grouped", enter=selected.enter, directory=tmp_path
            ) as active:
                active.play(selected.play, agents=agents)
    log = capfd.readouterr().err
    assert "[redacted]" in log and "synthetic-fixture-credential" not in log
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 1
    found = played(
        selected.tournament, experiment="grouped", enter=selected.enter, directory=tmp_path
    )
    assert len(found.new) == len(found.retry) == len(found.failed) == 1


def catches_agent_error(match: Match, agents: Sequence[Forward]) -> Tables:
    try:
        agents[0]([1])
    except Exception:
        pass
    return {"scores": [{"seat": 0, "score": 99}]}


class RaisingForward:
    def __call__(self, inputs: list[object]) -> list[object]:
        raise ValueError("fixture acting error")


def raises_forward(checkpoint: CheckpointRef) -> Forward:
    return RaisingForward()


def retry_load(checkpoint: CheckpointRef) -> Forward:
    raise RetryRequested("fixture transient loading error")


@pytest.mark.parametrize("loader", [raises_load, raises_forward, wrong_answers, retry_load])
@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_caught_agent_errors_fail_matches_without_publishing_returned_rows(
    tmp_path: Path, loader: Callable[[CheckpointRef], Forward]
) -> None:
    agents = Agents(load=loader, max_delay_seconds=0.25)
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


def selective_load(checkpoint: CheckpointRef) -> Forward:
    if str(checkpoint.key) == "broken":
        raise ValueError("fixture broken player")
    return load(checkpoint)


def selective_retry_load(checkpoint: CheckpointRef) -> Forward:
    if str(checkpoint.key) == "broken":
        raise RetryRequested("fixture player unavailable")
    return load(checkpoint)


def asks_or_returns(match: Match[dict[str, str]], agents: Sequence[Forward]) -> Tables:
    # The second tournament never asks for an agent, even when it seats the broken player.
    if match.config["lane"] == "two":
        return plain(match, list(agents))
    try:
        return play(match, agents)
    except Exception:
        return plain(match, list(agents))


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


def returns_before_loading(match: Match, agents: Sequence[Forward]) -> Tables:
    if any(player.name == "left" for player in match.players):
        return plain(match, list(agents))
    return play(match, agents)


@pytest.mark.parametrize("shared_agents", [False, True], indirect=True)
def test_loader_failure_preserves_previously_delivered_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=selective_load, max_batch=1)
    jobs = [
        job(name, agents).model_copy(
            update={
                "enter": (checkpoint("broken"), checkpoint("left"), checkpoint("right")),
                "play": returns_before_loading,
            }
        )
        for name in ("one", "two")
    ]
    selected = group(*jobs)
    scheduled = []
    for spec, active in zip(selected.evaluations, jobs, strict=True):
        matches = played(
            active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
        ).new
        # Deliver the broken player's match with left before attempting its load with right.
        first = next(m for m in matches if {p.name for p in m.players} == {"broken", "left"})
        scheduled.append(
            spec.model_copy(update={"matches": (first, *(m for m in matches if m != first))})
        )
    selected = selected.model_copy(update={"evaluations": tuple(scheduled)})
    with pytest.raises(EtudeError):
        hold_group(selected, directory=tmp_path)
    for active in jobs:
        found = played(
            active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path
        )
        assert len(found.failed) == 1
        assert len(found.delivered) == 2
        assert not found.new
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)] * 2
        assert db.sql("select count(distinct match) from scores").fetchone() == (4,)
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


def wire_agent(checkpoint: CheckpointRef) -> Forward:
    return WireAgent()


def test_protocol_counts_one_serialization_each_way_and_batches_shared_checkpoint() -> None:
    # Small real sockets retain the wire bytes; no load or clock threshold.
    checkpoint_player = checkpoint("wire")
    batches = _Batches(wire_agent, 2, 60, 1024)
    sockets = [socket.socketpair() for _ in range(2)]
    clients = [AgentConnection(parent.detach()) for parent, _ in sockets]
    receivers = [Connection(child.detach()) for _, child in sockets]
    answer_size = len(ForkingPickler.dumps(Answer([WireValue(0)])))
    WireValue.serializations = 0
    request_bytes = 0
    observations = []
    try:
        for turn in range(2):
            for index, (client, receiver) in enumerate(zip(clients, receivers, strict=True)):
                client.send(
                    Request(
                        checkpoint_player.checkpoint,
                        [WireValue(turn)],
                        "wire",
                        str(index),
                        checkpoint_player.key,
                    )
                )
                payload = receiver.recv_bytes()
                request_bytes += len(payload)
                batches.add(receiver, ForkingPickler.loads(payload), 0)
            observations.extend(batches.flush(0))
            for client in clients:
                assert client.poll(1)
                assert client.recv() == Answer([str(turn)])
        assert WireValue.serializations == 8  # Four decisions, one encoding each way.
        assert sum(client.requests for client in clients) == 4
        assert sum(client.answers for client in clients) == 4
        assert sum(client.request_bytes for client in clients) == request_bytes
        assert sum(client.answer_bytes for client in clients) == 4 * answer_size
        assert [len(batch.inputs) for batch in observations] == [2, 2]
        assert [batch.forward_calls for batch in observations] == [1, 1]
        assert [batch.loads for batch in observations] == [1, 0]
        assert batches.loads == 1
    finally:
        for client in clients:
            client.close()
        for receiver in receivers:
            receiver.close()


def test_protocol_percentiles_bound_injected_latencies_and_exclude_inference_wait() -> None:
    parent, child = socket.socketpair()
    with AgentConnection(parent.detach()) as connection, child:
        for seconds in (0.001, 0.0015, 0.003):
            connection.observe(seconds)
        report = _performance(connection, 9.0, 10.0, "fixture", "match")
    assert report.round_trip_p50_seconds == 0.002048
    assert report.round_trip_p95_seconds == 0.004096
    assert sum(report.latency_buckets) == 3
    assert report.round_trip_seconds == pytest.approx(0.0055)
    assert report.game_seconds == pytest.approx(0.9945)
    assert report.started_monotonic == 9.0
    assert report.finished_monotonic == 10.0
    assert report.transport_seconds == 0.0


def test_agentless_game_intervals_have_no_inference_wait(tmp_path: Path) -> None:
    selected = group(job("plain", None))
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [1]
    paths = list((tmp_path / "grouped/artifacts/agents").glob("protocol-*.json"))
    assert len(paths) == 1
    report = Performance.model_validate_json(paths[0].read_bytes())
    assert report.started_monotonic is not None
    assert report.finished_monotonic is not None
    assert report.game_seconds == report.finished_monotonic - report.started_monotonic
    assert report.requests == report.answers == 0
    assert report.round_trip_seconds == report.transport_seconds == 0.0
    assert sum(report.latency_buckets) == 0
    assert report.round_trip_p50_seconds is report.round_trip_p95_seconds is None
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0]
    assert len(list((tmp_path / "grouped/artifacts/agents").glob("protocol-*.json"))) == 1


def test_standalone_play_retains_intervals_without_a_group(tmp_path: Path) -> None:
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
    paths = list((tmp_path / "grouped/standalone/artifacts/agents").glob("protocol-*.json"))
    assert len(paths) == 1
    report = Performance.model_validate_json(paths[0].read_bytes())
    assert report.match == match.key
    assert report.started_monotonic is not None
    assert report.finished_monotonic is not None
    assert report.game_seconds == report.finished_monotonic - report.started_monotonic
    assert bucket.uploads == 7
    assert len([key for key in bucket.keys("") if "/protocol-" in key]) == 1


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
