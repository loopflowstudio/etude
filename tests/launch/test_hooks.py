"""Setup and cleanup are observable attempt phases with bounded, explicit replay."""

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from etude.launch.records import Job, job_identity
from etude.launch.schemas import Packet, Progress, Work
from etude.launch.worker.attempts import _run
from tests.launch.counting import job_text
from tests.launch.test_worker_attempts import local


@pytest.mark.parametrize(
    ("setup", "command", "cleanup", "outcome", "code", "trained"),
    [
        ("echo prepared", "echo trained", "echo cleaned", "succeeded", 0, True),
        ("echo rejected; exit 7", "echo trained", "echo cleaned", "failed", 7, False),
        ("echo prepared", "echo trained; exit 8", "echo cleanup-error; exit 9", "failed", 8, True),
        (None, "echo trained", "echo cleanup-error; exit 9", "failed", 9, True),
    ],
)
def test_phases_keep_original_failure_and_never_replay_user_errors(
    tmp_path: Path,
    setup: str | None,
    command: str,
    cleanup: str,
    outcome: str,
    code: int,
    trained: bool,
) -> None:
    scheduler = local(tmp_path)
    run = scheduler.launch(Job(setup=setup, command=command, cleanup=cleanup, max_hours=0.01))
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    text = job_text(scheduler.logs(run=run))
    assert status.outcome == outcome
    attempts = scheduler.attempts(run)
    assert len(attempts) == 1 and attempts[0].exit_code == code
    assert ("trained\n" in text) == trained
    assert "phase cleanup: started" in text
    if setup is not None:
        assert text.index("phase setup:") < text.index("phase cleanup:")


def test_cancellation_runs_cleanup_without_restarting_setup(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    ready = tmp_path / "ready"
    run = scheduler.launch(
        Job(
            setup="echo prepared",
            command=f'touch "{ready}"; sleep 30',
            cleanup='echo cleaned > "$ETUDE_OUTPUT_DIR/cleanup.txt"',
            max_hours=0.01,
        )
    )
    for _ in range(200):
        if ready.exists():
            break
        time.sleep(0.02)
    assert ready.exists()
    scheduler.cancel(run)
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    assert status.outcome == "cancelled"
    assert scheduler.fetch(run, "cleanup.txt", tmp_path / "out").read_text() == "cleaned\n"
    assert job_text(scheduler.logs(run=run)).count("prepared\n") == 1


def test_cleanup_can_follow_job_limit_but_cannot_run_unbounded(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    run = scheduler.launch(
        Job(
            command="sleep 30",
            cleanup="echo cleanup-began; sleep 30; echo too-late",
            cleanup_seconds=0.2,
            max_hours=1 / 3600,
        )
    )
    status = scheduler.wait(run, every=0.05, timeout=10)[0]
    assert status.outcome == "out_of_time"
    text = job_text(scheduler.logs(run=run))
    assert "cleanup-began\n" in text and "too-late" not in text
    assert "phase cleanup: deadline" in text


def test_cleanup_never_extends_paid_window(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    marker = tmp_path / "cleanup-ran"
    work = Work(
        attempt="attempt",
        run="run",
        command="sleep 30",
        cleanup=f'touch "{marker}"',
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )
    packet = Packet(
        session="session",
        deadline=datetime.now(UTC) + timedelta(seconds=0.2),
        work=(work,),
        cap_usd=Decimal(1),
    )
    _run(session, packet, work)
    home = session / "attempts/attempt"
    assert not marker.exists()
    text = (home / "log").read_text()
    assert "phase cleanup: deadline" in text
    record = Progress.model_validate(
        {
            "attempt": "attempt",
            "started_at": None,
            **json.loads((home / "finished.json").read_text()),
        }
    )
    assert record.outcome == "lost" and record.cause == "machine_lost"


def test_hooks_affect_identity_but_cleanup_allowance_does_not() -> None:
    original = Job(command="train", max_hours=1)
    setup = original.model_copy(update={"setup": "prepare"})
    cleanup = original.model_copy(update={"cleanup": "collect"})
    assert len({str(job_identity(j)) for j in (original, setup, cleanup)}) == 3
    assert job_identity(cleanup) == job_identity(cleanup.model_copy(update={"cleanup_seconds": 60}))
