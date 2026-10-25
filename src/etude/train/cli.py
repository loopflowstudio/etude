"""Tracking commands: `etude sync` and `etude checkpoints`.

Each imports what it runs inside itself (AGENTS.md): syncing files loads no
report and no launcher, and only sealing training logs loads NumPy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer


def sync(
    directory: Annotated[Path, typer.Option(help="the local runs directory")],
    to: Annotated[list[str] | None, typer.Option(help="s3://bucket/prefix or a path")] = None,
    seal: Annotated[bool, typer.Option(help="close segments a dead process left open")] = False,
) -> None:
    """Deliver sealed run files and stored checkpoints to each directory or bucket."""
    from etude.run.delivery import sync as deliver
    from etude.run.storage import storage_from_uri

    if seal:
        # Sealing turns training logs into blocks; only then does it need NumPy.
        from etude.train.run import seal_runs

        seal_runs(directory)
    undelivered = deliver(
        directory, storages=[storage_from_uri(uri) for uri in to or []], stopped=seal
    )
    for run, reasons in undelivered.items():
        print(f"{run}: {reasons}", file=sys.stderr)
    if undelivered:
        raise typer.Exit(1)


def checkpoints(
    source: Annotated[str, typer.Option(help="a runs directory or s3://bucket/prefix")],
    experiment: Annotated[str | None, typer.Option(help="only this experiment")] = None,
) -> None:
    """List recorded checkpoints."""
    from etude.run.listing import runs
    from etude.run.storage import storage_from_uri
    from etude.train.read import read

    storage = storage_from_uri(source)
    found = [pair for pair in runs(storage) if experiment in (None, pair[0])]
    for name, run in found:
        for record in read(storage, name, run).checkpoints:
            artifact = record.artifact
            size = "reference" if artifact.size_bytes is None else f"{artifact.size_bytes} bytes"
            at = " ".join(f"{clock}={value:g}" for clock, value in record.at.items())
            tags = ",".join(record.tags) or "-"
            print(f"{name}/{run}\t{record.ordinal}\t{at}\t{tags}\t{size}\t{artifact.uri}")
    if not found:
        raise typer.Exit(1)
