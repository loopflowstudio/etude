"""A tiny checkpoint loader; real models may import their framework here."""

from collections.abc import Sequence

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Agent


class Adder:
    def __init__(self, bias: int) -> None:
        self.bias = bias

    def act(self, rows: Sequence[object]) -> Sequence[object]:
        return [int(str(row)) + self.bias for row in rows]


def load(checkpoint: CheckpointRef) -> Agent:
    return Adder(int(checkpoint.path.read_text()))
