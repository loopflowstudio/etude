"""Optional SSH server: command transport, frozen inputs and a standing loop.

SSH supplies authentication. CLI and Python clients use the same typed requests;
rendering stays with the caller. Provider/storage keys stay in the
server's environment. The local setting never opens the launcher database.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field, RootModel, TypeAdapter

from etude.deadline import limit_time, remaining
from etude.errors import LauncherError, explain
from etude.launch.errors import LaunchError, SchemaAdvanced
from etude.launch.measurement import timed
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import (
    Availability,
    Diagnostic,
    Image,
    Launch,
    LogSnapshot,
    Machine,
    OnDemand,
    Pool,
    Provider,
    Quote,
    RentalCost,
    Standing,
    StartPhase,
    Status,
    Storage,
    Target,
)
from etude.launch.scheduler.loop import Scheduler
from etude.launch.source import freeze, home, retain, source_archive
from etude.launch.ssh import (
    _INSTALL_CODE,
    EncodedOutput,
    login_shell,
    remote_python,
)
from etude.launch.storage.files import write
from etude.launch.usage import Usage
from etude.records import RunRecord
from etude.spec import Job, Record, RunRequest, expand_runs


class Source(Record):
    repo: str
    commit: str
    sha256: str
    body: str


class LaunchInput(Record):
    runs: tuple[RunRequest, ...]
    pool: str | None
    rent: str | None = None
    sources: tuple[Source, ...] = ()
    queued: bool = False
    launch: str | None = None


class FetchRequest(Record):
    run: str
    name: str
    destination: Path


# Server operations, one record per shape of request. No credentials or
# provider authority cross this wire; both ends run the same program.


class SubmissionRequest(Record):
    operation: Literal["launch", "prepare_launch", "quote"]
    submission: LaunchInput


class FailLaunchRequest(Record):
    operation: Literal["fail_launch"] = "fail_launch"
    launch: str
    message: str


class OutputRequest(Record):
    operation: Literal["fetch"] = "fetch"
    run: str
    output: str  # the output's name below the run's output directory


class RunOperation(Record):
    operation: Literal["record", "status", "read", "cancel", "start_phases"]
    run: str


class StandingRequest(Record):
    operation: Literal["standing"] = "standing"
    experiment: str | None = None
    job: str | None = None


class WaitRequest(Record):
    operation: Literal["wait"] = "wait"
    runs: tuple[str, ...]
    every: float = 1.0
    timeout: float | None = None


class EvidenceRequest(Record):
    operation: Literal["diagnostics", "usage"]
    run: str | None = None
    machine: str | None = None


class LogsRequest(Record):
    operation: Literal["logs"] = "logs"
    run: str | None = None
    machine: str | None = None
    refresh: bool = False
    timeout: float | None = None


class RentRequest(Record):
    operation: Literal["rent"] = "rent"
    ondemand: str
    hours: float
    max_usd: Decimal | None = None


class TerminateRequest(Record):
    operation: Literal["terminate"] = "terminate"
    machine: str


class AvailabilityRequest(Record):
    operation: Literal["availability"] = "availability"
    ondemand: str
    timeout: float | None = None


class InventoryRequest(Record):
    operation: Literal[
        "pools",
        "providers",
        "storages",
        "ondemands",
        "rentals",
        "default_target",
        "now",
        "freshness",
    ]


class RunsRequest(Record):
    operation: Literal["runs"] = "runs"
    done: bool = False
    pool: str | None = None
    rent: str | None = None
    job: str | None = None
    contains: str | None = None
    experiment: str | None = None


class MachinesRequest(Record):
    operation: Literal["machines"] = "machines"
    supply: str


class SafeguardsRequest(Record):
    operation: Literal["safeguards"] = "safeguards"
    ondemand: str


class ProviderRequest(Record):
    operation: Literal["set_provider"] = "set_provider"
    provider: Provider


class StorageRequest(Record):
    operation: Literal["add_storage"] = "add_storage"
    storage: Storage


class PoolRequest(Record):
    operation: Literal["add_pool"] = "add_pool"
    pool: Pool


class OnDemandRequest(Record):
    operation: Literal["add_ondemand"] = "add_ondemand"
    ondemand: OnDemand


class StartupRequest(Record):
    operation: Literal["set_startup"] = "set_startup"
    name: str
    seconds: float | None


class MaximumRequest(Record):
    operation: Literal["set_ondemand_max"] = "set_ondemand_max"
    name: str
    maximum: int


class ImageSelectionRequest(Record):
    operation: Literal["set_ondemand_image"] = "set_ondemand_image"
    name: str
    image: Image | str


class TargetRequest(Record):
    operation: Literal["set_default"] = "set_default"
    target: Target


class PoolStateRequest(Record):
    operation: Literal["turn_pool"] = "turn_pool"
    name: str
    off: bool


class MachineRequest(Record):
    operation: Literal["add_machine"] = "add_machine"
    name: str
    address: str
    pool: str | None = None
    storage: str | None = None
    cost_usd: Decimal = Decimal(0)
    expires_at: datetime | None = None


class RunFilesRequest(Record):
    operation: Literal["held"] = "held"
    run: str


class EncodedFiles(Record):
    files: dict[str, str] | None


class ImageRequest(Record):
    operation: Literal["resolve_image"] = "resolve_image"
    reference: str
    platform: str = "linux/amd64"
    timeout: float | None = None


class LoopRequest(Record):
    operation: Literal["loop"] = "loop"


class Freshness(Record):
    last_loop: datetime | None
    warning: str | None


type Request = Annotated[
    SubmissionRequest
    | FailLaunchRequest
    | OutputRequest
    | RunOperation
    | StandingRequest
    | WaitRequest
    | EvidenceRequest
    | LogsRequest
    | RentRequest
    | TerminateRequest
    | AvailabilityRequest
    | InventoryRequest
    | RunsRequest
    | MachinesRequest
    | SafeguardsRequest
    | ProviderRequest
    | StorageRequest
    | PoolRequest
    | OnDemandRequest
    | StartupRequest
    | MaximumRequest
    | ImageSelectionRequest
    | TargetRequest
    | PoolStateRequest
    | MachineRequest
    | RunFilesRequest
    | ImageRequest
    | LoopRequest,
    Field(discriminator="operation"),
]
_REQUEST: TypeAdapter[Request] = TypeAdapter(Request)


type Response = (
    tuple[str, ...]
    | list[Status]
    | RunRecord
    | Status
    | LaunchInput
    | EncodedOutput
    | Machine
    | Usage
    | LogSnapshot
    | tuple[Diagnostic, ...]
    | tuple[RunRecord, ...]
    | tuple[Standing, ...]
    | Launch
    | Quote
    | Availability
    | str
    | datetime
    | Target
    | Freshness
    | EncodedFiles
    | Image
    | tuple[Pool, ...]
    | tuple[Provider, ...]
    | tuple[Storage, ...]
    | tuple[OnDemand, ...]
    | tuple[Machine, ...]
    | tuple[RentalCost, ...]
    | tuple[StartPhase, ...]
)


class ServerReply(RootModel[Response]):
    model_config = {"frozen": True}


def selected() -> str | None:
    if os.environ.get("ETUDE_SERVER_LOCAL"):
        return None
    path = home() / "server"
    return path.read_text().strip() if path.exists() else None


def set_address(address: str, *, startup_seconds: float | None = None) -> None:
    if not address or address.startswith("-") or any(c.isspace() for c in address):
        raise LauncherError("server needs an SSH address, such as user@host or an SSH alias")
    install(
        address, startup_seconds=startup_seconds
    )  # fails here, with a reason, if the machine cannot be a server
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    write(home() / "server", address)


def unset() -> None:
    (home() / "server").unlink(missing_ok=True)


# The server runs the client's own code, sent over SSH and kept by digest
# beside its database, installed with uv in its own environment. The two sides
# always use the same source and dependency lock.
_MISSING = 97  # the remote exit status meaning "that program is not here yet"
_INSTALL = "import sys\ncode=sys.stdin.buffer.read()\n" + _INSTALL_CODE


@cache
def _program() -> tuple[str, bytes]:
    code = program_archive()
    return hashlib.sha256(code).hexdigest(), code


def _command(address: str, argv: Sequence[str]) -> list[str]:
    # A remote setting cannot forward again. No local environment/keys are serialized.
    root = '"${ETUDE_HOME:-$HOME/.etude}/programs/' + _program()[0] + '"'
    run = root + "/.venv/bin/python -m etude"
    return [
        "ssh",
        "--",
        address,
        login_shell(
            f"test -f {root}/ready || exit {_MISSING}; "
            f"ETUDE_SERVER_LOCAL=1 exec {run} {shlex.join(argv)}"
        ),
    ]


@timed("ssh.server.install")
def install(address: str, *, startup_seconds: float | None = None) -> None:
    """Send this client's code to the server. Safe to repeat."""
    if startup_seconds is not None and (not math.isfinite(startup_seconds) or startup_seconds <= 0):
        raise ValueError("startup_seconds must be positive and finite, or None")
    allowance = remaining(startup_seconds)
    deadline = (
        (datetime.now(UTC) + timedelta(seconds=allowance)).isoformat()
        if allowance is not None
        else ""
    )
    program = "import os\nos.environ['ETUDE_STARTUP_DEADLINE']=" + repr(deadline) + "\n" + _INSTALL
    try:
        done = subprocess.run(
            ["ssh", "--", address, login_shell(shlex.join([remote_python(), "-c", program]))],
            input=_program()[1],
            capture_output=True,
            timeout=allowance,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise LauncherError(f"server {address!r} did not answer over SSH") from None
    if done.returncode:
        said = done.stderr.decode(errors="replace").strip().splitlines()
        raise LauncherError(
            f"server {address!r} could not take the program"
            + (f": {said[-1]}" if said else "")
            + f". It needs {remote_python()} and uv on its login shell's PATH and working SSH keys"
        )


@timed("ssh.server.command")
def _ssh(address: str, argv: Sequence[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
    """Run one command on the server, sending the program first if it is not there.

    Exit status 97 means the command did not start, so running it again is safe.
    """
    options["timeout"] = remaining(options.get("timeout"))
    done = subprocess.run(_command(address, argv), **options)
    if done.returncode == _MISSING:
        install(address)
        if "timeout" in options:
            options["timeout"] = remaining(options["timeout"])
        done = subprocess.run(_command(address, argv), **options)
    return done


def prepare(address: str, submission: LaunchInput) -> LaunchInput:
    client = PythonClient(address)
    launch = Launch.model_validate_json(
        client._request(SubmissionRequest(operation="prepare_launch", submission=submission))
    )
    requested = tuple(
        run.model_copy(update={"id": request.run})
        for run, request in zip(submission.runs, launch.requests, strict=True)
    )
    try:
        return _with_sources(
            submission.model_copy(
                update={"launch": launch.id, "runs": tuple(freeze(run) for run in requested)}
            )
        )
    except Exception as error:
        message = explain(error)
        try:
            client._request(FailLaunchRequest(launch=launch.id, message=message))
        except Exception:
            message += "; server evidence update unconfirmed"
        raise LaunchError(launch.id, tuple(r.run for r in launch.requests), message) from error


def _with_sources(submission: LaunchInput) -> LaunchInput:
    """Attach transport bytes to already frozen intent, without submitting a new run."""
    sources: dict[tuple[str, str], Source] = {}
    for run in submission.runs:
        job = run.job
        if job.repo is not None:
            repo = job.repo
            assert job.revision is not None
            commit = job.revision
            key = (repo, commit)
            if key not in sources:
                body = source_archive(repo, commit)
                sources[key] = Source(
                    repo=repo,
                    commit=commit,
                    sha256=hashlib.sha256(body).hexdigest(),
                    body=base64.b64encode(body).decode(),
                )
    return submission.model_copy(update={"sources": tuple(sources.values())})


def _retain(sources: Sequence[Source]) -> None:
    for source in sources:
        body = base64.b64decode(source.body, validate=True)
        if hashlib.sha256(body).hexdigest() != source.sha256:
            raise LauncherError("source archive digest mismatch")
        retain(source.repo, source.commit, body)


def _save_output(raw: EncodedOutput, request: FetchRequest) -> Path:
    content = base64.b64decode(raw.body, validate=True)
    if hashlib.sha256(content).hexdigest() != raw.sha256:
        raise LauncherError("fetched output digest mismatch")
    request.destination.mkdir(parents=True, exist_ok=True)
    target = request.destination / Path(request.name).name
    with tempfile.NamedTemporaryFile(dir=request.destination, delete=False) as temporary:
        temporary.write(content)
        partial = Path(temporary.name)
    partial.replace(target)
    return target


class PythonClient:
    """Typed Python calls over the same SSH server protocol as the CLI.

    Each request is sent once. In particular a lost launch reply never resubmits.
    Explicit Scheduler instances continue to own only their local database.
    """

    def __init__(self, address: str) -> None:
        self.address = address

    def _request(self, request: Request) -> bytes:
        done = _ssh(
            self.address,
            ["--server-request"],
            input=_REQUEST.dump_json(request),
            capture_output=True,
            **(
                {"timeout": remaining(request.timeout)}
                if isinstance(request, LogsRequest | AvailabilityRequest)
                else {}
            ),
        )
        if done.returncode:
            raise LauncherError(
                done.stderr.decode().strip() or "server request failed; outcome unknown"
            )
        return done.stdout

    def _submit(
        self,
        runs: Sequence[RunRequest],
        target: Target,
        queued: bool,
    ) -> tuple[str, ...]:
        submission = prepare(
            self.address,
            LaunchInput(
                runs=tuple(runs),
                pool=target.pool,
                rent=target.rent,
                queued=queued,
            ),
        )
        try:
            return TypeAdapter(tuple[str, ...]).validate_json(
                self._request(SubmissionRequest(operation="launch", submission=submission))
            )
        except Exception as error:
            assert submission.launch is not None
            raise LaunchError(
                submission.launch, tuple(str(run.id) for run in submission.runs), explain(error)
            ) from error

    def queue(
        self,
        runs: RunRequest | Job | Sequence[RunRequest | Job],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> tuple[str, ...]:
        given = [runs] if isinstance(runs, (Job, RunRequest)) else runs
        return self._submit(expand_runs(given), Target(pool=pool, rent=rent), True)

    def launch(
        self,
        runs: RunRequest | Job | Sequence[RunRequest | Job],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> tuple[str, ...]:
        given = [runs] if isinstance(runs, (Job, RunRequest)) else runs
        return self._submit(expand_runs(given), Target(pool=pool, rent=rent), False)

    def quote(
        self,
        runs: Sequence[RunRequest],
        pool: str | None = None,
        *,
        rent: str | None = None,
    ) -> Quote:
        submission = LaunchInput(runs=tuple(runs), pool=pool, rent=rent)
        return Quote.model_validate_json(
            self._request(SubmissionRequest(operation="quote", submission=submission))
        )

    def availability(self, rent: str, *, timeout: float | None = None) -> Availability:
        return Availability.model_validate_json(
            self._request(AvailabilityRequest(ondemand=rent, timeout=timeout))
        )

    def standing(
        self, experiment: str | None = None, *, job: str | None = None
    ) -> tuple[Standing, ...]:
        return TypeAdapter(tuple[Standing, ...]).validate_json(
            self._request(StandingRequest(experiment=experiment, job=job))
        )

    def record(self, run: str) -> RunRecord:
        return RunRecord.model_validate_json(
            self._request(RunOperation(operation="record", run=run))
        )

    def status(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="status", run=run)))

    def read(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="read", run=run)))

    def start_phases(self, run: str) -> tuple[StartPhase, ...]:
        return TypeAdapter(tuple[StartPhase, ...]).validate_json(
            self._request(RunOperation(operation="start_phases", run=run))
        )

    def runs(
        self,
        *,
        done: bool = False,
        pool: str | None = None,
        rent: str | None = None,
        job: str | None = None,
        contains: str | None = None,
        experiment: str | None = None,
    ) -> tuple[RunRecord, ...]:
        return TypeAdapter(tuple[RunRecord, ...]).validate_json(
            self._request(
                RunsRequest(
                    done=done,
                    pool=pool,
                    rent=rent,
                    job=job,
                    contains=contains,
                    experiment=experiment,
                )
            )
        )

    def machines(self, supply: str) -> tuple[Machine, ...]:
        return TypeAdapter(tuple[Machine, ...]).validate_json(
            self._request(MachinesRequest(supply=supply))
        )

    def safeguards(self, ondemand: str) -> tuple[str, ...]:
        return TypeAdapter(tuple[str, ...]).validate_json(
            self._request(SafeguardsRequest(ondemand=ondemand))
        )

    def held(self, run: str) -> dict[str, bytes] | None:
        files = EncodedFiles.model_validate_json(self._request(RunFilesRequest(run=run))).files
        return (
            None
            if files is None
            else {key: base64.b64decode(body, validate=True) for key, body in files.items()}
        )

    def resolve_image(
        self, reference: str, *, platform: str = "linux/amd64", timeout: float | None = None
    ) -> Image:
        return Image.model_validate_json(
            self._request(ImageRequest(reference=reference, platform=platform, timeout=timeout))
        )

    def run_loop(self) -> None:
        self._request(LoopRequest())

    def freshness(self) -> Freshness:
        return Freshness.model_validate_json(self._request(InventoryRequest(operation="freshness")))

    def pools(self) -> tuple[Pool, ...]:
        return TypeAdapter(tuple[Pool, ...]).validate_json(
            self._request(InventoryRequest(operation="pools"))
        )

    def providers(self) -> tuple[Provider, ...]:
        return TypeAdapter(tuple[Provider, ...]).validate_json(
            self._request(InventoryRequest(operation="providers"))
        )

    def storages(self) -> tuple[Storage, ...]:
        return TypeAdapter(tuple[Storage, ...]).validate_json(
            self._request(InventoryRequest(operation="storages"))
        )

    def ondemands(self) -> tuple[OnDemand, ...]:
        return TypeAdapter(tuple[OnDemand, ...]).validate_json(
            self._request(InventoryRequest(operation="ondemands"))
        )

    def rentals(self) -> tuple[RentalCost, ...]:
        return TypeAdapter(tuple[RentalCost, ...]).validate_json(
            self._request(InventoryRequest(operation="rentals"))
        )

    def default_target(self) -> Target:
        return TypeAdapter(Target).validate_json(
            self._request(InventoryRequest(operation="default_target"))
        )

    def now(self) -> datetime:
        return TypeAdapter(datetime).validate_json(self._request(InventoryRequest(operation="now")))

    def set_provider(self, provider: Provider) -> None:
        self._request(ProviderRequest(provider=provider))

    def add_storage(self, storage: Storage) -> None:
        self._request(StorageRequest(storage=storage))

    def add_pool(self, pool: Pool) -> None:
        self._request(PoolRequest(pool=pool))

    def add_ondemand(self, ondemand: OnDemand) -> None:
        self._request(OnDemandRequest(ondemand=ondemand))

    def set_startup(self, name: str, seconds: float | None) -> None:
        self._request(StartupRequest(name=name, seconds=seconds))

    def set_ondemand_max(self, name: str, maximum: int) -> None:
        self._request(MaximumRequest(name=name, maximum=maximum))

    def set_ondemand_image(self, name: str, image: Image | str) -> None:
        self._request(ImageSelectionRequest(name=name, image=image))

    def set_default(self, pool: str | None = None, *, rent: str | None = None) -> None:
        self._request(TargetRequest(target=Target(pool=pool, rent=rent)))

    def turn_pool(self, name: str, *, off: bool) -> None:
        self._request(PoolStateRequest(name=name, off=off))

    def add_machine(
        self,
        name: str,
        address: str,
        *,
        pool: str | None = None,
        storage: str | None = None,
        cost_usd: Decimal = Decimal(0),
        expires_at: datetime | None = None,
    ) -> None:
        self._request(
            MachineRequest(
                name=name,
                address=address,
                pool=pool,
                storage=storage,
                cost_usd=cost_usd,
                expires_at=expires_at,
            )
        )

    def wait(self, *runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
        return TypeAdapter(list[Status]).validate_json(
            self._request(WaitRequest(runs=runs, every=every, timeout=timeout))
        )

    def cancel(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="cancel", run=run)))

    def diagnostics(
        self, *, run: str | None = None, machine: str | None = None
    ) -> tuple[Diagnostic, ...]:
        return TypeAdapter(tuple[Diagnostic, ...]).validate_json(
            self._request(EvidenceRequest(operation="diagnostics", run=run, machine=machine))
        )

    def logs(
        self,
        *,
        run: str | None = None,
        machine: str | None = None,
        refresh: bool = False,
        timeout: float | None = None,
    ) -> LogSnapshot:
        if timeout is not None and not refresh:
            raise ValueError("a log timeout bounds a refresh")
        if timeout is not None and timeout <= 0:
            raise ValueError("log timeout must be positive")
        request = LogsRequest(run=run, machine=machine, refresh=refresh, timeout=timeout)
        try:
            with limit_time(timeout):
                return LogSnapshot.model_validate_json(self._request(request))
        except (subprocess.TimeoutExpired, TimeoutError):
            raise LauncherError(
                "server log retrieval time limit reached; evidence remains on server"
            ) from None

    def usage(self, *, run: str | None = None, machine: str | None = None) -> Usage:
        return Usage.model_validate_json(
            self._request(EvidenceRequest(operation="usage", run=run, machine=machine))
        )

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        return _save_output(
            EncodedOutput.model_validate_json(self._request(OutputRequest(run=run, output=name))),
            FetchRequest(run=run, name=name, destination=destination),
        )

    def rent(self, ondemand: str, hours: float, max_usd: Decimal | None = None) -> str:
        return TypeAdapter(str).validate_json(
            self._request(RentRequest(ondemand=ondemand, hours=hours, max_usd=max_usd))
        )

    def terminate(self, machine: str) -> Machine:
        return Machine.model_validate_json(self._request(TerminateRequest(machine=machine)))


def receive(scheduler: Scheduler) -> int:
    request = _REQUEST.validate_json(sys.stdin.buffer.read())
    response: Response
    match request:
        case SubmissionRequest(operation="prepare_launch", submission=submission):
            response = scheduler.prepare_launch(
                submission.runs, submission.pool, rent=submission.rent
            )
        case SubmissionRequest(operation="launch", submission=submission):
            try:
                _retain(submission.sources)
            except Exception as error:
                if submission.launch is not None:
                    scheduler.fail_launch(submission.launch, explain(error))
                raise
            if submission.launch is not None:
                ids = scheduler.submit_launch(
                    submission.launch,
                    submission.runs,
                    Target(pool=submission.pool, rent=submission.rent),
                    queued=submission.queued,
                )
            else:
                send = scheduler.queue if submission.queued else scheduler.launch
                ids = send(submission.runs, submission.pool, rent=submission.rent)
            response = tuple(ids)
        case SubmissionRequest(operation="quote", submission=submission):
            response = scheduler.quote(submission.runs, submission.pool, rent=submission.rent)
        case FailLaunchRequest():
            scheduler.fail_launch(request.launch, request.message)
            response = "failed; no run admitted"
        case OutputRequest():
            with tempfile.TemporaryDirectory() as temporary:
                path = scheduler.fetch(request.run, request.output, Path(temporary))
                body = path.read_bytes()
                response = EncodedOutput(
                    body=base64.b64encode(body).decode(), sha256=hashlib.sha256(body).hexdigest()
                )
        case RunOperation(operation="record"):
            response = scheduler.record(request.run)
        case RunOperation(operation="status"):
            response = scheduler.status(request.run)
        case RunOperation(operation="cancel"):
            response = scheduler.cancel(request.run)
        case StandingRequest():
            response = scheduler.standing(request.experiment, job=request.job)
        case WaitRequest():
            response = scheduler.wait(*request.runs, every=request.every, timeout=request.timeout)
        case EvidenceRequest(operation="usage"):
            response = scheduler.usage(run=request.run, machine=request.machine)
        case EvidenceRequest(operation="diagnostics"):
            response = scheduler.diagnostics(run=request.run, machine=request.machine)
        case LogsRequest():
            response = scheduler.logs(
                run=request.run,
                machine=request.machine,
                refresh=request.refresh,
                timeout=request.timeout,
            )
        case RentRequest():
            response = scheduler.rent(request.ondemand, request.hours, request.max_usd)
        case AvailabilityRequest():
            response = scheduler.availability(request.ondemand, timeout=request.timeout)
        case TerminateRequest():
            response = scheduler.terminate(request.machine)
        case RunOperation(operation="read"):
            response = scheduler.read(request.run)
        case RunOperation(operation="start_phases"):
            response = scheduler.start_phases(request.run)
        case RunsRequest():
            response = scheduler.runs(
                done=request.done,
                pool=request.pool,
                rent=request.rent,
                job=request.job,
                contains=request.contains,
                experiment=request.experiment,
            )
        case MachinesRequest():
            response = scheduler.machines(request.supply)
        case SafeguardsRequest():
            response = scheduler.safeguards(request.ondemand)
        case ProviderRequest():
            scheduler.set_provider(request.provider)
            response = "configured"
        case StorageRequest():
            scheduler.add_storage(request.storage)
            response = "configured"
        case PoolRequest():
            scheduler.add_pool(request.pool)
            response = "configured"
        case OnDemandRequest():
            scheduler.add_ondemand(request.ondemand)
            response = "configured"
        case StartupRequest():
            scheduler.set_startup(request.name, request.seconds)
            response = "configured"
        case MaximumRequest():
            scheduler.set_ondemand_max(request.name, request.maximum)
            response = "configured"
        case ImageSelectionRequest():
            scheduler.set_ondemand_image(request.name, request.image)
            response = "configured"
        case TargetRequest():
            scheduler.set_default(request.target.pool, rent=request.target.rent)
            response = "configured"
        case PoolStateRequest():
            scheduler.turn_pool(request.name, off=request.off)
            response = "configured"
        case MachineRequest():
            scheduler.add_machine(
                request.name,
                request.address,
                pool=request.pool,
                storage=request.storage,
                cost_usd=request.cost_usd,
                expires_at=request.expires_at,
            )
            response = "configured"
        case RunFilesRequest():
            files = scheduler.held(request.run)
            response = EncodedFiles(
                files=None
                if files is None
                else {key: base64.b64encode(body).decode() for key, body in files.items()}
            )
        case ImageRequest():
            # Server command entry point: registry code belongs only to this request.
            from etude.launch.provisioner.images import resolve_image  # noqa: PLC0415

            response = resolve_image(
                request.reference, platform=request.platform, timeout=request.timeout
            )
        case LoopRequest():
            loop(scheduler)
            return 0
        case InventoryRequest(operation="pools"):
            response = scheduler.pools()
        case InventoryRequest(operation="providers"):
            response = scheduler.providers()
        case InventoryRequest(operation="storages"):
            response = scheduler.storages()
        case InventoryRequest(operation="ondemands"):
            response = scheduler.ondemands()
        case InventoryRequest(operation="rentals"):
            response = scheduler.rentals()
        case InventoryRequest(operation="default_target"):
            response = scheduler.default_target()
        case InventoryRequest(operation="now"):
            response = scheduler.now()
        case InventoryRequest(operation="freshness"):
            response = _freshness(scheduler)
        case _:
            raise LauncherError(f"unknown server operation {request.operation!r}")
    print(ServerReply(response).model_dump_json())
    return 0


def loop(scheduler: Scheduler | PythonClient, every: float = 1.0) -> None:
    """Keep matching even when the queue empties; heartbeat means a completed tick.

    When a newer etude migrates the database, this loop replaces itself with that
    program's loop, so running rentals keep a server through an upgrade.
    """
    if isinstance(scheduler, PythonClient):
        scheduler.run_loop()
        return
    while True:
        try:
            scheduler.tick()
        except SchemaAdvanced as advanced:
            _hand_over(advanced.program)
        write(home() / "server-heartbeat", datetime.now(UTC).isoformat())
        time.sleep(every)


def _hand_over(program: str | None) -> NoReturn:
    if program is None:
        raise LauncherError("database upgraded; restart etude server run with the upgraded client")
    python = Path(program) / ".venv" / "bin" / "python"
    if not python.is_file():
        raise LauncherError(
            "upgrade program unavailable; restart etude server run with the upgraded client"
        )
    checked = subprocess.run(
        [
            str(python),
            "-c",
            "from pathlib import Path; from etude.launch.database import open_home; import sys; open_home(Path(sys.argv[1])).close()",
            str(home()),
        ],
        capture_output=True,
    )
    if checked.returncode:
        raise LauncherError(
            "installed program cannot open the upgraded database; restart etude server run with a compatible client"
        )
    print(f"etude was upgraded; the loop continues in {python}", file=sys.stderr, flush=True)
    os.execv(str(python), [str(python), "-m", "etude", "server", "run"])


def _freshness(scheduler: Scheduler) -> Freshness:
    try:
        at = datetime.fromisoformat((home() / "server-heartbeat").read_text().strip())
        age = (datetime.now(UTC) - at).total_seconds()
        recent = 0 <= age < 30
    except (OSError, ValueError, TypeError):
        at, recent = None, False
    waiting = not recent and any(
        scheduler.read(run.id).phase == "queued" for run in scheduler.runs()
    )
    return Freshness(
        last_loop=at,
        warning="work is waiting; no server loop has run recently (etude server run)"
        if waiting
        else None,
    )


def show_freshness(scheduler: Scheduler | PythonClient) -> None:
    """Print the selected server's retained loop evidence without polling workers or providers."""
    state = scheduler.freshness() if isinstance(scheduler, PythonClient) else _freshness(scheduler)
    at = state.last_loop
    if at is None:
        said = "never"
    else:
        age = int((datetime.now(UTC) - at).total_seconds())
        said = f"{age} seconds ago" if 0 <= age < 120 else f"{at:%Y-%m-%d %H:%M} UTC"
    print("last loop: " + said)
    if state.warning:
        print("warning: " + state.warning, file=sys.stderr)


def show(scheduler: Scheduler | PythonClient) -> None:
    if isinstance(scheduler, PythonClient):
        print("server: " + scheduler.address)
    elif not os.environ.get("ETUDE_SERVER_LOCAL"):
        print("server: this machine")  # a client has already named the server
    show_freshness(scheduler)
