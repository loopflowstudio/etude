"""Remote half of the SSH transport. No provider credentials cross this interface."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from etude.launch.ssh import (
    AddRequest,
    EncodedOutput,
    HeldRequest,
    HoldRequest,
    LogsRequest,
    OutputRequest,
    RemoteCall,
    RemoteReply,
    SessionRequest,
    StartRequest,
    StopRequest,
)
from etude.launch.startup import ProcessStart
from etude.launch.storage.disk import DirectoryJournal
from etude.launch.storage.files import digest, write


def invoke(call: RemoteCall) -> RemoteReply:
    root = Path(call.root).expanduser().resolve()
    journal = DirectoryJournal(root)
    request = call.request
    match request:
        case StartRequest() | AddRequest():
            for name, body in request.files.items():
                target = root / request.session / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(body))
    match request:
        case StartRequest():
            return _start(root, request)
        case AddRequest():
            added = root / request.session / "work"
            added.mkdir(exist_ok=True)
            if not (added / f"{request.work.attempt}.json").exists():
                write(added / f"{request.work.attempt}.json", request.work.model_dump_json())
            return RemoteReply()
        case SessionRequest(action="started"):
            return RemoteReply(started=journal.started(request.session))
        case SessionRequest(action="ended"):
            return RemoteReply(ended_at=journal.ended(request.session))
        case SessionRequest(action="end"):
            return RemoteReply(ended_at=journal.end(request.session))
        case SessionRequest(action="finish"):
            return RemoteReply(ended_at=journal.finish(request.session))
        case SessionRequest(action="observe"):
            return RemoteReply(observation=journal.observe(request.session))
        case HeldRequest():
            try:
                held = journal.held(request.run, request.key)
            except FileNotFoundError as error:
                return RemoteReply(missing=str(error))
            return RemoteReply(
                folder={key: base64.b64encode(b).decode() for key, b in held.items()}
            )
        case HoldRequest():
            journal.hold(
                request.run,
                {key: base64.b64decode(body) for key, body in request.folder.items()},
            )
            return RemoteReply()
        case StopRequest():
            journal.stop(request.session, request.attempt)
            return RemoteReply()
        case LogsRequest():
            return RemoteReply(
                stream=journal.logs(request.session, request.attempt, producer=request.producer)
            )
        case OutputRequest():
            with tempfile.TemporaryDirectory() as directory:
                path = journal.fetch(
                    request.session, request.attempt, request.output, Path(directory)
                )
                return RemoteReply(
                    output=EncodedOutput(
                        body=base64.b64encode(path.read_bytes()).decode(), sha256=digest(path)
                    )
                )
    raise ValueError(f"unknown remote operation {request.action!r}")


def _start(root: Path, request: StartRequest) -> RemoteReply:
    home = root / request.session
    # A directory can contain staged source files, but a start is exclusive.
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (home / "start-claim").exists():
        pid = home / "pid"  # the worker's process id, once it is running
        return RemoteReply(pid=int(pid.read_text()) if pid.exists() else None)
    with (home / "start-claim").open("x"):
        pass
    if request.bootstrap:
        command = [
            sys.executable,
            "-m",
            "etude",
            "worker",
            "--bootstrap",
            request.bootstrap.url,
            "--root",
            str(root),
            "--cleanup",
            json.dumps(request.cleanup),
        ]
        with (home / "courier.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                    "ETUDE_SESSION": request.session,
                    "ETUDE_WORKER_LOG_URL": request.bootstrap.worker_log_url,
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return RemoteReply(pid=process.pid)
    # Sources were staged above; this start uses the same process implementation.
    write(home / "packet.json", request.packet.model_dump_json())
    return RemoteReply(pid=ProcessStart(root).spawn(request.packet))


if __name__ == "__main__":
    print(invoke(RemoteCall.model_validate_json(sys.stdin.buffer.read())).model_dump_json())
