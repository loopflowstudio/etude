"""The launcher's calls as plain functions: queue, launch, status, wait and the rest.

    from decimal import Decimal

    from etude.launch.client import launch_cohort, wait
    from etude.launch.records import Job

    job = Job(command="python train.py", max_hours=6, max_usd=Decimal(5))
    runs = launch_cohort("first", job.runs(1, 2, 3))
    print(wait(*runs))

Each function uses the selected SSH controller, or a launcher under ETUDE_HOME
(default ~/.etude) with an initial pool, "local", holding this computer.
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
    Job,
    LauncherError,
    Machine,
    Pool,
    Run,
    Status,
)
from etude.launch.scheduler import Scheduler
from etude.launch.source import home
from etude.launch.ssh import SSH, SSHJournal, SSHStart
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys


@cache
def _open(home: Path) -> Scheduler:
    fresh = not (home / "launcher.db").exists()
    store = Database(home / "launcher.db")
    here = local(home / "sessions")

    def runpod(pool: Pool, machine: Machine | None) -> Execution:
        """Built when first needed, so only RunPod use needs RunPod's keys in the environment."""

        if pool.on_demand_template is None or "RUNPOD_API_KEY" not in os.environ:
            raise LauncherError("RUNPOD_API_KEY is not set")
        if pool.storage is None:
            raise LauncherError(f"pool {pool.name!r} has no storage")
        storage = store.storage(pool.storage)
        bucket = Bucket(storage.bucket, storage.region, Keys.from_environment())
        machines = RunPodMachines(
            store.provider(pool.on_demand_template.provider),
            pool.on_demand_template,
            RunPodAPI(os.environ["RUNPOD_API_KEY"]),
            bucket,
            storage.prefix,
        )
        return Executor(machines, None, BucketJournal(storage, bucket))

    def ssh(pool: Pool, machine: Machine | None) -> Execution:
        if machine is None or machine.address is None:
            raise LauncherError("an SSH machine needs an address")
        connection = SSH(machine.address)
        journal = SSHJournal(connection)
        if machine.storage:
            storage = store.storage(machine.storage)
            bucket = Bucket(storage.bucket, storage.region, Keys.from_environment())
            return Executor(
                OwnedMachine(machine.address),
                SSHStart(connection),
                BucketJournal(storage, bucket),
                control=journal,
            )
        return Executor(OwnedMachine(machine.address), SSHStart(connection), journal)

    runpod.safeguards = Safeguards(start_command=True)  # type: ignore[attr-defined]
    scheduler = Scheduler(
        store, {"local": lambda pool, machine: here, "ssh": ssh, "runpod": runpod}
    )
    if fresh:  # this computer is offered once; `etude pool off local` withdraws it
        scheduler.add_pool(Pool(name="local", setup_seconds=60))
        store.add_machine(Machine(name="local", pool="local", kind="local"))
    if os.environ.get("ETUDE_CONTROLLER_LOCAL"):
        store.name_controller_pool()
    return scheduler


def default() -> Scheduler:
    return _open(home())


def _client() -> Scheduler | PythonClient:
    address = selected()
    return PythonClient(address) if address else default()


def queue(run: Run | Job, pool: str | None = None) -> str:
    return _client().queue(run, pool)


def queue_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> tuple[str, ...]:
    return _client().queue_cohort(
        name,
        runs,
        pool,
        parallel=parallel,
        max_usd=max_usd,
    )


def launch(run: Run | Job, pool: str | None = None) -> str:
    return _client().launch(run, pool)


def launch_cohort(
    name: str,
    runs: list[Run],
    pool: str | None = None,
    *,
    parallel: int = 1,
    max_usd: Decimal | None = None,
) -> tuple[str, ...]:
    return _client().launch_cohort(
        name,
        runs,
        pool,
        parallel=parallel,
        max_usd=max_usd,
    )


def continue_from(run: str, until: str) -> Run:
    return _client().continue_from(run, until)


def status(run: str) -> Status:
    return _client().status(run)


def wait(*runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
    return _client().wait(*runs, every=every, timeout=timeout)


def cancel(run: str) -> Status:
    return _client().cancel(run)


def logs(run: str) -> str:
    return _client().logs(run)


def fetch(run: str, name: str, destination: Path) -> Path:
    return _client().fetch(run, name, destination)


def rent(pool: str, hours: float, max_usd: Decimal | None = None) -> str:
    """Make a paid machine in an on-demand pool and return its name."""
    return _client().rent(pool, hours, max_usd)


def terminate(machine: str) -> Machine:
    """Delete an idle rented machine and return its acquisition record."""
    return _client().terminate(machine)
