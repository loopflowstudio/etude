"""Tournaments end to end: benchmarks held on the go and in batch, round robins split across runs."""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from etude.eval.match import agent, environment, evaluate
from etude.eval.query import connect
from etude.eval.records import Agent, Benchmark, Matchup, Model, RoundRobin
from etude.eval.tournament import curves, hold, standings
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Clock
from etude.run.storage import DirectoryStorage
from etude.train.run import track
from tests.eval.games import play
from tests.run.storage import MeasuredStorage

WORLD = environment("duel", config={"world": "w6"})
OTHER = environment("duel", config={"world": "w7"})
DEALS = tuple({"deal": deal} for deal in range(3))
DIRECT, SEARCH = Model(config={"search": 0}), Model(config={"search": 2})


def scripted(tmp_path: Path, name: str, strength: int) -> Agent:
    script = tmp_path / "players" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"STRENGTH = {strength}\n")
    return agent(name, code=[script], config={"strength": strength})


def trained(tmp_path: Path, bucket: DirectoryStorage) -> list[ArtifactRef]:
    """Three checkpoints of one training run, delivered to the bucket."""
    run = track(
        experiment="ppo",
        run="ppo-1",
        clocks=[Clock(name="updates", kind="step", meaning="optimizer updates")],
        directory=tmp_path / "trainer",
        storages=[bucket],
    )
    saved = []
    for ordinal in (1, 2, 3):
        model = tmp_path / "latest.pt"
        model.write_bytes(f"weights {ordinal}".encode())
        saved.append(run.checkpoint(model, updates=100 * ordinal))
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

    def on_the_go(checkpoint: ArtifactRef, measured: MeasuredStorage, machine: str) -> int:
        # Each checkpoint's eval run starts on its own machine, with an empty disk.
        held = hold(progress, checkpoint=checkpoint, play=play, storages=[measured],
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
    batch = hold(grown, play=play, storages=[bucket], directory=tmp_path / "machines" / "batch")
    # Only the new matchup's matches, for every checkpoint held so far.
    assert (batch.played, batch.skipped) == (3 * len(DEALS), 0)

    lines = {line.matchup: line for line in curves(connect(bucket), "anchors", table="plays",
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
        hold(progress, checkpoint=each, play=play, storages=[bucket],
             directory=tmp_path / each.uri.rsplit("/", 1)[-1])  # fmt: skip
    measured = MeasuredStorage(bucket)
    again = hold(progress, play=play, storages=[measured], directory=tmp_path / "batch")
    assert again.played == 0
    # One listing of the bucket and one of its own matches; each eval run's tournament.json.
    assert (measured.listings, measured.requests) == (1 + 1, 3)

    # Eval runs held for no tournament already played a matchup about to be added:
    # two checkpoints' matches in this environment, the third's in another.
    added = Matchup(name="search vs greedy", players=(SEARCH, scripted(tmp_path, "greedy", 4)),
                    conditions=DEALS, games=2)  # fmt: skip
    for run, world, held in (
        ("adhoc", WORLD, checkpoints[:2]),
        ("elsewhere", OTHER, checkpoints[2:]),
    ):
        evaluate(experiment=run, environment=world, play=play, run=run, storages=[bucket],
                 matches=[match for each in held for match in added.matches(each)],
                 directory=tmp_path / run)  # fmt: skip
    grown = progress.model_copy(update={"matchups": (*progress.matchups, added)})
    measured = MeasuredStorage(bucket)
    batch = hold(grown, play=play, storages=[measured], directory=tmp_path / "batch")
    # Only the third checkpoint's: a match is reused by key within one environment.
    assert batch.played == len(DEALS)
    # Each eval run is read once more: the two held for no tournament, by environment.json.
    assert (measured.listings, measured.requests) == (1 + 1, 4 + 2)
    lines = {line.matchup: line for line in curves(connect(bucket), "anchors", table="plays",
                                                   value="won")}  # fmt: skip
    assert [p.matches for p in lines["search vs greedy"].points] == [len(DEALS)] * 3


def test_a_matchup_name_redefined_is_refused_by_a_batch_hold_and_on_read(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    first, second, _ = trained(tmp_path, bucket)
    progress = anchors(tmp_path)
    hold(progress, checkpoint=first, play=play, storages=[bucket], directory=tmp_path / "a")
    changed = progress.matchups[0].model_copy(update={"games": 8})
    other = progress.model_copy(update={"matchups": (changed,)})
    # On the go reads nothing of the tournament, so the redefinition is played...
    hold(other, checkpoint=second, play=play, storages=[bucket], directory=tmp_path / "b")
    # ...and found by everything that reads it.
    with pytest.raises(EtudeError) as refused:
        hold(other, play=play, storages=[bucket], directory=tmp_path / "c")
    assert refused.value.code == "matchup_conflict"
    db = connect(bucket)
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
        held = hold(progress, checkpoint=checkpoint, play=play)
        assert held.name == f"anchors-eval-{ordinal}"
        assert [path.name for path in track.iterdir()] == [held.name]
        shutil.copytree(track / held.name, bucket.root / "delivered" / held.name)
    monkeypatch.delenv("ETUDE_RUN")
    lines = curves(connect(bucket), "anchors", table="plays", value="won")
    assert sorted(line.matchup for line in lines) == sorted(m.name for m in progress.matchups)
    assert all([p.ordinal for p in line.points] == [1, 2, 3] for line in lines)


def test_a_round_robin_held_as_two_shares_stands_as_one(tmp_path: Path) -> None:
    finals = [scripted(tmp_path, f"final{strength}", strength) for strength in range(4)]
    league = RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=4)
    split, whole = DirectoryStorage(tmp_path / "split"), DirectoryStorage(tmp_path / "whole")
    halves = [
        hold(
            league,
            enter=finals,
            play=play,
            share=(k, 2),
            storages=[split],
            directory=tmp_path / f"machine-{k}",
        )  # fmt: skip
        for k in (0, 1)
    ]
    one = hold(league, enter=finals, play=play, workers=2, storages=[whole],
               directory=tmp_path / "machine")  # fmt: skip
    assert halves[0].name != halves[1].name
    # Every unordered pair once per deal.
    assert sum(half.played for half in halves) == one.played == 6 * len(DEALS)

    def table(storage: DirectoryStorage) -> list[tuple[str, str | None, float, int]]:
        found = standings(connect(storage), "finals", table="plays", value="won")
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
         enter=[entrant], play=play, directory=tmp_path)  # fmt: skip
    others: list[Benchmark | RoundRobin] = [
        RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=8),
        Benchmark(name="finals", environment=WORLD),
    ]
    for other in others:
        with pytest.raises(EtudeError) as refused:
            hold(other, play=play, directory=tmp_path)
        assert refused.value.code == "tournament_conflict"


def test_round_robin_shares_differ_by_at_most_one_and_count_matches_any_run_held(
    tmp_path: Path,
) -> None:
    finals = [scripted(tmp_path, f"final{strength}", strength) for strength in range(4)]
    league = RoundRobin(name="finals", environment=WORLD, conditions=DEALS, games=4)
    bucket = DirectoryStorage(tmp_path / "bucket")

    def share(k: int) -> int:
        return hold(league, enter=finals, play=play, share=(k, 4), storages=[bucket],
                    directory=tmp_path / f"machine-{k}").played  # fmt: skip

    # 18 matches dealt four ways; the fourth share is not played yet.
    assert [share(k) for k in range(3)] == [5, 5, 4]
    # A retry of a share deals it alike, and finds its part delivered.
    assert share(1) == 0
    assert not standings(connect(bucket), "finals", table="plays", value="won").complete
    # Another tournament in the same environment plays the same matches...
    rematch = league.model_copy(update={"name": "rematch"})
    hold(rematch, enter=finals, play=play, storages=[bucket], directory=tmp_path / "rematch")
    # ...so the first is complete, each match counted once.
    found = standings(connect(bucket), "finals", table="plays", value="won")
    assert (found.complete, found.scheduled) == (True, 18)
    assert {e.matches for e in found.entries if e.opponent is not None} == {len(DEALS)}


def test_curves_of_checkpoints_no_training_run_recorded_follow_their_names(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    saved = ["updates-1000.pt", "updates-150.pt", "updates-900.pt"]
    for name in saved:
        checkpoint = ArtifactRef(
            uri=f"s3://models/{name}", sha256=sha256(name.encode()).hexdigest()
        )
        hold(progress, checkpoint=checkpoint, play=play, storages=[bucket],
             directory=tmp_path / name)  # fmt: skip
    for line in curves(connect(bucket), "anchors", table="plays", value="won"):
        assert line.run is None
        assert [(p.name, p.ordinal) for p in line.points] == [
            ("updates-150.pt", None),
            ("updates-900.pt", None),
            ("updates-1000.pt", None),
        ]
