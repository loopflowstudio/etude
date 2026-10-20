"""Games the eval tests play, importable by name for worker processes."""

from __future__ import annotations

from etude.eval.match import Tables
from etude.eval.records import Match

type Rows = list[dict[str, bool | int | float | str | None]]


def play(match: Match) -> Tables:
    """A duel: the stronger agent wins, except the game's first player wins every third game."""
    strength = [int(str(seated.config["strength"])) for seated in match.agents]
    plays: Rows = []
    games: Rows = []
    for game in range(match.games):
        first = game % 2
        winner = first if game % 3 == 0 else max((1, 0), key=lambda seat: strength[seat])
        for seat in (0, 1):
            plays.append(
                {
                    "game": game,
                    "player": seat,
                    "won": seat == winner,
                    "on_play": seat == first,
                    "attacks": 4,
                    "hits": 3 if seat == winner else 1,
                }
            )
        games.append({"game": game, "group": "all", "turns": 10 + game})
    return {"plays": plays, "games": games}


def play_unkeyed(match: Match) -> Tables:
    return {"plays": [{"game": 0, "player": 0, "won": True}, {"game": 0, "won": False}]}


def play_text_hits(match: Match) -> Tables:
    return {"plays": [{"game": 0, "player": 0, "hits": "three"}]}


def play_teams(match: Match) -> Tables:
    return {
        "teams": [
            {"game": game, "group": team, "won": (team == "north") == (game < 3)}
            for game in range(4)
            for team in ("north", "south")
        ]
    }
