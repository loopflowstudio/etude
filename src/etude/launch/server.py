"""Optional SSH server: command transport, frozen inputs and a standing loop.

SSH streams ordinary CLI commands. Python submissions and file transfers use
a small wire envelope. Provider/storage keys stay in the
server's environment. The local setting never opens the launcher database.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import cache, partial
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field, RootModel, TypeAdapter, field_validator

from etude.deadline import limit_time, remaining
from etude.errors import LauncherError, explain, sanitize
from etude.home import home
from etude.launch.database import launcher_file, open_home
from etude.launch.errors import LaunchError, SchemaAdvanced
from etude.launch.logs import ConsoleTail
from etude.launch.openssh import ssh_command
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import (
    ExperimentLimit,
    Launch,
    LogSnapshot,
    MachineStatus,
    Quote,
    Standing,
    Status,
    Target,
)
from etude.launch.scheduler.loop import Pacer, Scheduler
from etude.launch.source import freeze, retain, source_archive, stored
from etude.launch.ssh import (
    _MISSING,
    EncodedOutput,
    boot_script,
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

    @field_validator("commit")
    @classmethod
    def _valid_commit(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value in (".", ".."):
            raise ValueError(f"invalid source commit {value!r}")
        return value


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
    operation: Literal["launch", "prepare_launch", "quote", "missing_sources"]
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
    operation: Literal["record", "status", "read", "cancel"]
    run: str


class StandingRequest(Record):
    operation: Literal["standing"] = "standing"
    experiment: str | None = None
    job: str | None = None


class LimitsRequest(Record):
    operation: Literal["limits"] = "limits"
    experiment: str | None = None
    include_default: bool = False


class LimitsReply(Record):
    limits: tuple[ExperimentLimit, ...] = ()
    default_target: Target | None = None

    @classmethod
    def read(cls, experiment: str | None, *, include_default: bool) -> LimitsReply:
        with retained_scheduler() as scheduler:
            if scheduler is None:
                return cls()
            target = scheduler.default_target() if include_default else None
            if target is not None and target.rent is None:
                return cls(default_target=target)
            if experiment is None:
                limits = scheduler.limits()
            else:
                limit = scheduler.limit(experiment)
                limits = (limit,) if limit is not None else ()
            return cls(limits=limits, default_target=target)


class ObservationRequest(Record):
    operation: Literal["observation"] = "observation"
    consoles: tuple[str, ...]


class LauncherObservation(Record):
    runs: tuple[Standing, ...] = ()
    machines: tuple[MachineStatus, ...] | None = None
    last_loop: datetime | None = None
    consoles: dict[str, ConsoleTail] = Field(default_factory=dict)

    @classmethod
    def read(cls, consoles: tuple[str, ...], *, machines: bool = False) -> LauncherObservation:
        with retained_scheduler() as scheduler:
            if scheduler is None:
                return cls()
            runs = scheduler.standing()
            requested = set(consoles)
            return cls(
                runs=runs,
                machines=scheduler.machine_statuses() if machines else None,
                last_loop=last_loop(),
                consoles={
                    item.record.id: ConsoleTail.from_snapshot(
                        scheduler.logs(run=item.record.id), item.status
                    )
                    for item in runs
                    if item.record.id in requested
                },
            )


@contextmanager
def retained_scheduler() -> Iterator[Scheduler | None]:
    """Open this machine's existing records without configuring providers or pools."""
    if not launcher_file(home()).exists():
        yield None
        return
    store = open_home(home())
    try:
        with store.reading():
            yield Scheduler(store, {})
    finally:
        store.close()


class WaitRequest(Record):
    operation: Literal["wait"] = "wait"
    runs: tuple[str, ...]
    every: float | None = None
    timeout: float | None = None


class EvidenceRequest(Record):
    operation: Literal["usage"] = "usage"
    run: str | None = None
    machine: str | None = None


class LogsRequest(Record):
    operation: Literal["logs"] = "logs"
    run: str | None = None
    machine: str | None = None
    refresh: bool = False
    timeout: float | None = None


type Request = Annotated[
    SubmissionRequest
    | FailLaunchRequest
    | OutputRequest
    | RunOperation
    | StandingRequest
    | LimitsRequest
    | ObservationRequest
    | WaitRequest
    | EvidenceRequest
    | LogsRequest,
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
    | Usage
    | LogSnapshot
    | LauncherObservation
    | LimitsReply
    | tuple[RunRecord, ...]
    | tuple[Standing, ...]
    | Launch
    | Quote
    | str
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


@cache
def _program() -> tuple[str, bytes]:
    code = program_archive()
    return hashlib.sha256(code).hexdigest(), code


def _command(address: str, argv: Sequence[str]) -> list[str]:
    # A remote setting cannot forward again. No local environment/keys are serialized.
    root = '"${ETUDE_HOME:-$HOME/.etude}/programs/' + _program()[0] + '"'
    run = root + "/.venv/bin/python -m etude"
    return ssh_command(
        address,
        login_shell(
            f"test -f {root}/ready || exit {_MISSING}; "
            f"ETUDE_SERVER_LOCAL=1 exec {run} {shlex.join(argv)}"
        ),
    )


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
    try:
        done = subprocess.run(
            ssh_command(
                address,
                login_shell(
                    shlex.join([remote_python(), "-c", boot_script(), "--install", deadline])
                ),
            ),
            input=_program()[1],
            capture_output=True,
            timeout=allowance,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise LauncherError(f"server {address!r} did not answer over SSH") from None
    if done.returncode:
        said = done.stderr.decode(errors="replace").strip().splitlines()
        detail = sanitize(said[-1]) if said else ""
        raise LauncherError(
            f"server {address!r} could not take the program"
            + (f": {detail}" if detail else "")
            + f". It needs {remote_python()} and uv on its login shell's PATH and working SSH keys"
        )


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
    try:
        return _with_sources(
            submission.model_copy(
                update={
                    "launch": launch.id,
                    "runs": tuple(freeze(run) for run in launch.addressed(submission.runs)),
                }
            ),
            client,
        )
    except BaseException as error:
        message = explain(error)
        try:
            client._request(FailLaunchRequest(launch=launch.id, message=message))
        except Exception:
            message += "; server evidence update unconfirmed"
        if not isinstance(error, Exception):
            raise
        raise LaunchError(launch.id, tuple(r.run for r in launch.requests), message) from error


def _with_sources(submission: LaunchInput, client: PythonClient) -> LaunchInput:
    """Attach transport bytes to already frozen intent, without submitting a new run."""
    missing = set(
        TypeAdapter(tuple[str, ...]).validate_json(
            client._request(SubmissionRequest(operation="missing_sources", submission=submission))
        )
    )
    sources: dict[tuple[str, str], Source] = {}
    for run in submission.runs:
        job = run.job
        if run.id in missing and job.repo is not None:
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


def forward(address: str, argv: Sequence[str]) -> int:
    """Run the same CLI on the selected host, preserving its output and exit status."""
    return _ssh(address, argv).returncode


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
    """Python submissions and retained evidence over SSH.

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
            **({"timeout": remaining(request.timeout)} if isinstance(request, LogsRequest) else {}),
        )
        if done.returncode:
            detail = sanitize(done.stderr.decode(errors="replace").strip())
            raise LauncherError(detail or "server request failed; outcome unknown")
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

    def standing(
        self, experiment: str | None = None, *, job: str | None = None
    ) -> tuple[Standing, ...]:
        return TypeAdapter(tuple[Standing, ...]).validate_json(
            self._request(StandingRequest(experiment=experiment, job=job))
        )

    def read_limits(self, experiment: str | None, *, include_default: bool = False) -> LimitsReply:
        return LimitsReply.model_validate_json(
            self._request(LimitsRequest(experiment=experiment, include_default=include_default))
        )

    def observation(self, consoles: tuple[str, ...]) -> LauncherObservation:
        return LauncherObservation.model_validate_json(
            self._request(ObservationRequest(consoles=consoles))
        )

    def record(self, run: str) -> RunRecord:
        return RunRecord.model_validate_json(
            self._request(RunOperation(operation="record", run=run))
        )

    def status(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="status", run=run)))

    def read(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="read", run=run)))

    def wait(
        self, *runs: str, every: float | None = None, timeout: float | None = None
    ) -> list[Status]:
        return TypeAdapter(list[Status]).validate_json(
            self._request(WaitRequest(runs=runs, every=every, timeout=timeout))
        )

    def cancel(self, run: str) -> Status:
        return Status.model_validate_json(self._request(RunOperation(operation="cancel", run=run)))

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
        return Usage.model_validate_json(self._request(EvidenceRequest(run=run, machine=machine)))

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        return _save_output(
            EncodedOutput.model_validate_json(self._request(OutputRequest(run=run, output=name))),
            FetchRequest(run=run, name=name, destination=destination),
        )


def receive(open_scheduler: Callable[[], Scheduler]) -> int:
    request = _REQUEST.validate_json(sys.stdin.buffer.read())
    response: Response
    if isinstance(request, ObservationRequest):
        response = LauncherObservation.read(request.consoles)
    elif isinstance(request, LimitsRequest):
        response = LimitsReply.read(request.experiment, include_default=request.include_default)
    else:
        response = _answer(open_scheduler(), request)
    print(ServerReply(response).model_dump_json())
    return 0


def _answer(scheduler: Scheduler, request: Request) -> Response:
    response: Response
    match request:
        case SubmissionRequest(operation="prepare_launch", submission=submission):
            response = scheduler.prepare_launch(
                submission.runs, submission.pool, rent=submission.rent
            )
        case SubmissionRequest(operation="missing_sources", submission=submission):
            if submission.launch is not None:
                scheduler.renew_preparation(submission.launch)
            response = tuple(
                run.id
                for run in submission.runs
                if run.id is not None
                and run.job.repo is not None
                and run.job.revision is not None
                and not stored(run.job.repo, run.job.revision).is_file()
            )
        case SubmissionRequest(operation="launch", submission=submission):
            identity, runs = submission.launch, submission.runs
            if identity is None:
                launch = scheduler.prepare_launch(runs, submission.pool, rent=submission.rent)
                identity, runs = launch.id, launch.addressed(runs)
            response = scheduler.submit_launch(
                identity,
                runs,
                queued=submission.queued,
                retain_source=partial(_retain, submission.sources),
            )
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
        case EvidenceRequest():
            response = scheduler.usage(run=request.run, machine=request.machine)
        case LogsRequest():
            response = scheduler.logs(
                run=request.run,
                machine=request.machine,
                refresh=request.refresh,
                timeout=request.timeout,
            )
        case RunOperation(operation="read"):
            response = scheduler.read(request.run)
        case _:
            raise LauncherError(f"unknown server operation {request.operation!r}")
    return response


_SERVER_IDLE_MAX = 4.0


def loop(scheduler: Scheduler, every: float | None = None) -> None:
    """Keep matching even when the queue empties; heartbeat means a completed tick.

    When a newer etude migrates the database, this loop replaces itself with that
    program's loop, so running rentals keep a server through an upgrade.
    """
    pacer = Pacer(every, idle_max=_SERVER_IDLE_MAX)
    while True:
        try:
            scheduler.tick()
        except SchemaAdvanced as error:
            _hand_over(error.program)
        write(home() / "server-heartbeat", datetime.now(UTC).isoformat())
        time.sleep(pacer.delay(scheduler._pace_snapshot() if every is None else None))


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


def last_loop() -> datetime | None:
    """UTC timestamp written on this server after a completed scheduler tick."""
    try:
        at = datetime.fromisoformat((home() / "server-heartbeat").read_text().strip())
        return at.astimezone(UTC) if at.tzinfo is not None else None
    except (ValueError, OSError):
        return None


def warning(scheduler: Scheduler) -> str | None:
    at = last_loop()
    recent = at is not None and 0 <= (datetime.now(UTC) - at).total_seconds() < 30
    if not recent and any(scheduler.read(run.id).phase == "queued" for run in scheduler.runs()):
        return "work is waiting; no server loop has run recently (etude server run)"
    return None


def show_freshness(scheduler: Scheduler) -> None:
    """Print when the server loop last passed: how fresh the database's answers are."""
    at = last_loop()
    if at is not None:
        age = int((datetime.now(UTC) - at).total_seconds())
        said = f"{age} seconds ago" if 0 <= age < 120 else f"{at:%Y-%m-%d %H:%M} UTC"
    else:
        said = "never"
    print("last loop: " + said)
    if problem := warning(scheduler):
        print("warning: " + problem, file=sys.stderr)


def show(scheduler: Scheduler) -> None:
    if not os.environ.get("ETUDE_SERVER_LOCAL"):
        print("server: this machine")  # a client has already named the server
    show_freshness(scheduler)
