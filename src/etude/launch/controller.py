"""Optional SSH controller: command transport, frozen inputs and a standing loop.

SSH supplies authentication and streams ordinary command output. Only launch
inputs and fetched files need a wire envelope. Provider/storage keys stay in the
controller's environment. The local setting never opens the launcher database.
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
from typing import Any, Literal

from pydantic import RootModel, TypeAdapter

from etude.launch.deadline import limit_time, remaining
from etude.launch.errors import LaunchError
from etude.launch.listing import show_runs
from etude.launch.logging import explain
from etude.launch.measurement import timed
from etude.launch.providers.package import program_archive
from etude.launch.records import (
    Cohort,
    CohortSpec,
    Diagnostic,
    Job,
    Launch,
    LauncherError,
    LogSnapshot,
    Machine,
    Record,
    Run,
    RunRecord,
    Status,
)
from etude.launch.scheduler import Scheduler
from etude.launch.source import freeze, home, retain, source_archive
from etude.launch.ssh import (
    _INSTALL_CODE,
    EncodedOutput,
    login_shell,
    remote_python,
)
from etude.launch.storage.files import write


class Source(Record):
    repo: str
    commit: str
    sha256: str
    body: str


class LaunchInput(Record):
    runs: tuple[Run, ...]
    cohort: CohortSpec | None
    pool: str | None
    sources: tuple[Source, ...] = ()
    queued: bool = False
    launch: str | None = None


class FetchRequest(Record):
    run: str
    name: str
    destination: Path


class Request(Record):
    """Controller operations; no credentials or provider authority cross this wire."""

    operation: Literal[
        "launch",
        "prepare_launch",
        "fail_launch",
        "fetch",
        "record",
        "status",
        "cancel",
        "wait",
        "logs",
        "diagnostics",
        "cohort_runs",
        "continue_from",
        "rent",
        "terminate",
    ]
    api: bool = False
    launch: str = ""
    message: str = ""
    submission: LaunchInput | None = None
    run: str = ""
    name: str = ""
    runs: tuple[str, ...] = ()
    every: float = 1.0
    timeout: float | None = None
    until: str = ""
    pool: str | None = None
    machine: str = ""
    hours: float = 0
    max_usd: Decimal | None = None


type Response = (
    tuple[str, ...]
    | list[Status]
    | RunRecord
    | Status
    | LaunchInput
    | EncodedOutput
    | Machine
    | LogSnapshot
    | tuple[Diagnostic, ...]
    | tuple[RunRecord, ...]
    | Launch
    | str
)


class Reply(RootModel[Response]):
    model_config = {"frozen": True}


def selected() -> str | None:
    if os.environ.get("ETUDE_CONTROLLER_LOCAL"):
        return None
    path = home() / "controller"
    return path.read_text().strip() if path.exists() else None


def set_address(address: str, *, startup_seconds: float | None = None) -> None:
    if not address or address.startswith("-") or any(c.isspace() for c in address):
        raise LauncherError("controller needs an SSH address, such as user@host or an SSH alias")
    install(
        address, startup_seconds=startup_seconds
    )  # fails here, with a reason, if the machine cannot be a controller
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    write(home() / "controller", address)


def unset() -> None:
    (home() / "controller").unlink(missing_ok=True)


# The controller runs the client's own code, sent over SSH and kept by digest
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
            f"ETUDE_CONTROLLER_LOCAL=1 exec {run} {shlex.join(argv)}"
        ),
    ]


@timed("ssh.controller.install")
def install(address: str, *, startup_seconds: float | None = None) -> None:
    """Send this client's code to the controller. Safe to repeat."""
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
        raise LauncherError(f"controller {address!r} did not answer over SSH") from None
    if done.returncode:
        said = done.stderr.decode(errors="replace").strip().splitlines()
        raise LauncherError(
            f"controller {address!r} could not take the program"
            + (f": {said[-1]}" if said else "")
            + f". It needs {remote_python()} and uv on its login shell's PATH and working SSH keys"
        )


@timed("ssh.controller.command")
def _ssh(address: str, argv: Sequence[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
    """Run one command on the controller, sending the program first if it is not there.

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
        client._request(Request(operation="prepare_launch", submission=submission))
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
            client._request(Request(operation="fail_launch", launch=launch.id, message=message))
        except Exception:
            message += "; controller evidence update unconfirmed"
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


def submit(scheduler: Scheduler, submission: LaunchInput) -> Sequence[str]:
    cohort = submission.cohort
    if submission.launch is not None:
        limits = (
            None
            if cohort is None
            else Cohort(
                name=cohort.name,
                parallel=cohort.parallel,
                max_usd=cohort.max_usd,
                created_at=datetime.now(UTC),
            )
        )
        return scheduler.submit_launch(
            submission.launch, submission.runs, submission.pool, limits, queued=submission.queued
        )
    if cohort is None:
        if len(submission.runs) != 1:
            raise LauncherError("several runs need a cohort")
        send_run = scheduler.queue if submission.queued else scheduler.launch
        return [send_run(submission.runs[0], submission.pool)]
    send_cohort = scheduler.queue_cohort if submission.queued else scheduler.launch_cohort
    return send_cohort(
        cohort.name,
        submission.runs,
        submission.pool,
        parallel=cohort.parallel,
        max_usd=cohort.max_usd,
    )


def forward(
    address: str,
    argv: Sequence[str],
    submission: LaunchInput | None = None,
    fetch: FetchRequest | None = None,
) -> int:
    if submission is None and fetch is None:
        return _ssh(address, argv).returncode
    request: Request
    if submission is not None:
        request = Request(operation="launch", submission=prepare(address, submission))
    else:
        assert fetch is not None
        request = Request(operation="fetch", run=fetch.run, name=fetch.name)
    done = _ssh(
        address,
        ["--controller-request"],
        input=request.model_dump_json().encode(),
        stdout=subprocess.PIPE if fetch is not None else None,
    )
    if done.returncode:
        return done.returncode  # a lost reply never retries a launch
    if fetch is not None:
        print(_save_output(EncodedOutput.model_validate_json(done.stdout), fetch))
    return 0


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
    """Typed Python calls over the same SSH controller protocol as the CLI.

    Each request is sent once. In particular a lost launch reply never resubmits.
    Explicit Launcher instances continue to own only their local database.
    """

    def __init__(self, address: str) -> None:
        self.address = address

    def _request(self, request: Request) -> bytes:
        done = _ssh(
            self.address,
            ["--controller-request"],
            input=request.model_copy(update={"api": True}).model_dump_json().encode(),
            capture_output=True,
            **({"timeout": remaining(request.timeout)} if request.operation == "logs" else {}),
        )
        if done.returncode:
            raise LauncherError(
                done.stderr.decode().strip() or "controller request failed; outcome unknown"
            )
        return done.stdout

    def _submit(
        self,
        runs: Sequence[Run],
        cohort: CohortSpec | None,
        pool: str | None,
        queued: bool,
    ) -> tuple[str, ...]:
        submission = prepare(
            self.address, LaunchInput(runs=tuple(runs), cohort=cohort, pool=pool, queued=queued)
        )
        try:
            return TypeAdapter(tuple[str, ...]).validate_json(
                self._request(Request(operation="launch", submission=submission))
            )
        except Exception as error:
            assert submission.launch is not None
            raise LaunchError(
                submission.launch, tuple(str(run.id) for run in submission.runs), explain(error)
            ) from error

    def queue(self, run: Run | Job, pool: str | None = None) -> str:
        return self._submit([Run(job=run) if isinstance(run, Job) else run], None, pool, True)[0]

    def launch(self, run: Run | Job, pool: str | None = None) -> str:
        return self._submit([Run(job=run) if isinstance(run, Job) else run], None, pool, False)[0]

    def queue_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        return self._submit(
            runs, CohortSpec(name=name, parallel=parallel, max_usd=max_usd), pool, True
        )

    def launch_cohort(
        self,
        name: str,
        runs: Sequence[Run],
        pool: str | None = None,
        *,
        parallel: int = 1,
        max_usd: Decimal | None = None,
    ) -> tuple[str, ...]:
        return self._submit(
            runs, CohortSpec(name=name, parallel=parallel, max_usd=max_usd), pool, False
        )

    def record(self, run: str) -> RunRecord:
        return RunRecord.model_validate_json(self._request(Request(operation="record", run=run)))

    def status(self, run: str) -> Status:
        return Status.model_validate_json(self._request(Request(operation="status", run=run)))

    def wait(self, *runs: str, every: float = 1.0, timeout: float | None = None) -> list[Status]:
        return TypeAdapter(list[Status]).validate_json(
            self._request(Request(operation="wait", runs=runs, every=every, timeout=timeout))
        )

    def cancel(self, run: str) -> Status:
        return Status.model_validate_json(self._request(Request(operation="cancel", run=run)))

    def cohort_runs(self, name: str) -> tuple[RunRecord, ...]:
        return TypeAdapter(tuple[RunRecord, ...]).validate_json(
            self._request(Request(operation="cohort_runs", name=name))
        )

    def diagnostics(
        self, *, run: str | None = None, machine: str | None = None
    ) -> tuple[Diagnostic, ...]:
        return TypeAdapter(tuple[Diagnostic, ...]).validate_json(
            self._request(Request(operation="diagnostics", run=run or "", machine=machine or ""))
        )

    def logs(
        self, *, run: str | None = None, machine: str | None = None, timeout: float | None = None
    ) -> LogSnapshot:
        if timeout is not None and timeout <= 0:
            raise ValueError("log timeout must be positive")
        try:
            with limit_time(timeout):
                return LogSnapshot.model_validate_json(
                    self._request(
                        Request(
                            operation="logs", run=run or "", machine=machine or "", timeout=timeout
                        )
                    )
                )
        except (subprocess.TimeoutExpired, TimeoutError):
            raise LauncherError(
                "controller log retrieval time limit reached; evidence remains on controller"
            ) from None

    def fetch(self, run: str, name: str, destination: Path) -> Path:
        return _save_output(
            EncodedOutput.model_validate_json(
                self._request(Request(operation="fetch", run=run, name=name))
            ),
            FetchRequest(run=run, name=name, destination=destination),
        )

    def rent(self, on: str, hours: float, max_usd: Decimal | None = None) -> str:
        return TypeAdapter(str).validate_json(
            self._request(Request(operation="rent", pool=on, hours=hours, max_usd=max_usd))
        )

    def terminate(self, machine: str) -> Machine:
        return Machine.model_validate_json(
            self._request(Request(operation="terminate", machine=machine))
        )

    def continue_from(self, run: str, until: str) -> Run:
        submission = LaunchInput.model_validate_json(
            self._request(Request(operation="continue_from", run=run, until=until))
        )
        _retain(submission.sources)
        return submission.runs[0]


def receive(scheduler: Scheduler) -> int:
    raw = Request.model_validate_json(sys.stdin.buffer.read())
    operation = raw.operation
    response: Response
    if operation == "prepare_launch":
        if raw.submission is None:
            raise LauncherError("launch needs a submission")
        response = scheduler.prepare_launch(raw.submission.runs, raw.submission.pool)
    elif operation == "fail_launch":
        scheduler.fail_launch(raw.launch, raw.message)
        response = "failed; no run admitted"
    elif operation == "launch":
        submission = raw.submission
        if submission is None:
            raise LauncherError("launch needs a submission")
        try:
            _retain(submission.sources)
        except Exception as error:
            if submission.launch is not None:
                scheduler.fail_launch(submission.launch, explain(error))
            raise
        ids = submit(scheduler, submission)
        if not raw.api:
            show_runs(scheduler, ids)
            return 0
        response = tuple(ids)
    elif operation == "fetch":
        with tempfile.TemporaryDirectory() as temporary:
            path = scheduler.fetch(raw.run, raw.name, Path(temporary))
            body = path.read_bytes()
            response = EncodedOutput(
                body=base64.b64encode(body).decode(), sha256=hashlib.sha256(body).hexdigest()
            )
    elif operation == "record":
        response = scheduler.record(raw.run)
    elif operation == "status":
        response = scheduler.status(raw.run)
    elif operation == "cancel":
        response = scheduler.cancel(raw.run)
    elif operation == "wait":
        response = [
            status for status in scheduler.wait(*raw.runs, every=raw.every, timeout=raw.timeout)
        ]
    elif operation == "diagnostics":
        response = scheduler.diagnostics(run=raw.run or None, machine=raw.machine or None)
    elif operation == "cohort_runs":
        response = scheduler.cohort_runs(raw.name)
    elif operation == "logs":
        response = scheduler.logs(
            run=raw.run or None, machine=raw.machine or None, timeout=raw.timeout
        )
    elif operation == "rent":
        if raw.pool is None:
            raise LauncherError("rent needs a pool")
        response = scheduler.rent(raw.pool, raw.hours, raw.max_usd)
    elif operation == "terminate":
        response = scheduler.terminate(raw.machine)
    elif operation == "continue_from":
        run = scheduler.continue_from(raw.run, raw.until)
        response = _with_sources(LaunchInput(runs=(run,), cohort=None, pool=None))
    else:
        raise LauncherError("unknown controller operation")
    print(Reply(response).model_dump_json())
    return 0


def loop(scheduler: Scheduler, every: float = 1.0) -> None:
    """Keep matching even when the queue empties; heartbeat means a completed tick."""
    while True:
        scheduler.tick()
        write(home() / "controller-heartbeat", datetime.now(UTC).isoformat())
        time.sleep(every)


def warning(scheduler: Scheduler) -> str | None:
    path = home() / "controller-heartbeat"
    try:
        at = datetime.fromisoformat(path.read_text())
        age = (datetime.now(UTC) - at).total_seconds()
        recent = 0 <= age < 30
    except (OSError, ValueError, TypeError):
        recent = False
    if not recent and any(scheduler.read(run.id).phase == "queued" for run in scheduler.runs()):
        return "work is waiting; no controller loop has run recently (etude controller run)"
    return None


def show(scheduler: Scheduler) -> None:
    if not os.environ.get("ETUDE_CONTROLLER_LOCAL"):
        print("controller: this machine")  # a client has already named the controller
    try:
        at = datetime.fromisoformat((home() / "controller-heartbeat").read_text().strip())
        age = int((datetime.now(UTC) - at).total_seconds())
        said = f"{age} seconds ago" if 0 <= age < 120 else f"{at:%Y-%m-%d %H:%M} UTC"
    except (OSError, ValueError, TypeError):
        said = "never"
    print("last loop: " + said)
    if problem := warning(scheduler):
        print("warning: " + problem, file=sys.stderr)
