"""Retain the thread budget visible when an authored loading module imports."""

import json
import os
from pathlib import Path

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Agent
from etude.eval.resources import THREAD_VARIABLES
from examples.agent_models import load

BUDGET = {name: os.environ.get(name) for name in THREAD_VARIABLES}


def load_checkpoint(checkpoint: CheckpointRef) -> Agent:
    Path(os.environ["ETUDE_AGENTS_DIR"], "threads.json").write_text(json.dumps(BUDGET))
    return load(checkpoint)
