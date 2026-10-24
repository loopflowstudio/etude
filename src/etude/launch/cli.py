"""The launcher's commands; `etude.cli` runs them as the `etude` command."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from etude.launch.commands import Kind, kind
from etude.launch.errors import LauncherError

if TYPE_CHECKING:
    from etude.launch.scheduler import Scheduler

# Entry points import only the command being run (AGENTS.md): help must not load
# the scheduler, worker or tracking stack. Imports below these entries stay at top.


def default() -> Scheduler:
    from etude.launch.client import default as open_scheduler

    return open_scheduler()


def _app(*, help: str | None = None, invoke_without_command: bool = False) -> typer.Typer:
    return typer.Typer(
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        add_completion=False,
        help=help,
        invoke_without_command=invoke_without_command,
    )


app = _app(help="Launch runs on your own or rented machines, and track what they record.")
pool_app = _app(help="Add a pool of machines you own, or change whether it takes runs.")
ondemand_app = _app(help="Add an on-demand to rent from: a machine template and a budget.")
machine_app = _app(help="Add a machine you own.")
controller_app = _app(
    help="Show or choose the machine that holds the database, or run its loop.",
    invoke_without_command=True,
)
provider_app = _app(help="Record an account that supplies machines.")
storage_app = _app(help="Add a place in a bucket for records.")
image_app = _app(help="Resolve container images before renting.")
for name, sub in (
    ("pool", pool_app),
    ("ondemand", ondemand_app),
    ("machine", machine_app),
    ("controller", controller_app),
    ("provider", provider_app),
    ("storage", storage_app),
    ("image", image_app),
):
    app.add_typer(sub, name=name)


def _submit(
    ctx: typer.Context,
    file: Path | None,
    pool: str | None,
    rent: str | None,
    cohort: str | None,
    parallel: int,
    max_usd: str | None,
    help: bool,
    queued: bool,
    quote: bool = False,
) -> None:
    if help:
        print(ctx.get_help())
    if file is None:
        if help:
            return
        raise typer.BadParameter("a Python FILE defining runs() is required")
    from etude.launch import controller
    from etude.launch.job_file import read_job_file
    from etude.launch.listing import show_runs
    from etude.launch.records import CohortSpec

    runs = read_job_file(file, ctx.args, help=help)
    if help:
        return
    if cohort is None and (len(runs) > 1 or parallel != 1 or max_usd is not None):
        raise LauncherError("several runs or cohort limits need --cohort NAME")
    spec = (
        CohortSpec(name=cohort, parallel=parallel, max_usd=Decimal(max_usd) if max_usd else None)
        if cohort
        else None
    )
    submission = controller.LaunchInput(
        runs=tuple(runs), cohort=spec, pool=pool, rent=rent, queued=queued
    )
    address = controller.selected()
    if quote:
        from etude.launch.listing import show_quote

        show_quote(
            controller.quote(controller.PythonClient(address) if address else default(), submission)
        )
        return
    if address:
        raise typer.Exit(controller.forward(address, [], submission))
    scheduler = default()
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
        bool, typer.Option(help="print the machines and the most they could cost; rent nothing")
    ] = False,
    help: FileHelp = False,
) -> None:
    """Queue a file's runs and get the machines to start them now, or fail and queue nothing."""
    _submit(ctx, file, pool, rent, cohort, parallel, max_usd, help, False, quote)


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
    help: FileHelp = False,
) -> None:
    """Add a file's runs to the queue. They start when a machine has room or can be rented."""
    _submit(ctx, file, pool, rent, cohort, parallel, max_usd, help, True)


@app.command()
@kind(Kind.wait)
def worker(
    directory: Annotated[Path | None, typer.Argument()] = None,
    bootstrap: str | None = None,
    root: Path = Path("/run/etude/sessions"),
    cleanup: Annotated[str | None, typer.Option(hidden=True)] = None,
) -> None:
    """Run a working session on this machine."""
    from etude.launch.worker.main import work

    if bool(directory) == bool(bootstrap):
        raise typer.BadParameter("worker needs DIRECTORY or --bootstrap URL")
    raise typer.Exit(work(directory, bootstrap, root, cleanup))


@app.command()
@kind(Kind.read)
def status(
    runs: Annotated[
        list[str] | None,
        typer.Argument(help="run ids or cohort names; none means all unfinished"),
    ] = None,
    phases: Annotated[
        bool,
        typer.Option(help="show when each stage of a run's start happened, from saved records"),
    ] = False,
) -> None:
    """Show where runs stand."""
    from etude.launch import controller
    from etude.launch.listing import resolve_runs, show_runs

    scheduler = default()
    if phases:
        for run in resolve_runs(scheduler, runs or []) or [r.id for r in scheduler.runs()]:
            for phase in scheduler.start_phases(run):
                at = phase.at.isoformat() if phase.at else "not recorded"
                attempt = f" attempt={phase.attempt}" if phase.attempt else ""
                evidence = f" ({phase.evidence})" if phase.evidence else ""
                print(f"{run} {phase.name} {at}{evidence}{attempt}")
        return
    scheduler.tick()
    show_runs(
        scheduler, resolve_runs(scheduler, runs or []) or [run.id for run in scheduler.runs()]
    )
    if problem := controller.warning(scheduler):
        print("warning: " + problem, file=sys.stderr)


@app.command("runs")
@kind(Kind.read)
def history(
    contains: Annotated[
        str | None, typer.Option(help="only runs whose source includes this git commit or tag")
    ] = None,
    cohort: Annotated[str | None, typer.Option(help="only runs of this cohort")] = None,
    pool: Annotated[str | None, typer.Option(help="only runs sent to this pool")] = None,
    rent: Annotated[str | None, typer.Option(help="only runs that may rent from this")] = None,
    job: Annotated[
        str | None, typer.Option(help="only runs of this job, by digest or name")
    ] = None,
) -> None:
    """List every run, finished ones included."""
    from etude.launch.listing import show_runs

    scheduler = default()
    show_runs(
        scheduler,
        [
            r.id
            for r in scheduler.runs(
                done=True, contains=contains, cohort=cohort, pool=pool, rent=rent, job=job
            )
        ],
    )


@app.command()
@kind(Kind.wait)
def source(
    run: str,
    directory: Annotated[
        Path | None, typer.Argument(help="where to put it; a new directory named for the run")
    ] = None,
) -> None:
    """Fetch the exact source a run used from git and check it out."""
    from etude.launch import controller
    from etude.launch.source import checkout

    address = controller.selected()
    client = controller.PythonClient(address) if address else default()
    record = client.record(run)
    print(checkout(record, directory or Path(f"etude-source-{record.id}")))


@app.command()
@kind(Kind.wait)
def wait(
    runs: Annotated[
        list[str] | None,
        typer.Argument(help="run ids or cohort names; none means all unfinished"),
    ] = None,
) -> None:
    """Keep work moving until these runs finish."""
    from etude.launch.listing import resolve_runs, show_runs

    scheduler = default()
    ids = resolve_runs(scheduler, runs or []) or [r.id for r in scheduler.runs()]
    scheduler.wait(*(ids if runs else ()))
    show_runs(scheduler, ids)


@app.command()
@kind(Kind.ask)
def cancel(
    runs: Annotated[list[str], typer.Argument(help="run ids or cohort names")],
) -> None:
    """Ask runs to stop."""
    from etude.launch.listing import resolve_runs, show_runs

    scheduler = default()
    ids = resolve_runs(scheduler, runs)
    for run in ids:
        scheduler.cancel(run)
    show_runs(scheduler, ids)


@app.command()
@kind(Kind.wait)
def logs(
    run: Annotated[
        str | None, typer.Option(help="requested run ID, including failed admission")
    ] = None,
    machine: Annotated[str | None, typer.Option(help="machine operational logs")] = None,
    cohort: Annotated[str | None, typer.Option(help="all runs in a cohort")] = None,
    job_only: Annotated[bool, typer.Option(help="only job stdout/stderr")] = False,
    follow: Annotated[
        bool, typer.Option(help="follow newly retained evidence, without scheduling")
    ] = False,
    timeout: Annotated[float, typer.Option(min=0, help="total seconds; 0 means no timeout")] = 0,
) -> None:
    """Read retained preparation, startup, execution and cleanup evidence."""
    import time

    from etude.launch.client import _client
    from etude.launch.logs import LogCursor, render

    if sum(value is not None for value in (run, machine, cohort)) != 1:
        raise typer.BadParameter("name exactly one --run, --machine or --cohort")
    scheduler = _client()
    runs = [r.id for r in scheduler.cohort_runs(cohort)] if cohort else [run]
    cursors = {identity: LogCursor() for identity in runs}
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        complete = True
        for identity in runs:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                print("log time limit reached; incomplete")
                return
            snapshot = scheduler.logs(run=identity, machine=machine, timeout=left)
            complete = complete and snapshot.complete
            sys.stdout.write(
                cursors[identity].render(snapshot, job_only=job_only)
                if follow
                else render(snapshot, job_only=job_only)
            )
        sys.stdout.flush()
        if not follow or complete:
            return
        if deadline is not None and time.monotonic() >= deadline:
            print("log time limit reached; incomplete")
            return
        time.sleep(1 if deadline is None else min(1, max(0, deadline - time.monotonic())))


@app.command()
@kind(Kind.wait)
def fetch(
    run: str,
    name: Annotated[str, typer.Argument(help="the output file's name, as the run wrote it")],
    directory: Annotated[Path, typer.Argument(help="where to copy it")] = Path("."),
) -> None:
    """Copy one of a run's output files here and check it against its digest."""
    from etude.launch import controller

    address = controller.selected()
    if address:
        raise typer.Exit(
            controller.forward(
                address,
                [],
                fetch=controller.FetchRequest(run=run, name=name, destination=directory),
            )
        )
    print(default().fetch(run, name, directory))


@app.command()
@kind(Kind.ask)
def rent(
    ondemand: Annotated[str, typer.Option(help="the on-demand to rent from")],
    hours: Annotated[float, typer.Option(help="how long to hold the machine")],
    max_usd: Annotated[
        str | None, typer.Option(help="end the window sooner if it would cost more than this")
    ] = None,
) -> None:
    """Rent a machine for a window of time. It takes queued runs. This costs money."""
    from etude.launch.listing import show_machines

    scheduler = default()
    scheduler.rent(ondemand, hours, Decimal(max_usd) if max_usd else None)
    show_machines(scheduler, ended=False)


@app.command()
@kind(Kind.ask)
def terminate(
    machine: Annotated[str, typer.Option(help="rented machine to delete")],
) -> None:
    """End a rented machine's window now. Refused while a run is on it."""
    from etude.launch.listing import show_machines

    scheduler = default()
    scheduler.terminate(machine)
    show_machines(scheduler, ended=False)


@app.command()
@kind(Kind.read)
def pools() -> None:
    """List pools: the machines you own in each, and the default."""
    from etude.launch.listing import show_pools

    show_pools(default())


@app.command()
@kind(Kind.read)
def ondemands() -> None:
    """List on-demands: what each rents, its budget and its machines now."""
    from etude.launch.listing import show_ondemands

    show_ondemands(default())


@app.command()
@kind(Kind.wait)
def availability(
    rent: Annotated[str, typer.Option(help="the on-demand to ask about")],
    timeout: Annotated[
        float, typer.Option(min=0, help="total seconds; 0 disables the timeout")
    ] = 0,
) -> None:
    """Ask the provider what an on-demand would rent now, and at what price. Rents nothing."""
    from etude.launch.listing import show_availability

    show_availability(default().availability(rent, timeout=timeout or None))


@app.command("default")
@kind(Kind.ask)
def choose_default(
    pool: Annotated[str | None, typer.Option(help="the pool runs use when none is named")] = None,
    rent: Annotated[
        str | None, typer.Option(help="the on-demand runs rent from when none is named")
    ] = None,
) -> None:
    """Choose where runs go when a launch names neither --pool nor --rent."""
    scheduler = default()
    scheduler.set_default(pool, rent=rent)
    chosen = scheduler.default_target()
    print("default:", f"pool {chosen.pool or 'none'},", f"rent {chosen.rent or 'none'}")


@app.command()
@kind(Kind.read)
def machines(
    all: Annotated[
        bool, typer.Option(help="include rented machines whose window has ended")
    ] = False,
    images: Annotated[
        bool, typer.Option(help="show retained image selections without registry access")
    ] = False,
) -> None:
    """List machines: added ones, and rented ones with their window and cost."""
    from etude.launch.listing import show_machines

    show_machines(default(), ended=all, images=images)


@app.command()
@kind(Kind.read)
def providers() -> None:
    """List the accounts that supply machines."""
    for known in default().providers():
        print(known.name, known.kind, f"storage {known.storage}", sep="  ")


@provider_app.command("set")
@kind(Kind.ask)
def provider_set(
    name: str,
    kind: Annotated[str, typer.Option(help="which provider this is, such as runpod")],
    storage: Annotated[str, typer.Option(help="the storage new pools on this provider use")],
    registry: Annotated[
        str, typer.Option(help="the provider's id for the credential that pulls private images")
    ] = "",
) -> None:
    """Record or replace an account that supplies machines. No secret is stored."""
    from etude.launch.records import Provider

    if kind != "runpod":
        raise ValueError(f"unsupported provider kind {kind!r}")
    default().set_provider(Provider(name=name, kind="runpod", registry=registry, storage=storage))
    providers()


@app.command()
@kind(Kind.read)
def storages() -> None:
    """List the places in buckets where records are kept."""
    for place in default().storages():
        print(place.name, place.uri, place.region or "-", sep="  ")


@storage_app.command("add")
@kind(Kind.ask)
def storage_add(
    name: str,
    bucket: Annotated[str, typer.Option(help="the bucket's name")],
    prefix: Annotated[str, typer.Option(help="the folder in that bucket to use")],
    provider: Annotated[
        Literal["s3", "r2"],
        typer.Option(help="s3, keys from AWS_*; or r2, account and keys from R2_*"),
    ] = "s3",
    region: Annotated[str | None, typer.Option(help="an S3 bucket's region")] = None,
) -> None:
    """Add a place in a bucket for records. It cannot be changed afterwards."""
    from pydantic import ValidationError

    from etude.launch.records import Storage

    try:
        place = Storage(name=name, bucket=bucket, prefix=prefix, provider=provider, region=region)
    except ValidationError as error:
        raise typer.BadParameter(error.errors()[0]["msg"].removeprefix("Value error, ")) from None
    default().add_storage(place)
    storages()


StartupSeconds = Annotated[float, typer.Option(min=0, help="startup allowance; 0 disables it")]
DeliverySeconds = Annotated[
    float, typer.Option(min=0, help="final delivery allowance; 0 disables it")
]
LogSeconds = Annotated[float, typer.Option(min=0, help="log publication allowance; 0 disables it")]


@pool_app.command("add")
@kind(Kind.ask)
def pool_add(
    name: str,
    startup_seconds: StartupSeconds = 0,
    delivery_seconds: DeliverySeconds = 0,
    log_seconds: LogSeconds = 0,
) -> None:
    """Add a pool: a named set of machines you own that runs are sent to."""
    from etude.launch.records import Pool

    default().add_pool(
        Pool(
            name=name,
            startup_seconds=startup_seconds or None,
            delivery_seconds=delivery_seconds or None,
            log_seconds=log_seconds or None,
        )
    )
    pools()


@pool_app.command("startup")
@kind(Kind.ask)
def pool_startup(name: str, seconds: StartupSeconds = 0) -> None:
    """Change the startup allowance for a pool's future sessions."""
    default().set_startup(name, seconds or None)
    pools()


@ondemand_app.command("add")
@kind(Kind.ask)
def ondemand_add(
    name: str,
    provider: Annotated[str, typer.Option(help="the account that rents its machines")],
    max: Annotated[int, typer.Option(help="how many machines it may rent at once")] = 1,
    max_usd_per_hour: Annotated[
        str | None, typer.Option(help="rent no machine that costs more than this per hour")
    ] = None,
    gpu: Annotated[
        str | None, typer.Option(help="the provider's name for the GPU; omit for CPU machines")
    ] = None,
    vcpus: Annotated[int | None, typer.Option(help="minimum virtual CPUs")] = None,
    memory_gb: Annotated[int | None, typer.Option(help="minimum memory")] = None,
    disk_gb: Annotated[int | None, typer.Option(help="disk size")] = None,
    image: Annotated[
        str | None, typer.Option(help="the image its machines boot, pinned by digest")
    ] = None,
    data_center: Annotated[
        str | None, typer.Option(help="the provider's name for the place; anywhere when omitted")
    ] = None,
    storage: Annotated[
        str | None,
        typer.Option(help="where its records go; the provider's storage when omitted"),
    ] = None,
    startup_seconds: StartupSeconds = 0,
    delivery_seconds: DeliverySeconds = 0,
    log_seconds: LogSeconds = 0,
) -> None:
    """Add an on-demand: a machine template to rent and a budget for renting it."""
    from etude.launch.records import Image, MachineTemplate, OnDemand

    default().add_ondemand(
        OnDemand(
            name=name,
            template=MachineTemplate(
                provider=provider,
                gpu=gpu,
                vcpus=vcpus,
                memory_gb=memory_gb,
                disk_gb=disk_gb,
                image=Image(requested=image, resolved=image) if image else None,
                data_center=data_center,
            ),
            max_machines=max,
            max_usd_per_hour=Decimal(max_usd_per_hour) if max_usd_per_hour else None,
            startup_seconds=startup_seconds or None,
            delivery_seconds=delivery_seconds or None,
            log_seconds=log_seconds or None,
            storage=storage,
        )
    )
    ondemands()


@ondemand_app.command("max")
@kind(Kind.ask)
def ondemand_max(name: str, maximum: int) -> None:
    """Change how many machines an on-demand may rent at once. Zero means only by hand."""
    default().set_ondemand_max(name, maximum)
    ondemands()


@ondemand_app.command("startup")
@kind(Kind.ask)
def ondemand_startup(name: str, seconds: StartupSeconds = 0) -> None:
    """Change the startup allowance for an on-demand's future machines."""
    default().set_startup(name, seconds or None)
    ondemands()


@image_app.command("prepare")
@kind(Kind.wait)
def image_prepare(
    reference: str,
    platform: str = "linux/amd64",
    timeout: Annotated[
        float, typer.Option(min=0, help="total seconds; 0 disables the timeout")
    ] = 0,
) -> None:
    """Wait for registry metadata and print the immutable selection; never rent."""
    from etude.launch.images import resolve_image

    print(resolve_image(reference, platform=platform, timeout=timeout or None).model_dump_json())


@ondemand_app.command("image")
@kind(Kind.wait)
def ondemand_image(
    name: str,
    image: str,
    timeout: Annotated[
        float, typer.Option(min=0, help="total seconds; 0 disables the timeout")
    ] = 0,
) -> None:
    """Resolve an image within timeout, then select it for future machines."""
    from etude.launch.images import resolve_image
    from etude.launch.records import Image

    selected = (
        Image(requested=image, resolved=image)
        if "@sha256:" in image
        else resolve_image(image, timeout=timeout or None)
    )
    default().set_ondemand_image(name, selected)
    print(selected.model_dump_json())


@pool_app.command("off")
@kind(Kind.ask)
def pool_off(name: str) -> None:
    """Stop sending new runs to a pool. What it holds finishes."""
    default().turn_pool(name, off=True)
    pools()


@pool_app.command("on")
@kind(Kind.ask)
def pool_on(name: str) -> None:
    """Send new runs to a pool again."""
    default().turn_pool(name, off=False)
    pools()


@machine_app.command("add")
@kind(Kind.ask)
def machine_add(
    name: str,
    address: Annotated[str, typer.Argument(help="anything your ssh accepts, such as user@host")],
    storage: Annotated[
        str | None, typer.Option(help="keep its records in this storage, not on its disk")
    ] = None,
    pool: Annotated[
        str | None,
        typer.Option(help="an existing pool to add it to; a pool of its own when omitted"),
    ] = None,
    cost_usd: Annotated[str, typer.Option(help="what it cost you, already committed")] = "0",
    expires_at: Annotated[
        datetime | None,
        typer.Option(help="when it stops being yours, in UTC; never when omitted"),
    ] = None,
) -> None:
    """Add a machine you own, reached by SSH."""
    default().add_machine(
        name,
        address,
        pool=pool,
        storage=storage,
        cost_usd=Decimal(cost_usd),
        expires_at=None if expires_at is None else expires_at.replace(tzinfo=UTC),
    )
    pools()


@controller_app.callback()
@kind(Kind.read)
def controller_show(ctx: typer.Context) -> None:
    """Show which machine holds the database, and when its loop last ran."""
    from etude.launch.controller import show

    if ctx.invoked_subcommand is None:
        show(default())


@controller_app.command("set")
@kind(Kind.ask)
def controller_set(
    address: str,
    startup_seconds: Annotated[
        float, typer.Option(min=0, help="install allowance; 0 disables it")
    ] = 0,
) -> None:
    """Name another machine as the controller. Commands then run there over SSH."""
    from etude.launch import controller

    controller.set_address(address, startup_seconds=startup_seconds or None)
    print("controller: " + address)


@controller_app.command("unset")
@kind(Kind.ask)
def controller_unset() -> None:
    """Use this machine's own database again."""
    from etude.launch.controller import unset

    unset()
    print("controller: this machine")


@controller_app.command("run")
@kind(Kind.wait)
def controller_run() -> None:
    """Keep matching waiting runs to machines until stopped. Run this on the controller."""
    from etude.launch.controller import loop

    loop(default())


@app.command()
@kind(Kind.read)
def usage(
    run: str | None = typer.Option(None, help="Run, including its shared session traffic."),
    machine: str | None = typer.Option(None, help="Machine name."),
) -> None:
    """Read retained transfer usage offline; does not refresh remote evidence."""
    from etude.launch.client import usage as read_usage

    report = read_usage(run=run, machine=machine)
    print(report.model_dump_json())
    print(
        f"received_bytes={report.received_bytes} supplied_bytes={report.supplied_bytes} "
        f"reused_bytes={report.reused_bytes} retransferred_bytes={report.retransferred_bytes} "
        f"requests={report.requests}"
    )
