"""Experiments end to end: eval(state) chooses checkpoints, and each pass plays only what is new."""

from __future__ import annotations

import json
import shutil
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Benchmark, Code, Inference, Seating
from etude.eval.tournament import scores
from etude.experiment import (
    Checkpoint,
    Experiment,
    ExperimentState,
    advance,
    observe,
    read_experiment,
)
from etude.jobs import _hold, evaluation, evaluation_spec
from etude.launch.records import Job, Outcome, Run, Status
from etude.run.errors import EtudeError
from etude.run.records import Clock
from etude.run.storage import DirectoryStorage
from etude.train.read import read
from etude.train.run import Run as Run_
from etude.train.run import track
from tests.eval.games import play, play_catching_errors, play_failing_deal_one
from tests.experiments import authored

WORLD = environment("duel", config={"world": "w6"})
DEALS = tuple({"deal": deal} for deal in range(2))
DIRECT = Inference(config={"search": 0})


def heuristic(tmp_path: Path) -> Code:
    script = tmp_path / "players" / "heuristic.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text("STRENGTH = 0\n")
    return Code(name="heuristic", source=[script], config={"strength": 0})


def trainer(bucket: DirectoryStorage, tmp_path: Path) -> Run_:
    return track(
        experiment="ppo",
        run="ppo-1",
        clocks=[Clock(name="updates", kind="step", meaning="optimizer updates")],
        directory=tmp_path / "trainer",
        storages=[bucket],
    )


def save(run: Run_, bucket: DirectoryStorage, tmp_path: Path, saves: range) -> None:
    """Checkpoints at 100 updates per ordinal, delivered as a training run delivers them."""
    for ordinal in saves:
        model = tmp_path / "latest.pt"
        model.write_bytes(f"weights {ordinal}".encode())
        run.checkpoint(model, updates=100 * ordinal)
    # Records are written in the background: deliver until the bucket has the last one.
    deadline = time.monotonic() + 30
    while saves and not any(c.ordinal == saves[-1] for c in _saved(run, bucket)):
        assert time.monotonic() < deadline, "checkpoints never delivered"
        run.ship()
        time.sleep(0.01)


def _saved(run: Run_, bucket: DirectoryStorage) -> tuple[Checkpoint, ...]:
    found = [each for each in observe(bucket, run.experiment).runs if each.name == run.name]
    return found[0].checkpoints if found else ()


def design(tmp_path: Path) -> Experiment:
    """Every second checkpoint against the heuristic, each against the previous one, and
    the final checkpoint against the first once training has finished."""
    anchor = heuristic(tmp_path)

    def eval_(state: ExperimentState) -> Job:
        run = state.run("ppo-1")
        rated = run.checkpoints.every(2)
        pairs = [
            Seating(name=f"{new.name} vs {old.name}", tags=("consecutive",),
                    players=(new.model(DIRECT), old.model(DIRECT)), conditions=DEALS, games=2)
            for old, new in zip(rated, rated[1:])
        ]  # fmt: skip
        if run.final is not None and run.checkpoints:
            first = run.checkpoints[0]
            pairs.append(
                Seating(
                    name="final vs first",
                    players=(run.final.model(DIRECT), first.model(DIRECT)),
                    conditions=DEALS,
                    games=2,
                )  # fmt: skip
            )
        anchors = Seating(name="vs heuristic", players=(DIRECT, anchor), conditions=DEALS,
                          games=2)  # fmt: skip
        progress = Benchmark(name="ladder", environment=WORLD, seatings=(anchors, *pairs))
        return evaluation(progress, play=play, checkpoints=rated, max_hours=0.1)

    return Experiment(name="ppo", eval=eval_)


def test_each_pass_plays_what_the_checkpoints_saved_so_far_call_for(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    eval_ = design(tmp_path)
    local = tmp_path / "evaluator"

    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 4))
    state = observe(bucket, "ppo")
    assert observe(bucket, "another").runs == ()
    assert [c.ordinal for c in state.run("ppo-1").checkpoints.every(2)] == [2]
    assert state.run("ppo-1").final is None
    (first,) = advance(eval_, source=bucket, directory=local)
    # Checkpoint 2 against the heuristic; no pair yet.
    assert (first.new, first.played) == (len(DEALS), len(DEALS))

    save(training, bucket, tmp_path, range(4, 6))
    training.finish(timeout=30)
    (second,) = advance(eval_, source=bucket, directory=local)
    # Checkpoint 4 against the heuristic, 4 against 2, and the final (5) against the first.
    assert (second.new, second.played) == (3 * len(DEALS), 3 * len(DEALS))
    (third,) = advance(eval_, source=bucket, directory=local)
    assert (third.new, third.played) == (0, 0)

    db = statsdb("ppo", bucket)
    assert sorted(db.sql("select distinct seating from schedule").fetchall()) == [
        ("final vs first",), ("updates-400.pt vs updates-200.pt",), ("vs heuristic",)
    ]  # fmt: skip
    assert db.sql(
        "select distinct seating from schedule where list_contains(tags, 'consecutive')"
    ).fetchall() == [("updates-400.pt vs updates-200.pt",)]
    (line,) = scores(db, "ladder", table="plays", value="won")
    assert [p.ordinal for p in line.points] == [2, 4]
    db.close()

    # After training, another design over every checkpoint: what the ladder already
    # played is the same match, so only checkpoints 1, 3 and 5 are played.
    anchor = heuristic(tmp_path)

    def post_hoc(state: ExperimentState) -> Job:
        every = Seating(name="vs heuristic", players=(DIRECT, anchor), conditions=DEALS, games=2)
        sweep = Benchmark(name="sweep", environment=WORLD, seatings=(every,))
        return evaluation(sweep, play=play, checkpoints=state.run("ppo-1").checkpoints,
                          max_hours=0.1)  # fmt: skip

    (later,) = advance(Experiment(name="ppo", eval=post_hoc), source=bucket, directory=local)
    assert (later.new, later.played) == (3 * len(DEALS), 3 * len(DEALS))
    with statsdb("ppo", bucket) as db:
        (swept,) = scores(db, "sweep", table="plays", value="won")
    assert [p.ordinal for p in swept.points] == [1, 2, 3, 4, 5]


def test_a_pass_plays_only_what_eval_returns_now(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 2))
    anchor = heuristic(tmp_path)
    greedy = Code(
        name="greedy", source=[tmp_path / "players" / "heuristic.py"], config={"strength": 4}
    )
    both = (
        Seating(name="vs heuristic", players=(DIRECT, anchor), conditions=DEALS, games=2),
        Seating(name="vs greedy", players=(DIRECT, greedy), conditions=DEALS, games=2),
    )

    def design(seatings: tuple[Seating, ...]) -> Experiment:
        def eval_(state: ExperimentState) -> Job:
            ladder = Benchmark(name="ladder", environment=WORLD, seatings=seatings)
            return evaluation(ladder, play=play, checkpoints=state.run("ppo-1").checkpoints,
                              max_hours=0.1)  # fmt: skip

        return Experiment(name="ppo", eval=eval_)

    local = tmp_path / "evaluator"
    (first,) = advance(design(both), source=bucket, directory=local)
    assert first.played == 2 * len(DEALS)
    # Greedy is dropped: a new checkpoint plays only against the heuristic.
    save(training, bucket, tmp_path, range(2, 3))
    (second,) = advance(design(both[:1]), source=bucket, directory=local)
    assert (second.new, second.played) == (len(DEALS), len(DEALS))


def test_a_failed_match_is_counted_and_not_replayed_unless_asked(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 2))
    anchor = heuristic(tmp_path)

    def eval_(state: ExperimentState) -> Job:
        vs = Seating(name="vs heuristic", players=(DIRECT, anchor), conditions=DEALS, games=2)
        ladder = Benchmark(name="ladder", environment=WORLD, seatings=(vs,))
        return evaluation(ladder, play=play_failing_deal_one,
                          checkpoints=state.run("ppo-1").checkpoints, max_hours=0.1)  # fmt: skip

    ppo, local = Experiment(name="ppo", eval=eval_), tmp_path / "evaluator"
    with pytest.raises(EtudeError, match="match_failed"):
        advance(ppo, source=bucket, directory=local)
    # Deal 1 is counted as failed and left alone; the rest is played.
    (counted,) = advance(ppo, source=bucket, directory=local)
    assert counted.failed == 1 and counted.played == counted.new
    (settled,) = advance(ppo, source=bucket, directory=local)
    assert (settled.new, settled.failed, settled.played) == (0, 1, 0)
    with pytest.raises(EtudeError, match="match_failed"):
        advance(ppo, source=bucket, directory=local, retry_failed=True)


def test_a_pass_whose_matches_all_return_no_rows_fails_and_a_retry_replays_them(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 2))
    anchor = heuristic(tmp_path)
    games = {"play": play_catching_errors}

    def eval_(state: ExperimentState) -> Job:
        vs = Seating(name="vs heuristic", players=(DIRECT, anchor), conditions=DEALS, games=2)
        ladder = Benchmark(name="ladder", environment=WORLD, seatings=(vs,))
        return evaluation(ladder, play=games["play"], checkpoints=state.run("ppo-1").checkpoints,
                          max_hours=0.1)  # fmt: skip

    ppo, local = Experiment(name="ppo", eval=eval_), tmp_path / "evaluator"
    with pytest.raises(EtudeError, match="match_failed") as stopped:
        advance(ppo, source=bucket, directory=local)
    cause = stopped.value.__cause__
    assert isinstance(cause, EtudeError) and cause.code == "match_has_no_rows"
    with pytest.raises(EtudeError, match="match_failed"):
        advance(ppo, source=bucket, directory=local)
    # Every match failed: each counts as failed, none as played, and every run ended failed.
    (settled,) = advance(ppo, source=bucket, directory=local)
    assert (settled.new, settled.failed, settled.played) == (0, len(DEALS), 0)
    db = statsdb("ppo", bucket)
    assert db.sql("select distinct state from runs").fetchall() == [("failed",)]
    assert db.sql("select count(*) from matches where state = 'done'").fetchone() == (0,)
    db.close()
    # Once the runtime is fixed, a retry plays the failed matches again.
    games["play"] = play
    (retried,) = advance(ppo, source=bucket, directory=local, retry_failed=True)
    assert (retried.new, retried.played) == (len(DEALS), len(DEALS))
    (done,) = advance(ppo, source=bucket, directory=local)
    assert (done.new, done.failed, done.played) == (0, 0, 0)
    # The kept database that read the failed end reads the retried run's new one.
    db = statsdb("ppo", bucket)
    assert db.sql("select distinct state from runs").fetchall() == [("finished",)]
    assert db.sql("select distinct state from matches").fetchall() == [("done",)]
    db.close()


def test_an_eval_job_belongs_to_the_experiment_of_its_file(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    anchors = Seating(name="vs heuristic", players=(DIRECT, heuristic(tmp_path)), conditions=DEALS,
                      games=2)  # fmt: skip
    progress = Benchmark(name="ladder", environment=WORLD, seatings=(anchors,))

    def elsewhere(state: ExperimentState) -> Job:
        return evaluation(progress, experiment="another", play=play, max_hours=0.1)

    with pytest.raises(EtudeError, match="eval_job_of_another_experiment"):
        advance(Experiment(name="ppo", eval=elsewhere), source=bucket, directory=tmp_path / "e")


def test_an_experiment_file_defines_eval(tmp_path: Path) -> None:
    file = tmp_path / "experiment.py"
    file.write_text("def eval(state):\n    return []\n")
    read = read_experiment(file)
    assert read.name == "experiment"
    assert read.eval(ExperimentState(experiment="experiment")) == []
    (tmp_path / "train_only.py").write_text("def train():\n    return []\n")
    with pytest.raises(EtudeError, match="experiment_file_defines_no_eval"):
        read_experiment(tmp_path / "train_only.py")


class _Launcher:
    """The launcher's two calls a pass makes, recorded instead of renting anything."""

    def __init__(self) -> None:
        self.launched: list[Run] = []
        self.outcome: Outcome | None = None

    def launch(self, run: Run, pool: str | None = None, *, rent: str | None = None) -> str:
        self.launched.append(run)
        return str(run.id)

    def status(self, run: str) -> Status:
        done = self.outcome is not None
        return Status(
            run=run, phase="done" if done else "running", reason=None,
            outcome=self.outcome, cause=None, until=None, clocks={}, usd=Decimal(0), attempts=1,
            rental_ended=None,
        )  # fmt: skip


def test_a_launched_pass_ships_its_checkpoints_and_is_not_launched_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import etude.launch.client as client  # noqa: PLC0415

    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "status", launcher.status)
    bucket = DirectoryStorage(tmp_path / "bucket")
    eval_ = design(tmp_path)
    local = tmp_path / "evaluator"
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 5))

    (first,) = advance(eval_, source=bucket, rent="cpu8", directory=local)
    (run,) = launcher.launched
    assert first.launched == run.id and first.new == 3 * len(DEALS)
    # Checkpoints 2 and 4 reach the machine as files of the training run, by digest.
    assert sorted(i.key for i in run.job.inputs) == [
        "checkpoints/updates-200.pt", "checkpoints/updates-400.pt"
    ]  # fmt: skip
    assert {i.run for i in run.job.inputs} == {"ppo-1"}

    # While it runs, a pass neither launches it again nor its matches.
    (again,) = advance(eval_, source=bucket, rent="cpu8", directory=local)
    assert (again.new, again.running, again.launched) == (3 * len(DEALS), 3 * len(DEALS), None)
    assert len(launcher.launched) == 1

    # The machine: inputs in their folder, the eval run delivered to the bucket.
    inputs = tmp_path / "machine" / "inputs"
    inputs.mkdir(parents=True)
    trained = tmp_path / "bucket"
    for given in run.job.inputs:
        (source,) = trained.glob(f"**/ppo-1/{given.key}")
        shutil.copy(source, inputs / given.name)
    monkeypatch.setenv("ETUDE_INPUTS_DIR", str(inputs))
    monkeypatch.setenv("ETUDE_RUN", str(run.id))
    monkeypatch.setenv("ETUDE_TRACK_DIR", str(tmp_path / "machine" / "track"))
    spec = evaluation_spec(run.job)
    _hold(spec)
    shutil.copytree(tmp_path / "machine" / "track", tmp_path / "bucket" / "delivered")
    db = statsdb("ppo", bucket)
    uris = {urlparse(u).path for (u,) in db.sql("select uri from players where uri is not null")
            .fetchall()}  # fmt: skip
    assert any(u.startswith(str(inputs)) for u in uris)

    launcher.outcome = "succeeded"
    (done,) = advance(eval_, source=bucket, rent="cpu8", directory=local)
    assert (done.new, done.launched) == (0, None)

    # A failed launched pass stops the next one instead of spending again.
    save(training, bucket, tmp_path, range(5, 7))
    advance(eval_, source=bucket, rent="cpu8", directory=local)
    launcher.outcome = "failed"
    with pytest.raises(EtudeError, match="launched_pass_failed"):
        advance(eval_, source=bucket, rent="cpu8", directory=local)


def test_a_failed_launched_pass_stops_only_a_pass_wanting_its_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import etude.launch.client as client  # noqa: PLC0415

    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "status", launcher.status)
    bucket = DirectoryStorage(tmp_path / "bucket")
    local = tmp_path / "evaluator"
    (local / "experiments").mkdir(parents=True)
    # Another experiment's failed pass shares this computer's ledger.
    (local / "experiments" / "launched.json").write_text(
        json.dumps([{"run": "other-experiment", "matches": ["0" * 64]}])
    )
    launcher.outcome = "failed"
    eval_ = design(tmp_path)
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 5))

    (first,) = advance(eval_, source=bucket, rent="cpu8", directory=local)
    assert first.launched is not None

    # Now that pass failed too, and what it was given is still wanted.
    with pytest.raises(EtudeError, match="launched_pass_failed"):
        advance(eval_, source=bucket, rent="cpu8", directory=local)
    (again,) = advance(eval_, source=bucket, rent="cpu8", directory=local, retry_failed=True)
    assert again.launched == f"{first.launched}-2"
    assert [str(run.id) for run in launcher.launched] == [first.launched, again.launched]


def test_a_pass_job_launched_as_given_is_refused_on_the_machine(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 3))
    training.finish(timeout=30)
    job = design(tmp_path).eval(observe(bucket, "ppo"))
    assert isinstance(job, Job)
    with pytest.raises(SystemExit, match="etude eval"):
        _hold(evaluation_spec(job))


def test_a_tracked_checkpoints_seat_says_where_its_run_stood_and_its_line(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = trainer(bucket, tmp_path)
    save(run, bucket, tmp_path, range(1, 2))
    run.finish(timeout=30)
    (saved,) = observe(bucket, "ppo").run("ppo-1").checkpoints
    assert saved.ref.at == saved.clocks
    assert authored(saved.clocks) == authored(saved.model(DIRECT).checkpoint.at) == {"updates": 100}
    assert saved.ref.line == saved.model(DIRECT).checkpoint.line == "ppo-1"


def test_a_checkpoint_carries_the_wall_time_its_record_stored(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = trainer(bucket, tmp_path)
    save(run, bucket, tmp_path, range(1, 3))
    # Still running: a design pairs checkpoints by training hours before either run ends.
    observed = observe(bucket, "ppo").run("ppo-1")
    assert not observed.ended
    stored = {each.ordinal: each.wall_ms for each in read(bucket, "ppo", "ppo-1").checkpoints}
    assert {each.ordinal: each.wall_ms for each in observed.checkpoints} == stored
    run.finish(timeout=30)


def test_a_run_the_launcher_lost_has_ended_at_its_last_stored_checkpoint(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = trainer(bucket, tmp_path)
    save(run, bucket, tmp_path, range(1, 3))
    # Its process never wrote an end, as when its machine is lost: storage says running.
    assert observe(bucket, "ppo").run("ppo-1").state == "running"
    # The launcher's own record of the run, closed without success.
    import etude.launch.client as client  # noqa: PLC0415 - as the passes above import it

    client.launch(Run(id="ppo-1", job=Job(command="exit 3", max_hours=0.1)), "local")
    (closed,) = client.wait("ppo-1", timeout=60)
    assert closed.outcome == "failed"
    state = observe(bucket, "ppo")
    assert state.run("ppo-1").state == "failed"
    (line,) = state.lines
    # So `Final` seats its last stored checkpoint instead of waiting for good.
    assert line.ended and authored(line.checkpoints[-1].at) == {"updates": 200}
    run.finish(timeout=30)


def test_a_checkpoint_serialized_before_wall_time_still_loads(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = trainer(bucket, tmp_path)
    save(run, bucket, tmp_path, range(1, 2))
    run.finish(timeout=30)
    (saved,) = observe(bucket, "ppo").run("ppo-1").checkpoints
    assert Checkpoint.model_validate_json(saved.model_dump_json()) == saved
    older = saved.model_dump(mode="json", exclude={"wall_ms"})
    assert Checkpoint.model_validate(older).wall_ms is None


def test_a_finished_run_without_checkpoints_has_no_final(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    trainer(bucket, tmp_path).finish(timeout=30)
    run = observe(bucket, "ppo").run("ppo-1")
    assert (run.state, run.final, run.checkpoints.last) == ("finished", None, None)


CAPACITY = """
from etude.eval.match import environment
from etude.eval.records import Aligned, Benchmark, Fraction, Inference, Seating
from etude.jobs import evaluation
from etude.report import Curve, Matches, Report, Score
from tests.eval.games import play

DIRECT = Inference(config={"search": 0})
DEALS = ({"deal": 0},)


def eval(state):
    seatings = (
        Seating(
            name="half-training",
            players=(DIRECT, Fraction(clock="updates", value=0.5, inference=DIRECT)),
            conditions=DEALS,
            games=2,
        ),
        Seating(
            name="large vs small",
            players=(DIRECT, Aligned(clock="updates", job="small", inference=DIRECT)),
            conditions=DEALS,
            games=2,
        ),
    )
    ladder = Benchmark(name="ladder", environment=environment("duel"), seatings=seatings)
    held = [c for run in state.runs for c in run.checkpoints]
    return evaluation(ladder, play=play, checkpoints=held, max_hours=0.1)


def report():
    return Report(
        score=Score(table="plays", value="won"),
        evals=(
            Curve(title="Half", matches=Matches(seating="half-training")),
            Curve(title="Large against small", matches=Matches(seating="large vs small")),
        ),
    )
"""


def test_relative_seats_fill_in_as_their_jobs_save_and_the_report_says_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Imported here: the page is the report's, and only this test draws one.
    from etude.page import build  # noqa: PLC0415
    from etude.report import Matches  # noqa: PLC0415

    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    bucket = DirectoryStorage(tmp_path / "bucket")
    file = tmp_path / "experiments" / "capacity.py"
    file.parent.mkdir()
    file.write_text(CAPACITY)
    clocks = [Clock(name="updates", kind="step", meaning="optimizer updates")]
    runs = {
        job: track(experiment="capacity", run=f"{job}-1", job=job, clocks=clocks,
                   directory=tmp_path / job, storages=[bucket])
        for job in ("small", "large")
    }  # fmt: skip
    save(runs["small"], bucket, tmp_path, range(1, 3))
    save(runs["large"], bucket, tmp_path, range(1, 5))
    state = observe(bucket, "capacity")
    assert {run.name: (run.job, run.line) for run in state.runs} == {
        "small-1": ("small", "small-1"),
        "large-1": ("large", "large-1"),
    }
    experiment = read_experiment(file)
    local = tmp_path / "evaluator"
    (first,) = advance(experiment, source=bucket, directory=local)
    # Half-training for 200 of each and 400 of large; large against small at 100 and 200,
    # and small against itself at both.
    assert (first.new, first.played) == (7, 7)

    type Why = tuple[str, str | None, float | None]

    def drawn(seating: str) -> dict[float, float | Why | None]:
        """Large's points on the seating's curve: each one's score, or why it has none:
        whether a checkpoint may still appear, the job looked in and the updates looked at."""
        page = build(bucket, file)
        chosen = page.selections.index(Matches(seating=seating))
        return {
            p.at["updates"]: p.value
            if p.gap is None
            else (p.gap.missing, p.gap.job, p.gap.position)
            for p in page.points
            if p.selection == chosen and p.run == "large-1"
        }

    against = drawn("large vs small")
    assert sorted(against) == [100, 200, 300, 400]
    assert isinstance(against[100], float) and isinstance(against[200], float)
    assert against[300] == ("not_yet", "small", 300)
    half = drawn("half-training")
    assert half[100] == ("never", None, 50)
    assert half[300] == ("never", None, 150)
    assert isinstance(half[200], float) and isinstance(half[400], float)

    # Small catches up to 300 and ends there: 300 is played, for large and against itself;
    # 400 will never be.
    save(runs["small"], bucket, tmp_path, range(3, 4))
    runs["small"].finish(timeout=30)
    (second,) = advance(experiment, source=bucket, directory=local)
    assert (second.new, second.played) == (2, 2)
    against = drawn("large vs small")
    assert isinstance(against[300], float)
    assert against[400] == ("never", "small", 400)
    with statsdb("capacity", bucket) as db:
        lines = {(s.seating, s.run): s for s in scores(db, "ladder", table="plays", value="won")}
    assert [p.ordinal for p in lines[("large vs small", "large-1")].points] == [1, 2, 3]
    half_points = lines[("half-training", "large-1")].points
    assert [p.ordinal for p in half_points] == [2, 4]
    # 200 also sits in 400's match, as its earlier partner; its point counts only the
    # match held for it: one condition, its seat's two games.
    assert [(p.matches, p.rows) for p in half_points] == [(1, 2), (1, 2)]
    page = build(bucket, file)
    (at_200,) = [
        p for p in page.points
        if p.selection == page.selections.index(Matches(seating="half-training"))
        and p.run == "large-1" and p.at["updates"] == 200
    ]  # fmt: skip
    (opponent,) = at_200.opponents
    assert authored(page.players[opponent].at) == {"updates": 100}
    assert (at_200.matches, at_200.rows) == (1, 2)
