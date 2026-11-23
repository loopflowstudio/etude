"""The `etude` command: launcher and tracking commands, and an experiment's report.

Each half registers its own commands (`etude.launch.cli`, `etude.train.cli`);
this module joins them, adds `etude report` (`etude.page`), which draws
training and evaluation together, sends launcher commands to a chosen
server, and turns expected failures into one line on stderr.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from etude.errors import EtudeError, LauncherError, RetryRequested
from etude.launch import cli as launch_cli
from etude.launch.commands import Kind, kind
from etude.train.cli import checkpoints, sync

app = launch_cli.app


ExperimentOption = Annotated[
    str | None, typer.Option(help="experiment label, defaulting to the file stem")
]


@app.command("train")
@kind(Kind.wait)
def train_recipe(
    file: Annotated[
        Path, typer.Argument(help="a Python file defining train(config) or training_jobs()")
    ],
    config: Annotated[
        str | None, typer.Option(help="JSON config fields; omitted on resume keeps stored config")
    ] = None,
    seed: Annotated[int | None, typer.Option(help="the run's seed")] = None,
    job: Annotated[str | None, typer.Option(help="select a job defined in training_jobs()")] = None,
    experiment: ExperimentOption = None,
    resume: Annotated[
        bool, typer.Option(help="resume the selected runs on current source")
    ] = False,
    until: Annotated[str | None, typer.Option(help="absolute target, such as updates=2000")] = None,
    seconds: Annotated[
        float | None, typer.Option(help="execution allowance before a saved stop")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="print a JSON result")] = False,
) -> None:
    """Train a recipe on this computer; 0 finished, 75 stopped, other failed."""
    from etude.controller.local import train
    from etude.spec import Until

    target = Until.model_validate(until) if until is not None else None
    raise typer.Exit(
        train(
            file,
            config=config,
            seed=seed,
            job=job,
            experiment=experiment,
            resume=resume,
            until=target,
            seconds=seconds,
            json_output=json_output,
        )
    )


def _submit(
    ctx: typer.Context,
    file: Path | None,
    pool: str | None,
    rent: str | None,
    max_usd: str | None,
    help: bool,
    *,
    queued: bool,
    smoke: bool,
    quote: bool = False,
    experiment: str | None = None,
    resume: bool = False,
    job: str | None = None,
    seed: int | None = None,
    until: str | None = None,
    source: str | None = None,
    watch: float | None = None,
    retry_failed: bool = False,
) -> None:
    if help:
        print(ctx.get_help())
    if file is None:
        if help:
            return
        raise typer.BadParameter("a Python experiment FILE is required")
    import time
    from decimal import Decimal, InvalidOperation

    from etude.controller.advance import advance, source_for
    from etude.controller.declarations import read_experiment, read_training_jobs
    from etude.spec import Until
    from etude.storage import storage_from_uri

    definition = read_experiment(file, experiment=experiment)
    if help:
        if callable(definition.training):
            read_training_jobs(file, ctx.args, help=True, function=definition.training)
        return
    if until is not None and not resume:
        raise LauncherError("--until needs --resume")
    if resume and quote:
        raise LauncherError("--resume cannot be combined with --quote")
    try:
        budget = None if max_usd is None else Decimal(max_usd)
    except InvalidOperation:
        raise LauncherError("--max-usd must be a dollar amount") from None
    storage = storage_from_uri(source_for(pool, rent, source))
    try:
        while True:
            advance(
                definition,
                source=storage,
                pool=pool,
                rent=rent,
                mode="queue" if queued else "launch",
                max_usd=budget,
                quote=quote,
                smoke_file_first=smoke,
                given=ctx.args,
                job=job,
                seed=seed,
                resume=resume,
                until=None if until is None else Until.parse(until),
                retry_failed=retry_failed,
            )
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        return


FileArgument = Annotated[
    Path | None,
    typer.Argument(help="a Python experiment file defining training_jobs() or eval_jobs(state)"),
]
PoolOption = Annotated[
    str | None,
    typer.Option(help="the pool of machines you own to use; the default when neither is named"),
]
RentOption = Annotated[
    str | None,
    typer.Option(help="the on-demand to rent from, after the pool's free machines"),
]
SmokeOption = Annotated[
    bool,
    typer.Option(
        help="smoke the file here first, as etude smoke does, and submit nothing if it fails"
    ),
]
FileHelp = Annotated[
    bool, typer.Option("--help", "-h", help="show these options, then the file's own parameters")
]


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
@kind(Kind.wait)
def launch(
    ctx: typer.Context,
    file: FileArgument = None,
    pool: PoolOption = None,
    rent: RentOption = None,
    quote: Annotated[
        bool, typer.Option(help="print the machines and planned rental costs; rent nothing")
    ] = False,
    smoke: SmokeOption = False,
    help: FileHelp = False,
    experiment: ExperimentOption = None,
    job: Annotated[str | None, typer.Option(help="select a declared job")] = None,
    seed: Annotated[int | None, typer.Option(help="select a declared seed")] = None,
    resume: Annotated[
        bool, typer.Option(help="resume the selected runs on current source")
    ] = False,
    until: Annotated[str | None, typer.Option(help="absolute target for the resumed run")] = None,
    max_usd: Annotated[
        str | None, typer.Option(help="experiment commitment limit in dollars")
    ] = None,
    source: Annotated[str | None, typer.Option(help="where experiment runs are delivered")] = None,
    retry_failed: Annotated[bool, typer.Option(help="retry failed eval matches")] = False,
    watch: Annotated[float | None, typer.Option(min=1, help="pass again every SECONDS")] = None,
) -> None:
    """Queue a file's runs and get the machines to start them now, or fail and queue nothing."""
    _submit(
        ctx,
        file,
        pool,
        rent,
        max_usd,
        help,
        queued=False,
        smoke=smoke,
        quote=quote,
        experiment=experiment,
        job=job,
        seed=seed,
        resume=resume,
        until=until,
        source=source,
        retry_failed=retry_failed,
        watch=watch,
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
@kind(Kind.ask)
def queue(
    ctx: typer.Context,
    file: FileArgument = None,
    pool: PoolOption = None,
    rent: RentOption = None,
    smoke: SmokeOption = False,
    help: FileHelp = False,
    experiment: ExperimentOption = None,
    job: Annotated[str | None, typer.Option(help="select a declared job")] = None,
    seed: Annotated[int | None, typer.Option(help="select a declared seed")] = None,
    max_usd: Annotated[
        str | None, typer.Option(help="experiment commitment limit in dollars")
    ] = None,
    source: Annotated[str | None, typer.Option(help="where experiment runs are delivered")] = None,
    retry_failed: Annotated[bool, typer.Option(help="retry failed eval matches")] = False,
) -> None:
    """Add a file's runs to the queue. They start when a machine has room or can be rented."""
    _submit(
        ctx,
        file,
        pool,
        rent,
        max_usd,
        help,
        experiment=experiment,
        job=job,
        seed=seed,
        queued=True,
        smoke=smoke,
        source=source,
        retry_failed=retry_failed,
    )


@app.command(
    "smoke",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
@kind(Kind.wait)
def smoke_experiment(
    ctx: typer.Context,
    file: FileArgument = None,
    step: Annotated[
        list[str] | None,
        typer.Option(help="smoke only this step: train, eval or report; repeat for several"),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(
            help="without --step train: the s3://bucket/prefix or runs directory whose "
            "checkpoints eval and report read; nothing is delivered there"
        ),
    ] = None,
    keep: Annotated[
        bool, typer.Option(help="keep the temporary folder and print where it is")
    ] = False,
    help: FileHelp = False,
) -> None:
    """Run a file's training, evaluation and report in smoke form on this computer. Spends nothing."""
    if help:
        print(ctx.get_help())
    if file is None:
        if help:
            return
        raise typer.BadParameter("an experiment FILE is required")
    from typing import cast

    from etude.controller.declarations import read_training_jobs
    from etude.controller.smoke import STEPS, Step, smoke_file
    from etude.experiment_file import defined_functions
    from etude.storage import storage_from_uri

    steps = step or list(STEPS)
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        raise typer.BadParameter(f"--step is train, eval or report, not {', '.join(unknown)}")
    runs = (
        read_training_jobs(file, ctx.args, help=help)
        if "train" in steps and "training_jobs" in defined_functions(file)
        else None
    )
    if help:
        return
    smoke_file(
        file,
        runs,
        steps=[cast(Step, each) for each in steps],
        source=storage_from_uri(source) if source else None,
        keep=keep,
    )


@app.command()
@kind(Kind.wait)
def view(
    path: Annotated[
        Path | None, typer.Argument(exists=True, readable=True, help="an experiment file or folder")
    ] = None,
    source: Annotated[str | None, typer.Option(help="a runs directory or bucket URI")] = None,
    host: Annotated[str, typer.Option(help="the address to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8765,
) -> None:
    """Serve live run charts and experiment reports from the selected source."""
    # This command alone loads the live index and HTTP server.
    from etude.storage import default_root, storage_from_uri
    from etude.view import serve

    serve(storage_from_uri(source or default_root()), path, (host, port))


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
    experiment: Annotated[
        str | None, typer.Option(help="override the file’s experiment name")
    ] = None,
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
            help="rewrite it every SECONDS, and reload the open page",
        ),
    ] = None,
) -> None:
    """Draw an experiment's evals, perf and ML details as one HTML page, from its runs' files.

    The page follows the file's report(), or the default report without one. Given a folder,
    it draws every experiment file in it (a .py file defining eval_jobs(state) or report()) and an
    index of them all: their runs, latest checkpoints and scores, and evaluation progress.
    """
    # The report reads runs and, once the experiment has eval runs or eval_jobs(), evaluations.
    import time
    import webbrowser

    from etude.page import Page, Pages, index, render, render_index, target, write
    from etude.storage import default_root, storage_from_uri

    if experiment is not None and path.is_dir():
        raise ValueError("--experiment needs one Python file")
    storage = storage_from_uri(source or default_root())
    shown: object = None
    # A watched page is built again only once its files change.
    pages = Pages(storage)
    try:
        while True:
            current: Page | dict[Path, Page | str] | None
            if path.is_dir():
                current = pages.build_each(path)
            else:
                try:
                    current = pages.build(path, experiment=experiment)
                except ValueError:
                    if watch is None:
                        raise
                    current = None  # Watching waits for the experiment's first run.
            if current is not None and current != shown:
                if isinstance(current, Page):
                    written = write(
                        render(current, refresh=watch), target(storage, current.experiment, out)
                    )
                else:
                    if "index" in {file.stem for file in current}:
                        raise EtudeError("experiment_named_index", 409)
                    for file, built in current.items():
                        if isinstance(built, Page):
                            write(render(built, refresh=watch), target(storage, file.stem, out))
                    found = index(storage, current)
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
        typer.Argument(
            help="a Python file defining eval_jobs(state); the experiment is named for it"
        ),
    ],
    experiment: ExperimentOption = None,
    source: Annotated[
        str | None,
        typer.Option(
            help="where training and eval runs are: s3://bucket/prefix or a runs directory; "
            "default: where runs are written on this computer"
        ),
    ] = None,
    watch: Annotated[
        float | None, typer.Option(min=1, help="pass again every SECONDS until Ctrl-C")
    ] = None,
    retry_failed: Annotated[
        bool,
        typer.Option(
            help="play again the matches that failed, rather than count them, and launch again "
            "what a failed launched pass left unplayed"
        ),
    ] = False,
) -> None:
    """Play here the matches eval_jobs(state) wants that no run delivered."""
    # Experiments join tracking, evaluation and the launcher.
    import time

    from etude.controller.advance import advance
    from etude.controller.declarations import read_experiment
    from etude.storage import default_root, storage_from_uri

    definition = read_experiment(file, experiment=experiment)
    storage = storage_from_uri(source or default_root())
    try:
        while True:
            advance(
                definition,
                source=storage,
                retry_failed=retry_failed,
            )
            if watch is None:
                break
            time.sleep(watch)
    except KeyboardInterrupt:
        pass


@app.command("convert-eval")
@kind(Kind.wait)
def convert_eval(
    run: Annotated[str, typer.Argument(help="the eval run, by name")],
    source: Annotated[str, typer.Option(help="s3://bucket/prefix or a runs directory")],
    experiment: ExperimentOption = None,
) -> None:
    """Write blocks holding an eval run's per-file records, beside them; nothing is deleted."""
    from etude.eval.convert import convert
    from etude.storage import storage_from_uri

    done = convert(run, storage_from_uri(source), experiment=experiment)
    print(
        f"{run}: {done.blocks} blocks, {done.matches} matches, {done.blobs} blobs,"
        f" replacing {done.replaced} files"
    )


@app.command("delete-eval-files")
@kind(Kind.wait)
def delete_eval_files(
    run: Annotated[str, typer.Argument(help="the eval run, by name")],
    source: Annotated[str, typer.Option(help="s3://bucket/prefix or a runs directory")],
    experiment: ExperimentOption = None,
) -> None:
    """Delete an eval run's per-file records that its blocks in SOURCE replace; refuses before."""
    from etude.eval.convert import delete_files
    from etude.storage import storage_from_uri

    print(
        f"{run}: deleted {delete_files(run, storage_from_uri(source), experiment=experiment)} files"
    )


@app.command("continue")
@kind(Kind.wait)
def continue_run(
    run: Annotated[str, typer.Option(help="a run address")],
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
        int | None,
        typer.Option(
            help="choose a checkpoint; default: the parent's out, or its latest copied "
            "checkpoint when out is empty"
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(help='typed config changes as a JSON object, e.g. {"rate":0.001}'),
    ] = None,
    experiment: Annotated[
        str | None, typer.Option(help="experiment label qualifying a bare run id")
    ] = None,
) -> None:
    """Go past a run's target from its out. This makes a new run with a parent."""
    # Continuation joins the launcher's runs with tracking's checkpoints.
    import tempfile

    from etude.carriage import put_back
    from etude.jobs import continue_from, continued_config
    from etude.launch.listing import resolve_runs, show_runs
    from etude.run import located_runs
    from etude.storage import default_root, storage_from_uri
    from etude.view_link import print_view

    scheduler = launch_cli.default()
    storage = storage_from_uri(source or default_root())
    try:
        parents = [
            scheduler.record(parent)
            for parent in resolve_runs(scheduler, [run], experiment=experiment)
        ]
    except LauncherError as error:
        raise LauncherError(f"{error}; a changed config needs a new --experiment") from error
    first = parents[0]
    with tempfile.TemporaryDirectory() as relayed:
        if source is None:
            # A parent that ran on another owned host: its records, from there.
            here = {item.file.id for item in located_runs(storage, storage.keys(""))}
            for parent in parents:
                files = None if parent.id in here else scheduler.held(parent.id)
                if files is not None:
                    put_back(Path(relayed) / parent.id, files)
        again = [
            continue_from(
                parent,
                until,
                source=relayed if (Path(relayed) / parent.id).is_dir() else storage,
                ordinal=ordinal,
                config=None if config is None else continued_config(parent, config),
            )
            for parent in parents
        ]
    ids = scheduler.queue(again[0], first.launch.pool, rent=first.launch.rent)
    show_runs(scheduler, ids)
    if not os.environ.get("ETUDE_SERVER_LOCAL"):
        for child in ids:
            print_view(child)


# Each may reach a bucket, so it is a wait rather than a read.
app.command()(kind(Kind.wait)(checkpoints))
app.command()(kind(Kind.wait)(sync))


# Commands that run on this computer even when a server is chosen: they read
# local files or a bucket, run a machine's worker, or submit through the server
# themselves. Every other command is sent to a chosen server.
_HERE = (
    "launch",
    "queue",
    "smoke",
    "train",
    "eval",
    "database",
    "source",
    "fetch",
    "worker",
    "logs",
    "report",
    "status",
    "view",
    "checkpoints",
    "sync",
    "convert-eval",
    "delete-eval-files",
)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] not in _HERE and not {"--help", "-h"} & set(argv):
            # Entry points import only the command being run (AGENTS.md).
            from etude.launch import server

            if argv == ["--server-request"]:
                return server.receive(launch_cli.default)
            # The server command chooses the server, so `set` and `unset` run here.
            local = argv[:2] in (["server", "set"], ["server", "unset"])
            if not local and (address := server.selected()):
                if argv[0] == "server":
                    print(f"server: {address}", flush=True)
                return server.forward(address, argv)
        result = app(args=argv, prog_name="etude", standalone_mode=False)
        return result if isinstance(result, int) else 0
    except typer.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        # Entry points load failure-specific dependencies only on the failing command.
        from etude.storage_timeout import StorageUnavailable

        if isinstance(
            error,
            (
                LauncherError,
                EtudeError,
                RetryRequested,
                StorageUnavailable,
                OSError,
                ValueError,
                typer.BadParameter,
            ),
        ):
            # A bad flag value names its flag: "Invalid value for '--hidden': ...".
            said = error.format_message() if isinstance(error, typer.BadParameter) else str(error)
            print(f"etude: {said}", file=sys.stderr)
            return 1
        # Typer's public invocation formats usage failures without importing its private parser.
        if hasattr(error, "show") and hasattr(error, "exit_code"):
            error.show()
            raise SystemExit(error.exit_code) from None
        raise
