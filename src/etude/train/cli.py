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
    from etude.run import sync as deliver
    from etude.storage import storage_from_uri

    if seal:
        # Sealing turns training logs into blocks; only then does it need NumPy.
        from etude.train.tracking import seal_runs

        seal_runs(directory)
    undelivered = deliver(
        directory, storages=[storage_from_uri(uri) for uri in to or []], stopped=seal
    )
    for run, reasons in undelivered.items():
        print(f"{run}: {reasons}", file=sys.stderr)
    if undelivered:
        raise typer.Exit(1)


def checkpoints(
    source: Annotated[
        str | None,
        typer.Option(
            help="a runs directory, s3://bucket/prefix or r2://bucket/prefix; "
            "default: where runs are written on this computer"
        ),
    ] = None,
    experiment: Annotated[str | None, typer.Option(help="only this experiment")] = None,
) -> None:
    """List recorded checkpoints."""
    from etude.identity import run_address
    from etude.run import runs
    from etude.storage import default_root, storage_from_uri
    from etude.train.read import read

    source = source or str(default_root())
    storage = storage_from_uri(source)
    found = [pair for pair in runs(storage) if experiment in (None, pair[0])]
    for name, run in found:
        data = read(storage, name, run)
        address = data.file.id if data.file is not None else run_address(run, name)
        for record in data.checkpoints:
            artifact = record.artifact
            size = "reference" if artifact.size_bytes is None else f"{artifact.size_bytes} bytes"
            at = " ".join(f"{clock}={value:g}" for clock, value in record.at.items())
            tags = ",".join(record.tags) or "-"
            print(f"{address}\t{record.ordinal}\t{at}\t{tags}\t{size}\t{artifact.uri}")
    if not found:
        selection = f" for experiment {experiment!r}" if experiment is not None else ""
        print(f"no runs{selection} in {source}", file=sys.stderr)
        raise typer.Exit(1)
