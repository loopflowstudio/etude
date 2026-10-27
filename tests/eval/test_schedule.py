"""Scheduled play order: fewer checkpoint loads for the same matches, and the same results."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import sha256
from pathlib import Path

import duckdb

from etude.eval.match import Tables, environment, evaluate
from etude.eval.records import CheckpointKey, CheckpointRef, Code, Inference, Match, Model
from etude.eval.schedule import checkpoints, ordered
from etude.run.records import ArtifactRef
from tests.eval.games import play

WORLD = environment("duel", config={"world": "w6"})
# The code that plays every player here.
GAMES = Path(__file__).with_name("games.py")


class Runtime:
    """An author's inference runtime: the two checkpoints used last stay loaded."""

    def __init__(self) -> None:
        self.resident: list[CheckpointKey] = []
        self.loads = 0
        self.played: list[str] = []

    def play(self, match: Match) -> Tables:
        for key in dict.fromkeys(
            seated.checkpoint.key for seated in match.players if isinstance(seated, Model)
        ):
            if key in self.resident:
                self.resident.remove(key)
            else:
                self.loads += 1
            self.resident = [*self.resident, key][-2:]
        self.played.append(match.key)
        return play(match)


def checkpoint(updates: int) -> Model:
    name = f"updates-{updates}.pt"
    artifact = ArtifactRef(uri=f"s3://models/{name}", sha256=sha256(name.encode()).hexdigest())
    ref = CheckpointRef(key=CheckpointKey(run="ppo-1", name=name), artifact=artifact)
    return Inference().model(ref)


def code(name: str, strength: int) -> Code:
    return Code(name=name, source=[GAMES], config={"strength": strength})


def rating(checkpoints: int, deals: int, paired: int) -> list[Match]:
    """Checkpoints against the heuristic on `deals` deals, and adjacent pairs on `paired`."""
    rated = [checkpoint(50 * ordinal) for ordinal in range(1, checkpoints + 1)]
    heuristic = code("heuristic", 3)
    grid = [
        Match(players=(each, heuristic), conditions={"deal": deal}, games=2)
        for each in rated
        for deal in range(deals)
    ]
    pairs = [
        Match(players=pair, conditions={"deal": deal}, games=2)
        for pair in zip(rated, rated[1:], strict=False)
        for deal in range(paired)
    ]
    # `hold` passes a tournament's matches in key order.
    return sorted(grid + pairs, key=lambda match: match.key)


def loads(matches: Iterable[Match]) -> int:
    runtime = Runtime()
    for match in matches:
        runtime.play(match)
    return runtime.loads


def rows(directory: Path) -> list[tuple[object, ...]]:
    found = duckdb.sql(
        f"select match, seat, game, won, hits from read_parquet('{directory}/**/plays*.parquet')"
        " order by all"
    )
    return found.fetchall()


def test_a_rating_benchmark_in_scheduled_order_loads_each_checkpoint_about_once() -> None:
    matches = rating(20, deals=25, paired=10)
    assert len(matches) == 690
    assert (loads(matches), loads(ordered(matches))) == (806, 21)


def test_evaluate_plays_in_scheduled_order_and_order_changes_no_row(tmp_path: Path) -> None:
    matches = rating(4, deals=5, paired=2)
    runtime = Runtime()
    run = evaluate(experiment="rating", environment=WORLD, matches=matches,
                   play=runtime.play, directory=tmp_path)  # fmt: skip
    assert (run.played, loads(matches), runtime.loads) == (26, 24, 5)
    # The same results, row for row, as playing in key order.
    expected = sorted(
        (match.key, row["seat"], row["game"], row["won"], row["hits"])
        for match in matches
        for row in play(match)["plays"]
    )
    assert rows(tmp_path) == expected


def test_a_queue_of_code_players_plays_in_the_callers_order(tmp_path: Path) -> None:
    field = [code(f"script{strength}", strength) for strength in range(4)]
    given: Sequence[Match] = [
        Match(players=(field[a], field[b]), conditions={"deal": deal})
        for deal in range(3)
        for a in range(4)
        for b in range(4)
        if a != b
    ]
    runtime = Runtime()
    evaluate(experiment="league", environment=WORLD, matches=given, play=runtime.play,
             directory=tmp_path)  # fmt: skip
    assert runtime.played == [match.key for match in given]


def test_two_checkpoint_keys_holding_the_same_bytes_are_two_checkpoints() -> None:
    file = ArtifactRef(uri="s3://models/init.pt", sha256=sha256(b"init").hexdigest())
    starts = [
        Inference().model(CheckpointRef(
            key=CheckpointKey(run=run, name="updates-100.pt"), artifact=file))
        for run in ("ppo-1", "ppo-2")
    ]  # fmt: skip
    assert len(checkpoints(Match(players=(starts[0], starts[1])))) == 2
