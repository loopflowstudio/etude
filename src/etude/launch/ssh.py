"""SSH startup and disk journals, using the same remote session program.

SSH configuration and host-key checking belong to OpenSSH. Addresses are passed
as one argument, never interpolated into a shell. A failed connection is unknown,
not proof that a session died. Python 3.12 must already be on the host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Annotated, Literal, cast, get_args

from pydantic import Field

from etude.deadline import remaining
from etude.errors import LauncherError, sanitize
from etude.launch.errors import Refused
from etude.launch.logs import snapshot_stream
from etude.launch.openssh import ssh_command
from etude.launch.provisioner.machines import Allocation
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import BootstrapLinks, LogProducer, LogStream
from etude.launch.schemas import Finished, Observation, Packet, Progress, Work
from etude.launch.source import source_archive
from etude.launch.storage.files import parse_result, parse_transfers
from etude.spec import Record
from etude.storage_timeout import storage_environment

# What the host is asked to do, one record per shape of request. Both ends run
# the same program version, which every invocation selects.


class StartRequest(Record):
    action: Literal["start"] = "start"
    session: str
    packet: Packet
    files: dict[str, str] = Field(default_factory=dict)  # base64 by path in the session's folder
    bootstrap: BootstrapLinks | None = None
    cleanup: tuple[str, ...] = ()


class AddRequest(Record):
    action: Literal["add"] = "add"
    session: str
    work: Work
    files: dict[str, str] = Field(default_factory=dict)  # base64 by path in the session's folder
    finish: bool = False


class SessionRequest(Record):
    action: Literal["started", "claimed", "ended", "end", "finish", "observe"]
    session: str


class StopRequest(Record):
    action: Literal["stop"] = "stop"
    session: str
    attempt: str


class LogsRequest(Record):
    action: Literal["logs"] = "logs"
    session: str
    producer: LogProducer
    attempt: str | None = None


class OutputRequest(Record):
    action: Literal["fetch"] = "fetch"
    session: str
    attempt: str
    output: str  # the output's name below the attempt's output directory


class HeldRequest(Record):
    action: Literal["held"] = "held"
    run: str
    key: str | None = None  # one file of the run's folder; None: what a retry needs


class HoldRequest(Record):
    action: Literal["hold"] = "hold"
    run: str
    # A run's folder relayed from another host, base64 by key (`etude.carriage.restore_set`).
    folder: dict[str, str]


type RemoteRequest = Annotated[
    StartRequest
    | AddRequest
    | SessionRequest
    | StopRequest
    | LogsRequest
    | OutputRequest
    | HeldRequest
    | HoldRequest,
    Field(discriminator="action"),
]


class RemoteCall(Record):
    """One SSH invocation: a request, where the host keeps sessions, and its time limit."""

    root: str
    storage_environment: dict[str, str] = Field(default_factory=dict)
    # A start's startup deadline; otherwise when the caller's allowance runs out.
    startup_deadline: datetime | None = None
    request: RemoteRequest


class EncodedOutput(Record):
    body: str
    sha256: str


class RawAttempt(Record):
    """Unvalidated attempt files read read-only by `ssh_boot.py` and validated on the client."""

    attempt: str
    claim: str | None = None
    finished: str | None = None
    result: str | None = None
    log_bytes: int = 0


class RawObservation(Record):
    """Host-side session state returned by stdlib `ssh_boot.py` before client validation."""

    alive: bool
    attempts: tuple[RawAttempt, ...] = ()
    usage: str | None = None
    start_unknown: bool = False


class RawLog(Record):
    """Bounded log bytes read read-only by `ssh_boot.py`."""

    body: str | None = None  # base64-encoded bytes; None when the log file does not exist
    closed: bool = False


class RemoteReply(Record):
    """What the host answered; each request fills the fields it has an answer for."""

    pid: int | None = None  # start: its worker process; None while another start holds it
    ended_at: datetime | None = None  # ended, end, finish: when the session ended, if it has
    log: RawLog | None = None
    started: bool = False
    claimed: bool = False  # claimed: a start reached the host (`boot-claim`) or is running
    observation: RawObservation | None = None
    output: EncodedOutput | None = None
    folder: dict[str, str] | None = None
    missing: str | None = None  # why "held" found no such record or file on the host
    refused: str | None = None  # start/add/stop: host refused request without mutating state


class Unreachable(LauncherError):
    """SSH returned no usable observation. The remote process may still be running."""


# Every invocation selects an immutable program version, so an upgrade cannot
# change the modules of an already running session.
# Bootstrap imports only the standard library before the environment is ready.
_MISSING = 97  # the remote exit status meaning "that program is not here yet"
# Only these requests run in the remote `.venv` and need the program archive or digest.
_PROGRAM_REQUESTS = (StartRequest, OutputRequest, HeldRequest, HoldRequest)


@cache
def boot_script() -> str:
    """Standard-library SSH bootstrap and session control source (`ssh_boot.py`)."""
    return Path(__file__).with_name("ssh_boot.py").read_text()


def remote_python() -> str:
    """The Python a remote machine runs the program with. `ETUDE_REMOTE_PYTHON` names another."""
    return os.environ.get("ETUDE_REMOTE_PYTHON", "python3.12")


def login_shell(command: str) -> str:
    """Wrap a command so the remote account's login shell runs it, with its usual PATH.

    A bare SSH command gets a minimal PATH. On a Mac that hides Homebrew's Python,
    so the same host works from a terminal and fails from here without this.
    """
    return 'exec "${SHELL:-/bin/sh}" -lc ' + shlex.quote(command)


class SSH:
    def __init__(
        self,
        address: str,
        root: str = "~/.etude/sessions",
    ) -> None:
        if not address or address.startswith("-") or any(c.isspace() for c in address):
            raise LauncherError("use one SSH address, with options in ~/.ssh/config")
        self.address, self.root = address, root
        self._archive: tuple[str, str] | None = None

    def _program(self) -> tuple[str, str]:
        if self._archive is None:
            code = program_archive()
            self._archive = (
                hashlib.sha256(code).hexdigest(),
                base64.b64encode(code).decode(),
            )
        return self._archive

    def call(self, request: RemoteRequest) -> RemoteReply:
        start = isinstance(request, StartRequest)
        startup_deadline = (
            request.packet.startup_deadline
            if isinstance(request, StartRequest)
            else (
                datetime.now(UTC) + timedelta(seconds=seconds)
                if (seconds := remaining()) is not None
                else None
            )
        )
        call = RemoteCall(
            root=self.root,
            storage_environment=storage_environment(),
            startup_deadline=startup_deadline,
            request=request,
        )
        sent: dict[str, object] = call.model_dump(mode="json")
        program: str | None = None
        if isinstance(request, _PROGRAM_REQUESTS):
            digest, program = self._program()
            sent["digest"] = digest
            if start:
                sent["program"] = program
        allowance = remaining()
        if start and startup_deadline is not None:
            startup_left = (startup_deadline - datetime.now(UTC)).total_seconds()
            if startup_left <= 0:
                raise Unreachable("startup deadline reached before SSH request")
            allowance = startup_left if allowance is None else min(startup_left, allowance)
        argv = ssh_command(
            self.address,
            login_shell(shlex.join([remote_python(), "-c", boot_script()])),
            batch=True,
        )
        result = self._run(argv, sent, allowance)
        if result.returncode == _MISSING and not start:
            retry_allowance = remaining()
            if retry_allowance is not None and retry_allowance <= 0:
                raise Unreachable(
                    f"SSH wait exceeded {allowance} seconds; remote outcome unconfirmed"
                )
            if program is None:
                digest, program = self._program()
                sent["digest"] = digest
            sent["program"] = program
            result = self._run(argv, sent, retry_allowance)
        if result.returncode:
            if b"uv is required" in result.stderr:
                raise Unreachable("uv is required on the machine")
            lines = [
                line.strip()
                for line in result.stderr.decode(errors="replace").splitlines()
                if line.strip()
            ]
            detail = sanitize(lines[-1]) if lines else ""
            raise Unreachable(
                f"SSH or the remote session command failed: {detail}"
                if detail
                else "SSH or the remote session command failed"
            )
        try:
            return RemoteReply.model_validate_json(result.stdout)
        except ValueError:
            raise Unreachable("SSH returned an unreadable reply") from None

    @staticmethod
    def _run(
        argv: list[str], sent: dict[str, object], allowance: float | None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                argv,
                input=json.dumps(sent).encode(),
                capture_output=True,
                timeout=allowance,
            )
        except subprocess.TimeoutExpired:
            raise Unreachable(
                f"SSH wait exceeded {allowance} seconds; remote outcome unconfirmed"
            ) from None
        except OSError:
            raise Unreachable("SSH transport failed; remote outcome unconfirmed") from None

    def files(self, work: Work) -> dict[str, str]:
        if work.repo is None or work.commit is None:
            return {}
        return {
            f"sources/{work.commit}.tar": base64.b64encode(
                source_archive(work.repo, work.commit)
            ).decode()
        }


class SSHStart:
    def __init__(self, ssh: SSH) -> None:
        self.ssh = ssh

    def start(self, machine: Allocation, packet: Packet, bootstrap: BootstrapLinks | None) -> None:
        ssh = self.ssh
        files = {path: body for work in packet.work for path, body in ssh.files(work).items()}
        reply = ssh.call(
            StartRequest(
                session=packet.session,
                packet=packet,
                files=files,
                bootstrap=bootstrap,
                cleanup=machine.cleanup,
            )
        )
        if reply.refused is not None:
            raise Refused(sanitize(reply.refused) or "host refused start")


class SSHJournal:
    def __init__(self, ssh: SSH) -> None:
        self.ssh = ssh

    def prepare(
        self, packet: Packet, *, expires_at: datetime | None = None
    ) -> BootstrapLinks | None:
        return None

    def stage(self, repo: str, commit: str) -> None:
        """Nothing to do ahead: source is copied to the host when its session starts."""

    def started(self, session_id: str) -> bool:
        return self.ssh.call(SessionRequest(action="started", session=session_id)).started

    def claimed(self, session_id: str) -> bool:
        return self.ssh.call(SessionRequest(action="claimed", session=session_id)).claimed

    def ended(self, session_id: str) -> datetime | None:
        return self.ssh.call(SessionRequest(action="ended", session=session_id)).ended_at

    def end(self, session_id: str) -> datetime | None:
        return self.ssh.call(SessionRequest(action="end", session=session_id)).ended_at

    def finish(self, session_id: str) -> datetime | None:
        return self.ssh.call(SessionRequest(action="finish", session=session_id)).ended_at

    def add(self, session_id: str, work: Work, *, finish: bool = False) -> None:
        reply = self.ssh.call(
            AddRequest(session=session_id, work=work, files=self.ssh.files(work), finish=finish)
        )
        if reply.refused is not None:
            raise Refused(sanitize(reply.refused) or "host refused work delivery")

    def held(self, run: str, key: str | None = None) -> dict[str, bytes]:
        reply = self.ssh.call(HeldRequest(run=run, key=key))
        if reply.missing is not None:
            raise FileNotFoundError(reply.missing)
        folder = reply.folder
        if folder is None:
            raise Unreachable("SSH returned no run folder")
        return {key: base64.b64decode(body) for key, body in folder.items()}

    def hold(self, run: str, files: dict[str, bytes]) -> None:
        encoded = {key: base64.b64encode(body).decode() for key, body in files.items()}
        self.ssh.call(HoldRequest(run=run, folder=encoded))

    def observe(self, session_id: str) -> Observation:
        raw = self.ssh.call(SessionRequest(action="observe", session=session_id)).observation
        if raw is None:
            raise Unreachable("SSH returned no observation")
        return _decode_observation(raw)

    def stop(self, session_id: str, attempt: str) -> None:
        reply = self.ssh.call(StopRequest(session=session_id, attempt=attempt))
        if reply.refused is not None:
            raise Refused(sanitize(reply.refused) or "host refused stop")

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        named = _producer(producer)
        reply = self.ssh.call(LogsRequest(session=session_id, producer=named, attempt=attempt))
        if reply.log is None:
            raise Unreachable("SSH returned no log snapshot")
        raw = base64.b64decode(reply.log.body) if reply.log.body is not None else None
        return snapshot_stream(session_id, attempt, named, raw, reply.log.closed)

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        raw = self.ssh.call(OutputRequest(session=session_id, attempt=attempt, output=name)).output
        assert raw is not None
        body = base64.b64decode(raw.body)
        if hashlib.sha256(body).hexdigest() != raw.sha256:
            raise LauncherError("the downloaded output does not match its digest")
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / Path(name).name
        path.write_bytes(body)
        return path


def _decode_observation(raw: RawObservation) -> Observation:
    progress: list[Progress] = []
    for entry in raw.attempts:
        started = (
            datetime.fromisoformat(entry.claim.strip())
            if entry.claim and entry.claim.strip()
            else None
        )
        if entry.finished is None:
            progress.append(
                Progress.running(
                    entry.attempt, started, parse_result(entry.result), log_bytes=entry.log_bytes
                )
            )
        else:
            progress.append(
                Finished.model_validate_json(entry.finished).progress(entry.attempt, started)
            )
    return Observation(
        alive=raw.alive,
        progress=tuple(progress),
        transfers=parse_transfers(raw.usage),
        start_unknown=raw.start_unknown,
    )


def _producer(name: str) -> LogProducer:
    # The journal protocol names producers as text; the wire names one of them.
    if name not in get_args(LogProducer):
        raise LauncherError(f"no log producer {name!r}")
    return cast(LogProducer, name)
