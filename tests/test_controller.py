"""One pass deduplicates addresses using only storage and public scheduler records."""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from etude.call import Function
from etude.checkpoints import CheckpointRecord, PolicyKey
from etude.cli import main
from etude.controller import advance
from etude.declarations import read_experiment
from etude.errors import EtudeError, LauncherError
from etude.eval.match import environment
from etude.eval.records import Benchmark, CheckpointPlayer
from etude.eval.tournament import StoredRuns
from etude.experiment import observe
from etude.jobs import EvalGroup, EvalJob, Evaluation, ExperimentState, TrainingJob, with_experiment
from etude.launch import client
from etude.launch.records import Pool
from etude.page import build
from etude.records import ArtifactRef, RunRecord
from etude.spec import Job, RunRequest, job_id
from etude.storage import DirectoryStorage, default_root
from etude.train.tracking import track
from tests.eval.games import play
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench
from tests.launch.tasks import Command, train_command
from tests.run.storage import MeasuredStorage
from tests.test_jobs import checkout

FILE = """from decimal import Decimal
from etude.jobs import TrainingJob
from tests.launch.tasks import Command, train_command

def training_jobs(state):
    base = TrainingJob(name="base", train=train_command, config=Command(command="true"),
                       repo=None, seeds=(0, 1), max_hours=1, max_usd=Decimal(1))
    fine = base.model_copy(update={"name": "fine", "seeds": (0,)})
    return [base, fine] if any(r.launch == "base" and r.seed == 0 and r.state == "finished"
                              for r in state.runs) else [base]
"""


def test_a_pass_leaves_known_runs_alone_and_queues_only_a_new_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    file = tmp_path / "two.py"
    file.write_text(FILE)
    definition = read_experiment(file)
    source = DirectoryStorage(tmp_path / "source")
    advance(definition, source=source, pool="waiting", mode="queue", max_usd=Decimal(3))
    assert [r.id for r in b.scheduler.runs()] == ["two/base@0", "two/base@1"]
    counts = Counting(b.store, b.service)
    measured = MeasuredStorage(source)
    with counts.record():
        advance(definition, source=measured, pool="waiting", mode="queue", max_usd=Decimal(3))
    assert counts.snapshot() == Counts(database=7)
    assert (measured.listings, measured.requests) == (1, 0)
    assert len(b.scheduler.runs()) == 2
    # Stored compact spelling must match its declared job and seed.
    with track(experiment="two", run="two@0", job="base", seed=0, directory=source.root):
        pass
    measured = MeasuredStorage(source)
    with counts.record():
        advance(definition, source=measured, pool="waiting", mode="queue", max_usd=Decimal(3))
    assert counts.snapshot() == Counts(database=38)
    assert measured.listings == 1
    assert [r.id for r in b.scheduler.runs()] == ["two/base@0", "two/base@1", "two/fine@0"]
    assert "two/base@0  finished" in capsys.readouterr().out
    assert not (tmp_path / "experiments").exists()


def test_an_experiment_limit_refuses_hand_down_before_any_rental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    file = tmp_path / "two.py"
    file.write_text(FILE)
    advance(
        read_experiment(file),
        source=DirectoryStorage(tmp_path / "source"),
        rent="l4",
        mode="launch",
        max_usd=Decimal("0.5"),
    )
    assert b.scheduler.runs() == ()
    assert b.service.creates == 0
    assert "not queued: --max-usd 0.5 ($0.00 committed)" in capsys.readouterr().out


def test_a_pass_prints_one_line_per_run_in_declared_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    file = tmp_path / "two.py"
    file.write_text(FILE)
    advance(
        read_experiment(file),
        source=DirectoryStorage(tmp_path / "source"),
        pool="waiting",
        mode="queue",
        max_usd=Decimal("1.5"),
    )
    assert capsys.readouterr().out.splitlines()[1:] == [
        "two/base@0  new, queued",
        "two/base@1  new, not queued: --max-usd 1.5 ($1.00 committed)",
    ]


def test_wrong_eval_kind_refuses_before_training_is_submitted(tmp_path: Path) -> None:
    file = tmp_path / "two.py"
    file.write_text(FILE + "\ndef eval_jobs(state):\n    return training_jobs(state)\n")
    with pytest.raises(LauncherError, match=r"^two\.py: eval_jobs\(\) must return EvalJob$"):
        advance(read_experiment(file), source=DirectoryStorage(tmp_path / "source"), mode="queue")


def test_typed_job_lowering_preserves_the_executable_record_and_identity() -> None:
    typed = TrainingJob(
        name="base",
        train=train_command,
        config=Command(command="true"),
        repo=None,
        max_hours=1,
        max_usd=Decimal(1),
    )
    plain = Job(
        name="base",
        command="uv run python -m etude.call --function tests.launch.tasks:train_command",
        config='{"command":"true","text":null}',
        repo=None,
        max_hours=1,
        max_usd=Decimal(1),
    )
    assert RunRequest(job=typed).model_dump_json() == RunRequest(job=plain).model_dump_json()
    assert job_id(typed) == job_id(plain)
    changed = typed.model_copy(update={"config": Command(command="echo changed")})
    assert changed.config.command == "echo changed"
    assert changed.plain().config == '{"command":"echo changed","text":null}'
    assert typed.config.command == "true"


def test_historical_cohort_field_is_read_only() -> None:
    record = RunRecord.model_validate({"id": "old", "experiment": "two", "cohort": "first"})
    assert record.cohort == "first"
    assert "cohort" not in record.model_dump()


def test_cli_drives_training_stages_and_eval_through_one_local_slot(
    tmp_path: Path,
    command_python: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    file = (
        checkout(
            tmp_path / "repo",
            {
                "two.py": f"""
from pathlib import Path
from pydantic import BaseModel
from etude.jobs import TrainingJob, EvalJob, ExperimentState
from etude.spec import Runtime
from etude.train.tracking import track
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, Benchmark, Seating, Code
from tests.eval.games import play

RUNTIME = Runtime(python={command_python!r})

class Config(BaseModel):
    value: int = 1

def train(config: Config) -> None:
    with track(config=config) as run:
        run.log({{"value": config.value}}, updates=1)
        path = run.out / "weights-1"
        path.write_bytes(f"weights {{run.seed}} {{config.value}}".encode())
        run.checkpoint(path, updates=1)

def training_jobs(state: ExperimentState) -> list[TrainingJob]:
    base = TrainingJob(name="base", train=train, config=Config(), seeds=(0, 1),
                       max_hours=0.05, runtime=RUNTIME)
    fine = base.model_copy(update={{"name": "fine", "config": Config(value=2), "seeds": (0,)}})
    return [base, fine] if any(r.launch == "base" and r.state == "finished" for r in state.runs) else [base]

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    ladder = Benchmark(name="ladder", environment=environment("duel", config={{"world": "w6"}}),
        seatings=(Seating(name="anchor", players=(stored_inference(config={{"search": 0}}),
                   Code(name="fixed", source=[__file__], config={{"strength": 0}})),
                   configs=({{"deal": 0}},), games=1),))
    return [EvalJob(tournament=ladder, play=play, checkpoints=[r.final for r in state.runs if r.final],
                    max_hours=0.05, runtime=RUNTIME, repo=str(Path(__file__).parent))]
"""
            },
        )
        / "two.py"
    )
    scheduler = client.default()
    try:
        assert main(["launch", str(file)]) == 0
        first = capsys.readouterr().out
        assert "two/base@0  new, running" in first
        assert "two/base@1  new, queued" in first
        assert "eval ladder: nothing to play" in first
        assert all(s.outcome == "succeeded" for s in scheduler.wait(every=0.05, timeout=30))
        assert main(["launch", str(file)]) == 0
        second = capsys.readouterr().out
        assert "two/fine@0  new, running" in second
        assert "queued two/evals-" in second
        scheduler.wait(every=0.05, timeout=30)
        assert all(scheduler.read(r.id).outcome == "succeeded" for r in scheduler.runs(done=True))
        assert main(["launch", str(file)]) == 0
        scheduler.wait(every=0.05, timeout=30)
        assert len(scheduler.runs(done=True)) == 5
        assert main(["launch", str(file)]) == 0
        assert len(scheduler.runs(done=True)) == 5
        assert main(["logs", "--run", "base@0", "--experiment", "two"]) == 0
        assert not (home / "experiments").exists()
    finally:
        for run in scheduler.runs():
            scheduler.cancel(run.id)
        scheduler.wait(every=0.05, timeout=30)


def test_declaration_types_are_defined_in_one_module() -> None:
    assert {kind.__module__ for kind in (TrainingJob, EvalJob, ExperimentState)} == {"etude.jobs"}


def test_eval_job_lowering_preserves_the_executable_record() -> None:
    tournament = Benchmark(name="ladder", environment=environment("duel"))
    typed = EvalJob(tournament=tournament, play=play, repo=None, max_hours=1)
    executable = with_experiment(typed, "two")
    config = Evaluation(
        tournament=tournament,
        experiment="two",
        play=Function(module="tests.eval.games", name="play"),
    )
    plain = Job(
        name="ladder",
        command="uv run python -m etude.call --function etude.jobs:_hold",
        config=EvalGroup(evaluations=(config,)).model_dump_json(),
        repo=None,
        experiment="two",
        max_hours=1,
    )
    assert RunRequest(job=executable).model_dump_json() == RunRequest(job=plain).model_dump_json()
    assert job_id(executable) == job_id(plain)


def test_unnamed_training_job_and_callable_tournament_are_refused() -> None:
    with pytest.raises(LauncherError, match="name="):
        TrainingJob.model_validate(
            {"train": train_command, "config": Command(command="true"), "repo": None}
        )
    with pytest.raises(LauncherError, match="call the function first"):
        EvalJob.model_validate(
            {"tournament": lambda: None, "play": play, "max_hours": 1, "repo": None}
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [("runs", "training_jobs"), ("training_runs", "training_jobs"), ("eval", "eval_jobs")],
)
def test_retired_declarations_are_refused_before_file_execution(
    tmp_path: Path, old: str, new: str
) -> None:
    file = tmp_path / "legacy.py"
    file.write_text(f"raise AssertionError('executed')\ndef {old}(): pass\n")
    with pytest.raises(LauncherError, match=f"rename {old}.*{new}"):
        read_experiment(file)


def test_cancel_requires_a_run_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    assert main(["cancel"]) == 1
    assert "name run addresses, --experiment or --job" in capsys.readouterr().err
    assert not home.exists()


def test_cli_hands_down_unaffected_work_before_reporting_failed_eval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    file = tmp_path / "two.py"
    file.write_text("""
from etude.jobs import TrainingJob, EvalJob, ExperimentState
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, RoundRobin, Code
from tests.eval.games import play
from tests.launch.tasks import Command, train_command

def training_jobs(state: ExperimentState) -> list[TrainingJob]:
    return [TrainingJob(name="fine", train=train_command, config=Command(command="true"),
                        repo=None, max_hours=1)] if state.eval_runs else []

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    entrants = (Code(name="a", source=[__file__], config={"strength": 1}),
                Code(name="b", source=[__file__], config={"strength": 0}))
    names = ("held", "free") if state.eval_runs else ("held",)
    return [EvalJob(tournament=RoundRobin(name=name, environment=environment("duel", config={"world": name}), configs=({"deal": name},), games=1),
                    enter=entrants, play=play, repo=None, max_hours=1) for name in names]
""")
    source = DirectoryStorage(tmp_path / "source")
    measured = MeasuredStorage(source)
    monkeypatch.setattr("etude.storage.storage_from_uri", lambda uri: measured)
    monkeypatch.setattr("etude.run.storage_from_uri", lambda uri: measured)
    args = [
        "queue",
        str(file),
        "--pool",
        "waiting",
        "--source",
        str(source.root),
        "--min-eval-hours",
        "0",
    ]
    assert main(args) == 0
    (held,) = b.scheduler.runs()
    b.scheduler.cancel(held.id)
    capsys.readouterr()
    counts = Counting(b.store, b.service)
    measured.listings = 0
    with counts.record():
        assert main(args) == 1
    first_counts = counts.snapshot()
    assert measured.listings == 1 and measured.requests == 0
    first = capsys.readouterr()
    assert "two/fine@0  new, queued" in first.out
    assert f"failed runs {held.id} (--retry-failed" in first.out
    assert "eval free:" in first.out and ", queued two/evals-" in first.out
    assert "launched_pass_failed" in first.err
    assert len(b.scheduler.runs(done=True)) == 3
    measured.listings = 0
    with counts.record():
        assert main(args) == 1
    second_counts = counts.snapshot()
    assert measured.listings == 1 and measured.requests == 0
    second = capsys.readouterr()
    assert "two/fine@0  queued" in second.out
    assert re.search(r"eval free: 0 new, [1-9]\d* running", second.out)
    assert "launched_pass_failed" in second.err
    assert len(b.scheduler.runs(done=True)) == 3
    assert first_counts == Counts(database=39)
    assert second_counts == Counts(database=9)
    assert main(["launch", *args[1:], "--quote"]) == 0
    quoted = capsys.readouterr()
    assert f"failed runs {held.id} (--retry-failed" in quoted.out
    assert "launched_pass_failed" not in quoted.err


@pytest.mark.parametrize("command", ["eval", "queue", "launch"])
@pytest.mark.parametrize("format", ["benchmark", "round_robin"])
def test_cli_eval_only_file_plays_a_checkpoint_from_another_experiment(
    tmp_path: Path,
    command_python: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    format: str,
) -> None:
    monkeypatch.setenv("EVAL_TEST_KEY", "local fixture credential")
    source = DirectoryStorage(default_root())
    with track(
        experiment="producer", run="base@0", directory=source.root, clocks=["updates"], strict=True
    ) as run:
        path = run.out / "weights.pt"
        path.write_bytes(b"earlier experiment weights")
        run.checkpoint(path, updates=1)
    checkpoint = observe(source, "producer").runs[0].final
    assert checkpoint is not None
    file = (
        checkout(
            tmp_path / "repo",
            {
                "review.py": f"""
from pathlib import Path
import os
from etude.experiment import observe
from etude.jobs import EvalJob, ExperimentState
from etude.spec import Runtime
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, Benchmark, RoundRobin, Seating, Code, CheckpointPlayer, Match
from etude.eval.run import Tables


def play(match: Match) -> Tables:
    players = [p for p in match.players if isinstance(p, CheckpointPlayer)]
    assert players
    if {command!r} == "eval":
        assert os.environ["EVAL_TEST_KEY"] == "local fixture credential"
    for player in players:
        assert player.checkpoint.key == {checkpoint.key!r}
        assert player.checkpoint.path.read_bytes() == b"earlier experiment weights"
    return {{"read_weights": [{{"seat": 0, "bytes": len(players[0].checkpoint.path.read_bytes())}}]}}

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    CHECKPOINT = observe({str(source.root)!r}, "producer").run("producer/base@0").final
    fixed = Code(name="fixed", source=[__file__], config={{"strength": 0}})
    if {format!r} == "benchmark":
        tournament = Benchmark(name="ladder", environment=environment("duel"),
            seatings=(Seating(name="anchor", players=(stored_inference(config={{}}), fixed),
                              configs=({{}},), games=1),))
        return [EvalJob(tournament=tournament, checkpoints=(CHECKPOINT,), play=play,
                        max_hours=0.05, runtime=Runtime(python={command_python!r}),
                        repo=str(Path(__file__).parent))]
    tournament = RoundRobin(name="ladder", environment=environment("duel"), configs=({{}},), games=1)
    return [EvalJob(tournament=tournament, enter=(CHECKPOINT.player(stored_inference(config={{}})), fixed),
                    play=play, max_hours=0.05, runtime=Runtime(python={command_python!r}),
                    repo=str(Path(__file__).parent))]
"""
            },
        )
        / "review.py"
    )
    scheduler = client.default()
    monkeypatch.setattr(client, "_client", lambda: scheduler)
    monkeypatch.setattr(client, "standing", scheduler.standing)
    counts = Counting(scheduler._store)
    measured = MeasuredStorage(source)
    monkeypatch.setattr("etude.storage.storage_from_uri", lambda uri: measured)
    monkeypatch.setattr("etude.run.storage_from_uri", lambda uri: measured)
    try:
        with counts.record():
            assert main([command, str(file), "--source", str(source.root)]) == 0
        assert measured.listings == 2
        observed_counts = counts.snapshot()
        output = capsys.readouterr()
        assert "0 training runs" in output.out
        if command != "eval":
            assert all(s.outcome == "succeeded" for s in scheduler.wait(every=0.05, timeout=30))
            assert len(scheduler.runs(done=True)) == 1
        else:
            assert "1 played" in output.out
        # A later pass observes delivery and submits or plays nothing again.
        assert main([command, str(file), "--source", str(source.root)]) == 0
        assert "nothing to play" in capsys.readouterr().out
        if command == "eval":
            assert observed_counts == Counts(git=2, database=2)
        elif command == "queue":
            # Two reads find the target's known CPUs, which divide estimated game seconds.
            assert observed_counts == Counts(git=12, database=31)
        else:
            assert observed_counts.model_copy(update={"database": 0}) == Counts(git=13)
        assert all(not key.endswith("weights.pt") for key in measured.got)
    finally:
        for active in scheduler.runs():
            scheduler.cancel(active.id)
        scheduler.wait(every=0.05, timeout=30)


@pytest.mark.parametrize("contents", [None, b"wrong bytes"])
def test_local_eval_names_a_missing_or_changed_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contents: bytes | None,
) -> None:
    file = tmp_path / "review.py"
    weights = tmp_path / "weights.pt"
    if contents is not None:
        weights.write_bytes(contents)
    file.write_text(f"""
from etude.checkpoints import CheckpointRef
from etude.records import ArtifactRef
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, RoundRobin, Code
from etude.jobs import EvalJob, ExperimentState
from tests.eval.games import play
from hashlib import sha256

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    checkpoint = CheckpointRef(address="producer/base@0/out/weights.pt", artifact=ArtifactRef(
        uri={weights.as_uri()!r}, sha256=sha256(b"right bytes").hexdigest(), size_bytes=11))
    return [EvalJob(tournament=RoundRobin(name="ladder", environment=environment("duel"),
                        configs=({{}},), games=1),
                    enter=(Held().player(checkpoint), Code(name="fixed", source=[__file__])),
                    play=play, repo=None, max_hours=1)]
""")
    assert main(["eval", str(file), "--source", str(tmp_path / "source")]) == 1
    error = capsys.readouterr().err
    code = "checkpoint_not_stored" if contents is None else "checkpoint_content_mismatch"
    assert code in error and "producer/base@0/out/weights.pt" in error


@pytest.mark.parametrize("command", ["eval", "queue", "launch"])
@pytest.mark.parametrize("format", ["benchmark", "round_robin"])
def test_eval_refuses_different_artifacts_with_one_checkpoint_key(
    tmp_path: Path, command: str, format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = DirectoryStorage(default_root())
    for experiment in ("a", "b"):
        with track(
            experiment=experiment, run="base@0", directory=source.root, clocks=["updates"]
        ) as run:
            path = run.out / "weights.pt"
            path.write_text(experiment)
            run.checkpoint(path, updates=1)
    file = tmp_path / "review.py"
    file.write_text(f"""
from etude.experiment import observe
from etude.jobs import EvalJob
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, Benchmark, RoundRobin, Code, Seating
from tests.eval.stored import stored_inference
from tests.eval.games import play

def eval_jobs(state):
    checkpoints = tuple(observe({str(source.root)!r}, name).runs[0].final.model_copy(update={{"experiment": "a"}}) for name in ("a", "b"))
    if {format!r} == "benchmark":
        tournament = Benchmark(name="ladder", environment=environment("duel"),
            seatings=(Seating(name="anchor", players=(stored_inference(), Code(name="fixed")), configs=({{}},), games=1),))
        return EvalJob(tournament=tournament, checkpoints=checkpoints, play=play, max_hours=0.1)
    return EvalJob(tournament=RoundRobin(name="ladder", environment=environment("duel"),
        configs=({{}},), games=1), enter=tuple(c.player(Held()) for c in checkpoints),
        play=play, max_hours=0.1)
""")
    assert main([command, str(file), "--source", str(source.root)]) == 1
    error = capsys.readouterr().err
    assert "checkpoint_key_conflict" in error
    assert "base@0/out/weights.pt" in error
    for experiment in ("a", "b"):
        assert (source.root / experiment / "base@0/out/weights.pt").as_uri() in error
    assert client.default().runs(done=True) == ()


@pytest.mark.parametrize("command", ["eval", "queue", "launch"])
def test_a_delivered_match_cannot_hide_another_artifact_under_its_key(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = DirectoryStorage(default_root())
    for name in ("a", "b"):
        with track(experiment=name, run="base@0", directory=source.root, clocks=["updates"]) as run:
            path = run.out / "weights.pt"
            path.write_text(name)
            run.checkpoint(path, updates=1)
    file = tmp_path / "review.py"
    text = f"""
from etude.experiment import observe
from etude.jobs import EvalJob
from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, RoundRobin, Code
from tests.eval.games import play

def eval_jobs(state):
    checkpoint = observe({str(source.root)!r}, "a").runs[0].final.model_copy(update={{"experiment": "a"}})
    return EvalJob(tournament=RoundRobin(name="first", environment=environment("duel"),
        configs=({{}},), games=1), enter=(checkpoint.player(stored_inference(config={{"strength": 1}})), Code(name="fixed", config={{"strength": 0}})),
        play=play, max_hours=0.1)
"""
    file.write_text(text)
    assert main(["eval", str(file), "--source", str(source.root)]) == 0
    capsys.readouterr()
    file.write_text(text.replace(', "a")', ', "b")').replace('name="first"', 'name="second"'))
    assert main([command, str(file), "--source", str(source.root)]) == 1
    error = capsys.readouterr().err
    assert "checkpoint_key_conflict: a/base@0/out/weights.pt" in error
    assert client.default().runs(done=True) == ()


@pytest.mark.parametrize("command", ["train", "queue", "launch"])
def test_wrong_training_kind_names_the_file_and_author_type(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "wrong_training.py"
    file.write_text("def training_jobs():\n    return 123\n")
    assert main([command, str(file)]) == 1
    assert "wrong_training.py: training_jobs() must return TrainingJob" in capsys.readouterr().err
    assert client.default().runs(done=True) == ()


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("command", ["eval", "queue", "launch"])
def test_wrong_eval_kind_names_the_file_and_author_type(
    tmp_path: Path, command: str, wrapped: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "wrong_eval.py"
    returned = "RunRequest(job=job)" if wrapped else "job"
    file.write_text(
        FILE + f"\nfrom etude.spec import RunRequest\n"
        f"def eval_jobs(state):\n    job = training_jobs(state)[0]\n    return {returned}\n"
    )
    assert main([command, str(file)]) == 1
    assert "wrong_eval.py: eval_jobs() must return EvalJob" in capsys.readouterr().err
    assert client.default().runs(done=True) == ()


@pytest.mark.parametrize("command", ["train", "queue", "launch"])
def test_known_run_address_survives_a_changed_job_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    b.scheduler.queue(
        [
            RunRequest(
                id="two/base@0",
                seed=0,
                job=Job(name="before", experiment="two", command="true", repo=None, max_hours=1),
            )
        ],
        "waiting",
    )
    file = tmp_path / "two.py"
    file.write_text(
        FILE + "\nfrom etude.spec import RunRequest\n"
        "def training_jobs():\n"
        "    job = TrainingJob(name='after', train=train_command, "
        "config=Command(command='true'), repo=None, max_hours=1)\n"
        "    return RunRequest(id='two/base@0', job=job, seed=0)\n"
    )
    assert main([command, str(file)]) == 0
    assert "two/base@0  queued" in capsys.readouterr().out
    assert [run.id for run in b.scheduler.runs(done=True)] == ["two/base@0"]


def test_a_launch_cancelled_before_its_first_save_resumes_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    recipe = """
from pydantic import BaseModel
from etude.jobs import TrainingJob
from etude.train.tracking import track

class Config(BaseModel):
    pass

def train(config: Config) -> None:
    with track(config=config, clocks=["updates"]) as run:
        run.log({"value": 1}, updates=1)

def training_jobs() -> list[TrainingJob]:
    return [TrainingJob(name="base", train=train, config=Config(), max_hours=1)]
"""
    file = checkout(tmp_path / "repo", {"cancelled.py": recipe}) / "cancelled.py"
    assert main(["queue", str(file), "--pool", "waiting"]) == 0
    (queued,) = b.scheduler.runs()
    b.scheduler.cancel(queued.id)
    capsys.readouterr()
    assert main(["train", str(file)]) == 0
    assert "cancelled/base@0  failed; --job base --seed 0 --resume" in capsys.readouterr().out
    assert main(["train", str(file), "--job", "base", "--seed", "0", "--resume"]) == 0
    (run,) = observe(default_root(), "cancelled").runs
    assert run.state == "finished"


def test_policy_key_fresh_experiment_submits_training_then_evaluates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")
    with pytest.raises(EtudeError, match="policy_not_found"):
        observe(source, "fresh_policy").policy(PolicyKey())
    file = tmp_path / "fresh_policy.py"
    file.write_text("""
from etude.checkpoints import PolicyKey
from etude.eval.match import environment
from etude.eval.records import Benchmark, Code, Seating
from tests.eval.stored import stored_inference
from etude.jobs import EvalJob, ExperimentState, TrainingJob
from tests.eval.games import play
from tests.launch.tasks import Command, train_command

def training_jobs(state: ExperimentState) -> list[TrainingJob]:
    return [TrainingJob(name="small", train=train_command, config=Command(command="true"),
                        repo=None, max_hours=1)]

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    tournament = Benchmark(name="progress", environment=environment("duel"), seatings=(
        Seating(name="vs fixed", players=(stored_inference(config={"strength": 1}),
                Code(name="fixed", config={"strength": 0})), configs=({},), games=1),))
    mine = state.policy(PolicyKey()).checkpoints
    return [EvalJob(tournament=tournament, checkpoints=mine, play=play, repo=None, max_hours=1)]
""")
    definition = read_experiment(file)
    first = advance(definition, source=source, pool="waiting", mode="queue")
    assert [(decision.new, decision.running) for decision in first] == [(0, 0)]
    assert [run.id for run in b.scheduler.runs()] == ["fresh_policy/small@0"]
    # A queued run exists before its first checkpoint; another pass stays safe.
    queued = advance(definition, source=source, pool="waiting", mode="queue")
    assert [(decision.new, decision.running) for decision in queued] == [(0, 0)]
    assert len(b.scheduler.runs()) == 1
    with track(
        experiment="fresh_policy",
        run="small@0",
        job="small",
        seed=0,
        directory=source.root,
        clocks=["updates"],
        strict=True,
    ) as run:
        path = run.out / "weights.pt"
        path.write_bytes(b"first checkpoint")
        run.checkpoint(path, updates=1)
    decisions = advance(definition, source=source, directory=tmp_path / "evals")
    assert [(decision.new, decision.played) for decision in decisions] == [(1, 1)]
    stored = StoredRuns.read("fresh_policy", source)
    assert sum(len(stored.records(folder)) for folder in stored.folders()) == 1


def test_policy_key_of_a_job_added_to_a_started_experiment_is_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    source = DirectoryStorage(tmp_path / "source")
    file = tmp_path / "grown_policy.py"
    jobs = """
from etude.checkpoints import PolicyKey
from etude.eval.match import environment
from etude.eval.records import Benchmark, Code, Seating
from tests.eval.stored import stored_inference
from etude.jobs import EvalJob, ExperimentState, TrainingJob
from tests.eval.games import play
from tests.launch.tasks import Command, train_command

def training_jobs(state: ExperimentState) -> list[TrainingJob]:
    return [TrainingJob(name=name, train=train_command, config=Command(command="true"),
                        repo=None, max_hours=1) for name in NAMES]
"""
    file.write_text(jobs + 'NAMES = ["small"]\n')
    assert advance(read_experiment(file), source=source, pool="waiting", mode="queue") == []
    file.write_text(
        jobs
        + """NAMES = ["small", "large"]

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    tournament = Benchmark(name="progress", environment=environment("duel"), seatings=(
        Seating(name="vs fixed", players=(stored_inference(config={"strength": 1}),
                Code(name="fixed", config={"strength": 0})), configs=({},), games=1),))
    large = state.policy(PolicyKey(job="large")).checkpoints
    return [EvalJob(tournament=tournament, checkpoints=large, play=play, repo=None, max_hours=1)]
"""
    )
    decisions = advance(read_experiment(file), source=source, pool="waiting", mode="queue")
    assert [(decision.new, decision.running) for decision in decisions] == [(0, 0)]
    assert sorted(run.id for run in b.scheduler.runs()) == [
        "grown_policy/large@0",
        "grown_policy/small@0",
    ]


def test_policy_key_of_a_run_over_budget_says_why_it_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="waiting"))
    monkeypatch.setattr(client, "_client", lambda: b.scheduler)
    monkeypatch.setattr(client, "standing", b.scheduler.standing)
    file = tmp_path / "over_budget.py"
    file.write_text("""
from etude.checkpoints import PolicyKey
from etude.eval.match import environment
from etude.eval.records import Benchmark, Code, Seating
from tests.eval.stored import stored_inference
from etude.jobs import EvalJob, ExperimentState, TrainingJob
from tests.eval.games import play
from tests.launch.tasks import Command, train_command

def training_jobs(state: ExperimentState) -> list[TrainingJob]:
    return [TrainingJob(name=name, train=train_command, config=Command(command="true"),
                        repo=None, max_hours=1, max_usd=1) for name in ("small", "large")]

def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    tournament = Benchmark(name="progress", environment=environment("duel"), seatings=(
        Seating(name="vs fixed", players=(stored_inference(config={"strength": 1}),
                Code(name="fixed", config={"strength": 0})), configs=({},), games=1),))
    large = state.policy(PolicyKey(job="large")).checkpoints
    return [EvalJob(tournament=tournament, checkpoints=large, play=play, repo=None, max_hours=1)]
""")
    source = DirectoryStorage(tmp_path / "source")
    with pytest.raises(EtudeError, match="policy_not_found"):
        advance(
            read_experiment(file),
            source=source,
            pool="waiting",
            mode="queue",
            max_usd=Decimal("1.5"),
        )
    assert "over_budget/large@0  new, not queued: --max-usd 1.5" in capsys.readouterr().out
    assert b.scheduler.runs() == ()


def test_policy_key_cross_experiment_seats_split_seeds_and_do_not_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = DirectoryStorage(tmp_path / "source")
    for experiment, seed in (("tla-frontier", 0), ("tla-frontier", 1), ("rival", 0)):
        with track(
            experiment=experiment,
            run=f"small@{seed}",
            job="small",
            seed=seed,
            directory=source.root,
            clocks=["updates"],
            strict=True,
        ) as run:
            path = run.out / "weights.pt"
            path.write_text(f"{experiment} {seed}")
            run.checkpoint(path, updates=100)
    file = tmp_path / "rival.py"
    file.write_text("""
from etude.checkpoints import PolicyKey
from etude.eval.match import environment
from etude.eval.records import Benchmark, Final, Seating
from tests.eval.stored import stored_inference
from etude.jobs import EvalJob
from tests.eval.games import play

def eval_jobs(state):
    direct = stored_inference(config={"strength": 1})
    tournament = Benchmark(name="frontier", environment=environment("duel"), seatings=(
        Seating(name="vs frontier", players=(direct, Final(experiment="tla-frontier", job="small")),
                configs=({},), games=8),))
    return EvalJob(tournament=tournament, checkpoints=state.policy(PolicyKey()).checkpoints,
                   play=play, repo=None, max_hours=1)
""")
    measured = MeasuredStorage(source)
    decisions = advance(read_experiment(file), source=measured, directory=tmp_path / "evals")
    assert [(d.new, d.played) for d in decisions] == [(2, 2)]
    assert measured.listings == 2  # one discovery, one check of the eval run's own output prefix
    assert measured.requests == 12  # three declarations, writers, checkpoints and outcomes
    assert all(measured.got.count(key) == 1 for key in measured.got)
    assert not any(key.endswith("weights.pt") for key in measured.got)
    stored = StoredRuns.read("rival", source)
    matches = [record.match for folder in stored.folders() for record in stored.records(folder)]
    assert {
        tuple(
            player.checkpoint.key
            for player in match.players
            if isinstance(player, CheckpointPlayer)
        ): match.games
        for match in matches
    } == {
        ("rival/small@0/out/weights.pt", f"tla-frontier/small@{seed}/out/weights.pt"): 4
        for seed in (0, 1)
    }
    # The second pass reads delivery under experiment-qualified identities.
    advance(read_experiment(file), source=source, directory=tmp_path / "evals")
    assert "eval frontier: nothing to play" in capsys.readouterr().out
    # The report keeps tla-frontier's runs apart from rival's own run of the same name.
    assert {(player.run, player.line) for player in build(source.root, file).players} == {
        ("small@0", "small@0"),
        (None, "tla-frontier/small@0"),
        (None, "tla-frontier/small@1"),
    }


def test_policy_key_refuses_held_run_without_experiment(tmp_path: Path) -> None:
    # Unlabelled command jobs and historical records have no experiment provenance.
    checkpoint = CheckpointRecord(
        run="unlabelled",
        ordinal=1,
        at={},
        artifact=ArtifactRef(uri=(tmp_path / "weights").as_uri(), sha256="a" * 64, size_bytes=1),
    )
    job = EvalJob(
        tournament=Benchmark(name="held", environment=environment("duel")),
        checkpoints=(checkpoint,),
        play=play,
        repo=None,
        max_hours=1,
    )
    with pytest.raises(EtudeError, match="checkpoint_names_no_experiment"):
        with_experiment(job, "league")
