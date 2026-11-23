"""The launcher's calls as plain functions: queue, launch, status, wait and the rest.

    from decimal import Decimal

    from etude.launch.client import launch, wait
    from etude.spec import Job

    job = Job(command="python train.py", max_hours=6, max_usd=Decimal(5))
    runs = launch(job.runs(1, 2, 3))
    print(wait(*runs))

Each function uses the selected SSH server, or a launcher under ETUDE_HOME
(default ~/.etude) with an initial pool, "local", holding this computer. A
call names a pool (`pool=`), an on-demand to rent from (`rent=`), or both.
`etude launch FILE --smoke` first smokes a whole experiment file here
(`etude.controller.smoke.smoke_file`).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path

from etude.errors import LauncherError
from etude.home import home
from etude.launch.aws import Keys
from etude.launch.database import open_home
from etude.launch.executor import Execution, Executor, ProviderService, local
from etude.launch.provisioner.machines import Machines, OwnedMachine, Safeguards
from etude.launch.provisioner.providers.colab.api import ColabAPI
from etude.launch.provisioner.providers.colab.api import (
    validate_template as validate_colab,
)
from etude.launch.provisioner.providers.colab.http import load_credentials as colab_credentials
from etude.launch.provisioner.providers.colab.machines import ColabMachines
from etude.launch.provisioner.providers.ec2.api import EC2API, location
from etude.launch.provisioner.providers.ec2.machines import EC2Machines
from etude.launch.provisioner.providers.lambda_cloud.api import LambdaAPI
from etude.launch.provisioner.providers.lambda_cloud.machines import LambdaMachines
from etude.launch.provisioner.providers.lium.api import LiumAPI
from etude.launch.provisioner.providers.lium.machines import LiumMachines
from etude.launch.provisioner.providers.runpod.api import RunPodAPI
from etude.launch.provisioner.providers.runpod.machines import RunPodMachines
from etude.launch.provisioner.providers.vast.api import VastAPI
from etude.launch.provisioner.providers.vast.machines import VastMachines
from etude.launch.records import (
    LogSnapshot,
    Machine,
    MachineTemplate,
    OnDemand,
    Provider,
    Quote,
    Standing,
    Status,
    Storage,
)
from etude.launch.scheduler.loop import Scheduler
from etude.launch.server import PythonClient, retained_scheduler, selected
from etude.launch.ssh import SSH, SSHJournal, SSHStart
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, _send, open_bucket
from etude.launch.storage.transport import HTTPReply, observe
from etude.launch.usage import Usage
from etude.spec import Job, RunRequest, expand_runs

__all__ = (
    "queue",
    "launch",
    "quote",
    "status",
    "wait",
    "cancel",
    "logs",
    "fetch",
    "default",
    "usage",
    "standing",
)


@cache
def _open(home: Path) -> Scheduler:
    store = open_home(home)
    here = local(home / "sessions")
    owner = threading.get_ident()

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> HTTPReply:
        # The store's SQLite connection belongs to the opening thread. A request on
        # another thread, such as a download part, keeps the sink its caller set there.
        if threading.get_ident() != owner:
            return _send(method, url, body, headers)
        with observe(store.retain_transfer_json):
            return _send(method, url, body, headers)

    def api_key(name: str) -> str:
        if key := os.environ.get(name):
            return key
        raise LauncherError(f"{name} is not set")

    def provider[K](
        credentials: Callable[[], K],
        make: Callable[[K, Provider, MachineTemplate, Bucket, Storage], Machines],
        safeguards: Safeguards,
        *,
        stock_image: bool = True,
        validate: Callable[[MachineTemplate], str | None] | None = None,
    ) -> ProviderService:
        def rented(held: Machine | OnDemand) -> Execution:
            if not isinstance(held, OnDemand) or held.storage is None:
                raise LauncherError("rentals need an on-demand with storage")
            keys = credentials()
            account = store.provider(held.template.provider)
            storage = store.storage(held.storage)
            bucket = open_bucket(storage, send)
            return Executor(
                make(keys, account, held.template, bucket, storage),
                None,
                BucketJournal(storage, bucket),
            )

        return ProviderService(rented, safeguards, stock_image=stock_image, validate=validate)

    def ec2(
        keys: Keys, account: Provider, template: MachineTemplate, bucket: Bucket, storage: Storage
    ) -> Machines:
        region, zone = location(template, storage.region)
        return EC2Machines(
            template,
            EC2API(keys, region, send),
            zone,
            bucket,
            storage.prefix,
        )

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

    scheduler = Scheduler(
        store,
        {
            "local": lambda machine: here,
            "ssh": ssh,
            "runpod": provider(
                lambda: api_key("RUNPOD_API_KEY"),
                lambda key, account, template, bucket, storage: RunPodMachines(
                    account,
                    template,
                    RunPodAPI(key),
                    bucket,
                    storage.prefix,
                ),
                RunPodMachines.safeguards,
            ),
            "vast": provider(
                lambda: api_key("VAST_API_KEY"),
                lambda key, _, template, bucket, storage: VastMachines(
                    template, VastAPI(key), bucket, storage.prefix
                ),
                VastMachines.safeguards,
            ),
            "lium": provider(
                lambda: api_key("LIUM_API_KEY"),
                lambda key, _, template, bucket, storage: LiumMachines(
                    template, LiumAPI(key), bucket, storage.prefix
                ),
                LiumMachines.safeguards,
            ),
            "lambda": provider(
                lambda: api_key("LAMBDA_API_KEY"),
                lambda key, _, template, bucket, storage: LambdaMachines(
                    template,
                    LambdaAPI(key),
                    os.environ.get("LAMBDA_TERMINATE_KEY"),
                    bucket,
                    storage.prefix,
                ),
                LambdaMachines.safeguards,
            ),
            "ec2": provider(lambda: Keys.from_environment("s3"), ec2, EC2Machines.safeguards),
            "colab": provider(
                colab_credentials,
                lambda creds, _, template, bucket, storage: ColabMachines(
                    template, ColabAPI(creds), bucket, storage.prefix
                ),
                ColabMachines.safeguards,
                stock_image=False,
                validate=validate_colab,
            ),
        },
    )
    if os.environ.get("ETUDE_SERVER_LOCAL"):
        store.name_server_pool()
    return scheduler


def default() -> Scheduler:
    return _open(home())


def _client() -> Scheduler | PythonClient:
    address = selected()
    return PythonClient(address) if address else default()


def _runs(runs: RunRequest | Job | Sequence[RunRequest | Job]) -> list[RunRequest]:
    return expand_runs([runs] if isinstance(runs, (Job, RunRequest)) else runs)


def queue(
    runs: RunRequest | Job | Sequence[RunRequest | Job],
    pool: str | None = None,
    *,
    rent: str | None = None,
) -> tuple[str, ...]:
    return _client().queue(_runs(runs), pool, rent=rent)


def launch(
    runs: RunRequest | Job | Sequence[RunRequest | Job],
    pool: str | None = None,
    *,
    rent: str | None = None,
) -> tuple[str, ...]:
    return _client().launch(_runs(runs), pool, rent=rent)


def quote(
    runs: list[RunRequest],
    pool: str | None = None,
    *,
    rent: str | None = None,
) -> Quote:
    """Quote first-wave placements and rental costs without renting."""
    return _client().quote(runs, pool, rent=rent)


def status(run: str) -> Status:
    return _client().status(run)


def wait(*runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
    return _client().wait(*runs, every=every, timeout=timeout)


def cancel(run: str) -> Status:
    return _client().cancel(run)


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


def usage(*, run: str | None = None, machine: str | None = None) -> Usage:
    """Read retained transfer usage without polling machines or storage."""
    return _client().usage(run=run, machine=machine)


def standing(
    experiment: str | None = None, *, local: bool = False, job: str | None = None
) -> tuple[Standing, ...]:
    """Retained runs and statuses; a page can explicitly read only this computer."""
    address = None if local else selected()
    if address:
        return PythonClient(address).standing(experiment, job=job)
    with retained_scheduler() as reader:
        return reader.standing(experiment, job=job) if reader is not None else ()
