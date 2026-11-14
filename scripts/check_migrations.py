"""Fail when a released migration was edited, renamed or deleted, or its record rewritten.

A real launcher.sqlite only ever holds released migrations, and refuses one whose file
later changed, so a released migration is frozen; add a new one instead. Unreleased
migrations only reach copies, so they may change until their release. Usage:
`uv run python scripts/check_migrations.py`; it needs `origin/main` and the release
tags fetched.
"""

from __future__ import annotations

import json
import subprocess
import sys

from etude.errors import LauncherError
from etude.launch.database import migrations, released

RELEASED = "src/etude/launch/migrations/released.json"


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def main() -> int:
    tag = _git("describe", "--tags", "--abbrev=0", "origin/main").decode().strip()
    current = [r.model_dump() for r in released()]
    problems = []
    for ref in ("origin/main", tag):
        listed = _git("ls-tree", "--name-only", ref, "--", RELEASED).decode().strip()
        recorded = json.loads(_git("show", f"{ref}:{RELEASED}")) if listed else []
        if current[: len(recorded)] != recorded:
            problems.append(f"{RELEASED} no longer begins with what {ref} recorded")
    try:
        migrations()
    except LauncherError as error:
        problems.append(str(error))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print("released migrations never change; add a new migration instead", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
