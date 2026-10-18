"""The machine-side entry point; never opens a launcher database."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from etude.launch.providers.cleanup import command as cleanup_command
from etude.launch.worker import attempts, courier


def work(directory: Path | None, bootstrap: str | None, root: Path, cleanup: str | None) -> int:
    if directory is not None:
        return attempts.run(directory)
    # Omitted cleanup uses the provider; an explicit empty command leaves it to the parent.
    command: list[str] = cleanup_command() if cleanup is None else json.loads(cleanup)

    def finish() -> None:
        if command:
            subprocess.run(command, check=True)

    assert bootstrap is not None
    courier.run(bootstrap, finish, root.expanduser())
    return 0
