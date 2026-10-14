"""The machine-side entry point; never opens a launcher database."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from etude.launch.providers.cleanup import command as cleanup_command
from etude.launch.worker import attempts, courier


def work(directory: Path | None, bootstrap: str | None, root: Path, cleanup: str) -> int:
    if directory is not None:
        return attempts.run(directory)
    command: list[str] = json.loads(cleanup)
    if not command:
        command = cleanup_command()

    def finish() -> None:
        if command:
            subprocess.run(command, check=True)

    assert bootstrap is not None
    courier.run(bootstrap, finish, root.expanduser())
    return 0
