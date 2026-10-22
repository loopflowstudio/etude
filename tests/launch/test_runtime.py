"""A selected job environment is reused, checked and kept separate from the client."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from etude.launch.records import Cause, Job, Outcome, Runtime, job_identity
from etude.launch.runtime import RuntimeInfo, select_runtime
from etude.launch.schemas import Packet, Work
from etude.launch.worker import attempts
from tests.launch.counting import job_text
from tests.launch.test_worker_attempts import local


@pytest.fixture
def python(tmp_path: Path) -> str:
    directory = tmp_path / "job-env"
    subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable, str(directory)],
        check=True,
        capture_output=True,
    )
    return str(directory / "bin/python")


def test_selected_runtime_runs_without_dependency_installation(tmp_path: Path, python: str) -> None:
    scheduler = local(tmp_path)
    command = """"$ETUDE_PYTHON" -c 'import json,os,sys; print("selected="+sys.executable); print("environment="+os.environ["VIRTUAL_ENV"])' """
    run = scheduler.launch(
        Job(command=command, max_hours=0.01, runtime=Runtime(python=python, python_version="3.12"))
    )
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    text = job_text(scheduler.logs(run=run))
    assert status.outcome == "succeeded"
    assert "selected=" + python in text
    assert "environment=" + str(Path(python).parent.parent) in text
    assert '"version":"3.12.' in text and '"packages":{}' in text


@pytest.mark.parametrize("mismatch", ["python", "package", "client"])
def test_runtime_failure_does_not_start_or_retry_training(
    tmp_path: Path, python: str, mismatch: str
) -> None:
    runtime = Runtime(
        python=sys.executable if mismatch == "client" else python,
        python_version="0.0" if mismatch == "python" else None,
        packages={"missing-etude-fixture": "1"} if mismatch == "package" else {},
    )
    scheduler = local(tmp_path)
    marker = tmp_path / "training-started"
    run = scheduler.launch(Job(command=f'touch "{marker}"', max_hours=0.01, runtime=runtime))
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    assert status.outcome == "failed" and not marker.exists()
    attempts = scheduler.attempts(run)
    assert len(attempts) == 1 and attempts[0].started_at is None
    assert "runtime" in job_text(scheduler.logs(run=run))


def test_runtime_is_part_of_job_identity_and_package_order_is_not() -> None:
    original = Job(command="train", max_hours=1)
    one = original.model_copy(
        update={"runtime": Runtime(python="/job/python", packages={"a": "1", "b": "2"})}
    )
    two = original.model_copy(
        update={"runtime": Runtime(python="/job/python", packages={"b": "2", "a": "1"})}
    )
    assert job_identity(original) != job_identity(one)
    assert json.dumps(job_identity(one), sort_keys=True) == json.dumps(
        job_identity(two), sort_keys=True
    )


def test_cuda_constraint_is_the_build_not_driver_version() -> None:
    info = RuntimeInfo(
        python="/job/bin/python",
        version="3.12.9",
        prefix="/job",
        base_prefix="/usr",
        packages={"torch": "2.10.0+cu128"},
        cuda="12.8",
    )
    runtime = Runtime(python=info.python, packages=info.packages, cuda="13.1")
    with pytest.raises(ValueError, match="CUDA build mismatch: expected 13.1, found 12.8"):
        select_runtime(runtime, info.model_dump_json().encode(), {})


def test_runtime_probe_shares_attempt_deadline(tmp_path: Path) -> None:
    interpreter = tmp_path / "slow-python"
    interpreter.write_text("#!/bin/sh\nexec sleep 30\n")
    interpreter.chmod(0o755)
    scheduler = local(tmp_path)
    marker = tmp_path / "training"
    run = scheduler.launch(
        Job(
            command=f'touch "{marker}"',
            max_hours=1 / 3600,
            runtime=Runtime(python=str(interpreter)),
        )
    )
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    assert status.outcome == "out_of_time" and not marker.exists()
    assert len(scheduler.attempts(run)) == 1
    assert scheduler.attempts(run)[0].started_at is None


def test_failure_diagnostic_precedes_completion_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    work = Work(
        attempt="attempt",
        run="run",
        command="echo never",
        runtime=Runtime(python="/missing/job-python"),
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )
    packet = Packet(session="session", deadline=None, work=(work,))
    published: list[str] = []
    finish = attempts._finish

    def observe_completion(
        home: Path, outcome: Outcome, cause: Cause | None = None, code: int | None = None
    ) -> None:
        published.append((home / "log").read_text())
        finish(home, outcome, cause, code)

    monkeypatch.setattr(attempts, "_finish", observe_completion)
    attempts._run(session, packet, work)
    assert (
        len(published) == 1 and "job interpreter" in published[0] and "unavailable" in published[0]
    )
