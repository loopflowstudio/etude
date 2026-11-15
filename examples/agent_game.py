"""Light game and player composition; model loading belongs in agent_models."""

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Agent
from etude.eval.records import CheckpointPlayer, Held, Match, Seat
from etude.eval.result import Tables
from etude.records import ArtifactRef, Record


class Shift(Record):
    over: Seat
    amount: int = 1


class Shifted:
    def __init__(self, over: Agent, amount: int) -> None:
        self.over = over
        self.amount = amount

    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return [int(str(value)) + self.amount for value in self.over.act(rows)]


def build(player: object, leaves: Mapping[str, Agent]) -> Agent:
    if isinstance(player, CheckpointPlayer):
        return leaves[player.checkpoint.key]
    if isinstance(player, Shift):
        return Shifted(build(player.over, leaves), player.amount)
    raise TypeError(f"unknown player {type(player).__name__}")


def play(match: Match, agents: Sequence[Agent]) -> Tables:
    return {
        "scores": [
            {"seat": seat, "score": int(str(agents[seat].act([match.games])[0]))}
            for seat in range(len(match.players))
        ]
    }


def checkpoint(name: str) -> CheckpointPlayer:
    path = Path(__file__).with_name("agent.txt").resolve()
    data = path.read_bytes()
    return Held().player(
        CheckpointRef(
            key=name,
            artifact=ArtifactRef(
                uri=path.as_uri(), sha256=sha256(data).hexdigest(), size_bytes=len(data)
            ),
        )
    )
