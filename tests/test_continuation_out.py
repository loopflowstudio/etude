"""Typed recipes continue from full state while the parent keeps its own files."""

import json
import sys
from hashlib import sha256
from importlib import import_module
from pathlib import Path

import pytest

from etude.cli import main
from etude.jobs import continue_from, continued_config
from etude.launch.errors import LauncherError
from etude.launch.job_file import read_job_file
from etude.launch.records import Input, Job
from etude.run.delivery import sync
from etude.run.records import Attempt
from etude.run.storage import DirectoryStorage
from etude.train.read import read
from etude.train.records import ParentRun, Step
from etude.train.report import training
from etude.train.run import track
from tests.launch.counting import job_text
from tests.launch.test_worker_attempts import local
from tests.run.storage import MeasuredStorage
from tests.test_jobs import checkout, job_python

__all__ = ["job_python"]  # fixture shared with typed-job tests

RECIPE = """
from pydantic import BaseModel
from etude.train.run import Stopped, track
from etude.train.records import Step

class Config(BaseModel):
    updates: int = 2
    rate: float = 0.125

def train(config: Config) -> None:
    with track(clocks=[Step("updates", "updates")], config=config.model_dump()) as run:
        updates = int(run.until("updates", default=config.updates))
        state = run.out / "state.json"
        if not state.exists():
            state.write_text('1')
            run.log({"loss": 1}, updates=1)
            raise Stopped
        previous = int(state.read_text())
        print(f"restored {previous} to {updates}, rate {config.rate}")
        state.write_text(str(updates))
        model = run.out / f"model-{updates}.pt"
        model.write_bytes(b"weights")
        run.log({"loss": 0}, updates=updates)
        run.checkpoint(model, updates=updates)
"""


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("carried", [False, True])
def test_continuation_and_report_use_the_same_position_after_an_empty_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool, carried: bool
) -> None:
    scheduler = local(tmp_path)
    parent = scheduler.queue(Job(command="train", max_hours=1), "local")
    source = tmp_path / "track"
    monkeypatch.setenv("ETUDE_OUT", str(source / parent / "out"))
    with track(
        experiment="recipe", run=parent, clocks=[Step("updates", "updates")], directory=source
    ) as run:
        (run.out / "state").write_bytes(b"optimizer state")
        (run.out / "model").write_bytes(b"weights")
        run.checkpoint(run.out / "model", updates=6)
        run.log({"loss": 1}, updates=6)
    data = read(source, "recipe", parent)
    # Old records can declare a resume point before logging; new empty attempts cannot.
    attempt = Attempt(
        writer="abcdef123456",
        host="fixture",
        started_ms=data.attempts[-1].started_ms + 1,
        resumed_at={"updates": 4} if historical else {},
        first_row=not historical,
    )
    (source / parent / "metrics/abcdef123456.json").write_text(attempt.model_dump_json())
    position = 4 if historical else 6
    destination = DirectoryStorage(tmp_path / "bucket" if carried else source)
    if carried:
        assert sync(source, storages=[destination]) == {}
    storage = MeasuredStorage(destination)
    child = continue_from(scheduler.record(parent), f"updates={position + 1}", source=storage)
    (lineage,) = child.job.environment
    assert ParentRun.model_validate_json(lineage.value).at == {"updates": position}
    assert storage.listings == 2
    assert storage.got.count(f"{parent}/out.json") == int(carried)
    out_reads = [key for key in storage.got if key.startswith(f"{parent}/out/")]
    assert out_reads == ([] if carried else [f"{parent}/out/model", f"{parent}/out/state"])
    assert {i.key: (i.sha256, i.size_bytes) for i in child.job.inputs} == {
        "out/model": (sha256(b"weights").hexdigest(), len(b"weights")),
        "out/state": (sha256(b"optimizer state").hexdigest(), len(b"optimizer state")),
    }
    assert training([read(source, "recipe", parent)]).runs[0].reached == {"updates": position}


def test_typed_stop_and_continuation_restore_full_state_and_only_change_target(
    tmp_path: Path,
    job_python: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = checkout(
        tmp_path / "repo",
        {
            "recipe_continuation.py": RECIPE,
            "job.py": """
from etude.jobs import training
from etude.launch.records import Job, Runtime
from recipe_continuation import Config, train

def runs(python: str) -> Job:
    return training(train, config=Config(), repo=None, max_hours=0.1,
                    runtime=Runtime(python=python))
""",
        },
    )
    # The file reader imports the recipe; include its source in the launched job.
    monkeypatch.setattr(sys, "path", list(sys.path))
    (given,) = read_job_file(repo / "job.py", ["--python", job_python])
    spec = given.job.model_copy(
        update={"repo": str(repo), "experiment": "recipe", "until": "updates=3"}
    )
    scheduler = local(tmp_path)
    parent = scheduler.launch(spec)
    status = scheduler.wait(parent, every=0.05, timeout=30)[0]
    assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=parent))
    assert [a.outcome for a in scheduler.attempts(parent)] == ["stopped", "succeeded"]
    tracked = tmp_path / "track"
    source = MeasuredStorage(DirectoryStorage(tracked))
    record = scheduler.record(parent)
    config = continued_config(record, '{"rate":0.25}')
    assert json.loads(config.model_dump_json()) == {"updates": 2, "rate": 0.25}
    with pytest.raises(LauncherError, match="unknown continuation config fields"):
        continued_config(record, '{"typo":4}')
    with pytest.raises(LauncherError, match="invalid continuation config"):
        continued_config(record, '{"updates":"no"}')
    for changes in ("[]", '{"updates":', ""):
        with pytest.raises(LauncherError, match="invalid continuation config"):
            continued_config(record, changes)
    next_run = continue_from(record, "updates=4", source=source)
    assert next_run.job.config == record.spec.config
    assert source.listings == 2
    assert source.got.count(f"{parent}/out/state.json") == 1
    assert source.got.count(f"{parent}/out/model-3.pt") == 1
    assert len([i for i in next_run.job.inputs if i.out is not None]) == 2
    child = scheduler.launch(next_run)
    status = scheduler.wait(child, every=0.05, timeout=30)[0]
    assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=child))
    assert (tracked / parent / "out/state.json").read_text() == "3"
    assert (tracked / child / "out/state.json").read_text() == "4"
    data = read(tracked, "recipe", child)
    assert data.reached == {"updates": 4}
    assert data.file is not None and data.file.declaration.continues is not None
    assert data.file.declaration.continues.parent.source_id == parent
    assert data.file.declaration.continues.value == 3
    changed = continue_from(record, "updates=5", source=tracked, config=config)
    assert json.loads(changed.job.config or "{}") == {"updates": 2, "rate": 0.25}
    chosen = continue_from(record, "updates=5", source=tracked, ordinal=1)
    assert len(chosen.job.inputs) == 1
    assert chosen.job.inputs[0].key == "out/model-3.pt"
    assert chosen.job.inputs[0].out is None
    original = (
        Input(name="parent-out-0", key="data", sha256="0" * 64, size_bytes=0),
        Input(name="parent-checkpoint.pt", key="more-data", sha256="1" * 64, size_bytes=0),
    )
    named = record.model_copy(update={"spec": record.spec.model_copy(update={"inputs": original})})
    for ordinal in (None, 1):
        again = continue_from(named, "updates=6", source=tracked, ordinal=ordinal)
        assert again.job.inputs[:2] == original
        assert len({i.name for i in again.job.inputs}) == len(again.job.inputs)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: scheduler)
    capsys.readouterr()
    assert (
        main(
            [
                "continue",
                "--run",
                parent,
                "--until",
                "updates=6",
                "--source",
                str(tracked),
            ]
        )
        == 0
    )
    queued = capsys.readouterr().out.split()[0]
    assert scheduler.record(queued).spec.config == record.spec.config
    assert scheduler.read(queued).phase == "queued"
    status = scheduler.wait(queued, every=0.05, timeout=30)[0]
    assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=queued))
    assert read(tracked, "recipe", queued).reached == {"updates": 6}
    assert (tracked / queued / "out/state.json").read_text() == "6"
    assert (tracked / parent / "out/state.json").read_text() == "3"
