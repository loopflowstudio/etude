"""Tournaments end to end: benchmarks held on the go and in batch, round robins split across runs."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest

from etude.eval.match import agent, environment, evaluate
from etude.eval.query import statsdb
from etude.eval.records import (
    Agent,
    Benchmark,
    CheckpointKey,
    CheckpointRef,
    Matchup,
    Model,
    RoundRobin,
    TournamentRun,
)
from etude.eval.tournament import curves, hold, played, standings
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Clock
from etude.run.storage import DirectoryStorage
from etude.train.run import track
from tests.eval.games import play, play_failing_deal_one
from tests.run.storage import MeasuredStorage

WORLD = environment("duel", config={"world": "w6"})
OTHER = environment("duel", config={"world": "w7"})
DEALS = tuple({"deal": deal} for deal in range(3))
DIRECT, SEARCH = Model(config={"search": 0}), Model(config={"search": 2})
# The experiment trains the model and evaluates it.
EXPERIMENT = "ppo"


def scripted(tmp_path: Path, name: str, strength: int) -> Agent:
    script = tmp_path / "players" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"STRENGTH = {strength}\n")
    return agent(name, code=[script], config={"strength": strength})


def trained(tmp_path: Path, bucket: DirectoryStorage) -> list[CheckpointRef]:
    """Three checkpoints of one training run, delivered to the bucket."""
    run = track(
        experiment=EXPERIMENT,
        run="ppo-1",
        clocks=[Clock(name="updates", kind="step", meaning="optimizer updates")],
        directory=tmp_path / "trainer",
        storages=[bucket],
    )
    saved = []
    for ordinal in (1, 2, 3):
        model = tmp_path / "latest.pt"
        model.write_bytes(f"weights {ordinal}".encode())
        artifact = run.checkpoint(model, updates=100 * ordinal)
        key = CheckpointKey(run="ppo-1", name=PurePosixPath(artifact.uri).name)
        saved.append(CheckpointRef(key=key, artifact=artifact))
    run.finish(timeout=30)
    return saved


def anchors(tmp_path: Path) -> Benchmark:
    heuristic, greedy = scripted(tmp_path, "heuristic", 0), scripted(tmp_path, "greedy", 4)
    return Benchmark(
        name="anchors",
        environment=WORLD,
        matchups=(
            Matchup(name="no search vs heuristic", players=(DIRECT, heuristic),
                    conditions=DEALS, games=4),
            Matchup(name="no search vs greedy", players=(DIRECT, greedy),
                    conditions=DEALS[:2], games=2),
            Matchup(name="search vs no search", players=(SEARCH, DIRECT),
                    conditions=DEALS[:1], games=6),
        ),
    )  # fmt: skip


def test_checkpoints_held_on_the_go_then_a_matchup_added_in_batch(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)
    progress = anchors(tmp_path)

    def on_the_go(checkpoint: CheckpointRef, measured: MeasuredStorage, machine: str) -> int:
        # Each checkpoint's eval run starts on its own machine, with an empty disk.
        held = hold(progress, experiment=EXPERIMENT, checkpoint=checkpoint, play=play, storages=[measured],
                    directory=tmp_path / "machines" / machine)  # fmt: skip
        return held.played

    per_checkpoint = 3 + 2 + 1
    assert on_the_go(checkpoints[0], MeasuredStorage(bucket), "first") == per_checkpoint
    later = [MeasuredStorage(bucket), MeasuredStorage(bucket)]
    with ThreadPoolExecutor(2) as pool:
        played = list(pool.map(on_the_go, checkpoints[1:], later, ["second", "third"]))
    assert played == [per_checkpoint] * 2
    # A fixed cost whatever the tournament's size: one checkpoint more in it changes nothing.
    second, third = ((m.listings, m.requests, m.uploads) for m in later)
    assert second == third
    # Nothing of the tournament is read: only the run's own matches are listed, to resume.
    assert (second[0], second[1]) == (1, 0)
    # A retry on another empty machine replays nothing.
    again = MeasuredStorage(bucket)
    assert on_the_go(checkpoints[0], again, "retry") == 0

    greedy = scripted(tmp_path, "greedy", 4)
    added = Matchup(name="search vs greedy", players=(SEARCH, greedy), conditions=DEALS, games=2)
    grown = progress.model_copy(update={"matchups": (*progress.matchups, added)})
    batch = hold(
        grown,
        experiment=EXPERIMENT,
        play=play,
        storages=[bucket],
        directory=tmp_path / "machines" / "batch",
    )
    # Only the new matchup's matches, for every checkpoint held so far.
    assert (batch.played, batch.skipped) == (3 * len(DEALS), 0)

    lines = {line.matchup: line for line in curves(statsdb(EXPERIMENT, bucket), "anchors", table="plays",
                                                   value="won")}  # fmt: skip
    assert sorted(lines) == sorted(each.name for each in grown.matchups)
    for line in lines.values():
        assert (line.run, line.experiment) == ("ppo-1", "ppo")
        assert [(p.ordinal, p.clocks) for p in line.points] == [
            (1, {"updates": 100.0}),
            (2, {"updates": 200.0}),
            (3, {"updates": 300.0}),
        ]
    assert [p.matches for p in lines["no search vs greedy"].points] == [2, 2, 2]
    # Training makes the model beat greedy more; search beats no search throughout.
    against_greedy = [p.value for p in lines["no search vs greedy"].points]
    assert against_greedy[0] < against_greedy[-1]
    assert all(p.value > 0.5 for p in lines["search vs no search"].points)


def test_a_batch_hold_skips_what_any_eval_run_of_its_environment_delivered(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    checkpoints = trained(tmp_path, bucket)
    for each in checkpoints:
        hold(progress, experiment=EXPERIMENT, checkpoint=each, play=play, storages=[bucket],
             directory=tmp_path / str(each.key.name))  # fmt: skip
    measured = MeasuredStorage(bucket)
    again = hold(
        progress,
        experiment=EXPERIMENT,
        play=play,
        storages=[measured],
        directory=tmp_path / "batch",
    )
    assert again.played == 0
    # One listing of the bucket and one of its own matches; each eval run's run.json and
    # tournament.json.
    assert (measured.listings, measured.requests) == (1 + 1, 3 + 3)

    # Eval runs held for no tournament already played a matchup about to be added:
    # two checkpoints' matches in this environment, the third's in another.
    added = Matchup(name="search vs greedy", players=(SEARCH, scripted(tmp_path, "greedy", 4)),
                    conditions=DEALS, games=2)  # fmt: skip
    for run, world, held in (
        ("adhoc", WORLD, checkpoints[:2]),
        ("elsewhere", OTHER, checkpoints[2:]),
    ):
        evaluate(experiment=EXPERIMENT, environment=world, play=play, run=run, storages=[bucket],
                 matches=[match for each in held for match in added.matches(each)],
                 directory=tmp_path / run)  # fmt: skip
    grown = progress.model_copy(update={"matchups": (*progress.matchups, added)})
    measured = MeasuredStorage(bucket)
    batch = hold(
        grown, experiment=EXPERIMENT, play=play, storages=[measured], directory=tmp_path / "batch"
    )
    # Only the third checkpoint's: a match is reused by key within one environment.
    assert batch.played == len(DEALS)
    # Each eval run's run.json; the four held for the tournament by tournament.json, the
    # two held for none by environment.json.
    assert (measured.listings, measured.requests) == (1 + 1, 6 + 4 + 2)
    lines = {line.matchup: line for line in curves(statsdb(EXPERIMENT, bucket), "anchors", table="plays",
                                                   value="won")}  # fmt: skip
    assert [p.matches for p in lines["search vs greedy"].points] == [len(DEALS)] * 3


def test_a_matchup_name_redefined_is_refused_by_a_batch_hold_and_on_read(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    first, second, _ = trained(tmp_path, bucket)
    progress = anchors(tmp_path)
    hold(
        progress,
        experiment=EXPERIMENT,
        checkpoint=first,
        play=play,
        storages=[bucket],
        directory=tmp_path / "a",
    )
    changed = progress.matchups[0].model_copy(update={"games": 8})
    other = progress.model_copy(update={"matchups": (changed,)})
    # On the go reads nothing of the tournament, so the redefinition is played...
    hold(
        other,
        experiment=EXPERIMENT,
        checkpoint=second,
        play=play,
        storages=[bucket],
        directory=tmp_path / "b",
    )
    # ...and found by everything that reads it.
    with pytest.raises(EtudeError) as refused:
        hold(other, experiment=EXPERIMENT, play=play, storages=[bucket], directory=tmp_path / "c")
    assert refused.value.code == "matchup_conflict"
    db = statsdb(EXPERIMENT, bucket)
    assert db.sql("select conflict from tournaments").fetchall() == [("matchup_conflict",)]
    with pytest.raises(EtudeError) as refused:
        curves(db, "anchors", table="plays", value="won")
    assert refused.value.code == "matchup_conflict"


def test_a_hold_launched_on_a_machine_keeps_its_tournament_in_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)
    progress = anchors(tmp_path)
    for ordinal, checkpoint in enumerate(checkpoints):
        # As `etude launch` runs it: the courier carries only `track/<ETUDE_RUN>/`.
        track = tmp_path / f"machine-{ordinal}" / "track"
        monkeypatch.setenv("ETUDE_RUN", f"anchors-eval-{ordinal}")
        monkeypatch.setenv("ETUDE_TRACK_DIR", str(track))
        held = hold(progress, experiment=EXPERIMENT, checkpoint=checkpoint, play=play)
        assert held.name == f"anchors-eval-{ordinal}"
        assert [path.name for path in track.iterdir()] == [held.name]
        shutil.copytree(track / held.name, bucket.root / "delivered" / held.name)
    monkeypatch.delenv("ETUDE_RUN")
    lines = curves(statsdb(EXPERIMENT, bucket), "anchors", table="plays", value="won")
    assert sorted(line.matchup for line in lines) == sorted(m.name for m in progress.matchups)
    assert all([p.ordinal for p in line.points] == [1, 2, 3] for line in lines)


def test_a_retried_hold_finds_its_checkpoint_by_digest_wherever_it_lies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkpoint, *_) = trained(tmp_path, DirectoryStorage(tmp_path / "bucket"))
    progress = anchors(tmp_path)
    monkeypatch.setenv("ETUDE_RUN", "anchors-eval")
    first = hold(
        progress,
        experiment=EXPERIMENT,
        checkpoint=checkpoint,
        play=play,
        directory=tmp_path / "machine",
    )
    # A second attempt is given the same file at another path.
    moved = checkpoint.model_copy(update={"uri": (tmp_path / "attempt-2" / "updates-100").as_uri()})
    again = hold(
        progress, experiment=EXPERIMENT, checkpoint=moved, play=play, directory=tmp_path / "machine"
    )
    assert first.played > 0
    assert (again.name, again.played, again.skipped) == ("anchors-eval", 0, first.played)


def test_a_round_robin_held_as_two_shares_stands_as_one(tmp_path: Path) -> None:
    finals = [scripted(tmp_path, f"final{strength}", strength) for strength in range(4)]
    league = RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=4)
    split, whole = DirectoryStorage(tmp_path / "split"), DirectoryStorage(tmp_path / "whole")
    halves = [
        hold(
            league,
            experiment=EXPERIMENT,
            enter=finals,
            play=play,
            share=(k, 2),
            storages=[split],
            directory=tmp_path / f"machine-{k}",
        )  # fmt: skip
        for k in (0, 1)
    ]
    one = hold(league, experiment=EXPERIMENT, enter=finals, play=play, workers=2, storages=[whole],
               directory=tmp_path / "machine")  # fmt: skip
    assert halves[0].name != halves[1].name
    # Every unordered pair once per deal.
    assert sum(half.played for half in halves) == one.played == 6 * len(DEALS)

    def table(storage: DirectoryStorage) -> list[tuple[str, str | None, float, int]]:
        found = standings(statsdb(EXPERIMENT, storage), "finals", table="plays", value="won")
        assert found.complete
        return [(e.name, e.opponent_name, e.value, e.matches) for e in found.entries]

    assert table(split) == table(whole)
    # Ordered by value against the field, best first.
    assert [name for name, opponent, *_ in table(whole) if opponent is None] == [
        "final3",
        "final2",
        "final1",
        "final0",
    ]


def test_a_tournament_of_the_same_name_with_other_rules_is_refused(tmp_path: Path) -> None:
    entrant = scripted(tmp_path, "s", 1)
    hold(RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=4),
         experiment=EXPERIMENT, enter=[entrant], play=play, directory=tmp_path)  # fmt: skip
    others: list[Benchmark | RoundRobin] = [
        RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=8),
        Benchmark(name="finals", environment=WORLD),
    ]
    for other in others:
        with pytest.raises(EtudeError) as refused:
            hold(other, experiment=EXPERIMENT, play=play, directory=tmp_path)
        assert refused.value.code == "tournament_conflict"


def test_round_robin_shares_differ_by_at_most_one_and_count_matches_any_run_held(
    tmp_path: Path,
) -> None:
    finals = [scripted(tmp_path, f"final{strength}", strength) for strength in range(4)]
    league = RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=4)
    bucket = DirectoryStorage(tmp_path / "bucket")

    def share(k: int) -> int:
        return hold(league, experiment=EXPERIMENT, enter=finals, play=play, share=(k, 4), storages=[bucket],
                    directory=tmp_path / f"machine-{k}").played  # fmt: skip

    # 18 matches dealt four ways; the fourth share is not played yet.
    assert [share(k) for k in range(3)] == [5, 5, 4]
    # A retry of a share deals it alike, and finds its part delivered.
    assert share(1) == 0
    assert not standings(statsdb(EXPERIMENT, bucket), "finals", table="plays", value="won").complete
    # Another tournament in the same environment plays the same matches...
    rematch = league.model_copy(update={"name": "rematch"})
    hold(
        rematch,
        experiment=EXPERIMENT,
        enter=finals,
        play=play,
        storages=[bucket],
        directory=tmp_path / "rematch",
    )
    # ...so the first is complete, each match counted once.
    found = standings(statsdb(EXPERIMENT, bucket), "finals", table="plays", value="won")
    assert (found.complete, found.scheduled) == (True, 18)
    assert {e.matches for e in found.entries if e.opponent is not None} == {len(DEALS)}


def test_curves_of_checkpoints_no_training_run_recorded_follow_their_names(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    saved = ["updates-1000.pt", "updates-150.pt", "updates-900.pt"]
    for name in saved:
        # An outside checkpoint, keyed by its address.
        artifact = ArtifactRef(uri=f"s3://models/{name}", sha256=sha256(name.encode()).hexdigest())
        checkpoint = CheckpointRef(key=CheckpointKey(address=artifact.uri), artifact=artifact)
        hold(progress, experiment=EXPERIMENT, checkpoint=checkpoint, play=play, storages=[bucket],
             directory=tmp_path / name)  # fmt: skip
    for line in curves(statsdb(EXPERIMENT, bucket), "anchors", table="plays", value="won"):
        assert line.run is None
        assert [(p.name, p.ordinal) for p in line.points] == [
            ("updates-150.pt", None),
            ("updates-900.pt", None),
            ("updates-1000.pt", None),
        ]


def test_pairs_of_checkpoints_named_as_they_appear_are_each_played_once(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)
    heuristic = scripted(tmp_path, "heuristic", 0)

    def wanted(saved: list[CheckpointRef]) -> Benchmark:
        """Each checkpoint against the heuristic, and each against the one before it."""
        consecutive = [
            Matchup(name=f"{DIRECT.agent(new).name} vs {DIRECT.agent(old).name}",
                    players=(DIRECT.agent(new), DIRECT.agent(old)), conditions=DEALS, games=2)
            for old, new in zip(saved, saved[1:])
        ]  # fmt: skip
        anchor = Matchup(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS,
                         games=2)  # fmt: skip
        return Benchmark(name="ladder", environment=WORLD, matchups=(anchor, *consecutive))

    # On the go, a checkpoint plays only the matchups its model seats call for.
    first = hold(wanted(checkpoints[:2]), experiment=EXPERIMENT, checkpoint=checkpoints[0], play=play,
                 storages=[bucket],
                 directory=tmp_path / "go")  # fmt: skip
    assert first.played == len(DEALS)

    def held(saved: list[CheckpointRef], machine: str) -> int:
        definition = wanted(saved)
        for checkpoint in saved:  # what a training run's on-the-go holds would add
            hold(definition, experiment=EXPERIMENT, checkpoint=checkpoint, play=play, storages=[bucket],
                 directory=tmp_path / machine / str(checkpoint.key.name))  # fmt: skip
        return hold(definition, experiment=EXPERIMENT, play=play, storages=[bucket],
                    directory=tmp_path / machine).played  # fmt: skip

    # The first pair is played when its second checkpoint appears, then only the new pair.
    assert held(checkpoints[:2], "pass-1") == len(DEALS)
    assert held(checkpoints, "pass-2") == len(DEALS)
    assert held(checkpoints, "pass-3") == 0

    db = statsdb(EXPERIMENT, bucket)
    pairs = db.sql(
        "select matchup, count(*) from schedule where tournament = 'ladder' and checkpoint is null"
        " group by matchup order by matchup"
    ).fetchall()
    assert pairs == [
        ("updates-200.pt vs updates-100.pt", 3),
        ("updates-300.pt vs updates-200.pt", 3),
    ]
    # Curves follow only the matchups with a model seat.
    assert [line.matchup for line in curves(db, "ladder", table="plays", value="won")] == [
        "vs heuristic"
    ]
    # The later checkpoint wins each pair; its results are found by matchup name.
    later = db.sql(
        "select s.matchup, avg(p.won::int) from plays p"
        " join schedule s on s.match = p.match and s.tournament = 'ladder'"
        " where s.checkpoint is null and p.player = 0 group by s.matchup"
    ).fetchall()
    assert all(value > 0.5 for _, value in later)


def test_two_keys_holding_the_same_bytes_are_two_agents_each_played(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    heuristic = scripted(tmp_path, "heuristic", 0)
    vs = Matchup(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS, games=2)
    ladder = Benchmark(name="ladder", environment=WORLD, matchups=(vs,))
    # Two runs warm-started from one file: the same bytes, two checkpoints.
    file = ArtifactRef(uri="s3://models/init.pt", sha256=sha256(b"init").hexdigest())
    starts = [
        CheckpointRef(key=CheckpointKey(run=run, name="updates-100.pt"), artifact=file)
        for run in ("ppo-1", "ppo-2")
    ]
    played = hold(ladder, experiment=EXPERIMENT, checkpoints=starts, play=play,
                  storages=[bucket], directory=tmp_path / "batch").played  # fmt: skip
    assert played == 2 * len(DEALS)
    found = (
        statsdb(EXPERIMENT, bucket)
        .sql("select distinct checkpoint from schedule order by checkpoint")
        .fetchall()
    )
    assert found == [("ppo-1/checkpoints/updates-100.pt",), ("ppo-2/checkpoints/updates-100.pt",)]


def test_a_new_version_of_an_agents_code_reuses_what_an_older_one_played(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)

    def ladder() -> Benchmark:
        heuristic = scripted(tmp_path, "heuristic", 0)
        vs = Matchup(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS, games=2)
        return Benchmark(name="ladder", environment=WORLD, matchups=(vs,))

    first = ladder()
    assert hold(first, experiment=EXPERIMENT, checkpoints=checkpoints[:1], play=play,
                storages=[bucket], directory=tmp_path / "v1").played == len(DEALS)  # fmt: skip
    # The heuristic's file changes: its version, not its key, so nothing is played again
    # and the matchup is not defined two ways.
    (tmp_path / "players" / "heuristic.py").write_text("STRENGTH = 0  # edited\n")
    second = ladder()
    before, after = first.matchups[0].players[1], second.matchups[0].players[1]
    assert isinstance(before, Agent) and isinstance(after, Agent)
    assert (after.key, after.version.commit) == (before.key, before.version.commit)
    assert hold(second, experiment=EXPERIMENT, checkpoints=checkpoints[:1], play=play,
                storages=[bucket], directory=tmp_path / "v2").played == 0  # fmt: skip
    seats = (
        statsdb(EXPERIMENT, bucket)
        .sql("select count(distinct source) from seats where agent like 'code:heuristic%'")
        .fetchone()
    )
    assert seats == (1,)


def test_eval_runs_from_etude_sdk_0_2_load_and_are_never_counted_as_played(
    tmp_path: Path,
) -> None:
    sha = sha256(b"weights").hexdigest()
    held = TournamentRun.model_validate(
        {
            "tournament": {"name": "anchors", "environment": WORLD.model_dump(mode="json")},
            "checkpoint": {"uri": "s3://models/updates-100.pt", "sha256": sha},
        }
    )
    (old,) = held.checkpoints
    assert str(old.key) == f"etude-sdk-0.2:{sha}"
    seated = Agent.model_validate(
        {"name": "updates-100.pt", "kind": "checkpoint", "artifact": sha,
         "uri": "s3://models/updates-100.pt", "config": {"search": 0}}
    )  # fmt: skip
    assert seated.kind == "checkpoint"
    assert seated.key.recorded is not None and seated.key.config == {"search": 0}
    # Its key is the digest it had, which no key since is: its matches stay its own.
    file = ArtifactRef(uri="s3://models/updates-100.pt", sha256=sha)
    now = DIRECT.agent(
        CheckpointRef(key=CheckpointKey(run="ppo-1", name="updates-100.pt"), artifact=file)
    )
    assert now.key != seated.key


def test_played_says_who_delivered_each_match_last_what_failed_and_what_is_new(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    checkpoints = trained(tmp_path, bucket)
    reader = tmp_path / "reader"
    hold(progress, experiment=EXPERIMENT, checkpoint=checkpoints[0], play=play,
         storages=[bucket], directory=tmp_path / "first")  # fmt: skip
    first = played(progress, experiment=EXPERIMENT, checkpoints=checkpoints[:2],
                   storages=[bucket], directory=reader)  # fmt: skip
    # Each checkpoint calls for 3 + 2 + 1 matches; only the first's were held.
    assert (len(first.delivered), len(first.failed), len(first.new)) == (6, 0, 6)
    (held,) = {each.run for each in first.delivered}
    # The same matches played again by another eval run: its rows are the latest.
    evaluate(experiment=EXPERIMENT, environment=WORLD, play=play, run="replayed",
             matches=[each.match for each in first.delivered], storages=[bucket],
             directory=tmp_path / "replayed")  # fmt: skip
    measured = MeasuredStorage(bucket)
    again = played(progress, experiment=EXPERIMENT, checkpoints=checkpoints[:2],
                   storages=[measured], directory=reader)  # fmt: skip
    assert {each.run for each in again.delivered} == {"replayed"} and held != "replayed"
    # One listing; each eval run's run.json, the tournament's tournament.json and the
    # other's environment.json; and both records of each match delivered twice.
    assert (measured.listings, measured.requests) == (1, 2 + 1 + 1 + 2 * 6)
    # A match that fails is failed, not new, until something delivers it.
    with pytest.raises(EtudeError, match="match_failed"):
        hold(progress, experiment=EXPERIMENT, checkpoint=checkpoints[2], play=play_failing_deal_one,
             storages=[bucket], directory=tmp_path / "third")  # fmt: skip
    third = played(progress, experiment=EXPERIMENT, checkpoints=checkpoints[2:],
                   storages=[bucket], directory=reader)  # fmt: skip
    (failed,) = third.failed
    assert failed.conditions == {"deal": 1} and not third.delivered
