"""A stopped recipe spends attempts and budgets, retaining the run's out."""

from hashlib import sha256
from pathlib import Path

import pytest

from etude.launch.records import Input, Job
from etude.launch.worker.attempts import _lay_out
from tests.launch.fake import bench
from tests.launch.test_scheduler import job
from tests.launch.test_worker_attempts import local


def test_exit_75_retries_with_the_same_directory_and_setup_runs_again(tmp_path: Path) -> None:
    scheduler = local(tmp_path)
    run = scheduler.launch(
        Job(
            setup='echo setup >> "$ETUDE_OUT/setups"',
            command='if test -f "$ETUDE_OUT/state"; then cat "$ETUDE_OUT/state" > "$ETUDE_OUTPUT_DIR/result"; else echo saved > "$ETUDE_OUT/state"; exit 75; fi',
            max_hours=0.01,
        )
    )
    status = scheduler.wait(run, every=0.05, timeout=15)[0]
    assert status.outcome == "succeeded"
    assert [a.outcome for a in scheduler.attempts(run)] == ["stopped", "succeeded"]
    assert scheduler.fetch(run, "result", tmp_path / "fetched").read_text() == "saved\n"
    assert (tmp_path / "track" / run / "setups").exists() is False
    assert (tmp_path / "track" / run / "out/setups").read_text() == "setup\nsetup\n"


@pytest.mark.parametrize(
    ("phase", "command"), [("setup", "true"), ("cleanup", "true"), ("cleanup", "exit 75")]
)
def test_exit_75_outside_training_is_a_failure(tmp_path: Path, phase: str, command: str) -> None:
    scheduler = local(tmp_path)
    spec = Job(command=command, max_hours=0.01)
    run = scheduler.launch(spec.model_copy(update={phase: "exit 75"}))
    status = scheduler.wait(run, every=0.05, timeout=15)[0]
    assert (status.outcome, status.attempts) == ("failed", 1)


@pytest.mark.parametrize("ending", ["attempts", "hours", "cancel"])
def test_stopped_attempts_obey_remaining_limits(tmp_path: Path, ending: str) -> None:
    b = bench(tmp_path)
    b.service.outcome = lambda work: "stopped"
    run = b.scheduler.queue(
        job(
            max_attempts=1 if ending == "attempts" else 3,
            max_hours=600 / 3600 if ending == "hours" else 1,
        ),
        rent="l4",
    )
    b.scheduler.tick()
    b.run(1)
    if ending == "cancel":
        b.scheduler.cancel(run)
    b.run(600)
    status = b.scheduler.read(run)
    assert (
        status.outcome
        == {"attempts": "stopped", "hours": "out_of_time", "cancel": "cancelled"}[ending]
    )
    assert status.attempts == 1


def test_stopped_near_hours_limit_rents_again_for_the_remaining_time(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.seconds = 560
    b.service.outcome = lambda work: "stopped"
    run = b.scheduler.queue(job(max_hours=600 / 3600), rent="l4")
    b.scheduler.tick()
    b.run(600)
    assert [attempt.limit_seconds for attempt in b.scheduler.attempts(run)] == [600, 40]

    # The retry cannot complete its 560 seconds of work in the remaining 40.
    b.run(200)
    status = b.scheduler.read(run)
    assert (status.phase, status.outcome, status.attempts) == ("done", "out_of_time", 2)
    assert [attempt.outcome for attempt in b.scheduler.attempts(run)] == ["stopped", "out_of_time"]
    assert b.service.creates == 2


def test_failed_input_delivery_retries_the_whole_initial_directory(tmp_path: Path) -> None:
    body = b"saved optimizer state"
    seed = Input(
        name="seed",
        key="state",
        out="state.pt",
        sha256=sha256(body).hexdigest(),
        size_bytes=len(body),
    )
    b = bench(tmp_path)
    run = b.scheduler.queue(job(inputs=(seed,)), rent="l4")
    b.scheduler.tick()
    first = b.scheduler.attempts(run)[0]
    b.store.fail_before_start(first, "input_undelivered", b.clock())
    # The worker never claimed the first attempt: the second must still seed out.
    retry = first.model_copy(update={"id": "retry", "number": 2})
    work = b.scheduler._work(retry)
    assert work is not None
    session = tmp_path / "machine/sessions/retry"
    home = session / "attempts/retry"
    home.mkdir(parents=True)
    (session / "inputs").mkdir()
    (session / "inputs" / seed.sha256).write_bytes(body)
    environment, _ = _lay_out(session, home, work)
    assert (Path(environment["ETUDE_OUT"]) / "state.pt").read_bytes() == body
