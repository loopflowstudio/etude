"""One pass deduplicates addresses using only storage and public scheduler records."""

from decimal import Decimal
from pathlib import Path

import pytest

from etude.call import Function
from etude.cli import main
from etude.controller import advance
from etude.declarations import read_experiment
from etude.errors import LauncherError
from etude.eval.match import environment
from etude.eval.records import Benchmark
from etude.jobs import EvalJob, Evaluation, ExperimentState, TrainingJob
from etude.launch import client
from etude.launch.records import Pool
from etude.records import RunRecord
from etude.spec import Job, RunRequest, job_id
from etude.storage import DirectoryStorage
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


def test_wrong_eval_kind_refuses_before_training_is_submitted(tmp_path: Path) -> None:
    file = tmp_path / "two.py"
    file.write_text(FILE + "\ndef eval_jobs(state):\n    return training_jobs(state)\n")
    with pytest.raises(LauncherError, match="eval_jobs.*EvalJob"):
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
from etude.eval.records import Benchmark, Seating, Inference, Code
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
        seatings=(Seating(name="anchor", players=(Inference(config={{"search": 0}}),
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
        assert "queued two/ladder-" in second
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
    typed = EvalJob(
        tournament=tournament, play=play, experiment="two", workers=2, repo=None, max_hours=1
    )
    config = Evaluation(
        tournament=tournament,
        experiment="two",
        play=Function(module="tests.eval.games", name="play"),
        workers=2,
    )
    plain = Job(
        name="ladder",
        command="uv run python -m etude.call --function etude.jobs:_hold",
        config=config.model_dump_json(),
        repo=None,
        experiment="two",
        max_hours=1,
    )
    assert RunRequest(job=typed).model_dump_json() == RunRequest(job=plain).model_dump_json()
    assert job_id(typed) == job_id(plain)


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
