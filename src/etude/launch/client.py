"""The launcher's calls as plain functions: queue, launch, status, wait and the rest.

    from decimal import Decimal

    from etude.launch.client import launch_cohort, wait
    from etude.launch.records import Job

    job = Job(command="python train.py", max_hours=6, max_usd=Decimal(5))
    runs = launch_cohort("first", job.runs(1, 2, 3))
    print(wait(*runs))

Each function uses the selected SSH controller, or a launcher under ETUDE_HOME
(default ~/.etude) with an initial pool, "local", holding this computer. A
call names a pool (`pool=`), an on-demand to rent from (`rent=`), or both.
With `smoke=True`, `queue`, `queue_cohort`, `launch` and `launch_cohort` first
run each job's tiny form here (`etude.launch.smoke`) and submit nothing if one
fails; each run records what its smoke did. `etude.smoke.smoke_file` smokes a
whole experiment file, evaluation and report too.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from decimal import Decimal
from functools import cache
from pathlib import Path

from etude.launch.controller import PythonClient, selected
from etude.launch.database import Database
from etude.launch.executor import Execution, Executor, local
from etude.launch.machines import Machines, OwnedMachine, Safeguards
from etude.launch.providers.lium.api import LiumAPI
from etude.launch.providers.lium.machines import LiumMachines
from etude.launch.providers.runpod.api import RunPodAPI
from etude.launch.providers.runpod.machines import RunPodMachines
from etude.launch.providers.vast.api import VastAPI
from etude.launch.providers.vast.machines import VastMachines
from etude.launch.records import (
    Availability,
    Diagnostic,
    Job,
    LauncherError,
    LogSnapshot,
    Machine,
    OnDemand,
    Pool,
    Quote,
    Run,
    Status,
)
from etude.launch.scheduler import Scheduler
from etude.launch.smoke import show, smoke_home, smoke_train, smoked
from etude.launch.source import home
from etude.launch.ssh import SSH, SSHJournal, SSHStart
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Reply, _send, open_bucket
from etude.launch.storage.transport import observe
from etude.launch.usage import Usage


@cache
def _open(home: Path) -> Scheduler:
    fresh = not (home / "launcher.db").exists()
    store = Database(home / "launcher.db")
    here = local(home / "sessions")
    owner = threading.get_ident()

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        # The store's SQLite connection belongs to the opening thread. A request on
        # another thread, such as a download part, keeps the sink its caller set there.
        if threading.get_ident() != owner:
            return _send(method, url, body, headers)
        with observe(store.retain_transfer_json):
            return _send(method, url, body, headers)

    def rented(held: Machine | OnDemand) -> Execution:
        """Construct only the selected provider, when a remote operation needs it."""
        if not isinstance(held, OnDemand):
            raise LauncherError("rentals need an on-demand")
        provider = store.provider(held.template.provider)
        key_name = {"runpod": "RUNPOD_API_KEY", "vast": "VAST_API_KEY", "lium": "LIUM_API_KEY"}[
            provider.kind
        ]
        if key_name not in os.environ:
            raise LauncherError(f"{key_name} is not set")
        if held.storage is None:
            raise LauncherError(f"on-demand {held.name!r} has no storage")
        storage = store.storage(held.storage)
        bucket = open_bucket(storage, send)
        machines: Machines
        if provider.kind == "runpod":
            machines = RunPodMachines(
                provider,
                held.template,
                RunPodAPI(os.environ[key_name]),
                bucket,
                storage.prefix,
            )
        elif provider.kind == "vast":
            machines = VastMachines(
                held.template,
                VastAPI(os.environ[key_name]),
                bucket,
                storage.prefix,
            )
        else:
            machines = LiumMachines(
                held.template, LiumAPI(os.environ[key_name]), bucket, storage.prefix
            )
        return Executor(machines, None, BucketJournal(storage, bucket))

    def ssh(machine: Machine | OnDemand) -> Execution:
        if not isinstance(machine, Machine) or machine.address is None:
            raise LauncherError("an SSH machine needs an address")
        connection = SSH(machine.address)
        journal = SSHJournal(connection)
        if machine.storage:
            storage = store.storage(machine.storage)
            bucket = open_bucket(storage, send)
            return Executor(
                OwnedMachine(machine.address),
                SSHStart(connection),
                BucketJournal(storage, bucket),
                control=journal,
            )
        return Executor(OwnedMachine(machine.address), SSHStart(connection), journal)

    def lium(held: Machine | OnDemand) -> Execution:
        return rented(held)

    lium.safeguards = LiumMachines.safeguards  # type: ignore[attr-defined]
    rented.safeguards = Safeguards(start_command=True)  # type: ignore[attr-defined]
    scheduler = Scheduler(
        store,
        {"local": lambda machine: here, "ssh": ssh, "runpod": rented, "vast": rented, "lium": lium},
    )
    if fresh:  # this computer is offered once; `etude pool off local` withdraws it
        scheduler.add_pool(Pool(name="local"))
        store.add_machine(Machine(name="local", pool="local", kind="local"))
    if os.environ.get("ETUDE_CONTROLLER_LOCAL"):
        store.name_controller_pool()
    return scheduler


def default() -> Scheduler:
    return _open(home())


def _client() -> Scheduler | PythonClient:
    address = selected()
    return PythonClient(address) if address else default()


def _smoked(runs: Sequence[Run | Job], smoke: bool) -> list[Run]:
    each = [run if isinstance(run, Run) else Run(job=run) for run in runs]
    if not smoke:
        return each
    with smoke_home() as folder:
        steps = smoke_train(each, folder)
    for step in steps:
        show(step)
    done = smoked(steps)
    return [run.model_copy(update={"smoke": done}) for run in each]


def queue(
    run: Run | Job, pool: str | None = None, *, rent: str | None = None, smoke: bool = False
) -> str:
    (smoked_run,) = _smoked([run], smoke)
    return _client().queue(smoked_run, pool, rent=rent)


def queue_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
    smoke: bool = False,
) -> tuple[str, ...]:
    return _client().queue_cohort(
        name, _smoked(runs, smoke), pool, rent=rent, parallel=parallel, max_usd=max_usd
    )


def launch(
    run: Run | Job, pool: str | None = None, *, rent: str | None = None, smoke: bool = False
) -> str:
    (smoked_run,) = _smoked([run], smoke)
    return _client().launch(smoked_run, pool, rent=rent)


def launch_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
    smoke: bool = False,
) -> tuple[str, ...]:
    return _client().launch_cohort(
        name, _smoked(runs, smoke), pool, rent=rent, parallel=parallel, max_usd=max_usd
    )


def quote(
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    cohort: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> Quote:
    """The machines a launch would take now and planned rental costs; rents nothing.

    Costs assume the quoted rate and timely deletion, and exclude external storage.
    Provider delays can exceed the admitted rental budget.
    """
    return _client().quote(runs, pool, rent=rent, cohort=cohort, parallel=parallel, max_usd=max_usd)


def availability(rent: str, *, timeout: float | None = None) -> Availability:
    """Ask an on-demand's provider what it would rent now, at what price; rents nothing."""
    return _client().availability(rent, timeout=timeout)


def status(run: str) -> Status:
    return _client().status(run)


def wait(*runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
    return _client().wait(*runs, every=every, timeout=timeout)


def cancel(run: str) -> Status:
    return _client().cancel(run)


def diagnostics(*, run: str | None = None, machine: str | None = None) -> tuple[Diagnostic, ...]:
    return _client().diagnostics(run=run, machine=machine)


def logs(
    *,
    run: str | None = None,
    machine: str | None = None,
    refresh: bool = False,
    timeout: float | None = None,
) -> LogSnapshot:
    """Retained evidence; `refresh` first collects from machines within `timeout`."""
    return _client().logs(run=run, machine=machine, refresh=refresh, timeout=timeout)


def fetch(run: str, name: str, destination: Path) -> Path:
    return _client().fetch(run, name, destination)


def rent(ondemand: str, hours: float, max_usd: Decimal | None = None) -> str:
    """Rent a machine from an on-demand for a window and return its name."""
    return _client().rent(ondemand, hours, max_usd)


def terminate(machine: str) -> Machine:
    """Delete an idle rented machine and return its acquisition record."""
    return _client().terminate(machine)


def usage(*, run: str | None = None, machine: str | None = None) -> Usage:
    """Read retained transfer usage without polling machines or storage."""
    return _client().usage(run=run, machine=machine)
