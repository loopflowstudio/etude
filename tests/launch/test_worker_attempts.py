"""Real processes on this pool: the session side, its journal and its limits."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from etude.launch.database import Database
from etude.launch.executor import local as local_service
from etude.launch.records import Job, LauncherError, Machine, Pool, Run, Status
from etude.launch.scheduler import Scheduler
from etude.track.read import read, runs
from etude.track.report import page_data
from tests.launch.counting import job_text

TRAIN = """
echo "seed $ETUDE_SEED from ${ETUDE_FROM_VALUE:-0} to $ETUDE_UNTIL_VALUE with $(cat "$ETUDE_CONFIG")"
cat "${ETUDE_CHECKPOINT:-/dev/null}" > "$ETUDE_OUTPUT_DIR/checkpoint.pt"
echo "trained to $ETUDE_UNTIL_VALUE" >> "$ETUDE_OUTPUT_DIR/checkpoint.pt"
printf '{"clocks": {"%s": %s, "wall_time_ms": 1500}, "checkpoint": "checkpoint.pt"}' \\
  "$ETUDE_UNTIL_CLOCK" "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"
"""


def local(tmp_path: Path) -> Scheduler:
    service = local_service(tmp_path / "sessions")
    scheduler = Scheduler(
        Database(tmp_path / "launcher.db"), {"local": lambda pool, machine: service}
    )
    scheduler.add_pool(Pool(name="local", setup_seconds=30))
    scheduler._store.add_machine(Machine(name="local", pool="local", kind="local"))
    return scheduler


def until(scheduler: Scheduler, job: str, phase: str) -> Status:
    for _ in range(300):
        status = scheduler.status(job)
        if status.phase == phase:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job never reached {phase}: {status}")


@pytest.mark.parametrize("revision", ["HEAD", None])
def test_a_run_from_a_commit_continues_from_its_own_checkpoint(
    tmp_path: Path, revision: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.sh").write_text(TRAIN)
    for command in (
        ["init", "-q"],
        ["add", "train.sh"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "train"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    # Explicit HEAD ignores edits; the default executes the working-tree snapshot.
    (repo / "train.sh").write_text("exit 1\n" if revision else TRAIN + "echo snapshot\n")

    scheduler = local(tmp_path)
    spec = Job(
        command="sh train.sh",
        repo=str(repo),
        revision=revision,
        config="arm = large",
        until="agent_steps=26000",
        max_hours=0.1,
        max_usd=Decimal(1),
    )
    first = scheduler.launch(Run(job=spec, seed=3))
    (status,) = scheduler.wait(first, every=0.05, timeout=30)
    assert (status.outcome, status.rental_ended) == ("succeeded", None)
    assert status.clocks == {"agent_steps": 26_000, "wall_time_ms": 1500}
    suffix = "" if revision else "snapshot\n"
    assert (
        job_text(scheduler.logs(run=first)) == "seed 3 from 0 to 26000 with arm = large\n" + suffix
    )

    second = scheduler.launch(scheduler.continue_from(first, "agent_steps=63000"))
    (status,) = scheduler.wait(second, every=0.05, timeout=30)
    assert (status.outcome, status.clocks["agent_steps"]) == ("succeeded", 63_000)
    assert (
        job_text(scheduler.logs(run=second))
        == "seed 3 from 26000 to 63000 with arm = large\n" + suffix
    )
    copy = scheduler.fetch(second, "checkpoint.pt", tmp_path / "out")
    assert copy.read_text() == "trained to 26000\ntrained to 63000\n"


def test_runs_queue_through_one_machine_and_cancel_stops_the_running_one(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    running = scheduler.launch(Job(command="sleep 30", max_hours=0.1, max_usd=Decimal(1)))
    queued = scheduler.queue(Job(command="echo next", max_hours=0.1, max_usd=Decimal(1)))
    scheduler.tick()
    until(scheduler, running, "running")
    assert scheduler.read(queued).reason == "waiting for local: pool cannot make more machines"
    scheduler.cancel(running)
    done = scheduler.wait(running, queued, every=0.05, timeout=30)
    assert [s.outcome for s in done] == ["cancelled", "succeeded"]
    assert job_text(scheduler.logs(run=queued)) == "next\n"


def test_a_run_past_its_hours_is_ended_on_the_machine_with_nobody_watching(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    one = scheduler.launch(Job(command="sleep 30", max_hours=1.5 / 3600, max_usd=Decimal(1)))
    del scheduler  # nobody is watching while the limit passes
    time.sleep(3)
    (status,) = local(tmp_path).wait(one, every=0.05, timeout=30)
    assert (status.outcome, status.rental_ended) == ("out_of_time", None)


def test_a_killed_session_retries_its_run(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    one = scheduler.launch(Job(command="sleep 5", max_hours=0.1, max_usd=Decimal(1)))
    status = until(scheduler, one, "running")
    session = scheduler.attempts(one)[0].session
    os.kill(
        int((tmp_path / "sessions" / session / "receipt").read_text()),
        signal.SIGKILL,
    )
    status = until(scheduler, one, "done")
    assert (status.outcome, status.cause, status.attempts) == ("succeeded", None, 2)
    assert status.rental_ended is None
    assert scheduler._store.session(scheduler.attempts(one)[-1].session).ended_at is not None


def test_added_work_has_a_session_but_no_rental_or_session_deadline(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    # A pool's session ceiling must not shorten an owned run.
    scheduler._store._db.execute("UPDATE pools SET max_rental_seconds=1")
    first = scheduler.launch(Job(command="sleep 2; echo one", max_hours=0.01, max_usd=Decimal(1)))
    attempt = scheduler.attempts(first)[0]
    session = scheduler._store.session(attempt.session)
    assert attempt.machine == "local" and scheduler._store.rental_for(session) is None
    packet = json.loads((tmp_path / "sessions" / session.id / "packet.json").read_text())
    assert packet["deadline"] is None
    assert scheduler.rentals() == ()
    assert scheduler.wait(first, every=0.05, timeout=10)[0].outcome == "succeeded"
    assert job_text(scheduler.logs(run=first)) == "one\n"
    with pytest.raises(LauncherError, match="only machines you added"):
        scheduler.rent("local", hours=1)


TRACKED = """
import os
from etude.track.records import Step
from etude.track.run import init
run = init(experiment="capacity", clocks=[Step("steps", "training steps")])
for step in range(3):
    run.log({"loss": 1.0 / (step + 1)}, steps=step)
weights = os.path.join(os.environ["ETUDE_OUTPUT_DIR"], "weights.pt")
open(weights, "w").write("weights")
run.checkpoint(weights, steps=2)
open(weights, "w").write("the next training update")
run.finish()
"""


@pytest.mark.parametrize("filename", ["weights.pt", "steps-2.pt"])
def test_a_launched_job_tracks_as_its_run_without_naming_itself(
    tmp_path: Path, filename: str
) -> None:
    scheduler = local(tmp_path)
    (tmp_path / "train.py").write_text(TRACKED.replace('"weights.pt"', repr(filename)))
    job = Job(
        command=f"{sys.executable} {tmp_path / 'train.py'}",
        name="hidden256",
        max_hours=0.1,
        max_usd=Decimal(1),
    )
    ids = scheduler.queue_cohort("seeds", job.runs(0, 1), "local")
    assert [status.outcome for status in scheduler.wait(*ids, every=0.05, timeout=30)] == [
        "succeeded"
    ] * 2

    pages = []
    for record in [scheduler.record(run) for run in ids]:
        (launched,) = scheduler._store.attempts(run=record.id)
        # One place on the machine for every run; the script named no directory.
        tracked = tmp_path / "track"
        assert ("capacity", record.id) in runs(tracked)
        data = read(tracked, "capacity", record.id)
        pages.append(data)
        assert data.file is not None and data.rows == 3
        declared = data.file.declaration
        assert (declared.job, declared.job_name, declared.seed, declared.cohort) == (
            record.job,
            "hidden256",
            record.seed,
            "seeds",
        )
        (attempt,) = data.attempts
        assert attempt.attempt == launched.id
        # The script never wrote etude-result.json; recording the checkpoint told the launcher.
        # Named for where its first clock stood, so it sits beside the run's record of it.
        snapshot = "steps-2.pt.snapshot" if filename == "steps-2.pt" else "steps-2.pt"
        assert (launched.checkpoint, launched.clocks) == (snapshot, {"steps": 2})
        (checkpoint,) = data.checkpoints
        (kept,) = [output for output in launched.outputs if output.name == snapshot]
        assert checkpoint.artifact.sha256 == kept.sha256
        assert scheduler.fetch(record.id, snapshot, tmp_path / "saved").read_bytes() == b"weights"
        assert (tracked / record.id / "checkpoints" / snapshot).read_bytes() == b"weights"
    page = page_data("capacity", pages)
    rows = page["runs"]
    assert isinstance(rows, list) and {row["job"] for row in rows} == {"hidden256"}
