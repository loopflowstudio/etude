"""Remote half of the SSH transport. No provider credentials cross this interface."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from etude.launch.schemas import Packet, Work
from etude.launch.ssh import EncodedOutput, RemoteReply, RemoteRequest
from etude.launch.startup import ProcessStart
from etude.launch.storage.disk import DirectoryJournal
from etude.launch.storage.files import digest, write
from etude.launch.storage.s3 import read


def invoke(request: RemoteRequest) -> RemoteReply:
    root = Path(request.root).expanduser().resolve()
    session = str(request.session)
    home = root / session
    journal = DirectoryJournal(root)
    action = request.action
    for name, body in request.files.items():
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(body))
    for name, source in request.checkpoints.items():
        target = root / name
        checkpoint_body = read(source.url) if source.url else base64.b64decode(source.content or "")
        if checkpoint_body is None:
            raise ValueError("checkpoint unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(checkpoint_body)
        if digest(target) != source.sha256:
            raise ValueError("checkpoint digest mismatch")
    if action == "start":
        packet = Packet.model_validate_json(request.packet)
        # A directory can contain staged source files, but a start is exclusive.
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (home / "start-claim").exists():
            return RemoteReply(
                text=(home / "receipt").read_text() if (home / "receipt").exists() else "pending"
            )
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
                        "ETUDE_SESSION": session,
                        "ETUDE_WORKER_LOG_URL": request.bootstrap.worker_log_url,
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            return RemoteReply(text=str(process.pid))
        # Sources were staged above; this start uses the same process implementation.
        write(home / "packet.json", request.packet)
        return RemoteReply(text=ProcessStart(root).spawn(packet))
    if action == "add":
        work = Work.model_validate_json(request.work)
        added = home / "work"
        added.mkdir(exist_ok=True)
        if not (added / f"{work.attempt}.json").exists():
            write(added / f"{work.attempt}.json", request.work)
        return RemoteReply()
    if action == "started":
        return RemoteReply(started=journal.started(session))
    if action == "ended":
        at = journal.ended(session)
        return RemoteReply(text=at.isoformat() if at else None)
    if action == "observe":
        return RemoteReply(observation=journal.observe(session))
    if action == "end":
        at = journal.end(session)
        return RemoteReply(text=at.isoformat() if at else None)
    attempt = str(request.attempt)
    if action == "stop":
        journal.stop(session, attempt)
        return RemoteReply()
    if action == "logs":
        return RemoteReply(
            stream=journal.logs(session, request.attempt or None, producer=request.name)
        )
    if action == "fetch":
        with tempfile.TemporaryDirectory() as directory:
            path = journal.fetch(session, attempt, str(request.name), Path(directory))
            return RemoteReply(
                output=EncodedOutput(
                    body=base64.b64encode(path.read_bytes()).decode(), sha256=digest(path)
                )
            )
    raise ValueError("unknown remote operation")


if __name__ == "__main__":
    print(invoke(RemoteRequest.model_validate_json(sys.stdin.buffer.read())).model_dump_json())
