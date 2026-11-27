"""A minimal two-action model returning logits; a real loader may import Torch."""

from etude.checkpoints import CheckpointRef
from etude.eval.agents import ModelForward


class Network:
    def __init__(self, bias: float) -> None:
        self.bias = bias

    def __call__(self, inputs: list[object]) -> list[object]:
        return [[0.0, self.bias] for _ in inputs]


def load(checkpoint: CheckpointRef) -> ModelForward:
    return Network(float(checkpoint.path.read_text()))
