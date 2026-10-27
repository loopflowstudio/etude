"""The `etude` command: launcher and tracking commands, and an experiment's report.

Each half registers its own commands (`etude.launch.cli`, `etude.train.cli`);
this module joins them, adds `etude report` (`etude.page`), which draws
training and evaluation together, sends launcher commands to a chosen
controller, and turns expected failures into one line on stderr.
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
from etude.train.cli import checkpoints, sync

app = launch_cli.app


@app.command()
@kind(Kind.wait)
def report(
    path: Annotated[
        Path,
        typer.Argument(
            help="an experiment's Python file, named for the experiment, or a folder: every "
            "experiment file in it, with an index"
        ),
    ],
    source: Annotated[
        str | None,
        typer.Option(
            help="a runs directory, s3://bucket/prefix or r2://bucket/prefix, searched at any "
            "depth; default: where runs are written on this computer"
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            help="the folder to write to; default: SOURCE/reports, or ./reports for a bucket"
        ),
    ] = None,
    open_: Annotated[bool, typer.Option("--open", help="show it in the browser")] = False,
    watch: Annotated[
        float | None,
        typer.Option(
            min=0.1,
            help="rewrite it every SECONDS, and the open page reloads; with --serve, how often "
            "the page reloads (default 5)",
        ),
    ] = None,
    serve: Annotated[
        bool,
        typer.Option(help="serve it from the files as they are at each request, and write nothing"),
    ] = False,
    host: Annotated[str, typer.Option(help="with --serve; it has no login")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="with --serve")] = 8765,
) -> None:
    """Draw an experiment's evals, perf and ML details as one HTML page, from its runs' files.

    The page follows the file's report(), or the default report without one. Given a folder,
    it draws every experiment file in it (a .py file defining eval(state) or report()) and an
    index of them all: their runs, latest checkpoints and scores, and evaluation progress.
    """
    # The report reads runs and, once the experiment has eval runs or eval(), evaluations.
    import time
    import webbrowser

    from etude.page import Page, Pages, Server, index, render, render_index, target, write
    from etude.run.storage import default_root, storage_from_uri

    storage = storage_from_uri(source or default_root())
    if serve:
        server = Server(storage, path, (host, port), refresh=5.0 if watch is None else watch)
        address = f"http://{host}:{server.server_port}/"
        print(address, flush=True)
        if open_:
            webbrowser.open(address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return
    shown: object = None
    pages = Pages(storage)  # a watched page is built again only once its files change
    try:
        while True:
            current: Page | dict[Path, Page | str] | None
            if path.is_dir():
                current = pages.build_each(path)
            else:
                try:
                    current = pages.build(path)
                except ValueError:
                    if watch is None:
                        raise
                    current = None  # Watching waits for the experiment's first run.
            if current is not None and current != shown:
                if isinstance(current, Page):
                    written = write(render(current, refresh=watch), target(storage, path.stem, out))
                else:
                    if "index" in {file.stem for file in current}:
                        raise EtudeError("experiment_named_index", 409)
                    for file, built in current.items():
                        if isinstance(built, Page):
                            write(render(built, refresh=watch), target(storage, file.stem, out))
                    found = index(storage, current, href=lambda stem: f"{stem}.html")
                    written = write(
                        render_index(found, refresh=watch), target(storage, "index", out)
                    )
                if shown is None:
                    print(written, flush=True)
                    if open_:
                        webbrowser.open(written.resolve().as_uri())
                shown = current
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        pass


@app.command("eval")
@kind(Kind.wait)
def eval_experiment(
    file: Annotated[
        Path,
        typer.Argument(help="a Python file defining eval(state); the experiment is named for it"),
    ],
    source: Annotated[
        str | None,
        typer.Option(
            help="where training and eval runs are: s3://bucket/prefix or a runs directory; "
            "default: where runs are written on this computer"
        ),
    ] = None,
    pool: Annotated[
        str | None, typer.Option(help="launch on this pool of machines you own")
    ] = None,
    rent: Annotated[
        str | None, typer.Option(help="launch on machines rented from this on-demand")
    ] = None,
    watch: Annotated[
        float | None, typer.Option(min=1, help="pass again every SECONDS until Ctrl-C")
    ] = None,
    retry_failed: Annotated[
        bool, typer.Option(help="play again the matches that failed, rather than count them")
    ] = False,
) -> None:
    """Play, or with --pool or --rent launch, the matches eval(state) wants that no run delivered."""
    # Experiments join tracking, evaluation and the launcher.
    import time

    from etude.experiment import advance, read_experiment
    from etude.run.storage import default_root, storage_from_uri

    experiment = read_experiment(file)
    storage = storage_from_uri(source or default_root())
    try:
        while True:
            for done in advance(
                experiment, source=storage, pool=pool, rent=rent, retry_failed=retry_failed
            ):
                line = f"{done.tournament}: {done.new} new"
                if done.failed:
                    line += f", {done.failed} failed (--retry-failed plays them again)"
                if done.running:
                    line += f", {done.running} already running"
                if done.launched:
                    line += f", launched {done.launched}"
                elif done.played:
                    line += f", {done.played} played"
                print(line, flush=True)
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        pass


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
            and argv[0] not in ("worker", "report", "checkpoints", "sync")
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
