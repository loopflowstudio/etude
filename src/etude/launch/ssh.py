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
from datetime import datetime
from pathlib import Path

from pydantic import Field

from etude.launch.log_read import remaining
from etude.launch.machines import Allocation
from etude.launch.measurement import timed
from etude.launch.providers.package import program_archive
from etude.launch.records import BootstrapLinks, LauncherError, LogStream, Record
from etude.launch.schemas import (
    CheckpointRef,
    Observation,
    Packet,
    Work,
)
from etude.launch.source import source_archive


class CheckpointFile(Record):
    content: str | None
    url: str | None
    sha256: str


class RemoteRequest(Record):
    action: str
    root: str
    session: str
    packet: str = ""
    work: str = ""
    attempt: str = ""
    name: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    checkpoints: dict[str, CheckpointFile] = Field(default_factory=dict)
    bootstrap: BootstrapLinks | None = None
    cleanup: tuple[str, ...] = ()


class EncodedOutput(Record):
    body: str
    sha256: str


class RemoteReply(Record):
    text: str | None = None
    stream: LogStream | None = None
    started: bool = False
    observation: Observation | None = None
    output: EncodedOutput | None = None


class Unreachable(LauncherError):
    """SSH returned no usable observation. The remote process may still be running."""


# Every invocation selects an immutable program version, so an upgrade cannot
# change the modules of an already running session.
# Bootstrap imports only the standard library before the environment is ready.
_INSTALL_CODE = """import base64,io,json,os,pathlib,sys,zipfile
with zipfile.ZipFile(io.BytesIO(code)) as archive:
    import types
    for name in ("etude", "etude.launch"):
        sys.modules.setdefault(name, types.ModuleType(name))
    logging=types.ModuleType("etude.launch.logging")
    exec(compile(archive.read("etude/launch/logging.py"),"logging.py","exec"),logging.__dict__)
    sys.modules[logging.__name__]=logging
    source=archive.read("etude/launch/environment.py")
namespace={"__name__":"etude_install"}
exec(compile(source,"environment.py","exec"),namespace)
import contextlib
with contextlib.redirect_stdout(sys.stderr):
    links=globals().get("request",{}).get("bootstrap")
    publication=logging.start_publication(pathlib.Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"]) if links else None
    try:
        root=namespace["install"](code)
    except Exception as error:
        logging.write_log("client install", logging.explain(error))
        raise
    finally:
        if os.environ.get("ETUDE_LOG_PATH"):
            logging.write_log("bootstrap", logging.CLOSED)
        if links:
            publication.set()
            logging.publish(pathlib.Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"])
"""
_BOOT = (
    """import base64,json,os,sys
request=json.load(sys.stdin)
code=base64.b64decode(request.pop("program"))
import pathlib
if request['action']=='start':
    log=pathlib.Path(request['root']).expanduser()/request['session']/'bootstrap.log'
    os.environ['ETUDE_LOG_PATH']=str(log)
else:
    os.environ.pop('ETUDE_LOG_PATH',None)
"""
    + _INSTALL_CODE
    + """
import tempfile
with tempfile.TemporaryFile() as payload:
    payload.write(json.dumps(request).encode()); payload.seek(0)
    os.dup2(payload.fileno(),0)
    os.execv(str(root/".venv/bin/python"),[str(root/".venv/bin/python"),"-m","etude.launch.worker.remote"])
"""
)


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
        executable: str = "ssh",
        python: str | None = None,
    ) -> None:
        if not address or address.startswith("-") or any(c.isspace() for c in address):
            raise LauncherError("use one SSH address, with options in ~/.ssh/config")
        self.address, self.root, self.executable = address, root, executable
        self.python = python or remote_python()
        self._program = base64.b64encode(program_archive()).decode()

    @timed("ssh.call")
    def call(
        self,
        action: str,
        session_id: str,
        *,
        packet: str = "",
        work: str = "",
        attempt: str = "",
        name: str = "",
        files: dict[str, str] | None = None,
        checkpoints: dict[str, CheckpointFile] | None = None,
        bootstrap: BootstrapLinks | None = None,
        cleanup: tuple[str, ...] = (),
    ) -> RemoteReply:
        request = {
            **RemoteRequest(
                action=action,
                root=self.root,
                session=session_id,
                packet=packet,
                work=work,
                attempt=attempt,
                name=name,
                files=files or {},
                checkpoints=checkpoints or {},
                bootstrap=bootstrap,
                cleanup=cleanup,
            ).model_dump(mode="json"),
            "program": self._program,
        }
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    self.address,
                    login_shell(shlex.join([self.python, "-c", _BOOT])),
                ],
                input=json.dumps(request).encode(),
                capture_output=True,
                timeout=remaining(150),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise Unreachable("SSH did not answer") from None
        if result.returncode:
            raise Unreachable(
                "uv is required on the machine"
                if b"uv is required" in result.stderr
                else "SSH or the remote session command failed"
            )
        try:
            return RemoteReply.model_validate_json(result.stdout)
        except ValueError:
            raise Unreachable("SSH returned an unreadable reply") from None

    def files(self, work: Work) -> dict[str, str]:
        if work.repo is None or work.commit is None:
            return {}
        return {
            f"sources/{work.commit}.tar": base64.b64encode(
                source_archive(work.repo, work.commit)
            ).decode()
        }


def checkpoint_files(works: tuple[Work, ...]) -> dict[str, CheckpointFile]:
    """Files needed by a continuation, shared by initial packets and later inbox work."""
    return {
        work.checkpoint.session
        + "/attempts/"
        + work.checkpoint.attempt
        + "/outputs/"
        + work.checkpoint.name: CheckpointFile(
            content=work.checkpoint.content, url=work.checkpoint.url, sha256=work.checkpoint.sha256
        )
        for work in works
        if work.checkpoint is not None
    }


class SSHStart:
    def __init__(self, ssh: SSH) -> None:
        self.ssh = ssh

    def start(self, machine: Allocation, packet: Packet, bootstrap: BootstrapLinks | None) -> str:
        ssh = self.ssh
        files = {path: body for work in packet.work for path, body in ssh.files(work).items()}
        return str(
            ssh.call(
                "start",
                packet.session,
                packet=packet.model_dump_json(),
                files=files,
                bootstrap=bootstrap,
                cleanup=machine.cleanup,
                checkpoints=checkpoint_files(packet.work),
            ).text
        )


class SSHJournal:
    def __init__(self, ssh: SSH) -> None:
        self.ssh = ssh

    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef:
        raw = self.ssh.call("fetch", ref.session, attempt=ref.attempt, name=ref.name).output
        assert raw is not None
        if raw.sha256 != ref.sha256:
            raise LauncherError("checkpoint digest mismatch")
        return ref.model_copy(update={"content": raw.body})

    def prepare(
        self, packet: Packet, *, expires_at: datetime | None = None
    ) -> BootstrapLinks | None:
        return None

    def started(self, session_id: str) -> bool:
        return self.ssh.call("started", session_id).started

    def ended(self, session_id: str) -> datetime | None:
        raw = self.ssh.call("ended", session_id).text
        return datetime.fromisoformat(raw) if raw else None

    def end(self, session_id: str) -> datetime | None:
        raw = self.ssh.call("end", session_id).text
        return datetime.fromisoformat(raw) if raw else None

    def add(self, session_id: str, work: Work) -> None:
        self.ssh.call(
            "add",
            session_id,
            work=work.model_dump_json(),
            files=self.ssh.files(work),
            checkpoints=checkpoint_files((work,)),
        )

    def observe(self, session_id: str) -> Observation:
        observation = self.ssh.call("observe", session_id).observation
        if observation is None:
            raise Unreachable("SSH returned no observation")
        return observation

    def stop(self, session_id: str, attempt: str) -> None:
        self.ssh.call("stop", session_id, attempt=attempt)

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        stream = self.ssh.call("logs", session_id, attempt=attempt or "", name=producer).stream
        if stream is None:
            raise Unreachable("SSH returned no log snapshot")
        return stream

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        raw = self.ssh.call("fetch", session_id, attempt=attempt, name=name).output
        assert raw is not None
        body = base64.b64decode(raw.body)
        if hashlib.sha256(body).hexdigest() != raw.sha256:
            raise LauncherError("the downloaded output does not match its digest")
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / Path(name).name
        path.write_bytes(body)
        return path
