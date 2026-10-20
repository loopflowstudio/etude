"""Tracking commands: `etude reports write|serve`, `etude sync` and `etude checkpoints`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

# Entry points load only the invoked command (AGENTS.md); help needs no NumPy,
# and syncing files does not load the report or the launcher.
app = typer.Typer(
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    add_completion=False,
    help="Write or serve each experiment's report of its runs' metrics.",
)


@app.command()
def write(
    source: Annotated[
        str | None,
        typer.Option(
            help="a runs directory or s3://bucket/prefix, searched at any depth; "
            "default: where runs are written on this computer"
        ),
    ] = None,
    experiment: Annotated[str | None, typer.Option(help="only this experiment")] = None,
    out: Annotated[
        Path | None, typer.Option(help="default: SOURCE/reports, or ./reports for a bucket")
    ] = None,
    open_: Annotated[bool, typer.Option("--open", help="show it in the browser")] = False,
    watch: Annotated[
        float | None,
        typer.Option(min=0.1, help="regenerate when the files change, checking every SECONDS"),
    ] = None,
) -> None:
    """Write each experiment's report to DIRECTORY/reports/EXPERIMENT.html, replacing the last."""
    import time
    import webbrowser

    from etude.runs.storage import default_root, storage_from_uri
    from etude.train.report import pages, write

    given = source or str(default_root())
    storage = storage_from_uri(given)
    local = not given.startswith("s3://")
    target_directory = out or (Path(given) / "reports" if local else Path("reports"))
    written: dict[str, dict[str, object]] = {}
    try:
        while True:
            for name, page in pages(storage, experiment).items():
                if written.get(name) == page:
                    continue
                target = write(page, target_directory, refresh=watch)
                if name not in written:
                    print(target, flush=True)
                    if open_:
                        webbrowser.open(target.resolve().as_uri())
                written[name] = page
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        pass
    if not written:
        print("no runs found", file=sys.stderr)
        raise typer.Exit(1)


@app.command()
def serve(
    source: Annotated[
        str | None, typer.Option(help="a runs directory or s3://bucket/prefix")
    ] = None,
    host: Annotated[
        str, typer.Option(help="it has no login; keep it on this computer")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8765,
    refresh: Annotated[float, typer.Option(help="seconds between a page reloading")] = 5.0,
) -> None:
    """Show every experiment's report in the browser, read fresh from the files each time."""
    from etude.runs.storage import default_root, storage_from_uri
    from etude.train.serve import Server

    storage = storage_from_uri(source or default_root())
    server = Server(storage, (host, port), refresh=refresh)
    print(f"http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def sync(
    directory: Annotated[Path, typer.Option(help="the local runs directory")],
    to: Annotated[list[str] | None, typer.Option(help="s3://bucket/prefix or a path")] = None,
    seal: Annotated[bool, typer.Option(help="close segments a dead process left open")] = False,
) -> None:
    """Deliver sealed run files and stored checkpoints to each directory or bucket."""
    from etude.runs.delivery import sync as deliver
    from etude.runs.storage import storage_from_uri

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
    from etude.runs.listing import runs
    from etude.runs.storage import storage_from_uri
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
