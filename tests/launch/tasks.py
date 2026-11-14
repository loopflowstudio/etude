"""A typed training loop for command-driven CLI fixtures.

The command stays fixture data. It runs under the assigned training identity
and can exercise tracking in a child process as production training code does.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from etude.spec import Runtime
from etude.stopping import Stopped

RUNTIME = Runtime(python=os.environ.get("ETUDE_TEST_PYTHON", sys.executable))


class Command(BaseModel):
    command: str
    text: str | None = None


def train_command(config: Command) -> None:
    with tempfile.TemporaryDirectory() as directory:
        environment = dict(os.environ)
        if config.text is not None:
            path = Path(directory) / "config"
            path.write_text(config.text)
            environment["ETUDE_CONFIG"] = str(path)
        done = subprocess.run(config.command, shell=True, env=environment, check=False)
        if done.returncode == 75:
            raise Stopped()
        done.check_returncode()
