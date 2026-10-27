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
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import cache
from pathlib import Path

from etude.launch.controller import PythonClient, selected
from etude.launch.database import Database
from etude.launch.executor import Execution, Executor, local
from etude.launch.machines import OwnedMachine, Safeguards
from etude.launch.providers.runpod.api import RunPodAPI
from etude.launch.providers.runpod.machines import RunPodMachines
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

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        with observe(store.retain_transfer_json):
            return _send(method, url, body, headers)

    def runpod(held: Machine | OnDemand) -> Execution:
        """Built when first needed, so only RunPod use needs RunPod's keys in the environment."""

        if not isinstance(held, OnDemand):
            raise LauncherError("RunPod reaches machines rented from an on-demand")
        if "RUNPOD_API_KEY" not in os.environ:
            raise LauncherError("RUNPOD_API_KEY is not set")
        if held.storage is None:
            raise LauncherError(f"on-demand {held.name!r} has no storage")
        storage = store.storage(held.storage)
        bucket = open_bucket(storage, send)
        machines = RunPodMachines(
            store.provider(held.template.provider),
            held.template,
            RunPodAPI(os.environ["RUNPOD_API_KEY"]),
            bucket,
            storage.prefix,
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

    runpod.safeguards = Safeguards(start_command=True)  # type: ignore[attr-defined]
    scheduler = Scheduler(store, {"local": lambda machine: here, "ssh": ssh, "runpod": runpod})
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


def queue(run: Run | Job, pool: str | None = None, *, rent: str | None = None) -> str:
    return _client().queue(run, pool, rent=rent)


def queue_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> tuple[str, ...]:
    return _client().queue_cohort(name, runs, pool, rent=rent, parallel=parallel, max_usd=max_usd)


def launch(run: Run | Job, pool: str | None = None, *, rent: str | None = None) -> str:
    return _client().launch(run, pool, rent=rent)


def launch_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> tuple[str, ...]:
    return _client().launch_cohort(name, runs, pool, rent=rent, parallel=parallel, max_usd=max_usd)


def quote(
    runs: list[Run],
    pool: str | None = None,
    *,
    rent: str | None = None,
    cohort: str | None = None,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> Quote:
    """The machines a launch would take now and the most they could cost; rents nothing."""
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
