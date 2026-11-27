"""Arithmetic player trees for composition and batching fixtures."""

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward, ModelForward
from etude.eval.records import CheckpointPlayer, Match, Seat, checkpoint_player
from etude.eval.result import Tables
from etude.records import ArtifactRef, Record


class Shift(Record):
    over: Seat
    amount: int = 1


class Shifted:
    def __init__(self, over: Forward, amount: int) -> None:
        self.over = over
        self.amount = amount

    async def __call__(self, inputs: list[object], *, rows: int | None = None) -> list[object]:
        return [int(str(value)) + self.amount for value in await self.over(inputs, rows=rows)]


def build(player: object, leaves: Mapping[str, Forward]) -> Forward:
    if isinstance(player, CheckpointPlayer):
        return leaves[player.checkpoint.key]
    if isinstance(player, Shift):
        return Shifted(build(player.over, leaves), player.amount)
    raise TypeError(f"unknown player {type(player).__name__}")


async def play(match: Match, agents: Sequence[Forward]) -> Tables:
    return {
        "scores": [
            {"seat": seat, "score": int(str((await agents[seat]([match.games]))[0]))}
            for seat in range(len(match.players))
        ]
    }


def checkpoint(name: str) -> CheckpointPlayer:
    path = Path(__file__).parents[2] / "examples/agent.txt"
    data = path.read_bytes()
    return checkpoint_player(
        CheckpointRef(
            address=name,
            artifact=ArtifactRef(
                uri=path.as_uri(), sha256=sha256(data).hexdigest(), size_bytes=len(data)
            ),
        )
    )


class Adder:
    def __init__(self, bias: int) -> None:
        self.bias = bias

    def __call__(self, inputs: list[object]) -> list[object]:
        return [int(str(item)) + self.bias for item in inputs]


def load(checkpoint: CheckpointRef) -> ModelForward:
    return Adder(int(checkpoint.path.read_text()))
