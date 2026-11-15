"""Grouped evals share agents and preserve tournament delivery and admission."""

import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, wait
from decimal import Decimal
from multiprocessing import active_children, get_context
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path

import pytest
from pydantic import ValidationError

from etude.call import Function
from etude.checkpoints import CheckpointRef
from etude.controller import _eval_cpus, advance
from etude.declarations import Experiment, read_experiment
from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval.agents import Agent, AgentConnection, Answer, Request
from etude.eval.batching import _Batches
from etude.eval.game import Execution, _performance
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Code, Config, Held, Match, RoundRobin
from etude.eval.resources import active_games, available_cpus, memory_bytes
from etude.eval.result import Tables
from etude.eval.run import Terminated
from etude.eval.tournament import evaluating, played
from etude.experiment import state_of
from etude.jobs import (
    Agents,
    EvalGroup,
    EvalJob,
    ExperimentState,
    evaluation_group,
    hold_group,
    with_experiment,
)
from etude.launch import client
from etude.launch.commands import DEFAULT_MIN_EVAL_HOURS
from etude.launch.records import Pool, Standing, Status
from etude.records import ArtifactRef, RunRecord, RunSubmission
from etude.smoke import Step, smoke_file
from etude.spec import Job, RunRequest
from etude.storage import DirectoryStorage, default_root
from etude.train.tracking import track
from examples.agent_game import checkpoint, play
from examples.agent_models import load
from tests.launch.counting import Counting, Counts, job_text
from tests.launch.fake import bench
from tests.test_jobs import checkout


def plain(match: Match) -> Tables:
    return {"scores": [{"seat": 0, "score": 7}]}


def tournament(name: str) -> RoundRobin:
    return RoundRobin(
        name=name, environment=environment("grouped"), configs=({"lane": name},), games=1
    )


def job(name: str, agents: Agents | None) -> EvalJob:
    return EvalJob(
        tournament=tournament(name),
        enter=(checkpoint("left"), checkpoint("right")),
        play=plain if agents is None else play,
        agents=agents,
        repo=None,
        max_hours=0.1,
        max_usd=Decimal(1),
    )


def group(*jobs: EvalJob) -> EvalGroup:
    return EvalGroup(
        evaluations=tuple(
            spec
            for job in jobs
            for spec in evaluation_group(with_experiment(job, "grouped")).evaluations
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


def test_estimated_machine_hours_wait_without_remote_calls_then_threshold_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")
    training = track(experiment="grouped", run="train@0", directory=source.root)

    def declare(state: ExperimentState) -> list[EvalJob]:
        return [job("one", None), job("two", None)]

    # Controlled historical durations: no sleep, resource pressure or artificial work.
    for name in ("one", "two"):
        historical = tournament(name)
        with evaluating(
            historical,
            experiment="grouped",
            enter=(Code(name="earlier"), checkpoint("right")),
            directory=source.root,
        ) as active:
            for match in active.matches:
                active.store(match, plain(match), seconds=1800)
    definition = Experiment(name="grouped", eval=declare)
    counts = Counting(b.store, b.service)
    try:
        with counts.record():
            decisions = advance(definition, source=source, pool="waiting", mode="queue")
        assert all(d.state == "waiting" and "2 eval matches" in (d.reason or "") for d in decisions)
        assert b.scheduler.runs() == ()
        observed = counts.snapshot()
        assert observed.provider == observed.bucket == observed.ssh == observed.git == 0
        assert DEFAULT_MIN_EVAL_HOURS == 2
        decisions = advance(
            definition, source=source, pool="waiting", mode="queue", min_eval_hours=0
        )
        assert len(b.scheduler.runs()) == 1
        assert decisions[0].launched == decisions[1].launched
    finally:
        training.finish("finished")


def test_room_reads_existing_capacity_after_queued_work_without_remote_calls(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    candidate = RunRequest(
        id="evals", job=Job(command="true", repo=None, max_hours=0.1, max_usd=Decimal(1))
    )
    counts = Counting(b.store, b.service)
    with counts.record():
        assert b.scheduler.room([candidate], rent="l4") == ()
    assert counts.snapshot().provider == 0
    assert b.service.creates == 0


def test_room_reserves_free_owned_slots_for_already_queued_work(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="owned", slots=1))
    b.scheduler.add_machine("cpu", "fixture-host", pool="owned")
    request = RunRequest(id="next", job=Job(command="true", repo=None, max_hours=0.1))
    counts = Counting(b.store, b.service)
    with counts.record():
        assert b.scheduler.room([request], "owned") == ("next",)
    observed = counts.snapshot()
    assert observed.provider == observed.bucket == observed.ssh == observed.git == 0
    b.scheduler.queue(request.model_copy(update={"id": "first"}), "owned")
    assert b.scheduler.room([request], "owned") == ()


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
        spec.tournament, experiment="grouped", enter=spec.entrants, storages=[source]
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

    with track(experiment="grouped", run="training", directory=source.root):
        decisions = advance(
            Experiment(name="grouped", eval=declare), source=source, pool="waiting", mode="queue"
        )
        assert decisions[0].launched is not None
        assert len(b.scheduler.runs()) == 1


def test_room_does_not_assign_another_targets_queued_job_to_the_wrong_pool(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="first"))
    b.scheduler.add_pool(Pool(name="other"))
    b.scheduler.add_machine("cpu", "fixture-host", pool="first")
    command = Job(command="true", repo=None, max_hours=0.1, max_usd=Decimal(1))
    b.scheduler.queue(RunRequest(id="before", job=command), "other", rent="l4")
    counts = Counting(b.store, b.service)
    with counts.record():
        assert b.scheduler.room([RunRequest(id="next", job=command)], "first", rent="l4") == (
            "next",
        )
    assert counts.snapshot().provider == 0


def test_room_cost_does_not_grow_with_other_targets_backlogs(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="room"))
    b.scheduler.add_pool(Pool(name="elsewhere"))
    b.scheduler.add_machine("cpu", "fixture-host", pool="room")
    command = Job(command="true", repo=None, max_hours=0.1, max_usd=Decimal(1))
    b.scheduler.queue([RunRequest(id=f"other-{i}", job=command) for i in range(1000)], "elsewhere")
    counts = Counting(b.store, b.service)
    with counts.record():
        assert b.scheduler.room([RunRequest(id="next", job=command)], "room") == ("next",)
    assert counts.snapshot() == Counts(database=6)


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
    old = statsdb("grouped", tmp_path)
    old.sql("drop index matches_identity")
    old.sql("alter table matches drop column retry")
    old.sql("update kept.statsdb set schema = 14")
    old.close()
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
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    # Each game asks one row per seat in turn; two concurrent games fill every
    # two-row batch, so batches flush on size, never on the delay.
    agents = Agents(load=load, max_batch=2, max_delay_seconds=60)
    selected = group(job("ladder", agents), job("gauntlet", agents))
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [1, 1]
    batches = json.loads((tmp_path / "grouped/artifacts/agents/batches.json").read_text())
    assert [{row["tournament"] for row in batch["rows"]} for batch in batches] == [
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
    assert log.count("agents: started") == 1
    assert log.count("agents: loaded") == 2
    assert "2 checkpoints loaded, 4 rows" in log
    assert [r.played for r in hold_group(selected, directory=tmp_path)] == [0, 0]
    assert "agents: started" not in capfd.readouterr().err
    assert len(list((tmp_path / "grouped/artifacts/agents").glob("protocol-*.json"))) == 2
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select score from scores").fetchall() == [(2,)] * 4
    finally:
        db.close()


def raises_load(checkpoint: CheckpointRef) -> Agent:
    raise ValueError("fixture loading error")


def exits_load(checkpoint: CheckpointRef) -> Agent:
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
    agents = Agents(load=load)
    with pytest.raises(LauncherError, match="and agents"):
        job("bad", agents).model_copy(update={"play": plain}).plain()
    with pytest.raises(LauncherError, match="without agents"):
        job("bad", None).model_copy(update={"play": play}).plain()
    with pytest.raises(ValidationError, match="workers"):
        EvalJob.model_validate({**job("bad", None).model_dump(), "workers": 2})


def test_active_games_respects_cpu_and_declared_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    assert active_games(20, agents=False) == 8
    assert active_games(20, agents=True) == 4
    assert active_games(20, agents=True, memory_gb=1.5) == 2
    assert active_games(1, agents=True) == 1
    assert active_games(0, agents=True) == 0


@pytest.mark.parametrize(("memory_gb", "concurrent"), [(None, 3), (1.75, 1)])
def test_group_execution_respects_declared_agent_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    memory_gb: float | None,
    concurrent: int,
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 4)
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
    agents = Agents(load=load, memory_gb=memory_gb)
    selected = group(*(job(name, agents) for name in ("one", "two", "three")))
    assert [result.played for result in hold_group(selected, directory=tmp_path)] == [1, 1, 1]
    assert max(outstanding) == concurrent
    assert f"{concurrent} games at a time" in capfd.readouterr().err
    for spec in selected.evaluations:
        found = played(
            spec.tournament, experiment="grouped", enter=spec.entrants, directory=tmp_path
        )
        assert len(found.delivered) == 1 and not found.new and not found.failed


def test_released_queued_evaluation_discards_workers() -> None:
    spec = evaluation_group(job("old", None)).evaluations[0]
    text = spec.model_dump()
    text["workers"] = 28
    assert EvalGroup.model_validate(text).evaluations == (spec,)


class Echo:
    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return rows


def echo(checkpoint: CheckpointRef) -> Agent:
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
        assert [len(batch.rows) for batch in [*full, *delayed]] == [2, 1]
    finally:
        child.close()
        parent.close()


def test_lru_charges_checkpoint_bytes_and_keeps_players_with_waiting_rows() -> None:
    players = [
        Held().player(
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
    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return []


def wrong_answers(checkpoint: CheckpointRef) -> Agent:
    return WrongAnswers()


class UnpicklableAnswers:
    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return [lambda: None for _ in rows]


def unpicklable_answers(checkpoint: CheckpointRef) -> Agent:
    return UnpicklableAnswers()


@pytest.mark.parametrize("loader", [wrong_answers, unpicklable_answers])
def test_bad_batch_answers_fail_all_asking_tournaments(
    tmp_path: Path, loader: Callable[[CheckpointRef], Agent]
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
from etude.eval.records import Code, RoundRobin
from etude.eval.match import environment
from etude.spec import Runtime
from examples.agent_models import load
from examples.agent_game import checkpoint
from examples.agent_game import play

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    agents = Agents(load=load)
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
        assert any("/artifacts/agents/batches.json" in key for key in source.keys(""))
        log = job_text(scheduler.logs(refresh=True, run=status.run))
        assert log.count("agents: started") == log.count("agents: stopped") == 1
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
from etude.eval.agents import Agent
from etude.eval.records import Match
from etude.eval.result import Tables

def play(match: Match, agents: Sequence[Agent]) -> Tables:
    assert "separate_loading" not in sys.modules
    assert "etude.eval.batching" not in sys.modules
    assert agents[0].act([]) == []
    return {"scores": [{"seat": 0, "score": int(str(agents[0].act([1])[0]))}]}
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
from etude.eval.game import play_match
from etude.eval.records import Match
from examples.agent_game import checkpoint

counts = {"sent": 0, "received": 0}
with TemporaryDirectory(dir="/tmp", prefix="etude-test-") as scratch, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
    address = scratch + "/a.sock"
    listener.bind(address)
    listener.listen()
    def answer() -> None:
        peer, _ = listener.accept()
        with Connection(peer.detach()) as connection:
            request = connection.recv()
            counts["received"] += 1
            connection.send(Answer(request.rows))
            counts["sent"] += 1
    thread = threading.Thread(target=answer)
    thread.start()
    result = play_match(Function(module="light_agent_game", name="play", path=sys.argv[1]),
                        Match(players=(checkpoint("one"),)), address)
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
    assert log.count("agents: started") == log.count("agents: stopped") == 1


def stop_parent(match: Match, agents: Sequence[Agent]) -> Tables:
    agents[0].act([1])
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
        batches.load = echo
        batches.add(child, Request(player.checkpoint, [2], "two", "b", player.key), 1)
        batches.flush(1)
        assert parent.recv() == first
        assert batches.loads == 0
    finally:
        parent.close()
        child.close()


def test_admission_reads_known_target_cpus_without_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(client, "default", lambda: b.scheduler)
    ondemand = next(r for r in b.scheduler.ondemands() if r.name == "l4")
    b.scheduler.add_ondemand(
        ondemand.model_copy(
            update={"name": "cpu8", "template": ondemand.template.model_copy(update={"vcpus": 8})}
        )
    )
    counts = Counting(b.store, b.service)
    with counts.record():
        assert _eval_cpus(None, "cpu8") == 8
    assert counts.snapshot().provider == counts.snapshot().ssh == 0
    b.scheduler.add_pool(Pool(name="unknown"))
    b.scheduler.add_machine("cpu", "fixture-host", pool="unknown")
    assert _eval_cpus("unknown", "cpu8") is None


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


def fails_game(match: Match, agents: Sequence[Agent]) -> Tables:
    raise ValueError("fixture game error")


def mixed_outcomes(match: Match[Config]) -> Tables:
    if match.config["outcome"] == "failed":
        raise ValueError("fixture game error")
    if match.config["outcome"] == "retry":
        raise RetryRequested("fixture transient error")
    return plain(match)


@pytest.mark.parametrize("retry", [False, True])
def test_a_game_failure_retains_in_flight_outcomes_and_resume_skips_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    # Keep acceptance order deterministic: failure, success, then another failure.
    monkeypatch.setattr("etude.eval.run.ordered", lambda matches: iter(matches))

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


@pytest.mark.parametrize("with_agents", [False, True])
def test_heavy_import_warning_is_once_and_only_for_games_with_agents(
    tmp_path: Path, with_agents: bool
) -> None:
    (tmp_path / "torch.py").write_text("# Controlled framework fixture.\n")
    (tmp_path / "heavy_fixture_game.py").write_text(
        "import torch\nfrom collections.abc import Sequence\nfrom etude.eval.records import Match\n"
        "from etude.eval.agents import Agent\nfrom etude.eval.result import Tables\n"
        "def play(match: Match"
        + (", agents: Sequence[Agent]" if with_agents else "")
        + ") -> Tables:\n"
        "    return {'scores': [{'seat': 0, 'score': 1}]}\n"
    )
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from heavy_fixture_game import play
from etude.eval.match import environment
from etude.eval.records import Code, RoundRobin
from etude.jobs import Agents, EvalGroup, EvalJob, evaluation_group, hold_group, with_experiment
from examples.agent_models import load
from examples.agent_game import checkpoint
agents = Agents(load=load) if sys.argv[2] == "True" else None
jobs = [EvalJob(tournament=RoundRobin(name=name, environment=environment("heavy"), configs=({},), games=1),
               enter=(checkpoint("one"), checkpoint("two")), play=play,
               agents=agents, repo=None, max_hours=0.1) for name in ("first", "second")]
hold_group(EvalGroup(evaluations=tuple(evaluation_group(with_experiment(job, "heavy")).evaluations[0] for job in jobs)),
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
    (tmp_path / "bad_agent_import_fixture.py").write_text(
        "import os\nraise RuntimeError('startup: ' + os.environ['ETUDE_FIXTURE_TOKEN'])\n"
    )
    selected = group(job("startup", Agents(load=load)))
    spec = selected.evaluations[0]
    assert spec.agents is not None
    spec = spec.model_copy(
        update={
            "agents": spec.agents.model_copy(
                update={
                    "load": Function(
                        module="bad_agent_import_fixture", name="load", path=str(tmp_path)
                    )
                }
            )
        }
    )
    with pytest.raises(RetryRequested, match="startup"):
        hold_group(EvalGroup(evaluations=(spec,)), directory=tmp_path)
    log = capfd.readouterr().err
    assert "[redacted]" in log and "synthetic-fixture-credential" not in log
    assert json.loads((outputs / "etude-result.json").read_text())["tally"]["failed"] == 1
    found = played(spec.tournament, experiment="grouped", enter=spec.entrants, directory=tmp_path)
    assert len(found.new) == len(found.retry) == len(found.failed) == 1


def catches_agent_error(match: Match, agents: Sequence[Agent]) -> Tables:
    try:
        agents[0].act([1])
    except Exception:
        pass
    return {"scores": [{"seat": 0, "score": 99}]}


class RaisingAgent:
    def act(self, rows: Sequence[object]) -> Sequence[object]:
        raise ValueError("fixture acting error")


def raises_act(checkpoint: CheckpointRef) -> Agent:
    return RaisingAgent()


def retry_load(checkpoint: CheckpointRef) -> Agent:
    raise RetryRequested("fixture transient loading error")


@pytest.mark.parametrize("loader", [raises_load, raises_act, wrong_answers, retry_load])
def test_caught_agent_errors_fail_matches_without_publishing_returned_rows(
    tmp_path: Path, loader: Callable[[CheckpointRef], Agent]
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


def selective_load(checkpoint: CheckpointRef) -> Agent:
    if str(checkpoint.key) == "broken":
        raise ValueError("fixture broken player")
    return load(checkpoint)


def selective_retry_load(checkpoint: CheckpointRef) -> Agent:
    if str(checkpoint.key) == "broken":
        raise RetryRequested("fixture player unavailable")
    return load(checkpoint)


def asks_or_returns(match: Match[dict[str, str]], agents: Sequence[Agent]) -> Tables:
    # The second tournament never asks for an agent, even when it seats the broken player.
    if match.config["lane"] == "two":
        return plain(match)
    try:
        return play(match, agents)
    except Exception:
        return plain(match)


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("in_flight", [False, True])
def test_loader_failure_reaches_undispatched_matches_across_tournaments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry: bool, in_flight: bool
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3 if in_flight else 2)
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
    with pytest.raises(RetryRequested if retry else EtudeError):
        hold_group(group(*jobs), directory=tmp_path)
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


def returns_before_loading(match: Match, agents: Sequence[Agent]) -> Tables:
    if any(player.name == "left" for player in match.players):
        return plain(match)
    return play(match, agents)


def test_loader_failure_preserves_previously_delivered_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    agents = Agents(load=selective_load)
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
    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return [WireValue(int(str(row))) for row in rows]


def wire_agent(checkpoint: CheckpointRef) -> Agent:
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
        assert [len(batch.rows) for batch in observations] == [2, 2]
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
        report = _performance(connection, 1.0, "fixture", "match")
    assert report.round_trip_p50_seconds == 0.002048
    assert report.round_trip_p95_seconds == 0.004096
    assert sum(report.latency_buckets) == 3
    assert report.round_trip_seconds == pytest.approx(0.0055)
    assert report.game_seconds == pytest.approx(0.9945)
    assert report.transport_seconds == 0.0
