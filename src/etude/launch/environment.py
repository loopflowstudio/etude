"""Install the transferred private package in a digest-specific environment.

Only the public dependencies are downloaded. This module and the guardian use
only the standard library, so neither depends on a successful installation.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from etude.launch.logging import Console, explain, write_log


def startup_timeout() -> float | None:
    """Remaining startup/rental allowance from the launch, never a renewed duration."""
    limits = [
        datetime.fromisoformat(value)
        for name in ("ETUDE_STARTUP_DEADLINE", "ETUDE_DEADLINE")
        if (value := os.environ.get(name))
    ]
    if not limits:
        return None
    left = (min(limits) - datetime.now(UTC)).total_seconds()
    if left <= 0:
        raise TimeoutError("startup or rental deadline reached")
    return left


def _remaining(deadline: float | None) -> float | None:
    if (cancel := os.environ.get("ETUDE_STARTUP_CANCEL")) and Path(cancel).exists():
        raise InterruptedError("startup cancelled")
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("client installation startup deadline reached")
    return left


def _sync(command: list[str], root: Path, deadline: float | None) -> None:
    """Collect complete sanitized lines while uv runs; reap its group on failure."""
    console = Console("client install")
    with subprocess.Popen(
        command,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        },
    ) as child:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            try:
                while selector.get_map():
                    for key, _ in selector.select(min(0.1, _remaining(deadline) or 0.1)):
                        body = os.read(key.fd, 65536)
                        console.write(body, final=not body)
                        if not body:
                            selector.unregister(key.fileobj)
                status = child.wait(timeout=_remaining(deadline))
                if status:
                    raise subprocess.CalledProcessError(status, command)
            except BaseException:
                # uv can own download/build children; none may keep the lock or pipe alive.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                console.write(child.stdout.read(), final=True)
                raise


def install(code: bytes) -> Path:
    seconds = startup_timeout()
    deadline = None if seconds is None else time.monotonic() + seconds
    write_log(
        "client install",
        f"startup allowance remaining: {seconds if seconds is not None else 'unlimited'} seconds",
    )
    base = Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude") / "programs"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = base / hashlib.sha256(code).hexdigest()
    root.mkdir(exist_ok=True, mode=0o700)
    with (root / "install.lock").open("w") as lock:
        while True:
            _remaining(deadline)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = _remaining(deadline)
                time.sleep(min(0.05, left) if left is not None else 0.05)
        if not (root / "ready").exists():
            uv = shutil.which("uv")
            if uv is None:
                raise RuntimeError("uv is required on the machine; put uv on its login shell PATH")
            with zipfile.ZipFile(io.BytesIO(code)) as archive:
                archive.extractall(root)
            write_log("client install", "starting uv sync")
            try:
                _sync(
                    [
                        uv,
                        "sync",
                        "--frozen",
                        "--no-dev",
                        "--no-editable",
                        "--python",
                        sys.executable,
                    ],
                    root,
                    deadline,
                )
            except (subprocess.SubprocessError, TimeoutError) as error:
                write_log("client install", explain(error))
                raise
            write_log("client install", "completed")
            (root / "ready").touch()
    return root
