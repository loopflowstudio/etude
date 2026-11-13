"""Standard-library rental bootstrap and independent deadline guardian.

RunPod/Vast supply self-scoped deletion, budget facts and SSH startup; Lium uses
a local watchdog and leaves deletion to provider timers and the controller.
run() dispatches commands; boot() owns installation, console publication and cleanup.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from etude.deadline import call, limit_time
from etude.launch.environment import install, startup_timeout
from etude.launch.errors import ProviderUnavailable
from etude.launch.logging import (
    CLOSED,
    Console,
    explain,
    log_to,
    publish,
    start_publication,
    write_log,
)
from etude.launch.storage.transport import attribute, send


class Guardian(Protocol):
    def budget_deadline(self, machine: str, max_usd: Decimal) -> datetime | None: ...
    def delete_self(self, machine: str) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def guard(
    api: Guardian,
    machine: str,
    deadline: datetime,
    max_usd: Decimal,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for the deadline, then retry the provider's deletion or local stop action."""
    latest = monotonic() + max(0.0, (deadline - now()).total_seconds())
    try:
        left = (deadline - now()).total_seconds()
        if left > 0:
            with limit_time(left):
                paid_until = call(lambda: api.budget_deadline(machine, max_usd))
            if paid_until is not None:
                deadline = min(deadline, paid_until)
    except (ProviderUnavailable, ValueError, TimeoutError):
        pass  # the deadline it was given still stands
    # A clock set backwards must not buy time: stop on whichever measure runs out first.
    latest = min(latest, monotonic() + max(0.0, (deadline - now()).total_seconds()))
    while now() < deadline and monotonic() < latest:
        sleep(min(1.0, max(0.0, latest - monotonic())))
    while True:
        try:
            api.delete_self(machine)
            return
        except (ProviderUnavailable, ValueError, TimeoutError):
            sleep(2.0)


# Same pinned Linux x86_64 uv artifact used by the former private image.
GUARDIAN_MARKER = Path("/run/etude/guardian-started")
UV_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.8.22/uv-x86_64-unknown-linux-gnu.tar.gz"
)
UV_SHA256 = "741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf"


def download(url: str) -> bytes:
    with limit_time(startup_timeout()), attribute(purpose="runtime"):
        status, body = send("GET", url, None, {})
        if status != 200:
            raise OSError(f"runtime download status {status}")
        return body


def ensure_uv() -> None:
    """Supply uv when a stock image has only Python; verify before extraction."""
    if shutil.which("uv") is not None:
        return
    code = download(UV_URL)
    if hashlib.sha256(code).hexdigest() != UV_SHA256:
        raise ValueError("uv download checksum does not match")
    with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as archive:
        member = archive.extractfile("uv-x86_64-unknown-linux-gnu/uv")
        if member is None:
            raise ValueError("uv archive contains no executable")
        path = Path("/usr/local/bin/uv")
        path.write_bytes(member.read())
        path.chmod(0o755)


def run(
    command: str,
    *,
    api: Guardian,
    machine: str,
    module: str,
    start_ssh: Callable[[], None],
) -> int:
    """Run the instance's boot, guard or delete command with self-scoped credentials."""

    def delete_self() -> None:
        while True:
            try:
                api.delete_self(machine)
                return
            except (ProviderUnavailable, TimeoutError):
                time.sleep(2.0)

    if command == "boot":
        return boot(module, delete_self, start_ssh)
    if command == "delete":
        delete_self()
    elif command == "guard":
        deadline = datetime.fromisoformat(os.environ["ETUDE_DEADLINE"])
        cap = Decimal(os.environ["ETUDE_MAX_USD"])
        GUARDIAN_MARKER.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        GUARDIAN_MARKER.write_text(datetime.now(UTC).isoformat())
        guard(api, machine, deadline, cap)
    else:
        raise ValueError("expected guard, boot or delete")
    return 0


def boot(
    module: str,
    delete_self: Callable[[], None] | None,
    start_ssh: Callable[[], None] | None,
) -> int:
    # A readiness handshake makes the guardian precede every download/install.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    marker = GUARDIAN_MARKER
    marker.unlink(missing_ok=True)
    log = (
        GUARDIAN_MARKER.parent
        / "sessions"
        / os.environ.get("ETUDE_SESSION", "bootstrap")
        / "bootstrap.log"
    )
    url = os.environ.get("ETUDE_BOOTSTRAP_LOG_URL", "")
    publishing = start_publication(log, url)
    with log_to(log):
        try:
            timer = subprocess.Popen([sys.executable, "-m", module, "guard"], env=env)
            seconds = startup_timeout()
            latest = None if seconds is None else time.monotonic() + seconds
            while not marker.exists():
                if timer.poll() is not None or (latest is not None and time.monotonic() >= latest):
                    raise RuntimeError("guardian did not start; refusing client installation")
                time.sleep(0.05)
            write_log("guardian", "ready; independent deadline active")
            write_log("image", os.environ.get("ETUDE_IMAGE", "image metadata unavailable"))
            startup_timeout()
            if start_ssh is not None:
                start_ssh()
            write_log("package download", "ensuring uv")
            ensure_uv()
            write_log("package download", "fetching client")
            code = download(os.environ["ETUDE_PACKAGE_URL"])
            if hashlib.sha256(code).hexdigest() != os.environ["ETUDE_PACKAGE_SHA256"]:
                raise ValueError("client package digest does not match")
            root = install(code)
            python = str(root / ".venv/bin/python")
            write_log("worker entry", "starting installed client")
            # Keep this producer open across spawn, imports and courier exit.
            # The child leaves deletion to us so its final stderr is collected first.
            with subprocess.Popen(
                [
                    python,
                    "-m",
                    "etude",
                    "worker",
                    "--bootstrap",
                    os.environ["ETUDE_BOOTSTRAP_URL"],
                    "--cleanup",
                    "[]",
                ],
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "ETUDE_LOG_PATH"}
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            ) as worker:
                assert worker.stdout is not None
                console = Console("worker console")
                while body := worker.stdout.read(65536):
                    console.write(body)
                console.write(b"", final=True)
                status = worker.wait()
            write_log("worker exit", f"exit status {status}")
            return status
        except Exception as error:
            write_log("bootstrap", explain(error))
            write_log("bootstrap exit", "exit status 1")
            return 1
        finally:
            write_log(
                "cleanup",
                "requesting machine deletion"
                if delete_self is not None
                else "provider timer and controller own machine deletion",
            )
            write_log("bootstrap", CLOSED)
            publishing.set()
            # Publication inherits the explicit log/rental allowance.
            try:
                publish(log, url)
            finally:
                if delete_self is not None:
                    delete_self()
