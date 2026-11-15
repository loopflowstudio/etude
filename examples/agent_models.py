"""A tiny loaded player; real models may import their framework in this module."""

from collections.abc import Sequence

from pydantic import BaseModel

from etude.eval.agents import Agent
from etude.eval.records import Player


class Loading(BaseModel):
    bias: int = 1


class Adder:
    def __init__(self, bias: int) -> None:
        self.bias = bias

    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return [int(str(row)) + self.bias for row in rows]


def load(player: Player, config: Loading) -> Agent:
    return Adder(config.bias)
