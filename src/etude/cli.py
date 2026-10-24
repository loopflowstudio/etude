"""The `etude` command: launcher and tracking commands, with reports under `etude reports`.

Each half registers its own commands (`etude.launch.cli`, `etude.train.cli`);
this module joins them, adds the eval report (`etude reports eval`), sends
launcher commands to a chosen controller, and turns expected failures into one
line on stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from etude.launch import cli as launch_cli
from etude.launch.commands import Kind, kind
from etude.launch.errors import LauncherError
from etude.run.errors import EtudeError
from etude.train.cli import app as reports_app
from etude.train.cli import checkpoints, sync

app = launch_cli.app
app.add_typer(reports_app, name="reports")


@reports_app.command("eval")
@kind(Kind.wait)
def evaluations(
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
        typer.Option(min=0.1, help="regenerate the report every SECONDS"),
    ] = None,
) -> None:
    """Write each experiment's report comparing its eval runs' agents to DIRECTORY/reports/eval/EXPERIMENT.html."""
    # Eval runs are composed here, so training's reports never import `etude.eval`.
    import time
    import webbrowser

    from etude.eval.report import write
    from etude.run.storage import default_root, storage_from_uri

    given = source or str(default_root())
    local = not given.startswith("s3://")
    storage = storage_from_uri(given)
    directory = out or (Path(given) / "reports" if local else Path("reports"))
    written: set[Path] = set()
    try:
        while True:
            for target in write(storage, directory, experiment=experiment, refresh=watch):
                if target not in written:
                    print(target, flush=True)
                    if open_:
                        webbrowser.open(target.resolve().as_uri())
                    written.add(target)
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        pass
    if not written:
        print("no eval runs found", file=sys.stderr)
        raise typer.Exit(1)


@app.command("continue")
@kind(Kind.wait)
def continue_run(
    run: Annotated[str, typer.Option(help="a run id, or a cohort name for all of its runs")],
    until: Annotated[str, typer.Option(help="how far to go, as clock=value, such as steps=63000")],
    source: Annotated[
        str | None,
        typer.Option(
            help="where the parents' tracked folders are: s3://bucket/prefix, or a runs "
            "directory; default: where runs are written on this computer, else the "
            "machine that last ran the parent"
        ),
    ] = None,
    ordinal: Annotated[
        int | None, typer.Option(help="the parent's checkpoint to start from; default: its latest")
    ] = None,
    cohort: Annotated[
        str | None, typer.Option(help="the new cohort's name, when continuing a cohort")
    ] = None,
    max_usd: Annotated[str | None, typer.Option(help="the new cohort's dollar ceiling")] = None,
) -> None:
    """Go past a run's target from a checkpoint it recorded. This makes a new run with a parent."""
    # Continuation joins the launcher's runs with tracking's checkpoints.
    import tempfile
    from decimal import Decimal

    from etude.carriage import put_back
    from etude.jobs import continue_from
    from etude.launch.listing import resolve_runs, show_runs
    from etude.run.listing import places
    from etude.run.storage import default_root, storage_from_uri

    scheduler = launch_cli.default()
    storage = storage_from_uri(source or default_root())
    parents = resolve_runs(scheduler, [run])
    first = scheduler.record(parents[0])
    with tempfile.TemporaryDirectory() as relayed:
        if source is None:
            # A parent that ran on another owned host: its records, from there.
            here = places(storage)
            for parent in parents:
                files = None if parent in here else scheduler.held(parent)
                if files is not None:
                    put_back(Path(relayed) / parent, files)
        again = [
            continue_from(
                scheduler.record(parent),
                until,
                source=relayed if (Path(relayed) / parent).is_dir() else storage,
                ordinal=ordinal,
            )
            for parent in parents
        ]
    if first.cohort != run:
        ids = [scheduler.queue(again[0], first.pool, rent=first.rent)]
    elif cohort is None:
        raise LauncherError("continuing a cohort needs --cohort with the new cohort's name")
    else:
        old = scheduler.cohort(run)
        ids = list(
            scheduler.queue_cohort(
                cohort,
                again,
                first.pool,
                rent=first.rent,
                parallel=old.parallel,
                max_usd=Decimal(max_usd) if max_usd else old.max_usd,
            )
        )
    show_runs(scheduler, ids)


# Each may reach a bucket, so it is a wait rather than a read.
app.command()(kind(Kind.wait)(checkpoints))
app.command()(kind(Kind.wait)(sync))

# Commands that run on this computer even when a controller is chosen: they read
# local files, or they choose the controller.
_LOCAL = ("launch", "queue", "source", "fetch", "worker", "logs")


def _main(argv: list[str]) -> int:
    try:
        if (
            argv
            and argv[0] not in ("worker", "reports", "checkpoints", "sync")
            and not {"--help", "-h"} & set(argv)
        ):
            # Entry points import only the command being run (AGENTS.md).
            from etude.launch import controller

            if argv == ["--controller-request"]:
                return controller.receive(launch_cli.default())
            local = argv[0] in _LOCAL or argv[:2] in (
                ["controller", "set"],
                ["controller", "unset"],
            )
            if not local and (address := controller.selected()):
                if argv[0] == "controller":
                    print(f"controller: {address}", flush=True)
                return controller.forward(address, argv)
        result = app(args=argv, prog_name="etude", standalone_mode=False)
        return result if isinstance(result, int) else 0
    except typer.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        if isinstance(error, (LauncherError, EtudeError, OSError, ValueError, typer.BadParameter)):
            # A bad flag value names its flag: "Invalid value for '--hidden': ...".
            said = error.format_message() if isinstance(error, typer.BadParameter) else str(error)
            print(f"etude: {said}", file=sys.stderr)
            return 1
        # Typer's public invocation formats usage failures without importing its private parser.
        if hasattr(error, "show") and hasattr(error, "exit_code"):
            error.show()
            raise SystemExit(error.exit_code) from None
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Profile a whole invocation with `etude --profile FILE COMMAND ...`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--profile":
        return _main(args)
    if len(args) < 3:
        print("etude: --profile needs a file and a command", file=sys.stderr)
        return 1

    import cProfile

    profile = cProfile.Profile()
    try:
        return int(profile.runcall(_main, args[2:]))
    finally:
        profile.dump_stats(args[1])
