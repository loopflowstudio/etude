"""Bounded mirror-shaped search: advance every world/action branch once per ply.

These arithmetic forwards keep batching, search and author-owned randomness
observable without a model framework or an expensive workload.
"""

from collections.abc import Mapping, Sequence
from random import Random
from typing import Literal, cast

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward
from etude.eval.records import Code, Match, Seat, player_checkpoints
from etude.eval.result import Tables
from etude.records import Record


class Mirror(Record):
    over: Seat
    worlds: int = 3
    actions: int = 2
    depth: int = 2
    choice: Literal["greedy", "sample"] = "sample"
    temperature: float = 1.0


class Search:
    def __init__(self, player: Mirror, forward: Forward) -> None:
        self.player = player
        self.forward = forward

    def choose(self, random: Random) -> int:
        branches: list[object] = list(range(self.player.worlds * self.player.actions))
        for _ in range(self.player.depth):
            branches = self.forward(branches)
        scores = [
            sum(
                cast(int, branches[world * self.player.actions + action])
                for world in range(self.player.worlds)
            )
            for action in range(self.player.actions)
        ]
        if self.player.choice == "greedy":
            return max(range(self.player.actions), key=scores.__getitem__)
        return random.choices(
            range(self.player.actions),
            weights=[score ** (1 / self.player.temperature) for score in scores],
        )[0]


def forward(inputs: list[object]) -> list[object]:
    return [cast(int, value) + 1 for value in inputs]


def load(checkpoint: CheckpointRef) -> Forward:
    return forward


def build(player: object, leaves: Mapping[str, Forward]) -> Search:
    if isinstance(player, Code):
        return Search(Mirror(over=player), forward)
    if not isinstance(player, Mirror):
        raise TypeError("expected Mirror")
    # The field has been resolved by the game-side reconstruction.
    return Search(player, leaves[player_checkpoints(player.over)[0].key])


def play(match: Match, agents: Sequence[Search]) -> Tables:
    random = Random(match.seed)
    return {
        "choices": [
            {"seat": seat, "decision": decision, "action": agent.choose(random)}
            for decision in range(2)
            for seat, agent in enumerate(agents)
        ]
    }
