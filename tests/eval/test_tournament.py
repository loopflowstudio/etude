"""Tournaments end to end: benchmarks held on the go and in batch, round robins split across runs."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path, PurePosixPath

import duckdb
import pytest

from etude.checkpoints import CheckpointRef, checkpoint_id
from etude.errors import EtudeError
from etude.eval.match import environment, evaluate
from etude.eval.query import statsdb
from etude.eval.records import (
    Aligned,
    Benchmark,
    CheckpointPlayer,
    Code,
    Inference,
    Line,
    Match,
    RoundRobin,
    Seating,
    Share,
    TournamentRun,
)
from etude.eval.run import Tables
from etude.eval.tournament import hold, played, scores, standings, stored_runs
from etude.records import ArtifactRef, Clock
from etude.storage import DirectoryStorage
from etude.train.tracking import track
from tests.eval.games import play, play_failing_deal_one
from tests.experiments import authored
from tests.run.storage import MeasuredStorage

WORLD = environment("duel", config={"world": "w6"})
OTHER = environment("duel", config={"world": "w7"})
DEALS = tuple({"deal": deal} for deal in range(3))
DIRECT, SEARCH = Inference(config={"search": 0}), Inference(config={"search": 2})
# The experiment trains the model and evaluates it.
EXPERIMENT = "ppo"


def scripted(tmp_path: Path, name: str, strength: int) -> Code:
    script = tmp_path / "players" / f"{name}.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"STRENGTH = {strength}\n")
    return Code(name=name, source=[script], config={"strength": strength})


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
        key = checkpoint_id(run="ppo-1", name=PurePosixPath(artifact.uri).name)
        saved.append(CheckpointRef(key=key, artifact=artifact))
    run.finish(timeout=30)
    return saved


def anchors(tmp_path: Path) -> Benchmark:
    heuristic, greedy = scripted(tmp_path, "heuristic", 0), scripted(tmp_path, "greedy", 4)
    return Benchmark(
        name="anchors",
        environment=WORLD,
        seatings=(
            Seating(name="no search vs heuristic", players=(DIRECT, heuristic),
                    conditions=DEALS, games=4),
            Seating(name="no search vs greedy", players=(DIRECT, greedy),
                    conditions=DEALS[:2], games=2),
            Seating(name="search vs no search", players=(SEARCH, DIRECT),
                    conditions=DEALS[:1], games=6),
        ),
    )  # fmt: skip


def test_a_relative_seat_waits_for_its_checkpoint_and_a_batch_hold_plays_it_once(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    saved = trained(tmp_path, bucket)
    # Two learners' checkpoints at the same positions, as two training runs would key them.
    small, large = (
        [ref.model_copy(update={"key": checkpoint_id(run=line, name=ref.key.rsplit("/", 1)[-1]),
                                "at": {"updates": 100 * n}, "line": line})
         for n, ref in enumerate(saved, 1)]
        for line in ("small-1", "large-1")
    )  # fmt: skip
    seating = Seating(
        name="large vs small",
        players=(DIRECT, Aligned(clock="updates", job="small", inference=DIRECT)),
        conditions=DEALS[:1],
        games=2,
    )
    # A tournament's name may hold spaces, as a seating's may.
    benchmark = Benchmark(name="paired sizes", environment=WORLD, seatings=(seating,))

    def lines(saved: int) -> list[Line]:
        return [
            Line(name="small-1", job="small", checkpoints=tuple(small[:saved])),
            Line(name="large-1", job="large", checkpoints=tuple(large)),
        ]

    def held(
        lines: list[Line],
        checkpoint: CheckpointRef | None = None,
        checkpoints: Sequence[CheckpointRef] = (),
    ) -> int:
        return hold(
            benchmark,
            experiment=EXPERIMENT,
            play=play,
            storages=[bucket],
            checkpoint=checkpoint,
            checkpoints=checkpoints,
            lines=lines,
        ).played

    # Small has saved only its first checkpoint: large's second waits; small's plays itself.
    assert held(lines(1), checkpoint=large[1]) == 0
    assert held(lines(1), checkpoint=small[0]) == 1
    assert held(lines(1), checkpoints=large) == 1
    # Small saves its second: a batch hold plays large's second against it, once.
    assert held(lines(2)) == 1
    again = [hold(benchmark, experiment=EXPERIMENT, play=play, storages=[bucket], lines=lines(2))
             for _ in range(2)]  # fmt: skip
    # Nothing new is found again: one eval run, resumed, and nothing played.
    assert [each.played for each in again] == [0, 0] and again[0].name == again[1].name
    with statsdb(EXPERIMENT, bucket) as db:
        (line,) = scores(db, "paired sizes", table="plays", value="won")
    # No training run recorded these checkpoints: one line of every held checkpoint played.
    assert sorted(p.checkpoint for p in line.points) == sorted(
        str(ref.key) for ref in (large[0], large[1], small[0])
    )


def test_an_aligned_hours_seat_takes_the_other_jobs_latest_checkpoint_once_it_has_passed(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    saved = trained(tmp_path, bucket)
    # No checkpoint is saved at an exact hour: small saves a little before each, large on it.
    small, large = (
        [ref.model_copy(update={"key": checkpoint_id(run=line, name=ref.key.rsplit("/", 1)[-1]),
                                "at": {"running_hours": hours}, "line": line})
         for ref, hours in zip(saved, positions, strict=True)]
        for line, positions in (("small-1", (0.9, 1.8, 2.7)), ("large-1", (1.0, 2.0, 3.0)))
    )  # fmt: skip
    seat = Aligned(clock="running_hours", job="small", at_or_before=True, inference=DIRECT)
    seating = Seating(name="large vs small", players=(DIRECT, seat), conditions=DEALS[:1], games=2)
    benchmark = Benchmark(name="paired", environment=WORLD, seatings=(seating,))

    def lines(saved: int) -> list[Line]:
        return [
            Line(name="small-1", job="small", checkpoints=tuple(small[:saved])),
            Line(name="large-1", job="large", checkpoints=tuple(large[:2])),
        ]

    def seated() -> dict[str, str]:
        """Each held large checkpoint, by name, and the small checkpoint it played."""
        delivered = played(benchmark, experiment=EXPERIMENT, checkpoints=large[:2],
                           storages=[bucket], lines=lines(3)).delivered  # fmt: skip
        pairs = [each.match.players for each in delivered]
        return {
            str(held.checkpoint.key): str(other.checkpoint.key)
            for held, other in pairs
            if isinstance(held, CheckpointPlayer)
            and isinstance(other, CheckpointPlayer)
            and held.checkpoint.line == "large-1"
        }

    # Small has saved 0.9h and 1.8h: large's 1h seats 0.9h; large's 2h is not yet,
    # since small may still save nearer before 2h.
    assert hold(benchmark, experiment=EXPERIMENT, play=play, storages=[bucket],
                checkpoints=large[:2], lines=lines(2)).played == 1  # fmt: skip
    assert seated() == {str(large[0].key): str(small[0].key)}
    # Small saves 2.7h, past 2h: large's 2h seats 1.8h, the latest at or before it.
    assert hold(benchmark, experiment=EXPERIMENT, play=play, storages=[bucket],
                lines=lines(3)).played == 1  # fmt: skip
    assert seated() == {str(large[0].key): str(small[0].key), str(large[1].key): str(small[1].key)}


def test_checkpoints_held_on_the_go_then_a_seating_added_in_batch(tmp_path: Path) -> None:
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
    added = Seating(name="search vs greedy", players=(SEARCH, greedy), conditions=DEALS, games=2)
    grown = progress.model_copy(update={"seatings": (*progress.seatings, added)})
    batch = hold(
        grown,
        experiment=EXPERIMENT,
        play=play,
        storages=[bucket],
        directory=tmp_path / "machines" / "batch",
    )
    # Only the new seating's matches, for every checkpoint held so far.
    assert (batch.played, batch.skipped) == (3 * len(DEALS), 0)

    lines = {line.seating: line for line in scores(statsdb(EXPERIMENT, bucket), "anchors", table="plays",
                                                   value="won")}  # fmt: skip
    assert sorted(lines) == sorted(each.name for each in grown.seatings)
    for line in lines.values():
        assert (line.run, line.experiment) == ("ppo-1", "ppo")
        assert [(p.ordinal, authored(p.at)) for p in line.points] == [
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
             directory=tmp_path / str(each.key.rsplit("/", 1)[-1]))  # fmt: skip
    measured = MeasuredStorage(bucket)
    again = hold(
        progress,
        experiment=EXPERIMENT,
        play=play,
        storages=[measured],
        directory=tmp_path / "batch",
    )
    assert again.played == 0
    # One listing of the bucket and one of its own run; each eval run's run.json,
    # tournament.json and its one block's index.
    assert (measured.listings, measured.requests) == (1 + 1, 3 + 3 + 3)

    # Eval runs held for no tournament already played a seating about to be added:
    # two checkpoints' matches in this environment, the third's in another.
    added = Seating(name="search vs greedy", players=(SEARCH, scripted(tmp_path, "greedy", 4)),
                    conditions=DEALS, games=2)  # fmt: skip
    for run, world, held in (
        ("adhoc", WORLD, checkpoints[:2]),
        ("elsewhere", OTHER, checkpoints[2:]),
    ):
        evaluate(experiment=EXPERIMENT, environment=world, play=play, run=run, storages=[bucket],
                 matches=[match for each in held for match in added.matches(each)],
                 directory=tmp_path / run)  # fmt: skip
    grown = progress.model_copy(update={"seatings": (*progress.seatings, added)})
    measured = MeasuredStorage(bucket)
    batch = hold(
        grown, experiment=EXPERIMENT, play=play, storages=[measured], directory=tmp_path / "batch"
    )
    # Only the third checkpoint's: a match is reused by key within one environment.
    assert batch.played == len(DEALS)
    # Each eval run's run.json; the four held for the tournament by tournament.json, the
    # two held for none by environment.json; the index of each block in this environment.
    assert (measured.listings, measured.requests) == (1 + 1, 6 + 4 + 2 + 4)
    lines = {line.seating: line for line in scores(statsdb(EXPERIMENT, bucket), "anchors", table="plays",
                                                   value="won")}  # fmt: skip
    assert [p.matches for p in lines["search vs greedy"].points] == [len(DEALS)] * 3


def _agent(player: object) -> object:
    """A player as published etude-sdk stored it: an `Agent` keyed by checkpoint or code."""
    if not isinstance(player, dict):
        return player
    if player.get("kind") == "model":
        key = {
            "checkpoint": player["checkpoint"]["key"],
            "code": None,
            "recorded": None,
            "config": player["inference"]["config"],
        }
        return {
            "key": key,
            "name": player["name"],
            "artifact": player["checkpoint"]["artifact"],
            "version": player["inference"]["version"],
        }
    if player.get("kind") == "code":
        key = {
            "checkpoint": None,
            "code": player["name"],
            "recorded": None,
            "config": player["config"],
        }
        return {"key": key, "name": player["name"], "artifact": None, "version": player["version"]}
    return {name: _published(value) for name, value in player.items()}


def _published(value: object) -> object:
    if isinstance(value, list):
        return [_agent(each) for each in value]
    return _agent(value)


def _as_published(bucket: Path) -> None:
    """Rewrite every eval run as published etude-sdk stored it: agents, and seats as `player`."""
    for file in bucket.rglob("tournament.json"):
        file.write_text(json.dumps(_published(json.loads(file.read_text()))))
    for file in bucket.rglob("matches/*.json"):
        record = json.loads(file.read_text())
        record["match"]["agents"] = _published(record["match"].pop("players"))
        file.write_text(json.dumps(record))
    for file in bucket.rglob("tables/*.parquet"):
        duckdb.sql(
            f"copy (select * rename (seat as player) from read_parquet('{file}'))"
            f" to '{file}' (format parquet)"
        )


def test_a_batch_hold_over_eval_runs_stored_with_agents_replays_nothing(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    for each in trained(tmp_path, bucket):
        hold(progress, experiment=EXPERIMENT, checkpoint=each, play=play, storages=[bucket],
             directory=tmp_path / str(each.key.rsplit("/", 1)[-1]))  # fmt: skip
    before = scores(statsdb(EXPERIMENT, bucket), "anchors", table="plays", value="won")
    _as_published(tmp_path / "bucket")
    assert all(
        '"agents"' in file.read_text() for file in (tmp_path / "bucket").rglob("matches/*.json")
    )

    again = hold(
        progress, experiment=EXPERIMENT, play=play, storages=[bucket], directory=tmp_path / "batch"
    )
    # The stored keys and seating definitions are unchanged: nothing replays, nothing conflicts.
    assert again.played == 0
    db = statsdb(EXPERIMENT, bucket, rebuild=True)
    assert scores(db, "anchors", table="plays", value="won") == before


def test_a_seating_name_redefined_is_refused_by_a_batch_hold_and_on_read(
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
    changed = progress.seatings[0].model_copy(update={"games": 8})
    other = progress.model_copy(update={"seatings": (changed,)})
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
    assert refused.value.code == "seating_conflict"
    db = statsdb(EXPERIMENT, bucket)
    assert db.sql("select conflict from tournaments").fetchall() == [("seating_conflict",)]
    with pytest.raises(EtudeError) as refused:
        scores(db, "anchors", table="plays", value="won")
    assert refused.value.code == "seating_conflict"


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
        assert [path.name for path in (track / "ppo").iterdir()] == [held.name]
        shutil.copytree(track / "ppo" / held.name, bucket.root / "delivered" / held.name)
    monkeypatch.delenv("ETUDE_RUN")
    lines = scores(statsdb(EXPERIMENT, bucket), "anchors", table="plays", value="won")
    assert sorted(line.seating for line in lines) == sorted(m.name for m in progress.seatings)
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
            share=Share(index=k, count=2),
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
        return hold(league, experiment=EXPERIMENT, enter=finals, play=play, share=Share(index=k, count=4), storages=[bucket],
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


def test_each_share_of_a_checkpoint_benchmark_seats_fewer_checkpoints_than_a_round_deal(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)
    shares: list[list[Match]] = [[], []]
    for k, share in enumerate(shares):

        def seen(match: Match, share: list[Match] = share) -> Tables:
            share.append(match)
            return play(match)

        hold(anchors(tmp_path), experiment=EXPERIMENT, checkpoints=checkpoints, play=seen,
             share=Share(index=k, count=2), storages=[bucket], directory=tmp_path / f"machine-{k}")  # fmt: skip

    def seated(matches: Sequence[Match]) -> int:
        return len(
            {
                each.checkpoint.key
                for match in matches
                for each in match.players
                if isinstance(each, CheckpointPlayer)
            }
        )

    # Each checkpoint calls for 3 + 2 + 1 matches.
    assert [len(share) for share in shares] == [9, 9]
    every = sorted((match for share in shares for match in share), key=lambda match: match.key)
    round_deal = [seated(every[k::2]) for k in (0, 1)]
    assert [seated(share) for share in shares] == [2, 2] and round_deal == [3, 3]


def test_a_seed_added_splits_the_point_again_and_counts_only_the_new_split(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    saved = trained(tmp_path, bucket)[:1]
    small, small_2, large = (
        ref.model_copy(update={"key": checkpoint_id(run=line, name=ref.key.rsplit("/", 1)[-1]),
                               "at": {"updates": 100}, "line": line})
        for ref in saved
        for line in ("small-1", "small-2", "large-1")
    )  # fmt: skip
    seating = Seating(
        name="large vs small",
        players=(DIRECT, Aligned(clock="updates", job="small", inference=DIRECT)),
        conditions=DEALS[:1],
        games=4,
    )
    benchmark = Benchmark(name="paired", environment=WORLD, seatings=(seating,))
    one = [
        Line(name="small-1", seed=1, job="small", checkpoints=(small,)),
        Line(name="large-1", seed=1, job="large", checkpoints=(large,)),
    ]
    two = [*one, Line(name="small-2", seed=2, job="small", checkpoints=(small_2,))]

    def held(lines: list[Line]) -> int:
        return hold(benchmark, experiment=EXPERIMENT, play=play, storages=[bucket],
                    checkpoints=[large], lines=lines).played  # fmt: skip

    def point() -> tuple[int, int]:
        with statsdb(EXPERIMENT, bucket) as db:
            (line,) = scores(db, "paired", table="plays", value="won")
        (only,) = line.points
        return only.matches, only.rows

    # One seed each: one match of every game.
    assert held(one) == 1
    assert point() == (1, 4)
    # A second seed of small: two matches of two games each, and the first no longer counts.
    assert held(two) == 2
    assert point() == (2, 4)
    assert held(two) == 0


def test_scores_of_checkpoints_no_training_run_recorded_follow_their_names(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    saved = ["updates-1000.pt", "updates-150.pt", "updates-900.pt"]
    for name in saved:
        # An outside checkpoint, keyed by its address.
        artifact = ArtifactRef(uri=f"s3://models/{name}", sha256=sha256(name.encode()).hexdigest())
        checkpoint = CheckpointRef(key=checkpoint_id(address=artifact.uri), artifact=artifact)
        hold(progress, experiment=EXPERIMENT, checkpoint=checkpoint, play=play, storages=[bucket],
             directory=tmp_path / name)  # fmt: skip
    for line in scores(statsdb(EXPERIMENT, bucket), "anchors", table="plays", value="won"):
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
            Seating(name=f"{DIRECT.player(new).name} vs {DIRECT.player(old).name}",
                    players=(DIRECT.player(new), DIRECT.player(old)), conditions=DEALS, games=2)
            for old, new in zip(saved, saved[1:])
        ]  # fmt: skip
        anchor = Seating(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS,
                         games=2)  # fmt: skip
        return Benchmark(name="ladder", environment=WORLD, seatings=(anchor, *consecutive))

    # On the go, a checkpoint plays only the seatings its model seats call for.
    first = hold(wanted(checkpoints[:2]), experiment=EXPERIMENT, checkpoint=checkpoints[0], play=play,
                 storages=[bucket],
                 directory=tmp_path / "go")  # fmt: skip
    assert first.played == len(DEALS)

    def held(saved: list[CheckpointRef], machine: str) -> int:
        definition = wanted(saved)
        for checkpoint in saved:  # what a training run's on-the-go holds would add
            hold(definition, experiment=EXPERIMENT, checkpoint=checkpoint, play=play, storages=[bucket],
                 directory=tmp_path / machine / str(checkpoint.key.rsplit("/", 1)[-1]))  # fmt: skip
        return hold(definition, experiment=EXPERIMENT, play=play, storages=[bucket],
                    directory=tmp_path / machine).played  # fmt: skip

    # The first pair is played when its second checkpoint appears, then only the new pair.
    assert held(checkpoints[:2], "pass-1") == len(DEALS)
    assert held(checkpoints, "pass-2") == len(DEALS)
    assert held(checkpoints, "pass-3") == 0

    db = statsdb(EXPERIMENT, bucket)
    pairs = db.sql(
        "select seating, count(*) from schedule where tournament = 'ladder' and checkpoint is null"
        " group by seating order by seating"
    ).fetchall()
    assert pairs == [
        ("updates-200.pt vs updates-100.pt", 3),
        ("updates-300.pt vs updates-200.pt", 3),
    ]
    # Scores follow only the seatings with a model seat.
    assert [line.seating for line in scores(db, "ladder", table="plays", value="won")] == [
        "vs heuristic"
    ]
    # The later checkpoint wins each pair; its results are found by seating name.
    later = db.sql(
        "select s.seating, avg(p.won::int) from plays p"
        " join schedule s on s.match = p.match and s.tournament = 'ladder'"
        " where s.checkpoint is null and p.seat = 0 group by s.seating"
    ).fetchall()
    assert all(value > 0.5 for _, value in later)


def test_two_keys_holding_the_same_bytes_are_two_models_each_played(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    heuristic = scripted(tmp_path, "heuristic", 0)
    vs = Seating(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS, games=2)
    ladder = Benchmark(name="ladder", environment=WORLD, seatings=(vs,))
    # Two runs warm-started from one file: the same bytes, two checkpoints.
    file = ArtifactRef(uri="s3://models/init.pt", sha256=sha256(b"init").hexdigest())
    starts = [
        CheckpointRef(key=checkpoint_id(run=run, name="updates-100.pt"), artifact=file)
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


def test_a_new_version_of_a_players_code_reuses_what_an_older_one_played(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    checkpoints = trained(tmp_path, bucket)

    def ladder() -> Benchmark:
        heuristic = scripted(tmp_path, "heuristic", 0)
        vs = Seating(name="vs heuristic", players=(DIRECT, heuristic), conditions=DEALS, games=2)
        return Benchmark(name="ladder", environment=WORLD, seatings=(vs,))

    first = ladder()
    assert hold(first, experiment=EXPERIMENT, checkpoints=checkpoints[:1], play=play,
                storages=[bucket], directory=tmp_path / "v1").played == len(DEALS)  # fmt: skip
    # The heuristic's file changes: its version, not its key, so nothing is played again
    # and the seating is not defined two ways.
    (tmp_path / "players" / "heuristic.py").write_text("STRENGTH = 0  # edited\n")
    second = ladder()
    before, after = first.seatings[0].players[1], second.seatings[0].players[1]
    assert isinstance(before, Code) and isinstance(after, Code)
    assert (after.key, after.version.commit) == (before.key, before.version.commit)
    assert hold(second, experiment=EXPERIMENT, checkpoints=checkpoints[:1], play=play,
                storages=[bucket], directory=tmp_path / "v2").played == 0  # fmt: skip
    seats = (
        statsdb(EXPERIMENT, bucket)
        .sql("select count(distinct source) from seats where player like 'code:heuristic%'")
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
    # Its key is the digest it had, which no key since is: its matches stay its own.
    assert str(old.key) == f"etude-sdk-0.2:{sha}"


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
    # other's environment.json; and each run's one block index, wherever it delivers.
    assert (measured.listings, measured.requests) == (1, 2 + 1 + 1 + 2)
    # A match that fails is failed, not new, until something delivers it.
    with pytest.raises(EtudeError, match="match_failed"):
        hold(progress, experiment=EXPERIMENT, checkpoint=checkpoints[2], play=play_failing_deal_one,
             storages=[bucket], directory=tmp_path / "third")  # fmt: skip
    third = played(progress, experiment=EXPERIMENT, checkpoints=checkpoints[2:],
                   storages=[bucket], directory=reader)  # fmt: skip
    (failed,) = third.failed
    assert failed.conditions == {"deal": 1} and not third.delivered


def test_stored_runs_read_once_answer_played_for_every_tournament_asked(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    progress = anchors(tmp_path)
    checkpoints = trained(tmp_path, bucket)
    hold(progress, experiment=EXPERIMENT, checkpoint=checkpoints[0], play=play,
         storages=[bucket], directory=tmp_path / "first")  # fmt: skip
    evaluate(experiment=EXPERIMENT, environment=WORLD, play=play, run="other",
             matches=[Match(players=(scripted(tmp_path, "a", 1), scripted(tmp_path, "b", 2)),
                            conditions={"deal": 9}, games=1)],
             storages=[bucket], directory=tmp_path / "other")  # fmt: skip
    reader = tmp_path / "reader"
    measured = MeasuredStorage(bucket)
    runs = stored_runs(EXPERIMENT, storages=[measured], directory=reader)
    asked = [
        played(progress, experiment=EXPERIMENT, checkpoints=each, runs=runs)
        for each in (checkpoints[:1], checkpoints[1:2], checkpoints[2:])
    ]
    # Three tournaments' worth of asking reads what one does: one listing, each eval
    # run's run.json, the tournament's tournament.json and the other's environment.json,
    # and each run's block index once.
    assert (measured.listings, measured.requests) == (1, 2 + 1 + 1 + 2)
    assert [len(each.delivered) for each in asked] == [6, 0, 0]
    assert asked[0] == played(progress, experiment=EXPERIMENT, checkpoints=checkpoints[:1],
                              storages=[bucket], directory=reader)  # fmt: skip
    with pytest.raises(EtudeError, match="stored_runs_of_another_experiment"):
        played(progress, experiment="other", runs=runs)
