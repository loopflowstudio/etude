"""Games the eval tests play, importable by name for worker processes."""

from __future__ import annotations

import re

from etude.eval.match import Tables
from etude.eval.records import Agent, Match

type Rows = list[dict[str, bool | int | float | str | None]]


def strength(seated: Agent) -> int:
    """The configured strength, or a checkpoint's, which grows with training, plus its search."""
    if "strength" in seated.config:
        return int(str(seated.config["strength"]))
    # The test trainer saves at updates 100, 200 and 300, named `updates-N`: 1, 3 and 5.
    updates = int(re.sub(r"\D", "", seated.name))
    return updates // 50 - 1 + int(str(seated.config.get("search", 0)))


def play(match: Match) -> Tables:
    """A duel: the stronger agent wins, except the game's first player wins every third game."""
    strengths = [strength(seated) for seated in match.agents]
    plays: Rows = []
    games: Rows = []
    for game in range(match.games):
        first = game % 2
        winner = first if game % 3 == 0 else max((1, 0), key=lambda seat: strengths[seat])
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
