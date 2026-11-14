"""One pass deduplicates addresses using only storage and public scheduler records."""

from decimal import Decimal
from pathlib import Path

import pytest

from etude.controller import advance
from etude.declarations import read_experiment
from etude.errors import LauncherError
from etude.jobs import TrainingJob
from etude.launch import client
from etude.launch.records import Pool
from etude.records import RunRecord
from etude.spec import Job, RunRequest, job_id
from etude.storage import DirectoryStorage
from etude.train.tracking import track
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench
from tests.launch.tasks import Command, train_command
from tests.run.storage import MeasuredStorage

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
