"""Games the eval tests play, importable by name for worker processes."""

from __future__ import annotations

import re

from etude.eval.match import Tables
from etude.eval.records import Match, Player

type Rows = list[dict[str, bool | int | float | str | None]]


def strength(player: Player) -> int:
    """The configured strength, or a checkpoint's, which grows with training, plus its search."""
    config = player.config
    if "strength" in config:
        return int(str(config["strength"]))
    # The test trainer saves at updates 100, 200 and 300, named `updates-N`: 1, 3 and 5.
    updates = int(re.sub(r"\D", "", player.name))
    return updates // 50 - 1 + int(str(config.get("search", 0)))


def play(match: Match) -> Tables:
    """A duel: the stronger player wins, except the game's first player wins every third game."""
    strengths = [strength(player) for player in match.players]
    plays: Rows = []
    games: Rows = []
    for game in range(match.games):
        first = game % 2
        winner = first if game % 3 == 0 else max((1, 0), key=lambda seat: strengths[seat])
        for seat in (0, 1):
            plays.append(
                {
                    "game": game,
                    "seat": seat,
                    "won": seat == winner,
                    "on_play": seat == first,
                    "attacks": 4,
                    "hits": 3 if seat == winner else 1,
                }
            )
        games.append({"game": game, "group": "all", "turns": 10 + game})
    return {"plays": plays, "games": games}


def play_game_seats(match: Match) -> Tables:
    """`play`, with the game's own seat: the players sit the other way round in odd games."""
    tables = play(match)
    plays = [
        {**row, "game_seat": (int(str(row["seat"])) + int(str(row["game"]))) % 2}
        for row in tables["plays"]
    ]
    return {**tables, "plays": plays}


def play_unkeyed(match: Match) -> Tables:
    """A row naming its seat as published etude-sdk did, `player`, names no seat."""
    return {"plays": [{"game": 0, "seat": 0, "won": True}, {"game": 0, "player": 1, "won": False}]}


def play_seat_and_player(match: Match) -> Tables:
    """A row with a column `player`, the seat's name in older stored tables."""
    return {"plays": [{"game": 0, "seat": 0, "player": 1, "won": True}]}


def play_text_hits(match: Match) -> Tables:
    return {"plays": [{"game": 0, "seat": 0, "hits": "three"}]}


def play_teams(match: Match) -> Tables:
    return {
        "teams": [
            {"game": game, "group": team, "won": (team == "north") == (game < 3)}
            for game in range(4)
            for team in ("north", "south")
        ]
    }


def play_failing_deal_one(match: Match) -> Tables:
    """`play`, except that deal 1 raises, as a game with a bug in one deal would."""
    if match.conditions.get("deal") == 1:
        raise RuntimeError("deal 1 crashed")
    return play(match)
