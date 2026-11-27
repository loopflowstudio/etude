"""Scheduled play order: fewer checkpoint loads for the same matches, and the same results."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from hashlib import sha256
from pathlib import Path

import duckdb

from etude.checkpoints import CheckpointRef
from etude.eval.match import environment
from etude.eval.records import (
    CheckpointPlayer,
    Code,
    Match,
    checkpoint_player,
    player_checkpoints,
)
from etude.eval.schedule import checkpoints, ordered
from etude.records import ArtifactRef
from tests.eval.games import play
from tests.eval.playing import play_matches

WORLD = environment("duel", config={"world": "w6"})
# The code that plays every player here.
GAMES = Path(__file__).with_name("games.py")


def checkpoint(updates: int) -> CheckpointPlayer:
    name = f"updates-{updates}.pt"
    artifact = ArtifactRef(uri=f"s3://models/{name}", sha256=sha256(name.encode()).hexdigest())
    ref = CheckpointRef(experiment="", job="ppo-1", name=name, artifact=artifact)
    return checkpoint_player(ref)


def code(name: str, strength: int) -> Code:
    return Code(name=name, source=[GAMES], config={"strength": strength})


def rating(checkpoints: int, deals: int, paired: int) -> list[Match]:
    """Checkpoints against the heuristic on `deals` deals, and adjacent pairs on `paired`."""
    rated = [checkpoint(50 * ordinal) for ordinal in range(1, checkpoints + 1)]
    heuristic = code("heuristic", 3)
    grid = [
        Match(players=(each, heuristic), config={"deal": deal}, games=2)
        for each in rated
        for deal in range(deals)
    ]
    pairs = [
        Match(players=pair, config={"deal": deal}, games=2)
        for pair in zip(rated, rated[1:], strict=False)
        for deal in range(paired)
    ]
    # `hold` passes a tournament's matches in key order.
    return sorted(grid + pairs, key=lambda match: match.key)


def loads(matches: Iterable[Match]) -> int:
    """Count loads with the two most recently used checkpoints kept resident."""
    resident: list[str] = []
    count = 0
    for match in matches:
        for key in dict.fromkeys(
            ref.key for player in match.players for ref in player_checkpoints(player)
        ):
            if key in resident:
                resident.remove(key)
            else:
                count += 1
            resident = [*resident, key][-2:]
    return count


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
    run = play_matches(experiment="rating", environment=WORLD, matches=matches,
                   play=play, directory=tmp_path)  # fmt: skip
    assert (run.played, loads(matches), loads(ordered(matches))) == (26, 24, 5)
    # The same results, row for row, as playing in key order.
    expected = sorted(
        (match.key, row["seat"], row["game"], row["won"], row["hits"])
        for match in matches
        for row in play(match, [])["plays"]
    )
    assert rows(tmp_path) == expected


def test_a_queue_of_code_players_plays_in_the_callers_order() -> None:
    field = [code(f"script{strength}", strength) for strength in range(4)]
    given: Sequence[Match] = [
        Match(players=(field[a], field[b]), config={"deal": deal})
        for deal in range(3)
        for a in range(4)
        for b in range(4)
        if a != b
    ]
    assert list(ordered(given)) == list(given)


def test_two_checkpoint_keys_holding_the_same_bytes_are_two_checkpoints() -> None:
    file = ArtifactRef(uri="s3://models/init.pt", sha256=sha256(b"init").hexdigest())
    starts = [
        checkpoint_player(CheckpointRef(
            experiment="", job=run, name="updates-100.pt", artifact=file))
        for run in ("ppo-1", "ppo-2")
    ]  # fmt: skip
    assert len(checkpoints(Match(players=(starts[0], starts[1])))) == 2
