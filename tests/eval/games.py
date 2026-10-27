"""Games the eval tests play, importable by name for worker processes."""

from __future__ import annotations

import re
from hashlib import sha256

from etude.eval.match import Result, Tables
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


def play_with_evidence(match: Match) -> Result:
    """`play`, keeping each game's evidence as a blob that its `games` row names."""
    tables = play(match)
    evidence = [f"{match.key} game {row['game']}".encode() * 40 for row in tables["games"]]
    games = [
        {**row, "evidence_sha256": sha256(blob).hexdigest()}
        for row, blob in zip(tables["games"], evidence, strict=True)
    ]
    return Result(tables={"plays": tables["plays"], "games": games}, blobs=evidence)


def play_cards(match: Match) -> Tables:
    """manabot's card tables: one `card_plays` row per game and seat, `card_counts` per card.

    Deal 2's games have no winner, so no row of that match gives `win`.
    """
    deal = int(str(match.conditions["deal"]))
    plays: Rows = []
    counts: Rows = []
    for game in range(match.games):
        name = f"{deal}-{game}"
        eligible = deal != 2
        for seat in (0, 1):
            plays.append(
                {
                    "game": name,
                    "seat": seat,
                    "deal": deal,
                    "deck": ("red", "blue")[(seat + game) % 2],
                    "opponent_deck": ("red", "blue")[(seat + game + 1) % 2],
                    "on_play": seat == game % 2,
                    "eligible": eligible,
                    "outcome": "win" if seat == (deal + game) % 2 else "loss",
                    "win": int(seat == (deal + game) % 2) if eligible else None,
                }
            )
            for card in range(3):
                vary = deal + game + seat + card
                counts.append(
                    {
                        "game": name,
                        "seat": seat,
                        "card": f"card{card}",
                        "deck_copies": vary % 3,
                        "opening_copies": vary % 2,
                        "drawn_copies": (vary + 1) % 2,
                        "gih_copies": vary % 2 if vary % 3 else 0,
                    }
                )
    return {"card_plays": plays, "card_counts": counts}
