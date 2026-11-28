"""Retain the thread budget visible when an authored loading module imports."""

import json
import os
from pathlib import Path
from typing import cast

import numpy as np

from etude.checkpoints import CheckpointRef
from etude.eval.agents import ModelForward
from etude.eval.resources import THREAD_VARIABLES
from tests.eval.arithmetic import load

BUDGET = {name: os.environ.get(name) for name in THREAD_VARIABLES}


def load_checkpoint(checkpoint: CheckpointRef) -> ModelForward:
    Path(os.environ["ETUDE_AGENTS_DIR"], "threads.json").write_text(json.dumps(BUDGET))
    return load(checkpoint)


def load_array_checkpoint(checkpoint: CheckpointRef) -> ModelForward:
    Path(os.environ["ETUDE_AGENTS_DIR"], "threads.json").write_text(json.dumps(BUDGET))
    bias = int(checkpoint.path.read_text())

    def forward(inputs: list[object]) -> list[object]:
        return [cast(np.ndarray, batch) + bias for batch in inputs]

    return forward
