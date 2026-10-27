"""A job's preparation on a real local pool: kept, reused, rejected and rebuilt."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from etude.cli import main
from etude.launch.client import default
from etude.launch.records import Input, Job, Preparation, Runtime, Status
from etude.launch.scheduler import Scheduler
from etude.launch.worker.prepared import Receipt, evict, home
from tests.launch.counting import job_text
from tests.launch.test_worker_attempts import local

PREPARE = """
echo built >> "{count}"
cp lock.txt "$ETUDE_PREPARED_DIR/lock"
for name in $(ls "$ETUDE_PREPARED_INPUTS" 2>/dev/null); do echo "saw $name" >> "{count}"; done
"""
RUN = 'cat "$ETUDE_PREPARED_DIR/lock"'


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lock.txt").write_text("lock 1\n")
    (repo / "other.txt").write_text("other 1\n")
    _commit(repo)
    return repo


def _commit(repo: Path) -> None:
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "change"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)


def _input(tmp_path: Path, name: str, body: bytes) -> Input:
    """An input this machine already holds in a run's folder, so no storage is needed."""
    folder = tmp_path / "track" / "given"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    return Input(
        name=name,
        key=name,
        run="given",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def _job(repo: Path, count: Path, **changes: object) -> Job:
    job = Job(
        command=RUN,
        repo=str(repo),
        revision="HEAD",
        prepare=Preparation(command=PREPARE.format(count=count), paths=("lock.txt",)),
        max_hours=0.1,
        max_usd=Decimal(1),
        max_attempts=1,
    )
    return job.model_copy(update=changes)


def _launch(scheduler: Scheduler, job: Job) -> tuple[Status, str]:
    run = scheduler.launch(job)
    (status,) = scheduler.wait(run, every=0.05, timeout=60)
    return status, job_text(scheduler.logs(run=run))


def _outcomes(log: str) -> list[str]:
    return [line.split()[1] for line in log.splitlines() if line.startswith("prepare: ")]


def test_a_second_attempt_reuses_the_prepared_directory(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    first, log = _launch(scheduler, _job(repo, count))
    assert first.outcome == "succeeded" and _outcomes(log) == ["built"]
    second, log = _launch(scheduler, _job(repo, count))
    assert second.outcome == "succeeded" and _outcomes(log) == ["reused"]
    assert log.endswith("lock 1\n")
    assert count.read_text() == "built\n"  # the command ran once


def test_status_and_usage_say_what_a_second_attempt_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    repo, count = _repo(tmp_path), tmp_path / "count"
    weights = _input(tmp_path / "home", "weights", b"w" * 1000)
    job = _job(repo, count, inputs=(weights,))
    scheduler = default()
    runs = []
    for _ in range(2):
        status, _log = _launch(scheduler, job)
        assert status.outcome == "succeeded"
        runs.append(status.run)
    capsys.readouterr()

    for run, outcome in zip(runs, ("built", "reused"), strict=True):
        assert main(["status", run, "--phases"]) == 0
        # Each line is "RUN NAME AT (EVIDENCE) attempt=ID"; keep the names, in order.
        lines = capsys.readouterr().out.splitlines()
        said = [re.split(r" \d{4}-\d\d-\d\dT", line)[0] for line in lines]
        prepared = f"{run} prepared ({outcome})"
        assert said[-3:] == [f"{run} code started", prepared, f"{run} finished"]

    assert main(["usage", "--run", runs[1]]) == 0
    assert "input: received 0 bytes, reused 1000 bytes" in capsys.readouterr().out.splitlines()
    assert count.read_text() == "built\n"  # the command ran once


def test_only_declared_paths_and_inputs_decide_the_key(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    engine, weights = _input(tmp_path, "engine", b"engine 1"), _input(tmp_path, "weights", b"w1")
    prepare = Preparation(
        command=PREPARE.format(count=count), paths=("lock.txt",), inputs=("engine",)
    )
    job = _job(repo, count, prepare=prepare, inputs=(engine, weights))
    assert _outcomes(_launch(scheduler, job)[1]) == ["built"]
    assert count.read_text() == "built\nsaw engine\n"  # undeclared inputs are not visible

    (repo / "other.txt").write_text("other 2\n")
    _commit(repo)
    assert _outcomes(_launch(scheduler, job)[1]) == ["reused"]

    new_weights = _input(tmp_path, "weights2", b"w2").model_copy(update={"name": "weights"})
    assert _outcomes(
        _launch(scheduler, job.model_copy(update={"inputs": (engine, new_weights)}))[1]
    ) == ["reused"]

    new_engine = _input(tmp_path, "engine2", b"engine 2").model_copy(update={"name": "engine"})
    assert _outcomes(
        _launch(scheduler, job.model_copy(update={"inputs": (new_engine, weights)}))[1]
    ) == ["built"]

    (repo / "lock.txt").write_text("lock 2\n")
    _commit(repo)
    status, log = _launch(scheduler, job)
    assert _outcomes(log) == ["built"] and log.endswith("lock 2\n")


def test_reading_an_undeclared_input_fails_the_build(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    weights = _input(tmp_path, "weights", b"w1")
    prepare = Preparation(
        command='cat "$ETUDE_INPUTS_DIR/weights" > "$ETUDE_PREPARED_DIR/w"', paths=("lock.txt",)
    )
    status, log = _launch(local(tmp_path), _job(repo, count, prepare=prepare, inputs=(weights,)))
    assert status.outcome == "failed"
    assert "/weights" in log and "phase prepare: exit 1" in log
    assert not list(home().glob("*.json"))  # no receipt


def test_an_unknown_input_name_is_refused_at_launch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not given"):
        Job(
            command="true",
            max_hours=1,
            prepare=Preparation(command="true", paths=("lock.txt",), inputs=("engine",)),
        )


def test_a_changed_prepared_file_is_rejected_and_rebuilt(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    assert _outcomes(_launch(scheduler, _job(repo, count))[1]) == ["built"]
    (prepared,) = (p for p in home().iterdir() if p.is_dir())
    (prepared / "lock").chmod(0o644)
    (prepared / "lock").write_text("lock X\n")
    status, log = _launch(scheduler, _job(repo, count))
    assert status.outcome == "succeeded" and _outcomes(log) == ["rejected", "built"]
    assert "(lock digest mismatch)" in log and log.endswith("lock 1\n")


def test_a_build_stopped_at_its_deadline_leaves_nothing(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    stuck = Preparation(
        command='touch "$ETUDE_PREPARED_DIR/partial"; sleep 30', paths=("lock.txt",)
    )
    status, log = _launch(local(tmp_path), _job(repo, count, prepare=stuck, max_hours=2 / 3600))
    assert status.outcome == "out_of_time" and "phase prepare: deadline" in log
    assert not [p for p in home().iterdir() if p.suffix != ".lock"]


def test_a_killed_build_leaves_no_receipt_and_is_built_again(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    finish = tmp_path / "finish"
    command = (
        f'if [ -f "{finish}" ]; then touch "$ETUDE_PREPARED_DIR/done";'
        ' else touch "$ETUDE_PREPARED_DIR/partial"; sleep 5; fi'
    )
    prepare = Preparation(command=command, paths=("lock.txt",))
    run = scheduler.launch(_job(repo, count, prepare=prepare, command="true", max_attempts=2))
    for _ in range(300):
        partial = [p for p in home().glob("*/partial")] if home().exists() else []
        if partial:
            break
        time.sleep(0.05)
    (leftover,) = (p.parent for p in partial)
    assert not list(home().glob("*.json"))
    finish.touch()
    session = scheduler.attempts(run)[0].session
    os.kill(int((tmp_path / "sessions" / session / "receipt").read_text()), signal.SIGKILL)
    (status,) = scheduler.wait(run, every=0.05, timeout=60)
    assert (status.outcome, status.attempts) == ("succeeded", 2)
    assert _outcomes(job_text(scheduler.logs(run=run))) == ["built"]
    assert sorted(p.name for p in leftover.iterdir()) == ["done"]


def test_a_reused_runtime_is_still_checked_against_its_pins(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    venv = Preparation(
        command='python3 -m venv --without-pip "$ETUDE_PREPARED_DIR/.venv"',
        paths=("lock.txt",),
        python=".venv/bin/python",
    )
    job = _job(repo, count, prepare=venv, command='"$ETUDE_PYTHON" -c "print(7)"')
    status, log = _launch(scheduler, job)
    assert status.outcome == "succeeded" and log.endswith("7\n")
    pinned = Runtime(packages={"etude-sdk": "0.0.0"})
    status, log = _launch(scheduler, job.model_copy(update={"runtime": pinned}))
    assert status.outcome == "failed" and _outcomes(log) == ["reused"]
    assert "No package metadata was found for etude-sdk" in log


def test_reading_an_undeclared_source_file_fails_the_build(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    prepare = Preparation(
        command='cat lock.txt other.txt > "$ETUDE_PREPARED_DIR/both"', paths=("lock.txt",)
    )
    status, log = _launch(local(tmp_path), _job(repo, count, prepare=prepare))
    assert status.outcome == "failed"
    assert "other.txt" in log and "phase prepare: exit 1" in log
    assert not list(home().glob("*.json"))  # no receipt


def test_the_interpreter_is_named_once() -> None:
    prepare = Preparation(command="true", paths=("lock.txt",), python=".venv/bin/python")
    with pytest.raises(ValueError, match="name the job interpreter once"):
        Job(command="true", max_hours=1, prepare=prepare, runtime=Runtime(python="python3"))
    with pytest.raises(ValueError, match="name the job interpreter once"):
        Job(command="true", max_hours=1, runtime=Runtime(packages={"torch": "2.9.0"}))


def test_eviction_removes_the_least_recently_used_directory_not_one_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    for text in ("lock a\n", "lock b\n", "lock c\n"):
        (repo / "lock.txt").write_text(text)
        _commit(repo)
        _launch(scheduler, _job(repo, count))
    receipts = sorted(home().glob("*.json"), key=lambda p: p.stat().st_mtime)
    oldest, middle, newest = receipts
    assert Receipt.model_validate_json(newest.read_text()).size_bytes > 0
    with (home() / f"{middle.stem}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)  # an attempt is using it
        lines: list[str] = []
        # A full disk: keep half of what is held.
        monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        evict(lines.append)
    assert [p.name for p in home().glob("*.json")] == [middle.name]
    assert not (home() / oldest.stem).exists() and not (home() / newest.stem).exists()
    assert [line.split()[1] for line in lines] == ["evicted", "evicted"]


def test_an_unreadable_receipt_does_not_fail_another_build(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    home().mkdir(parents=True)
    (home() / f"{'0' * 64}.json").write_text("{not json")
    status, log = _launch(local(tmp_path), _job(repo, count))
    assert status.outcome == "succeeded" and _outcomes(log) == ["built"]
