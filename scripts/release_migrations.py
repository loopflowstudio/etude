"""Record every unreleased migration in `migrations/released.json`, with its reader floor.

`lf release run` runs this while preparing a release, after the version bump:
`uv run python scripts/release_migrations.py --version 0.1.3`. When the release raises the floor,
it prints the line the release notes carry, and adds it to `release/vVERSION/NOTES.md`
when that file exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from etude.launch.database import additive, migrations, release, released

RELEASED = Path("src/etude/launch/migrations/released.json")


def main(version: Annotated[str, typer.Option("--version")]) -> None:
    before = released()
    after = release(version)
    added = after[len(before) :]
    if not added:
        print("no unreleased migrations")
        return
    RELEASED.write_text(
        "[\n" + ",\n".join(f"  {json.dumps(r.model_dump())}" for r in after) + "\n]\n"
    )
    print(f"released {', '.join(r.name for r in added)}")
    ordered = migrations()
    raised = [
        m.name
        for i, m in enumerate(ordered)
        if m.name in {r.name for r in added} and not additive(m, ordered[:i])
    ]
    if raised:
        line = (
            f"etude < {version} can no longer open launcher.sqlite (raised by: {', '.join(raised)})"
        )
        print(line)
        notes = Path(f"release/v{version}/NOTES.md")
        if notes.exists() and line not in notes.read_text():
            notes.write_text(notes.read_text().rstrip("\n") + f"\n\n{line}\n")
    released.cache_clear()
    migrations.cache_clear()
    migrations()  # the recorded digests must match the files


if __name__ == "__main__":
    typer.run(main)
