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
from etude.tracking.errors import EtudeError
from etude.train.cli import checkpoints, sync

app = launch_cli.app


ExperimentOption = Annotated[
    str | None, typer.Option(help="experiment label, defaulting to the file stem")
]


@app.command("run")
@kind(Kind.wait)
def run_recipe(
    file: Annotated[Path, typer.Argument(help="a Python file defining train(config)")],
    config: Annotated[
        str, typer.Option(help="JSON config fields; omitted fields use defaults")
    ] = "{}",
    seed: Annotated[int | None, typer.Option(help="the run's seed")] = None,
    name: Annotated[str | None, typer.Option(help="job name, defaulting to the file stem")] = None,
    experiment: ExperimentOption = None,
    resume: Annotated[str | None, typer.Option(help="resume this run on current source")] = None,
    until: Annotated[str | None, typer.Option(help="absolute target, such as updates=2000")] = None,
    seconds: Annotated[
        float | None, typer.Option(help="execution allowance before a saved stop")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="print a JSON result")] = False,
) -> None:
    """Run a training recipe on this computer; 0 finished, 75 stopped, other failed."""
    from etude.launch.records import Until
    from etude.local import run

    target = Until.model_validate(until) if until is not None else None
    raise typer.Exit(
        run(
            file,
            config=config,
            seed=seed,
            name=name,
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
    cohort: str | None,
    parallel: int,
    max_usd: str | None,
    help: bool,
    *,
    queued: bool,
    smoke: bool,
    quote: bool = False,
    experiment: str | None = None,
    resume: str | None = None,
    until: str | None = None,
) -> None:
    if help:
        print(ctx.get_help())
    if file is None:
        if help:
            return
        raise typer.BadParameter("a Python FILE defining runs() is required")
    from decimal import Decimal

    from etude.launch import controller
    from etude.launch.job_file import read_job_file
    from etude.launch.listing import show_runs
    from etude.launch.records import CohortSpec, Until

    runs = read_job_file(file, ctx.args, help=help, experiment=experiment)
    if help:
        return
    if until is not None and resume is None:
        raise LauncherError("--until needs --resume")
    if resume is not None:
        if quote or cohort is not None:
            raise LauncherError("--resume cannot be combined with --quote or --cohort")
        selected = [run for run in runs if run.id == resume]
        if not selected and len(runs) == 1:
            selected = runs
        elif not selected:
            address = controller.selected()
            reader = controller.PythonClient(address) if address else launch_cli.default()
            prior = reader.record(resume)
            selected = [
                run for run in runs if run.seed == prior.seed and run.job.name == prior.spec.name
            ]
        if len(selected) != 1:
            raise LauncherError("--resume must select one run from the file's jobs and seeds")
        request = selected[0]
        if until is not None:
            request = request.model_copy(
                update={"job": request.job.model_copy(update={"until": Until.parse(until)})}
            )
        runs = [request.model_copy(update={"id": resume, "resume": True})]
    if cohort is None and (len(runs) > 1 or parallel != 1 or max_usd is not None):
        raise LauncherError("several runs or cohort limits need --cohort NAME")
    spec = (
        CohortSpec(name=cohort, parallel=parallel, max_usd=Decimal(max_usd) if max_usd else None)
        if cohort
        else None
    )
    if not quote:
        # Before anything is queued, rented or published: a failure raises and submits nothing.
        if smoke:
            from etude.smoke import smoke_file

            runs = smoke_file(file, runs).stamp(runs)
    submission = controller.LaunchInput(
        runs=tuple(runs), cohort=spec, pool=pool, rent=rent, queued=queued
    )
    address = controller.selected()
    if quote:
        from etude.launch.listing import show_quote

        show_quote(
            controller.quote(
                controller.PythonClient(address) if address else launch_cli.default(), submission
            )
        )
        return
    if address:
        raise typer.Exit(controller.forward(address, [], submission))
    scheduler = launch_cli.default()
    show_runs(scheduler, controller.submit(scheduler, submission))


FileArgument = Annotated[Path | None, typer.Argument(help="a Python file defining runs()")]
PoolOption = Annotated[
    str | None,
    typer.Option(help="the pool of machines you own to use; the default when neither is named"),
]
RentOption = Annotated[
    str | None,
    typer.Option(help="the on-demand to rent from, after the pool's free machines"),
]
CohortOption = Annotated[
    str | None, typer.Option(help="a name for these runs together; required for several runs")
]
ParallelOption = Annotated[int, typer.Option(help="machines at once for this cohort")]
CohortUsd = Annotated[str | None, typer.Option(help="dollar ceiling for the whole cohort")]
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
    cohort: CohortOption = None,
    parallel: ParallelOption = 1,
    max_usd: CohortUsd = None,
    quote: Annotated[
        bool, typer.Option(help="print the machines and planned rental costs; rent nothing")
    ] = False,
    smoke: SmokeOption = False,
    help: FileHelp = False,
    experiment: ExperimentOption = None,
    resume: Annotated[str | None, typer.Option(help="resume this run on current source")] = None,
    until: Annotated[str | None, typer.Option(help="absolute target for the resumed run")] = None,
) -> None:
    """Queue a file's runs and get the machines to start them now, or fail and queue nothing."""
    _submit(
        ctx,
        file,
        pool,
        rent,
        cohort,
        parallel,
        max_usd,
        help,
        queued=False,
        smoke=smoke,
        quote=quote,
        experiment=experiment,
        resume=resume,
        until=until,
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
    cohort: CohortOption = None,
    parallel: ParallelOption = 1,
    max_usd: CohortUsd = None,
    smoke: SmokeOption = False,
    help: FileHelp = False,
    experiment: ExperimentOption = None,
) -> None:
    """Add a file's runs to the queue. They start when a machine has room or can be rented."""
    _submit(
        ctx,
        file,
        pool,
        rent,
        cohort,
        parallel,
        max_usd,
        help,
        queued=True,
        smoke=smoke,
        experiment=experiment,
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
        raise typer.BadParameter("a Python FILE defining runs() is required")
    from typing import cast

    from etude.launch.job_file import read_job_file
    from etude.smoke import STEPS, Step, smoke_file
    from etude.tracking.storage import storage_from_uri

    steps = step or list(STEPS)
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        raise typer.BadParameter(f"--step is train, eval or report, not {', '.join(unknown)}")
    runs = read_job_file(file, ctx.args, help=help) if "train" in steps else None
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
            "the page refreshes (no timer by default)",
        ),
    ] = None,
    serve: Annotated[
        bool,
        typer.Option(help="serve it with refresh on demand, and write nothing"),
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
    from etude.tracking.storage import default_root, storage_from_uri

    storage = storage_from_uri(source or default_root())
    if serve:
        server = Server(storage, path, (host, port), refresh=watch)
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
        bool,
        typer.Option(
            help="play again the matches that failed, rather than count them, and launch again "
            "what a failed launched pass left unplayed"
        ),
    ] = False,
    smoke: Annotated[
        bool,
        typer.Option(
            help="with --pool or --rent, smoke eval and report here first against SOURCE "
            "and launch nothing if it fails"
        ),
    ] = False,
    queue: Annotated[
        bool, typer.Option(help="queue jobs until the chosen pool or on-demand has capacity")
    ] = False,
) -> None:
    """Play, or with --pool or --rent launch, the matches eval(state) wants that no run delivered."""
    # Experiments join tracking, evaluation and the launcher.
    import time

    from etude.experiment import advance, read_experiment
    from etude.launch.records import Smoke
    from etude.smoke import smoke_file
    from etude.tracking.storage import default_root, storage_from_uri

    experiment = read_experiment(file)
    storage = storage_from_uri(source or default_root())
    before = Smoke()
    if smoke and (pool or rent):
        # Before a launch spends anything: a failure raises and launches nothing.
        before = Smoke(steps=smoke_file(file, steps=("eval", "report"), source=storage).rest)
    try:
        while True:
            for done in advance(
                experiment,
                source=storage,
                pool=pool,
                rent=rent,
                retry_failed=retry_failed,
                smoke=before,
                queue=queue,
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


@app.command("convert-eval")
@kind(Kind.wait)
def convert_eval(
    run: Annotated[str, typer.Argument(help="the eval run, by name")],
    source: Annotated[str, typer.Option(help="s3://bucket/prefix or a runs directory")],
) -> None:
    """Write blocks holding an eval run's per-file records, beside them; nothing is deleted."""
    from etude.eval.convert import convert
    from etude.tracking.storage import storage_from_uri

    done = convert(run, storage_from_uri(source))
    print(
        f"{run}: {done.blocks} blocks, {done.matches} matches, {done.blobs} blobs,"
        f" replacing {done.replaced} files"
    )


@app.command("delete-eval-files")
@kind(Kind.wait)
def delete_eval_files(
    run: Annotated[str, typer.Argument(help="the eval run, by name")],
    source: Annotated[str, typer.Option(help="s3://bucket/prefix or a runs directory")],
) -> None:
    """Delete an eval run's per-file records that its blocks in SOURCE replace; refuses before."""
    from etude.eval.convert import delete_files
    from etude.tracking.storage import storage_from_uri

    print(f"{run}: deleted {delete_files(run, storage_from_uri(source))} files")


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
        int | None,
        typer.Option(
            help="choose a checkpoint; default: the parent's out, or its latest copied "
            "checkpoint when out is empty"
        ),
    ] = None,
    cohort: Annotated[
        str | None, typer.Option(help="the new cohort's name, when continuing a cohort")
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(help='typed config changes as a JSON object, e.g. {"rate":0.001}'),
    ] = None,
    max_usd: Annotated[str | None, typer.Option(help="the new cohort's dollar ceiling")] = None,
) -> None:
    """Go past a run's target from its out. This makes a new run with a parent."""
    # Continuation joins the launcher's runs with tracking's checkpoints.
    import tempfile
    from decimal import Decimal

    from etude.carriage import put_back
    from etude.jobs import continue_from, continued_config
    from etude.launch.listing import resolve_runs, show_runs
    from etude.tracking.listing import run_folders
    from etude.tracking.storage import default_root, storage_from_uri

    scheduler = launch_cli.default()
    storage = storage_from_uri(source or default_root())
    parents = [scheduler.record(parent) for parent in resolve_runs(scheduler, [run])]
    first = parents[0]
    with tempfile.TemporaryDirectory() as relayed:
        if source is None:
            # A parent that ran on another owned host: its records, from there.
            here = run_folders(storage.keys(""))
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
# local files or a bucket, run a machine's worker, or submit through the controller
# themselves. Every other command is sent to a chosen controller.
_HERE = (
    "launch",
    "queue",
    "smoke",
    "source",
    "fetch",
    "worker",
    "logs",
    "report",
    "checkpoints",
    "sync",
    "convert-eval",
    "delete-eval-files",
)


def _main(argv: list[str]) -> int:
    try:
        if argv and argv[0] not in _HERE and not {"--help", "-h"} & set(argv):
            # Entry points import only the command being run (AGENTS.md).
            from etude.launch import controller

            if argv == ["--controller-request"]:
                return controller.receive(launch_cli.default())
            # The controller command chooses the controller, so `set` and `unset` run here.
            local = argv[:2] in (["controller", "set"], ["controller", "unset"])
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
