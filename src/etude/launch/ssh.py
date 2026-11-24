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
from pathlib import Path
from typing import Annotated, Literal, cast, get_args

from pydantic import Field

from etude.deadline import remaining
from etude.errors import LauncherError
from etude.launch.logs import snapshot_stream
from etude.launch.provisioner.machines import Allocation
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import BootstrapLinks, LogProducer, LogStream
from etude.launch.schemas import Observation, Packet, Work
from etude.launch.source import source_archive
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


class RemoteReply(Record):
    """What the host answered; each request fills the fields it has an answer for."""

    pid: int | None = None  # start: its worker process; None while another start holds it
    ended_at: datetime | None = None  # ended, end, finish: when the session ended, if it has
    log: str | None = None  # logs of the bootstrap producer: its text, None while it has none
    stream: LogStream | None = None
    started: bool = False
    claimed: bool = False  # claimed: a start reached the host (`boot-claim`) or is running
    observation: Observation | None = None
    output: EncodedOutput | None = None
    folder: dict[str, str] | None = None
    missing: str | None = None  # why "held" found no such record or file on the host


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
    for name in ("etude.errors", "etude.deadline", "etude.storage_timeout", "etude.launch.logging"):
        module=types.ModuleType(name)
        exec(compile(archive.read(name.replace('.', '/')+'.py'),name,'exec'),module.__dict__)
        sys.modules[name]=module
    logging=sys.modules["etude.launch.logging"]
    source=archive.read("etude/launch/environment.py")
namespace={"__name__":"etude_install"}
exec(compile(source,"environment.py","exec"),namespace)
import contextlib
with contextlib.redirect_stdout(sys.stderr):
    links=globals().get("request",{}).get("bootstrap")
    publication=logging.start_publication(pathlib.Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"]) if links else None
    failed=False
    try:
        root=namespace["install"](code)
    except Exception as error:
        failed=True
        logging.write_log("client install", sys.modules["etude.errors"].explain(error))
        if globals().get('request',{}).get('action')=='start':
            from datetime import datetime,UTC
            (home/'ended').write_text(datetime.now(UTC).isoformat())
        raise
    finally:
        if os.environ.get("ETUDE_LOG_PATH") and (failed or globals().get("request",{}).get("action") != "start"):
            logging.write_log("bootstrap", logging.CLOSED)
        if links:
            publication.set()
            logging.publish(pathlib.Path(os.environ["ETUDE_LOG_PATH"]), links["log_url"])
"""
# It reads the RemoteCall as JSON: `request` is its request, and replies are RemoteReply.
_BOOT = (
    """import base64,json,os,pathlib,signal,sys
call=json.load(sys.stdin)
os.environ.update(call.get('storage_environment', {}))
code=base64.b64decode(call.pop("program"))
request=call['request']
home=pathlib.Path(call['root']).expanduser()/request.get('session','')
def _recorded(name: str) -> int | None:
    path=home/name
    try: text=path.read_text().strip()
    except FileNotFoundError: return None
    return int(text) if text.isdigit() else 0
def _pid_alive(pid: int | None) -> bool:
    if pid is None: return False
    if pid==0: return True  # a fresh or older boot-claim can be empty
    try: os.kill(pid,0)
    except ProcessLookupError: return False
    except PermissionError: pass
    return True
def _host_alive() -> bool | None:
    # Check boot before reading the PID it hands off to, so its exit cannot hide the handoff.
    booting=not (home/'ended').exists() and _pid_alive(_recorded('boot-claim'))
    worker=(home/'pid').exists()
    if (courier:=_recorded('courier-pid')) is not None: return _pid_alive(courier)  # owns final delivery
    if not worker: return booting
    if (home/'courier-claim').exists(): return True  # v0.4.6 recorded no courier PID
    return None  # the disk journal owns a pure SSH worker's progress and liveness
if request['action']=='logs' and request['producer']=='bootstrap':
    path=home/'bootstrap.log'
    print(json.dumps({'log':path.read_text() if path.exists() else None})); sys.exit(0)
if request['action']=='started' and not (home/'pid').exists():
    print(json.dumps({'started':False})); sys.exit(0)
if request['action']=='claimed':
    print(json.dumps({'claimed':(home/'boot-claim').exists() or (home/'pid').exists()})); sys.exit(0)
if request['action']=='observe':
    if (alive:=_host_alive()) is not None:
        print(json.dumps({'observation':{'alive':alive,'progress':[],'start_unknown':alive}})); sys.exit(0)
if request['action']=='ended':
    path=home/'ended'
    print(json.dumps({'ended_at':path.read_text() if path.exists() else None})); sys.exit(0)
if request['action']=='end':
    home.mkdir(parents=True,exist_ok=True)
    (home/'cancel-start').touch()
    path=home/'ended'
    if not (home/'pid').exists() and not path.exists():
        # The cancel stops an install; a courier still fetching stops at once.
        if courier:=_recorded('courier-pid'):
            try: os.kill(courier,signal.SIGTERM)
            except (ProcessLookupError,PermissionError): pass
        if _host_alive() is False and not (home/'pid').exists():
            # Nothing runs here, and the cancel fences any start still in flight.
            from datetime import datetime,UTC
            path.write_text(datetime.now(UTC).isoformat())
    if path.exists() or not (home/'pid').exists():
        print(json.dumps({'ended_at':path.read_text() if path.exists() else None})); sys.exit(0)
if request['action']=='start':
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (home/'cancel-start').exists():
        print(json.dumps({})); sys.exit(0)
    try:
        (home/'boot-claim').open('x').close()
    except FileExistsError:
        print(json.dumps({})); sys.exit(0)
    pid=os.fork()
    if pid:
        (home/'boot-claim').write_text(str(pid))
        print(json.dumps({}),flush=True); os._exit(0)
    (home/'boot-claim').write_text(str(os.getpid()))  # either write names this process
    os.setsid()
    with open(os.devnull,'r+b') as sink:
        for fd in (0,1,2): os.dup2(sink.fileno(),fd)
    os.environ['ETUDE_STARTUP_DEADLINE']=call.get('startup_deadline') or ''
    os.environ['ETUDE_STARTUP_CANCEL']=str(home/'cancel-start')
    os.environ['ETUDE_LOG_PATH']=str(home/'bootstrap.log')
else:
    os.environ.pop('ETUDE_LOG_PATH',None)
    os.environ['ETUDE_STARTUP_DEADLINE']=call.get('startup_deadline') or ''
"""
    + _INSTALL_CODE
    + """
if request['action']=='start':
    import subprocess
    from datetime import datetime,UTC
    invoked=False
    try:
        if (home/'cancel-start').exists():
            raise RuntimeError('startup cancelled before worker entry')
        invoked=True
        done=subprocess.run([str(root/'.venv/bin/python'),'-m','etude.launch.worker.remote'],
            input=json.dumps(call).encode(),capture_output=True,timeout=namespace['startup_timeout']())
        console=logging.Console('worker entry')
        console.write(done.stderr,final=True)
        if done.returncode:
            raise RuntimeError(f'worker entry exited with status {done.returncode}')
    except Exception as error:
        logging.write_log('worker entry',sys.modules["etude.errors"].explain(error))
        if not invoked:
            (home/'ended').write_text(datetime.now(UTC).isoformat())
        else:
            logging.write_log('worker entry','reply uncertain; retaining start claim')
    finally:
        logging.write_log('bootstrap',logging.CLOSED)
        if links: logging.publish(home/'bootstrap.log',links['log_url'])
    sys.exit(0)
import tempfile
with tempfile.TemporaryFile() as payload:
    payload.write(json.dumps(call).encode()); payload.seek(0)
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
    ) -> None:
        if not address or address.startswith("-") or any(c.isspace() for c in address):
            raise LauncherError("use one SSH address, with options in ~/.ssh/config")
        self.address, self.root = address, root
        self._program = base64.b64encode(program_archive()).decode()

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
        sent = {**call.model_dump(mode="json"), "program": self._program}
        allowance = remaining()
        if start and startup_deadline is not None:
            startup_left = (startup_deadline - datetime.now(UTC)).total_seconds()
            if startup_left <= 0:
                raise Unreachable("startup deadline reached before SSH request")
            allowance = startup_left if allowance is None else min(startup_left, allowance)
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.address,
                    login_shell(shlex.join([remote_python(), "-c", _BOOT])),
                ],
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


class SSHStart:
    def __init__(self, ssh: SSH) -> None:
        self.ssh = ssh

    def start(self, machine: Allocation, packet: Packet, bootstrap: BootstrapLinks | None) -> None:
        ssh = self.ssh
        files = {path: body for work in packet.work for path, body in ssh.files(work).items()}
        ssh.call(
            StartRequest(
                session=packet.session,
                packet=packet,
                files=files,
                bootstrap=bootstrap,
                cleanup=machine.cleanup,
            )
        )


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

    def add(self, session_id: str, work: Work) -> None:
        self.ssh.call(AddRequest(session=session_id, work=work, files=self.ssh.files(work)))

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
        observation = self.ssh.call(
            SessionRequest(action="observe", session=session_id)
        ).observation
        if observation is None:
            raise Unreachable("SSH returned no observation")
        return observation

    def stop(self, session_id: str, attempt: str) -> None:
        self.ssh.call(StopRequest(session=session_id, attempt=attempt))

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        named = _producer(producer)
        reply = self.ssh.call(LogsRequest(session=session_id, producer=named, attempt=attempt))
        if named == "bootstrap":
            return snapshot_stream(
                session_id,
                None,
                named,
                reply.log.encode() if reply.log is not None else None,
                False,
            )
        stream = reply.stream
        if stream is None:
            raise Unreachable("SSH returned no log snapshot")
        return stream

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


def _producer(name: str) -> LogProducer:
    # The journal protocol names producers as text; the wire names one of them.
    if name not in get_args(LogProducer):
        raise LauncherError(f"no log producer {name!r}")
    return cast(LogProducer, name)
