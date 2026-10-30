"""Real processes on this pool: the session side, its journal and its limits."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

from etude.cache import Artifact, Cache, Fetched
from etude.jobs import continue_from
from etude.launch.database import Database
from etude.launch.executor import local as local_service
from etude.launch.records import (
    Input,
    Job,
    LauncherError,
    Machine,
    Pool,
    Run,
    Status,
    job_identity,
)
from etude.launch.scheduler import Scheduler
from etude.run.listing import runs
from etude.train.read import read
from etude.train.records import Step
from etude.train.report import training
from etude.train.run import track
from tests.launch.counting import job_text

TRAIN = """
test -f "$ETUDE_SOURCE_DIR/train.sh" || exit 3
echo "seed $ETUDE_SEED to $ETUDE_UNTIL_VALUE with $(cat "$ETUDE_CONFIG")"
echo "trained to $ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/weights.pt"
printf '{"clocks": {"%s": %s, "wall_time_ms": 1500}}' \\
  "$ETUDE_UNTIL_CLOCK" "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"
"""


def local(tmp_path: Path) -> Scheduler:
    service = local_service(tmp_path / "sessions")
    scheduler = Scheduler(Database(tmp_path / "launcher.db"), {"local": lambda held: service})
    scheduler.add_pool(Pool(name="local"))
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
def test_a_run_from_a_commit_reports_progress_and_keeps_its_outputs(
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
        job_text(scheduler.logs(refresh=True, run=first))
        == "seed 3 to 26000 with arm = large\n" + suffix
    )
    copy = scheduler.fetch(first, "weights.pt", tmp_path / "out")
    assert copy.read_text() == "trained to 26000\n"


CONTINUED = """
import os
from pathlib import Path
from urllib.parse import urlparse
from etude.train.records import Step
from etude.train.run import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
latest = run.latest_checkpoint()
start = 0 if latest is None else int(latest.at["steps"])
kept = "" if latest is None else Path(urlparse(latest.artifact.uri).path).read_text()
target = int(os.environ["ETUDE_UNTIL_VALUE"])
for step in range(start + 1, target + 1):
    run.log({"loss": 1.0 / step}, steps=step)
weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.pt"
weights.write_text(kept + f"trained to {target}\\n")
run.checkpoint(weights, steps=target)
run.finish()
"""


def test_a_continuation_starts_from_its_parents_recorded_checkpoint(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    (tmp_path / "train.py").write_text(CONTINUED)
    spec = Job(
        command=f"{sys.executable} {tmp_path / 'train.py'}",
        until="steps=2",
        max_hours=0.1,
        max_usd=Decimal(1),
    )
    first = scheduler.launch(Run(job=spec, seed=3))
    assert scheduler.wait(first, every=0.05, timeout=30)[0].outcome == "succeeded"
    tracked = tmp_path / "track"
    with pytest.raises(LauncherError, match="not beyond the checkpoint's steps=2"):
        continue_from(scheduler.record(first), "steps=2", source=tracked)
    with pytest.raises(LauncherError, match="a clock its checkpoint recorded: steps"):
        continue_from(scheduler.record(first), "updates=9", source=tracked)

    # Tagging the parent's checkpoint later does not make the continuation another job.
    identity = job_identity(continue_from(scheduler.record(first), "steps=5", source=tracked).job)
    parent = track(
        experiment="capacity",
        clocks=[Step("steps", "training steps")],
        run=first,
        directory=tracked,
    )
    parent.tag(1, "best")
    parent.finish()
    assert read(tracked, "capacity", first).checkpoints[0].tags == ("best",)
    continued = continue_from(scheduler.record(first), "steps=5", source=tracked)
    assert job_identity(continued.job) == identity

    second = scheduler.launch(continued)
    (status,) = scheduler.wait(second, every=0.05, timeout=30)
    assert (status.outcome, status.clocks) == ("succeeded", {"steps": 5})
    data = read(tracked, "capacity", second)
    (checkpoint,) = data.checkpoints
    assert Path(urlparse(checkpoint.artifact.uri).path).read_text() == (
        "trained to 2\ntrained to 5\n"
    )
    assert data.file is not None and data.file.declaration.continues is not None
    continues = data.file.declaration.continues
    assert (continues.parent.source_id, continues.clock, continues.value) == (first, "steps", 2)
    assert scheduler.record(second).seed == 3


def test_runs_queue_through_one_machine_and_cancel_stops_the_running_one(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    running = scheduler.launch(Job(command="sleep 30", max_hours=0.1, max_usd=Decimal(1)))
    queued = scheduler.queue(Job(command="echo next", max_hours=0.1, max_usd=Decimal(1)))
    scheduler.tick()
    until(scheduler, running, "running")
    assert scheduler.read(queued).reason == "waiting for local: no free machine"
    scheduler.cancel(running)
    done = scheduler.wait(running, queued, every=0.05, timeout=30)
    assert [s.outcome for s in done] == ["cancelled", "succeeded"]
    assert job_text(scheduler.logs(refresh=True, run=queued)) == "next\n"


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
    first = scheduler.launch(Job(command="sleep 2; echo one", max_hours=0.01, max_usd=Decimal(1)))
    attempt = scheduler.attempts(first)[0]
    session = scheduler._store.session(attempt.session)
    assert attempt.machine == "local" and scheduler._store.rental_for(session) is None
    packet = json.loads((tmp_path / "sessions" / session.id / "packet.json").read_text())
    assert packet["deadline"] is None
    assert scheduler.rentals() == ()
    assert scheduler.wait(first, every=0.05, timeout=10)[0].outcome == "succeeded"
    assert job_text(scheduler.logs(refresh=True, run=first)) == "one\n"
    with pytest.raises(LauncherError, match="no on-demand named 'local'"):
        scheduler.rent("local", hours=1)


TRACKED = """
import os
from etude.train.records import Step
from etude.train.run import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
for step in range(3):
    run.log({"loss": 1.0 / (step + 1)}, steps=step)
weights = os.path.join(os.environ["ETUDE_OUTPUT_DIR"], "weights.pt")
open(weights, "w").write("weights")
run.checkpoint(weights, steps=2)
open(weights, "w").write("the next training update")
run.finish()
"""


def test_a_launched_job_tracks_as_its_run_without_naming_itself(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    (tmp_path / "train.py").write_text(TRACKED)
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

    found = []
    for record in [scheduler.record(run) for run in ids]:
        (launched,) = scheduler._store.attempts(run=record.id)
        # One place on the machine for every run; the script named no directory.
        tracked = tmp_path / "track"
        assert ("capacity", record.id) in runs(tracked)
        data = read(tracked, "capacity", record.id)
        found.append(data)
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
        # The script never wrote etude-result.json; tracking reported its progress.
        assert launched.clocks == {"steps": 2}
        (checkpoint,) = data.checkpoints
        # Named for where its first clock stood; the launcher holds no copy of it.
        stored = tracked / record.id / "checkpoints" / "steps-2.pt"
        assert stored.read_bytes() == b"weights"
        assert checkpoint.artifact.sha256 == hashlib.sha256(b"weights").hexdigest()
        assert [o.name for o in launched.outputs] == ["etude-result.json", "weights.pt"]
    # The report labels the job as its author named it, with its seed count.
    (drawn,) = training(found).jobs
    assert drawn.name == "hidden256" and drawn.label.startswith("hidden256 · ")


class _Weights:
    """Bytes already on this machine, so a test needs no storage."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        sha256 = hashlib.sha256(body).hexdigest()
        self.artifact = Artifact(
            location="https://lab.example/weights", sha256=sha256, size_bytes=len(body)
        )
        self.input = Input(name="weights", key="weights", sha256=sha256, size_bytes=len(body))

    def read(self, start: int, end: int | None, etag: str | None) -> Fetched:
        return Fetched(body=self.body[start : None if end is None else end + 1], etag=None)

    def changed(self, etag: str) -> Fetched | None:
        return None

    def reused(self, size_bytes: int, byte_range: str | None) -> None:
        pass


def test_an_attempt_links_its_held_inputs_read_only_without_copying(tmp_path: Path) -> None:
    weights = _Weights(b"weights " * 1000)
    held = Cache().hold(weights.artifact, weights, holder="session", staging=tmp_path / "x")
    scheduler = local(tmp_path)
    one = scheduler.launch(
        Job(
            command='cat "$ETUDE_INPUTS_DIR/weights" | wc -c',
            inputs=(weights.input,),
            max_hours=0.1,
            max_usd=Decimal(1),
        )
    )
    (status,) = scheduler.wait(one, every=0.05, timeout=30)
    assert status.outcome == "succeeded"
    assert job_text(scheduler.logs(refresh=True, run=one)).split()[-1] == "8000"
    attempt = scheduler.attempts(one)[0]
    given = tmp_path / "sessions" / attempt.session / "attempts" / attempt.id / "inputs/weights"
    assert given.stat().st_ino == held.stat().st_ino  # one copy on this machine
    assert given.stat().st_mode & 0o777 == 0o444


def test_a_corrupted_held_input_is_refused_before_the_job_starts(tmp_path: Path) -> None:
    weights = _Weights(b"weights " * 1000)
    held = Cache().hold(weights.artifact, weights, holder="session", staging=tmp_path / "x")
    held.chmod(0o644)
    held.write_bytes(b"W" + weights.body[1:])
    scheduler = local(tmp_path)
    one = scheduler.launch(
        Job(
            command="touch started",
            inputs=(weights.input,),
            max_hours=0.1,
            max_usd=Decimal(1),
            max_attempts=1,
        )
    )
    (status,) = scheduler.wait(one, every=0.05, timeout=30)
    assert (status.outcome, status.cause) == ("lost", "input_undelivered")
    assert scheduler.attempts(one)[0].started_at is None
    assert not held.exists()  # the corrupt copy is discarded, not trusted again
