"""Explicit resumes keep one run and spend the remaining cumulative allowances."""

import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import default
from etude.spec import RunRequest, Until
from etude.storage import default_root
from etude.train.read import read
from tests.launch.counting import Counting
from tests.launch.fake import bench
from tests.launch.test_scheduler import job
from tests.launch.test_source import git


def test_resume_retains_allocations_and_uses_only_remaining_hours(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job(until=Until(clock="steps", value=10), config='{"lr": 0.1}')
    identity = b.scheduler.launch(RunRequest(job=spec, seed=7), rent="l4")
    b.run(601)
    first = b.scheduler.attempts(identity)[0]
    assert first.outcome == "succeeded"
    original_job = b.scheduler.record(identity).job
    changed = spec.model_copy(update={"until": Until(clock="steps", value=20)})
    request = RunRequest(job=changed, seed=7, id=identity, resume=True)
    assert b.scheduler.launch(request, rent="l4") == identity
    second = b.scheduler.attempts(identity)[1]
    assert second.number == 2 and second.limit_seconds == 3000
    assert b.scheduler.record(identity).job == original_job
    assert b.scheduler.attempts(identity)[0] == first
    b.run(601)
    seen = b.scheduler.read(identity)
    assert (seen.outcome, seen.attempts, seen.clocks) == ("succeeded", 2, {"steps": 20})
    assert seen.usd > Decimal("0.19")


@pytest.mark.parametrize("change", ["target", "seed", "config", "attempts", "hours", "dollars"])
def test_refused_resume_preserves_finished_run_and_has_no_external_effects(
    tmp_path: Path, change: str
) -> None:
    b = bench(tmp_path)
    spec = job(until=Until(clock="steps", value=10), config='{"lr": 0.1}')
    identity = b.scheduler.launch(RunRequest(job=spec, seed=7), rent="l4")
    b.run(601)
    prior = b.scheduler.record(identity)
    attempts = b.scheduler.attempts(identity)
    changed = spec.model_copy(update={"until": Until(clock="steps", value=20)})
    changes: dict[str, dict[str, object]] = {
        "target": {"until": spec.until},
        "config": {"config": '{"lr": 0.2}'},
        "seed": {},
        "attempts": {"max_attempts": 1},
        "hours": {"max_hours": 600 / 3600},
        "dollars": {"max_usd": Decimal("0.01")},
    }
    request = RunRequest(
        job=changed.model_copy(update=changes[change]),
        seed=8 if change == "seed" else 7,
        id=identity,
        resume=True,
    )
    counts = Counting(b.store, b.service)
    with counts.record(), pytest.raises(LauncherError) as error:
        b.scheduler.launch(request, rent="l4")
    observed = counts.snapshot()
    assert (observed.provider, observed.bucket, observed.ssh, observed.git) == (0, 0, 0, 0)
    if change == "config":
        assert "lr was" in str(error.value)
    assert b.scheduler.record(identity) == prior
    assert b.scheduler.attempts(identity) == attempts
    assert b.scheduler.read(identity).outcome == "succeeded"


def test_resume_refuses_a_live_allocation(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job()
    identity = b.scheduler.launch(spec, rent="l4")
    with pytest.raises(LauncherError, match="allocated attempt"):
        b.scheduler.launch(RunRequest(job=spec, id=identity, resume=True), rent="l4")
    assert len(b.scheduler.attempts(identity)) == 1


def test_resumed_failure_can_stop_and_retry_without_replaying_old_failures(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.outcome = lambda work: "failed"
    spec = job(max_attempts=4, until=Until(clock="steps", value=10))
    identity = b.scheduler.launch(spec, rent="l4")
    b.run(601)
    b.service.outcome = lambda work: (
        "stopped" if work.attempt == b.scheduler.attempts(identity)[1].id else "succeeded"
    )
    b.scheduler.queue(RunRequest(job=spec, id=identity, resume=True), rent="l4")
    b.scheduler.tick()
    b.run(601)
    b.run(601)
    assert [a.outcome for a in b.scheduler.attempts(identity)] == ["failed", "stopped", "succeeded"]
    assert b.scheduler.read(identity).outcome == "succeeded"


def test_cli_resume_runs_current_source_over_the_same_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # CLI and worker execute the real recipe; the source repository and origin are disposable.
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    origin = tmp_path / "origin"
    repo.mkdir()
    origin.mkdir()
    git(origin, "init", "--bare")
    git(repo, "init")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.test")
    git(repo, "remote", "add", "origin", str(origin))
    recipe = """import json, os
from pathlib import Path
from pydantic import BaseModel
from etude.train.records import Step
from etude.train.tracking import track
class State(BaseModel):
    steps: int = 0
with track(clocks=[Step("steps", "training steps")], config=json.loads(Path(os.environ["ETUDE_CONFIG"]).read_text())) as run:
    state = run.resume(extra=State())
    for step in run.steps("steps", to=2, save_every=1):
        state.steps = step
        run.log({"revision": 1}, steps=step)
        run.save()
"""
    (repo / "train.py").write_text(recipe)
    file = repo / "job.py"
    file.write_text(
        "from etude.launch.records import Job\n"
        "from pathlib import Path\n"
        "def runs() -> Job:\n"
        f"    return Job(command={sys.executable!r} + ' train.py', repo=str(Path(__file__).parent), "
        "max_hours=0.1, seeds=(7,), config='{\"rate\": 0.1}')\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "First recipe")
    first_commit = git(repo, "rev-parse", "HEAD")
    assert main(["launch", str(file)]) == 0
    scheduler = default()
    identity = scheduler.runs()[0].id
    assert scheduler.wait(identity, every=0.01, timeout=10)[0].outcome == "succeeded"
    assert main(["launch", str(file), "--resume"]) == 1
    assert "later target" in capsys.readouterr().err
    (repo / "train.py").write_text(recipe.replace('"revision": 1', '"revision": 2'))
    git(repo, "commit", "-am", "Revised recipe")
    next_commit = git(repo, "rev-parse", "HEAD")
    assert main(["launch", str(file), "--resume", "--until", "steps=4"]) == 0
    assert scheduler.wait(identity, every=0.01, timeout=10)[0].outcome == "succeeded"
    data = read(default_root(), "job", identity)
    assert [attempt.commit for attempt in data.attempts] == [first_commit, next_commit]
    points = data.points("revision", "steps")
    assert points.x.tolist() == [1, 2, 3, 4]
    assert points.y.tolist() == [1, 1, 2, 2]
    assert len(scheduler.runs(done=True)) == 1


def test_failed_resume_admission_leaves_the_previous_run_untouched(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job(until=Until(clock="steps", value=10))
    identity = b.scheduler.launch(spec, rent="l4")
    b.run(601)
    prior = b.scheduler.record(identity)
    b.scheduler.set_ondemand_max("l4", 0)
    request = RunRequest(
        job=spec.model_copy(update={"until": Until(clock="steps", value=20)}),
        id=identity,
        resume=True,
    )
    with pytest.raises(LauncherError, match="machine limit"):
        b.scheduler.launch(request, rent="l4")
    assert b.scheduler.record(identity) == prior
    assert b.scheduler.read(identity).outcome == "succeeded"
    assert len(b.scheduler.attempts(identity)) == 1


def test_resume_keeps_the_cohorts_spent_dollars(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job(until=Until(clock="steps", value=10), max_hours=600 / 3600)
    (identity,) = b.scheduler.launch_cohort(
        "experiment", [RunRequest(job=spec)], rent="l4", max_usd=Decimal("0.1")
    )
    b.run(601)
    prior = b.scheduler.record(identity)
    request = RunRequest(
        job=spec.model_copy(
            update={"max_hours": 1200 / 3600, "until": Until(clock="steps", value=20)}
        ),
        id=identity,
        resume=True,
    )
    with pytest.raises(LauncherError, match="cohort dollars"):
        b.scheduler.launch(request, rent="l4")
    assert b.scheduler.record(identity) == prior
    assert b.scheduler.read(identity).outcome == "succeeded"


def test_resumed_waiting_query_indexes_supply_and_allocation_number(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.outcome = lambda work: "failed"
    spec = job()
    identity = b.scheduler.launch(spec, rent="l4")
    b.run(601)
    b.scheduler.queue(RunRequest(job=spec, id=identity, resume=True), rent="l4")
    statements: list[str] = []
    b.store._db.set_trace_callback(statements.append)
    try:
        assert [run.id for run in b.store.waiting("l4")] == [identity]
    finally:
        b.store._db.set_trace_callback(None)
    plan = [row[3] for row in b.store._db.execute("EXPLAIN QUERY PLAN " + statements[0])]
    assert any("USING INDEX runs_pool" in step for step in plan)
    assert any("USING INDEX runs_rent" in step for step in plan)
    assert any("USING INDEX attempts_run (run=? AND number>?)" in step for step in plan)


def test_resume_can_replace_unallocated_queued_work(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job(command="first recipe")
    identity = b.scheduler.queue(spec, rent="l4")
    request = RunRequest(
        job=spec.model_copy(update={"command": "fixed recipe"}), id=identity, resume=True
    )
    assert b.scheduler.queue(request, rent="l4") == identity
    b.scheduler.tick()
    b.run(601)
    assert b.scheduler.read(identity).outcome == "succeeded"
    assert b.scheduler.record(identity).launch.spec.command == "fixed recipe"
    assert len(b.scheduler.attempts(identity)) == 1


def test_cli_selects_the_stored_job_and_seed_from_hand_built_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    spec = job(name="recipe", experiment="job", until=Until(clock="steps", value=10))
    identity = b.scheduler.launch(RunRequest(job=spec, seed=7), rent="l4")
    b.run(601)
    file = tmp_path / "job.py"
    file.write_text(
        "from etude.launch.records import Job, RunRequest\n"
        "def runs() -> list[RunRequest]:\n"
        f"    job = Job.model_validate_json({spec.model_dump_json()!r})\n"
        f"    return [RunRequest(job=job, seed=7, id={identity!r}), RunRequest(job=job, seed=8)]\n"
    )
    monkeypatch.setattr("etude.launch.cli.default", lambda: b.scheduler)
    assert (
        main(
            ["launch", str(file), "--seed", "7", "--resume", "--rent", "l4", "--until", "steps=20"]
        )
        == 0
    )
    b.run(601)
    assert b.scheduler.read(identity).clocks == {"steps": 20}
    assert b.scheduler.record(identity).seed == 7
    assert len(b.scheduler.runs(done=True)) == 1


def test_unallocated_resume_keeps_the_cohorts_machine_limit(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job()
    _, identity = b.scheduler.launch_cohort(
        "shared", spec.runs(1, 2), rent="l4", parallel=1, max_usd=Decimal(5)
    )
    prior = b.scheduler.record(identity)
    assert not b.scheduler.attempts(identity)
    with pytest.raises(LauncherError, match="cohort is at its machine limit"):
        b.scheduler.launch(RunRequest(job=spec, seed=2, id=identity, resume=True), rent="l4")
    assert b.scheduler.record(identity) == prior
    assert not b.scheduler.attempts(identity)
    assert b.service.creates == 1


def test_run_names_are_scoped_before_source_or_capacity_effects(tmp_path: Path) -> None:
    b = bench(tmp_path)
    first = RunRequest(job=job(experiment="one"), seed=7, id="baseline-seed-7")
    second = RunRequest(job=job(experiment="two"), seed=7, id="baseline-seed-7")
    assert b.scheduler.queue(first, rent="l4") == "one/baseline-seed-7"
    assert b.scheduler.queue(second, rent="l4") == "two/baseline-seed-7"
    assert [(item.experiment, item.id) for item in b.scheduler.runs()] == [
        ("one", "one/baseline-seed-7"),
        ("two", "two/baseline-seed-7"),
    ]
    counts = Counting(b.store, b.service)
    with counts.record(), pytest.raises(LauncherError, match="already admitted"):
        b.scheduler.launch(first, rent="l4")
    seen = counts.snapshot()
    assert (seen.provider, seen.bucket, seen.ssh, seen.git) == (0, 0, 0, 0)
    assert not b.scheduler.attempts("one/baseline-seed-7")
    plan = [
        row[3]
        for row in b.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM runs WHERE id = ?", ("one/baseline-seed-7",)
        )
    ]
    assert any("USING COVERING INDEX sqlite_autoindex_runs_1 (id=?)" in step for step in plan)


def test_scoped_run_reads_use_the_experiment_index_at_4096_records(tmp_path: Path) -> None:
    b = bench(tmp_path)
    specs = [job(experiment=f"e-{index}").model_dump_json() for index in range(128)]
    created = b.scheduler.now().isoformat()
    b.store._db.executemany(
        "INSERT INTO runs(id, job, spec, seed, rent, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (f"e-{index % 128}/r-{index}", "digest", specs[index % 128], 7, "l4", created)
            for index in range(4096)
        ],
    )
    counts = Counting(b.store, b.service)
    started = time.perf_counter()
    with counts.record():
        assert b.store.run("e-127/r-4095").id == "e-127/r-4095"
        records = b.store.runs(experiment="e-127")
    elapsed = time.perf_counter() - started
    assert len(records) == 32 and all(record.experiment == "e-127" for record in records)
    assert counts.snapshot().database == 2
    assert (
        counts.snapshot().provider,
        counts.snapshot().bucket,
        counts.snapshot().ssh,
        counts.snapshot().git,
    ) == (0, 0, 0, 0)
    plan = [
        row[3]
        for row in b.store._db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE json_extract(spec, '$.experiment') = ? ORDER BY seq",
            ("e-127",),
        )
    ]
    assert any("USING INDEX runs_experiment" in step for step in plan)
    print(f"4096 stored runs: existence + 32 scoped records, 2 SQL statements, {elapsed:.6f}s")
