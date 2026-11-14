"""Grouped evals share serving lifetimes and preserve tournament delivery and admission."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future
from contextlib import AbstractContextManager, contextmanager
from decimal import Decimal
from multiprocessing import get_context
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path

import pytest
from pydantic import BaseModel

from etude.controller import advance
from etude.declarations import Experiment, read_experiment
from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.eval.game import Execution
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Code, Config, Match, RoundRobin
from etude.eval.resources import THREAD_VARIABLES, available_cpus
from etude.eval.result import Tables
from etude.eval.run import Terminated
from etude.eval.tournament import evaluating, played
from etude.experiment import state_of
from etude.jobs import (
    EvalGroup,
    EvalJob,
    ExperimentState,
    InferenceServer,
    evaluation_group,
    hold_group,
)
from etude.launch import client
from etude.launch.commands import DEFAULT_MIN_EVAL_HOURS
from etude.launch.records import Pool, Standing, Status
from etude.records import RunRecord, RunSubmission
from etude.smoke import Step, smoke_file
from etude.spec import Job, RunRequest
from etude.storage import DirectoryStorage, default_root
from etude.train.tracking import track
from examples.served import Endpoint, ServeConfig, play, serve
from tests.launch.counting import Counting, Counts, job_text
from tests.launch.fake import bench
from tests.test_jobs import checkout


class CountedConfig(BaseModel):
    events: Path
    bias: int = 1


@contextmanager
def counted(config: CountedConfig) -> Iterator[Endpoint]:
    with config.events.open("a") as log:
        with serve(ServeConfig(bias=config.bias)) as endpoint:
            log.write("started\n")
            log.flush()
            try:
                yield endpoint
            finally:
                log.write("stopped\n")


@contextmanager
def unpicklable(config: CountedConfig) -> Iterator[object]:
    yield lambda: config.bias


@contextmanager
def broken(config: CountedConfig) -> Iterator[object]:
    raise ValueError("server could not load weights")
    yield  # pragma: no cover - defines a context manager that fails before entry


def fails(match: Match, inference: Endpoint) -> Tables:
    raise ValueError("bad game")


def terminate(match: Match, inference: Endpoint) -> Tables:
    os.kill(os.getpid(), signal.SIGTERM)
    return {}


def plain(match: Match) -> Tables:
    return {"scores": [{"seat": 0, "score": 7}]}


def tournament(name: str) -> RoundRobin:
    return RoundRobin(
        name=name, environment=environment("served"), configs=({"lane": name},), games=1
    )


def job(name: str, server: InferenceServer | None, *, workers: int = 2) -> EvalJob:
    return EvalJob(
        tournament=tournament(name),
        experiment="grouped",
        enter=(Code(name="left"), Code(name="right")),
        play=plain if server is None else play,
        server=server,
        workers=workers,
        repo=None,
        max_hours=0.1,
        max_usd=Decimal(1),
    )


def server_at(path: Path) -> InferenceServer:
    return InferenceServer(name="net", serve=counted, config=CountedConfig(events=path))


def group(*jobs: EvalJob) -> EvalGroup:
    return EvalGroup(
        evaluations=tuple(spec for job in jobs for spec in evaluation_group(job).evaluations)
    )


def test_two_tournaments_share_one_real_serving_child_and_resume_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events"
    server = server_at(events)
    jobs = (job("ladder", server), job("gauntlet", server), job("plain", None))
    (tmp_path / "output").mkdir()
    monkeypatch.setenv("ETUDE_OUTPUT_DIR", str(tmp_path / "output"))
    held = hold_group(group(*jobs), directory=tmp_path / "runs")
    assert [result.played for result in held] == [1, 1, 1]
    assert events.read_text().splitlines() == ["started", "stopped"]
    assert json.loads((tmp_path / "output" / "etude-result.json").read_text())["tally"]["done"] == 3
    again = hold_group(group(*jobs), directory=tmp_path / "runs")
    assert [result.played for result in again] == [0, 0, 0]
    assert events.read_text().splitlines() == ["started", "stopped"]
    for each in jobs:
        assert not played(
            each.tournament, experiment="grouped", enter=each.enter, directory=tmp_path / "runs"
        ).new


def test_match_failure_finishes_its_tournament_and_allows_the_next(tmp_path: Path) -> None:
    events = tmp_path / "events"
    server = server_at(events)
    bad = job("bad", server).model_copy(update={"play": fails})
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(group(bad, job("good", server)), directory=tmp_path)
    assert events.read_text().splitlines() == ["started", "stopped"]
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select count(*) from scores").fetchone() == (2,)
        assert {row[0] for row in db.sql("select state from runs").fetchall()} == {
            "failed",
            "finished",
        }
    finally:
        db.close()


def test_sigterm_leaves_the_serving_context(tmp_path: Path) -> None:
    events = tmp_path / "events"
    active = job("stop", server_at(events), workers=1).model_copy(update={"play": terminate})
    with pytest.raises(RetryRequested):
        hold_group(group(active), directory=tmp_path)
    assert events.read_text().splitlines() == ["started", "stopped"]


@pytest.mark.parametrize(
    "function, message",
    [(broken, "could not load"), (unpicklable, "inference_handle_not_picklable")],
)
def test_server_refusal_records_retryable_failures_without_results(
    tmp_path: Path,
    function: Callable[[CountedConfig], AbstractContextManager[object]],
    message: str,
) -> None:
    server = InferenceServer(
        name="net", serve=function, config=CountedConfig(events=tmp_path / "events")
    )
    active = job("ladder", server)
    with pytest.raises(RetryRequested, match=message):
        hold_group(group(active), directory=tmp_path)
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.new) == len(found.failed) == len(found.retry) == 1


def test_server_conflicts_and_play_arity_are_refused_before_games(tmp_path: Path) -> None:
    first = server_at(tmp_path / "one")
    second = server_at(tmp_path / "two")
    with pytest.raises(EtudeError, match="inference_server_conflict"):
        hold_group(group(job("one", first), job("two", second)), directory=tmp_path)
    with pytest.raises(LauncherError, match="inference handle"):
        job("wrong", first).model_copy(update={"play": plain}).plain()
    with pytest.raises(LauncherError, match="without an inference server"):
        job("wrong", None).model_copy(update={"play": play}).plain()


def test_a_pass_groups_by_server_and_keeps_group_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    server = server_at(tmp_path / "events")
    jobs = [job("one", server), job("two", server), job("plain", None)]

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
            enter=(Code(name="earlier"), Code(name="right")),
            directory=source.root,
        ) as active:
            for match in active.matches:
                active.store(match, plain(match), seconds=3600)
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


def _batch_pair(ready: Connection, events: Path) -> None:
    """A real two-request batch: neither tournament can finish without the other."""
    with Listener(("127.0.0.1", 0)) as listener:
        ready.send(listener.address)
        ready.close()
        connections = []
        try:
            for _ in range(2):
                connection = listener.accept()
                connections.append(connection)
            requests = [connection.recv() for connection in connections]
            events.write_text(f"batch={len(requests)}\n")
            for connection, request in zip(connections, requests, strict=True):
                connection.send(int(request) + 1)
        finally:
            for connection in connections:
                connection.close()


@contextmanager
def batched(config: CountedConfig) -> Iterator[Endpoint]:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_batch_pair, args=(child, config.events))
    process.start()
    child.close()
    try:
        if not parent.poll(10):
            raise RuntimeError("batch server did not start")
        yield Endpoint(address=parent.recv())
    finally:
        parent.close()
        process.terminate()
        process.join()


def batch_play(match: Match, inference: Endpoint) -> Tables:
    with Client(inference.address) as connection:
        connection.send(match.games)
        if not connection.poll(5):
            raise RuntimeError("the other tournament never reached the batch")
        score = connection.recv()
    return {"scores": [{"seat": 0, "score": score}]}


def test_tournaments_feed_the_same_inference_batch_concurrently(tmp_path: Path) -> None:
    events = tmp_path / "batch"
    server = InferenceServer(name="net", serve=batched, config=CountedConfig(events=events))
    held = hold_group(
        group(
            *(
                job(name, server, workers=2).model_copy(update={"play": batch_play})
                for name in ("one", "two")
            )
        ),
        directory=tmp_path,
    )
    assert [result.played for result in held] == [1, 1]
    assert events.read_text() == "batch=2\n"


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


@pytest.mark.parametrize("refuse_first", [False, True])
def test_a_worker_delivers_both_nested_tournaments_and_the_next_pass_plays_nothing(
    tmp_path: Path, command_python: str, monkeypatch: pytest.MonkeyPatch, refuse_first: bool
) -> None:
    ready = tmp_path / "ready"
    if not refuse_first:
        ready.touch()
    repo = checkout(
        tmp_path / "repo",
        {
            "served_group.py": f"""
from etude.jobs import EvalJob, ExperimentState, InferenceServer
from etude.eval.records import Code, RoundRobin
from etude.eval.match import environment
from etude.spec import Runtime
from examples.served import serve, ServeConfig
from tests.test_eval_groups import play_when_ready as play

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    net = InferenceServer(name="net", serve=serve, config=ServeConfig())
    return [EvalJob(tournament=RoundRobin(name=name, environment=environment("served"),
                  configs=({{"lane": name, "ready": {str(ready)!r}}},), games=1),
                  enter=(Code(name="left"), Code(name="right")), play=play, server=net,
                  workers=2, repo=None, runtime=Runtime(python={command_python!r}), max_hours=0.05)
            for name in ("ladder", "gauntlet")]
"""
        },
    )
    scheduler = client.default()
    monkeypatch.setattr(client, "_client", lambda: scheduler)
    monkeypatch.setattr(client, "standing", scheduler.standing)
    source = DirectoryStorage(default_root())
    definition = read_experiment(repo / "served_group.py")
    try:
        decisions = advance(definition, source=source, pool="local", mode="launch")
        assert decisions[0].launched == decisions[1].launched
        (status,) = scheduler.wait(str(decisions[0].launched), every=0.05, timeout=30)
        if refuse_first:
            assert status.outcome == "failed"
            ready.touch()
            decisions = advance(definition, source=source, pool="local", mode="launch")
            assert all(d.new == 1 and not d.stopped for d in decisions)
            (status,) = scheduler.wait(str(decisions[0].launched), every=0.05, timeout=30)
        assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=status.run))
        assert len([key for key in source.keys("") if key.endswith("tournament.json")]) == (
            4 if refuse_first else 2
        )
        assert all(
            "/evals-" in key and "/served_group/" in key
            for key in source.keys("")
            if key.endswith("tournament.json")
        )
        log = job_text(scheduler.logs(refresh=True, run=status.run))
        assert log.count("server net: started") == log.count("server net: stopped") == 1
        again = advance(definition, source=source, pool="local", mode="launch")
        assert all(d.new == 0 and d.launched is None for d in again)
        assert len(scheduler.runs(done=True)) == (2 if refuse_first else 1)
    finally:
        for active in scheduler.runs():
            scheduler.cancel(active.id)
        scheduler.wait(every=0.05, timeout=30)


def refused(match: Match) -> Tables:
    raise RetryRequested("the runtime refused this allocation")


def disconnected(match: Match, inference: Endpoint) -> Tables:
    raise RetryRequested("the serving process exited")


@pytest.mark.parametrize("with_server", [False, True])
def test_infrastructure_refusal_keeps_matches_new_and_never_records_success(
    tmp_path: Path, with_server: bool
) -> None:
    server = server_at(tmp_path / "events") if with_server else None
    active = job("refused", server).model_copy(
        update={"play": disconnected if with_server else refused}
    )
    with pytest.raises(RetryRequested):
        hold_group(group(active), directory=tmp_path)
    found = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(found.new) == len(found.retry) == 1
    assert len(found.failed) == 1 and not found.delivered
    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs").fetchall() == [("failed",)]
        assert db.sql("select state, retry from matches").fetchall() == [("failed", True)]
    finally:
        db.close()
    repaired = job("refused", server)
    assert hold_group(group(repaired), directory=tmp_path)[0].played == 1


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


def resource_play(match: Match) -> Tables:
    assert Path(os.environ["ETUDE_SCRATCH_DIR"]).is_dir()
    receipts = Path(os.environ["ETUDE_RECEIPTS_DIR"])
    receipts.joinpath("environment.json").write_text(
        json.dumps({name: os.environ[name] for name in THREAD_VARIABLES})
    )
    return {"threads": [{"seat": 0, "threads": int(os.environ["OMP_NUM_THREADS"])}]}


def test_group_workers_get_container_cpu_budget_and_fast_scratch_with_retained_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 6)
    active = job("resources", None).model_copy(update={"play": resource_play})
    old = os.environ.get("OMP_NUM_THREADS")
    hold_group(group(active), directory=tmp_path)
    receipt = tmp_path / "grouped" / "artifacts" / "inference" / "environment.json"
    assert set(json.loads(receipt.read_text()).values()) == {"3"}
    assert os.environ.get("OMP_NUM_THREADS") == old


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


def play_when_ready(match: Match[Config], inference: Endpoint) -> Tables:
    if not Path(str(match.config["ready"])).exists():
        raise RetryRequested("the serving runtime refused this allocation")
    return play(match, inference)


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


def terminate_parent(match: Match, inference: Endpoint) -> Tables:
    os.kill(os.getppid(), signal.SIGTERM)
    return {"scores": [{"seat": 0, "score": 1}]}


def test_sigterm_reaps_the_shared_worker_pool_before_leaving_the_server(tmp_path: Path) -> None:
    events = tmp_path / "events"
    server = server_at(events)
    stopping = job("signal", server).model_copy(update={"play": terminate_parent})
    with pytest.raises(Terminated):
        hold_group(group(stopping, job("other", server)), directory=tmp_path)
    assert events.read_text().splitlines() == ["started", "stopped"]


def test_an_eval_only_smoke_starts_the_shared_server_without_training_checkpoints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps: tuple[Step, ...] = ("eval",)
    result = smoke_file(Path("examples/served.py"), steps=steps, source=DirectoryStorage(tmp_path))
    assert len(result.rest) == 2
    assert all(step.outcome == "passed" for step in result.rest)
    log = capsys.readouterr().err
    assert log.count("server net: started") == log.count("server net: stopped") == 1


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
        assert upgraded.sql("select schema from kept.statsdb").fetchone() == (15,)
    finally:
        upgraded.close()


@pytest.mark.parametrize("heavy, expected", [(False, 1), (True, 2)])
def test_game_startup_imports_only_the_defining_module_and_counts_its_imports(
    tmp_path: Path, heavy: bool, expected: int
) -> None:
    (tmp_path / "torch.py").write_text(
        "# Controlled import fixture; no framework or resource load.\n"
    )
    (tmp_path / "game_module.py").write_text(
        ("import torch\n" if heavy else "")
        + "def play(match):\n    return {'scores': [{'seat': 0, 'score': 1}]}\n"
    )
    (tmp_path / "experiment_module.py").write_text(
        "raise RuntimeError('a game must not import the experiment')\n"
    )
    program = """
import sys
from etude.call import Function
from etude.eval.game import play_match
from etude.eval.records import Match, Code
match = Match(players=(Code(name="one"),))
result = play_match(Function(module="game_module", name="play", path=sys.argv[1]), match)
print(result.imports.model_dump_json())
assert "experiment_module" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    record = json.loads(result.stdout)
    assert record["count"] == expected
    assert record["heavy"] == (["torch"] if heavy else [])
    assert record["importer"] == ("game_module" if heavy else None)


@pytest.mark.parametrize("fails", [False, True])
def test_heavy_game_import_warning_is_reported_once_for_the_group(
    tmp_path: Path, fails: bool
) -> None:
    (tmp_path / "torch.py").write_text("# Controlled framework fixture.\n")
    game = (
        "raise RuntimeError('fixture game failed')"
        if fails
        else "return {'scores': [{'seat': 0, 'score': 1}]}"
    )
    (tmp_path / "game_module.py").write_text("import torch\ndef play(match):\n    " + game + "\n")
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from game_module import play
from etude.eval.tournament import evaluating
from etude.errors import EtudeError
from etude.eval.records import Environment, Match, Code
matches = [Match(players=(Code(name="one"),), config={"case": i}) for i in range(2)]
with evaluating(Environment(name="imports", files="0" * 64), experiment="imports", matches=matches,
                workers=2, directory=sys.argv[1]) as active:
    try:
        assert active.play(play).played == 2
    except EtudeError:
        assert sys.argv[2] == "True"
    else:
        assert sys.argv[2] == "False"
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(fails)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("game module game_module: imported torch via game_module") == 1
    assert "define play in a lightweight module" in result.stderr


class OfflineStorage(DirectoryStorage):
    def put(self, key: str, data: bytes) -> None:
        raise OSError("fixture storage is offline")

    def put_file(self, key: str, path: Path) -> None:
        raise OSError("fixture storage is offline")


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


@pytest.mark.parametrize("version", [1, 2])
def test_container_cpu_count_respects_nested_cgroup_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> None:
    root = tmp_path / "cgroup"
    membership = tmp_path / "membership"
    if version == 2:
        (root / "tenant").mkdir(parents=True)
        (root / "tenant" / "cpu.max").write_text("max 100000")
        (root / "cpu.max").write_text("200000 100000")
        membership.write_text("0::/tenant\n")
    else:
        (root / "cpu" / "tenant").mkdir(parents=True)
        (root / "cpu" / "tenant" / "cpu.cfs_quota_us").write_text("200000")
        (root / "cpu" / "tenant" / "cpu.cfs_period_us").write_text("100000")
        membership.write_text("2:cpu,cpuacct:/tenant\n")

    def fixture_path(value: str) -> Path:
        return (
            root
            if value == "/sys/fs/cgroup"
            else membership
            if value == "/proc/self/cgroup"
            else Path(value)
        )

    monkeypatch.setattr("etude.eval.resources.Path", fixture_path)

    def allowed_cpus(pid: int) -> set[int]:
        return set(range(32))

    monkeypatch.setattr(os, "sched_getaffinity", allowed_cpus, raising=False)
    assert available_cpus() == 2


def game_eof(match: Match, inference: Endpoint) -> Tables:
    raise EOFError("a game parsing error")


def test_an_ordinary_play_exception_does_not_request_retry_even_with_a_server(
    tmp_path: Path,
) -> None:
    active = job("game-error", server_at(tmp_path / "events")).model_copy(update={"play": game_eof})
    with pytest.raises(EtudeError, match="match_failed"):
        hold_group(group(active), directory=tmp_path)
    result = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(result.failed) == 1 and not result.retry and not result.new


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


class MissingReceiptStorage(DirectoryStorage):
    def put(self, key: str, data: bytes) -> None:
        if key.startswith("game-imports-"):
            raise OSError("fixture cannot retain receipt")
        super().put(key, data)


def test_an_import_receipt_write_failure_requests_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.run.DirectoryStorage", MissingReceiptStorage)
    active = job("receipt", None)
    with pytest.raises(RetryRequested, match="receipt could not be retained"):
        hold_group(group(active), directory=tmp_path)
    result = played(active.tournament, experiment="grouped", enter=active.enter, directory=tmp_path)
    assert len(result.new) == len(result.failed) == len(result.retry) == 1
