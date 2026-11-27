"""Declared sampling choices and the game-side objects that choose actions."""

from collections.abc import Mapping, Sequence
from hashlib import sha256
from math import exp
from pathlib import Path
from random import Random
from typing import cast

from pydantic import Field

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward
from etude.eval.records import CheckpointPlayer, Match, Seat, checkpoint_player
from etude.eval.result import Tables
from etude.records import ArtifactRef, Record


class Sampled(Record):
    """The declared player: these fields, including temperature, define its identity."""

    over: Seat
    temperature: float = Field(default=1.0, gt=0, allow_inf_nan=False)


class Sampler:
    """The built object: play calls choose, and owns its random stream."""

    def __init__(self, forward: Forward, temperature: float | None) -> None:
        self.forward = forward
        self.temperature = temperature

    async def choose(self, observation: object, random: Random) -> int:
        # This game owns the logits shape returned by its loading module.
        [output] = await self.forward([observation])
        logits = cast(list[float], output)
        if self.temperature is None:
            return max(range(len(logits)), key=logits.__getitem__)
        weights = [exp((value - max(logits)) / self.temperature) for value in logits]
        return random.choices(range(len(logits)), weights=weights)[0]


def build(player: object, leaves: Mapping[str, Forward]) -> Sampler:
    if isinstance(player, Sampled) and isinstance(player.over, CheckpointPlayer):
        return Sampler(leaves[player.over.checkpoint.key], player.temperature)
    raise TypeError(f"unknown player {type(player).__name__}")


async def play(match: Match, agents: Sequence[Forward | Sampler]) -> Tables:
    random = Random(match.seed)
    players = [agent if isinstance(agent, Sampler) else Sampler(agent, None) for agent in agents]
    # A two-action bandit: action 1 earns a reward, action 0 does not.
    return {
        "scores": [
            {"seat": seat, "score": await player.choose(None, random)}
            for seat, player in enumerate(players)
        ]
    }


def checkpoint(name: str) -> CheckpointPlayer:
    path = Path(__file__).with_name("agent.txt").resolve()
    data = path.read_bytes()
    return checkpoint_player(
        CheckpointRef(
            address=name,
            artifact=ArtifactRef(
                uri=path.as_uri(), sha256=sha256(data).hexdigest(), size_bytes=len(data)
            ),
        )
    )
