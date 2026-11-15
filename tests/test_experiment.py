"""Experiments end to end: eval_jobs(state) chooses checkpoints, and each pass plays only what is new."""

from __future__ import annotations

import shutil
import time
from contextlib import nullcontext
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import pytest
from pydantic import BaseModel

import etude.launch.client as client
from etude.checkpoints import CheckpointRecord
from etude.controller import advance
from etude.declarations import Experiment, read_experiment
from etude.errors import EtudeError, LauncherError
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Benchmark, Code, Inference, Seating
from etude.eval.tournament import evaluating, scores
from etude.experiment import observe
from etude.jobs import (
    EvalJob,
    ExperimentState,
    OutMembers,
    _hold,
    evaluation_spec,
    with_experiment,
)
from etude.launch.records import Outcome, Standing, Status
from etude.local import _place_inputs
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.records import Clock, RunCounters, RunOutcome, RunRecord, RunSubmission
from etude.spec import Job, RunRequest
from etude.stopping import Stopped
from etude.storage import DirectoryStorage
from etude.train.read import read
from etude.train.tracking import TrainingRun as Run_
from etude.train.tracking import track
from tests.eval.games import play, play_catching_errors, play_failing_deal_one
from tests.experiments import authored
from tests.run.storage import MeasuredStorage

WORLD = environment("duel", config={"world": "w6"})
DEALS = tuple({"deal": deal} for deal in range(2))
DIRECT = Inference(config={"search": 0})


def _store_out_members(storage: DirectoryStorage, folder: str) -> OutFiles:
    storage.put(
        folder + "/run.json",
        RunRecord(id=folder, experiment="packs", job="player", seed=0).model_dump_json().encode(),
    )
    storage.put(
        folder + "/outcome.json",
        RunOutcome(state="finished", counters=RunCounters()).model_dump_json().encode(),
    )
    files = []
    for path in ("packs/0001.pack", "packs/deeper/0002.pack", "model", "removed"):
        body = path.encode()
        key = "out/" + path
        files.append(OutFile(key=key, sha256=sha256(body).hexdigest(), size_bytes=len(body)))
        if path != "removed":
            storage.put(folder + "/" + key, body)
    membership = OutFiles(files=tuple(files))
    storage.put(folder + "/" + MEMBERSHIP, membership.model_dump_json().encode())
    storage.put(folder + "/out/unpublished", b"not yet stored in membership")
    return membership


@pytest.mark.parametrize(
    ("folder", "name", "owner"),
    [
        ("packs/player@0", "player@0", "packs/player@0"),
        ("packs", "packs", "packs"),
        ("packs@0", "packs@0", "packs@0"),
        ("packs/packs@0", "packs@0", "packs/packs@0"),
    ],
)
def test_out_members_are_lazy_listed_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str, name: str, owner: str
) -> None:
    storage = DirectoryStorage(tmp_path / "bucket")
    membership = _store_out_members(storage, folder)
    measured = MeasuredStorage(storage)
    state = observe(measured, "packs")
    assert (measured.listings, measured.requests) == (1, 2)
    summary = state.run(name)
    members = summary.out
    assert summary.out is members
    assert (measured.listings, measured.requests) == (1, 3)
    assert measured.got == [folder + "/run.json", folder + "/outcome.json", folder + "/out.json"]
    assert [member.key for member in members] == [file.key for file in membership.files[:-1]]
    assert [(m.sha256, m.size_bytes) for m in members] == [
        (file.sha256, file.size_bytes) for file in membership.files[:-1]
    ]
    assert all(member.run == owner for member in members)
    assert all(member.uri == storage.uri(folder + "/" + member.key) for member in members)
    assert members.file("model") == members[-1]
    with pytest.raises(EtudeError, match=f"out_file_not_found: absent is not stored in {owner}$"):
        members.file("absent")
    packs = members.matching("packs/*.pack")
    assert isinstance(packs[:1], OutMembers)
    assert packs[:1].matching("*.pack").file("packs/0001.pack") == packs[0]
    assert len(packs) == 2
    assert packs.matching("PACKS/*") == ()
    inputs = packs.inputs("packs")
    assert [given.name for given in inputs] == [
        "packs/packs/0001.pack",
        "packs/packs/deeper/0002.pack",
    ]
    assert all(given.out is None and given.run == owner for given in inputs)

    placed = tmp_path / "inputs"
    _place_inputs(inputs, placed, tmp_path / "artifacts")
    monkeypatch.setenv("ETUDE_INPUTS_DIR", str(placed))
    with track(experiment="consumer", directory=tmp_path / "consumer") as consumer:
        assert (consumer.input("packs") / "packs/0001.pack").read_bytes() == b"packs/0001.pack"
    single = members.file("model").input("model")
    assert single.name == "model" and single.key == "out/model"
    storage.put(folder + "/out/model", b"replacement")
    with pytest.raises(EtudeError, match="input_undelivered"):
        _place_inputs((single,), tmp_path / "changed", tmp_path / "fresh-artifacts")


def test_out_members_without_membership_read_no_files(tmp_path: Path) -> None:
    storage = DirectoryStorage(tmp_path / "bucket")
    _store_out_members(storage, "packs/player@0")
    storage.delete("packs/player@0/out.json")
    measured = MeasuredStorage(storage)
    summary = observe(measured, "packs").run("player@0")
    assert summary.out == ()
    assert (measured.listings, measured.requests) == (1, 2)


def test_out_members_listed_but_unreadable_raise(tmp_path: Path) -> None:
    storage = DirectoryStorage(tmp_path / "bucket")
    _store_out_members(storage, "packs/player@0")
    summary = observe(storage, "packs").run("player@0")
    storage.delete("packs/player@0/out.json")
    with pytest.raises(FileNotFoundError):
        _ = summary.out


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


def _saved(run: Run_, bucket: DirectoryStorage) -> tuple[CheckpointRecord, ...]:
    found = [each for each in observe(bucket, run.experiment).runs if each.name == run.name]
    return found[0].checkpoints if found else ()


class ArenaConfig(BaseModel):
    seatings: tuple[Seating, ...]


def arena(config: ArenaConfig) -> Benchmark:
    return Benchmark(name="ladder", environment=WORLD, seatings=config.seatings)


def design(tmp_path: Path) -> Experiment:
    """Every second checkpoint against the heuristic, each against the previous one, and
    the final checkpoint against the first once training has finished."""
    anchor = heuristic(tmp_path)

    def eval_(state: ExperimentState) -> Job:
        run = state.run("ppo-1")
        rated = run.checkpoints.every(2)
        pairs = [
            Seating(name=f"{new.name} vs {old.name}", tags=("consecutive",),
                    players=(new.player(DIRECT), old.player(DIRECT)), configs=DEALS, games=2)
            for old, new in zip(rated, rated[1:])
        ]  # fmt: skip
        if run.final is not None and run.checkpoints:
            first = run.checkpoints[0]
            pairs.append(
                Seating(
                    name="final vs first",
                    players=(run.final.player(DIRECT), first.player(DIRECT)),
                    configs=DEALS,
                    games=2,
                )  # fmt: skip
            )
        anchors = Seating(name="vs heuristic", players=(DIRECT, anchor), configs=DEALS,
                          games=2)  # fmt: skip
        return EvalJob(
            tournament=arena(ArenaConfig(seatings=(anchors, *pairs))),
            play=play,
            checkpoints=rated,
            max_hours=0.1,
        )

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
        every = Seating(name="vs heuristic", players=(DIRECT, anchor), configs=DEALS, games=2)
        sweep = Benchmark(name="sweep", environment=WORLD, seatings=(every,))
        return EvalJob(tournament=sweep, play=play, checkpoints=state.run("ppo-1").checkpoints,
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
        Seating(name="vs heuristic", players=(DIRECT, anchor), configs=DEALS, games=2),
        Seating(name="vs greedy", players=(DIRECT, greedy), configs=DEALS, games=2),
    )

    def design(seatings: tuple[Seating, ...]) -> Experiment:
        def eval_(state: ExperimentState) -> Job:
            ladder = Benchmark(name="ladder", environment=WORLD, seatings=seatings)
            return EvalJob(tournament=ladder, play=play, checkpoints=state.run("ppo-1").checkpoints,
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
        vs = Seating(name="vs heuristic", players=(DIRECT, anchor), configs=DEALS, games=2)
        ladder = Benchmark(name="ladder", environment=WORLD, seatings=(vs,))
        return EvalJob(tournament=ladder, play=play_failing_deal_one,
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
        vs = Seating(name="vs heuristic", players=(DIRECT, anchor), configs=DEALS, games=2)
        ladder = Benchmark(name="ladder", environment=WORLD, seatings=(vs,))
        return EvalJob(tournament=ladder, play=games["play"], checkpoints=state.run("ppo-1").checkpoints,
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
    anchors = Seating(name="vs heuristic", players=(DIRECT, heuristic(tmp_path)), configs=DEALS,
                      games=2)  # fmt: skip
    progress = Benchmark(name="ladder", environment=WORLD, seatings=(anchors,))

    def elsewhere(state: ExperimentState) -> Job:
        return EvalJob(tournament=progress, experiment="another", play=play, max_hours=0.1)

    with pytest.raises(LauncherError, match="EvalJob belongs to its file"):
        advance(Experiment(name="ppo", eval=elsewhere), source=bucket, directory=tmp_path / "e")


def test_an_experiment_file_defines_eval(tmp_path: Path) -> None:
    file = tmp_path / "experiment.py"
    file.write_text("def eval_jobs(state):\n    return []\n")
    read = read_experiment(file)
    assert read.name == "experiment"
    assert read.eval is not None
    assert read.eval(ExperimentState(experiment="experiment")) == []
    (tmp_path / "train_only.py").write_text("def train():\n    return []\n")
    with pytest.raises(LauncherError, match="define training_jobs"):
        read_experiment(tmp_path / "train_only.py")


class _Launcher:
    """The launcher's two calls a pass makes, recorded instead of renting anything."""

    def __init__(self) -> None:
        self.launched: list[RunRequest] = []
        self.outcome: Outcome | None = None
        self.queued = False

    def launch(
        self, runs: list[RunRequest], pool: str | None = None, *, rent: str | None = None
    ) -> tuple[str, ...]:
        self.launched.extend(runs)
        return tuple(str(run.id) for run in runs)

    def queue(
        self, runs: list[RunRequest], pool: str | None = None, *, rent: str | None = None
    ) -> tuple[str, ...]:
        self.queued = True
        self.launched.extend(runs)
        return tuple(str(run.id) for run in runs)

    def standing(self, experiment: str, *, local: bool = False) -> tuple[Standing, ...]:
        return tuple(
            Standing(
                record=RunRecord(
                    id=str(run.id),
                    experiment=experiment,
                    job="eval",
                    seed=run.seed,
                    submission=RunSubmission(spec=run.job, pool="local"),
                ),
                status=self.status(str(run.id)),
            )
            for run in self.launched
            if run.job.experiment == experiment
        )

    def status(self, run: str) -> Status:
        done = self.outcome is not None
        return Status(
            run=run, phase="done" if done else "queued" if self.queued else "running", reason=None,
            outcome=self.outcome, cause=None, until=None, clocks={}, usd=Decimal(0), attempts=1,
            rental_ended=None,
        )  # fmt: skip


@pytest.mark.parametrize("phase", ["running", "queued", "failed"])
def test_saved_stop_keeps_the_launchers_continuation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    training.finish("stopped", timeout=30)
    (stopped,) = observe(bucket, "ppo").runs
    assert stopped.state == "stopped"
    launcher = _Launcher()
    launcher.launched.append(
        RunRequest(
            id=f"ppo/{stopped.name}",
            job=Job(name=stopped.launch, experiment="ppo", command="true"),
        )
    )
    launcher.queued = phase == "queued"
    launcher.outcome = "failed" if phase == "failed" else None
    monkeypatch.setattr(client, "standing", launcher.standing)
    (continued,) = observe(bucket, "ppo").runs
    assert continued.state == phase


def test_a_launched_pass_ships_its_checkpoints_and_is_not_launched_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "standing", launcher.standing)
    bucket = DirectoryStorage(tmp_path / "bucket")
    eval_ = design(tmp_path)
    local = tmp_path / "evaluator"
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 5))

    (first,) = advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    (run,) = launcher.launched
    assert first.launched == run.id and first.new == 3 * len(DEALS)
    # Checkpoints 2 and 4 reach the machine as files of the training run, by digest.
    assert sorted(i.key for i in run.job.inputs) == [
        "checkpoints/updates-200.pt", "checkpoints/updates-400.pt"
    ]  # fmt: skip
    assert {i.run for i in run.job.inputs} == {"ppo/ppo-1"}

    # While it runs, a pass neither launches it again nor its matches.
    (again,) = advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    assert (again.new, again.running, again.launched) == (0, 3 * len(DEALS), None)
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
    (done,) = advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    assert (done.new, done.launched) == (0, None)

    # A failed launched pass stops the next one instead of spending again.
    save(training, bucket, tmp_path, range(5, 7))
    advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    launcher.outcome = "failed"
    with pytest.raises(EtudeError, match="launched_pass_failed"):
        advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)


def test_a_failed_launched_pass_stops_only_a_pass_wanting_its_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "standing", launcher.standing)
    bucket = DirectoryStorage(tmp_path / "bucket")
    local = tmp_path / "evaluator"
    (local / "experiments").mkdir(parents=True)
    launcher.outcome = "failed"
    eval_ = design(tmp_path)
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 5))

    (first,) = advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    assert first.launched is not None

    # Now that pass failed too, and what it was given is still wanted.
    with pytest.raises(EtudeError, match="launched_pass_failed"):
        advance(eval_, source=bucket, mode="launch", rent="cpu8", directory=local)
    (again,) = advance(
        eval_, source=bucket, mode="launch", rent="cpu8", directory=local, retry_failed=True
    )
    assert again.launched == f"{first.launched}-2"
    assert [str(run.id) for run in launcher.launched] == [first.launched, again.launched]


def test_queued_passes_retain_every_job_while_capacity_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _Launcher()
    monkeypatch.setattr(client, "queue", launcher.queue)
    monkeypatch.setattr(client, "standing", launcher.standing)
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 3))
    anchor = heuristic(tmp_path)
    benchmark = Benchmark(
        name="queued",
        environment=WORLD,
        seatings=(Seating(name="anchor", players=(DIRECT, anchor), configs=DEALS, games=2),),
    )

    def choose(state: ExperimentState) -> list[Job]:
        return [
            EvalJob(tournament=benchmark, play=play, checkpoints=[cp], max_hours=0.1)
            for cp in state.run("ppo-1").checkpoints
        ]

    experiment = Experiment(name="ppo", eval=choose)
    local = tmp_path / "evaluator"
    first = advance(experiment, source=bucket, pool="occupied", directory=local, mode="queue")
    assert len(first) == 2 and all(p.launched is not None for p in first)
    assert all(launcher.status(str(run.id)).phase == "queued" for run in launcher.launched)
    again = advance(experiment, source=bucket, pool="occupied", directory=local, mode="queue")
    assert all((p.running, p.launched) == (len(DEALS), None) for p in again)
    assert len(launcher.launched) == 2
    training.finish(timeout=30)


def test_a_pass_job_launched_as_given_is_refused_on_the_machine(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = trainer(bucket, tmp_path)
    save(training, bucket, tmp_path, range(1, 3))
    training.finish(timeout=30)
    definition = design(tmp_path)
    assert definition.eval is not None
    job = definition.eval(observe(bucket, "ppo"))
    assert isinstance(job, Job)
    with pytest.raises(SystemExit, match="etude eval"):
        _hold(evaluation_spec(job))


def test_a_tracked_checkpoints_seat_says_where_its_run_stood_and_its_line(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = trainer(bucket, tmp_path)
    save(run, bucket, tmp_path, range(1, 2))
    run.finish(timeout=30)
    (saved,) = observe(bucket, "ppo").run("ppo-1").checkpoints
    assert saved.ref.at == saved.at
    assert authored(saved.at) == authored(saved.player(DIRECT).checkpoint.at) == {"updates": 100}
    assert saved.ref.line == saved.player(DIRECT).checkpoint.line == "ppo-1"


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

    client.launch(
        RunRequest(id="ppo-1", job=Job(command="exit 3", max_hours=0.1, experiment="ppo")), "local"
    )
    (closed,) = client.wait("ppo/ppo-1", timeout=60)
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
    assert CheckpointRecord.model_validate_json(saved.model_dump_json()) == saved
    older = saved.model_dump(mode="json", exclude={"wall_ms"})
    assert CheckpointRecord.model_validate(older).wall_ms is None


def test_a_finished_run_without_checkpoints_has_no_final(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    trainer(bucket, tmp_path).finish(timeout=30)
    run = observe(bucket, "ppo").run("ppo-1")
    assert (run.state, run.final, run.checkpoints.last) == ("finished", None, None)


CAPACITY = """
from etude.eval.match import environment
from etude.eval.records import Aligned, Benchmark, Fraction, Inference, Seating
from etude.jobs import EvalJob
from etude.report import Curve, Matches, Report, Score
from tests.eval.games import play

DIRECT = Inference(config={"search": 0})
DEALS = ({"deal": 0},)


def eval_jobs(state):
    seatings = (
        Seating(
            name="half-training",
            players=(DIRECT, Fraction(clock="updates", value=0.5, inference=DIRECT)),
            configs=DEALS,
            games=2,
        ),
        Seating(
            name="large vs small",
            players=(DIRECT, Aligned(clock="updates", job="small", inference=DIRECT)),
            configs=DEALS,
            games=2,
        ),
    )
    ladder = Benchmark(name="ladder", environment=environment("duel"), seatings=seatings)
    held = [c for run in state.runs for c in run.checkpoints]
    return EvalJob(tournament=ladder, play=play, checkpoints=held, max_hours=0.1)


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
            else (p.gap.missing, p.gap.seat.job, p.gap.value)
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


def test_a_failed_eval_holds_only_its_matches_within_the_same_tournament(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "standing", launcher.standing)
    bucket = DirectoryStorage(tmp_path / "bucket")
    experiment = design(tmp_path)
    training = trainer(bucket, tmp_path)
    try:
        save(training, bucket, tmp_path, range(1, 3))
        advance(experiment, source=bucket, mode="launch")
        (first,) = launcher.launched
        first_keys = {match.key for match in evaluation_spec(first.job).matches or ()}
        launcher.outcome = "failed"
        save(training, bucket, tmp_path, range(3, 5))
        with pytest.raises(EtudeError, match="launched_pass_failed"):
            advance(experiment, source=bucket, mode="launch")
        assert len(launcher.launched) == 2
        new_keys = {match.key for match in evaluation_spec(launcher.launched[1].job).matches or ()}
        assert new_keys and new_keys.isdisjoint(first_keys)
        assert len(new_keys) == 2 * len(DEALS)
    finally:
        training.finish(timeout=30)


@pytest.mark.parametrize("phase", ["absent", "running", "queued", "failed", "succeeded"])
@pytest.mark.parametrize("outcome", ["stopped", "finished", "failed"])
def test_saved_eval_outcomes_keep_the_launchers_continuation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, outcome: str
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    tournament = Benchmark(name="ladder", environment=WORLD)
    with (
        pytest.raises(Stopped if outcome == "stopped" else ValueError)
        if outcome != "finished"
        else nullcontext(),
        evaluating(
            tournament,
            experiment="ppo",
            run="ladder",
            directory=tmp_path / "eval",
            storages=[bucket],
        ),
    ):
        if outcome == "stopped":
            raise Stopped()
        if outcome == "failed":
            raise ValueError("evaluation failed")
    launcher = _Launcher()
    if phase != "absent":
        launcher.launched.append(
            RunRequest(
                id="ppo/ladder",
                job=with_experiment(
                    EvalJob(tournament=tournament, play=play, repo=None, max_hours=1), "ppo"
                ),
            )
        )
    launcher.queued = phase == "queued"
    launcher.outcome = (
        "failed" if phase == "failed" else "succeeded" if phase == "succeeded" else None
    )
    monkeypatch.setattr(client, "standing", launcher.standing)
    (run,) = observe(bucket, "ppo").eval_runs
    assert run.name == "ladder"
    assert run.state == (
        phase if outcome == "stopped" and phase not in {"absent", "succeeded"} else outcome
    )


@pytest.mark.parametrize("retained", [False, True], ids=["absent", "done"])
def test_stopped_eval_undelivered_matches_are_new_on_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retained: bool
) -> None:
    launcher = _Launcher()
    monkeypatch.setattr(client, "launch", launcher.launch)
    monkeypatch.setattr(client, "queue", launcher.queue)
    monkeypatch.setattr(client, "standing", launcher.standing)
    bucket = DirectoryStorage(tmp_path / "bucket")
    experiment = design(tmp_path)
    training = trainer(bucket, tmp_path)
    try:
        save(training, bucket, tmp_path, range(1, 3))
        advance(experiment, source=bucket, mode="launch")
        (first,) = launcher.launched
        spec = evaluation_spec(first.job)
        with (
            pytest.raises(Stopped),
            evaluating(
                spec.tournament,
                experiment="ppo",
                run=str(first.id).removeprefix("ppo/"),
                directory=tmp_path / "eval",
                storages=[bucket],
            ),
        ):
            raise Stopped()
        if retained:
            launcher.outcome = "succeeded"
        else:
            launcher.launched.clear()
        (stopped,) = observe(bucket, "ppo").eval_runs
        assert stopped.state == "stopped"
        (decision,) = advance(experiment, source=bucket, mode="queue")
        assert decision.new == len(spec.matches or ()) > 0
        assert decision.running == decision.failed == 0
        assert decision.launched != first.id
        assert evaluation_spec(launcher.launched[-1].job).matches == spec.matches
    finally:
        training.finish(timeout=30)


@pytest.mark.parametrize("phase", ["queued", "running", "failed", "succeeded"])
@pytest.mark.parametrize("outcome", ["stopped", "finished", "failed"])
def test_launcher_phase_preserves_compact_training_addresses_and_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    outcome: Literal["stopped", "finished", "failed"],
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    training = track(
        experiment="ppo",
        run="ppo@0",
        job="base",
        seed=0,
        directory=tmp_path / "training",
        storages=[bucket],
    )
    training.finish(outcome, timeout=30)
    launcher = _Launcher()
    launcher.launched.append(
        RunRequest(
            id="ppo/base@0",
            seed=0,
            job=Job(name="base", experiment="ppo", command="true"),
        )
    )
    launcher.queued = phase == "queued"
    launcher.outcome = (
        "failed" if phase == "failed" else "succeeded" if phase == "succeeded" else None
    )
    monkeypatch.setattr(client, "standing", launcher.standing)
    (run,) = observe(bucket, "ppo").runs
    assert run.name == "ppo@0"
    assert run.state == (phase if outcome == "stopped" and phase != "succeeded" else outcome)


@pytest.mark.parametrize("phase", ["queued", "running"])
def test_unstored_summaries_use_local_names_and_accept_both_lookup_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    launcher = _Launcher()
    launcher.launched.extend(
        [
            RunRequest(
                id="ppo/base@0",
                seed=0,
                job=Job(name="base", experiment="ppo", command="true"),
            ),
            RunRequest(
                id="ppo/ladder-123",
                job=with_experiment(
                    EvalJob(
                        tournament=Benchmark(name="ladder", environment=WORLD),
                        play=play,
                        repo=None,
                        max_hours=1,
                    ),
                    "ppo",
                ),
            ),
        ]
    )
    launcher.queued = phase == "queued"
    monkeypatch.setattr(client, "standing", launcher.standing)
    state = observe(DirectoryStorage(tmp_path / "bucket"), "ppo")
    run = state.run("ppo/base@0")
    assert state.run("base@0") == run
    assert (run.name, run.line, run.state) == ("base@0", "base@0", phase)
    (evaluation,) = state.eval_runs
    assert (evaluation.name, evaluation.state) == ("ladder-123", phase)
