"""A minimal pre-collated NumPy model; a real loader may import Torch or MLX."""

from typing import cast

import numpy as np

from etude.checkpoints import CheckpointRef
from etude.eval.agents import ModelForward


def load(saved: CheckpointRef) -> ModelForward:
    bias = int(saved.path.read_text())

    def forward(inputs: list[object]) -> list[object]:
        # Each input is a pre-collated 1-D NumPy array, with one output per input.
        return [cast(np.ndarray, batch) + bias for batch in inputs]

    return forward
