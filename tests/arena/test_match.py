import json
from pathlib import Path

from manabot.arena.match import play_cell
from manabot.arena.models import ArenaContract
from manabot.arena.replay import read_trace, replay_games

ROOT = Path(__file__).resolve().parents[2]


def test_same_deal_seat_swap_retains_and_replays_commands(tmp_path: Path) -> None:
    contract = ArenaContract.model_validate(
        json.loads(
            (ROOT / "experiments/contracts/int-6-skill-arena-v1.json").read_text()
        )
    )
    random, scripted = contract.anchors[:2]
    rows, trace, replay = play_cell(
        key=contract.key,
        player_a=random,
        player_b=scripted,
        deal_seeds=(777,),
        out_dir=tmp_path,
    )
    assert len(rows) == 2
    assert {row["leg"] for row in rows} == {0, 1}
    assert {row["deal_seed"] for row in rows} == {777}
    assert {row["player_a_seat"] for row in rows} == {0, 1}
    assert all(row["replay_passed"] for row in rows)
    assert replay["passed"]
    games = read_trace(Path(trace["path"]))
    assert replay_games(games).passed
    assert all(game["decisions"] for game in games)
