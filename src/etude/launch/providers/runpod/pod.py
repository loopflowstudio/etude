"""What runs inside a RunPod pod. Standard library only.

The stock-image bootstrap starts two children:

    python -m etude.launch.providers.runpod.pod guard   # first, and independent of everything else
    etude worker --bootstrap URL --cleanup '[]'

`guard` deletes the pod at its deadline, or sooner if the dollars run out at the
price actually charged. It needs nothing from the launcher or from storage.
The worker reads its bootstrap link, handles its inbox and carries evidence to
storage. Bootstrap owns its console until exit, publishes final evidence, then
deletes the pod. The guardian remains independent of both processes.

The pod holds no account key and no storage keys. It can read the links it was
given and write under its own folder, until they expire.
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
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.request import urlopen

from etude.launch.environment import install, startup_timeout
from etude.launch.logging import (
    CLOSED,
    Console,
    explain,
    log_to,
    publish,
    start_publication,
    write_log,
)
from etude.launch.providers.runpod.http import PodHTTP, ProviderUnavailable


class GuardianAPI(PodHTTP):
    """Read only the two financial facts needed to shorten this pod's timer."""

    def budget_deadline(self, pod: str, max_usd: Decimal) -> datetime | None:
        raw = self._ask("GET", f"pods/{pod}", absent_ok=True)
        if raw is None:
            return None
        price = Decimal(str(raw["cost"]))
        return (
            (datetime.fromisoformat(raw["createdAt"]) + timedelta(hours=float(max_usd / price)))
            if price > 0
            else None
        )


def _now() -> datetime:
    return datetime.now(UTC)


def pod_key() -> str:
    """The key RunPod gives a pod for itself. It cannot create or touch other pods."""
    value = os.environ.get("RUNPOD_API_KEY")
    if value:
        return value
    config = tomllib.loads((Path.home() / ".runpod" / "config.toml").read_text())
    return str(config["apikey"])


def guard(
    api: GuardianAPI,
    pod: str,
    deadline: datetime,
    max_usd: Decimal,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = _now,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for the deadline, then delete this pod, for as long as that takes."""
    try:
        paid_until = api.budget_deadline(pod, max_usd)
        if paid_until is not None:
            deadline = min(deadline, paid_until)
    except (ProviderUnavailable, ValueError):
        pass  # the deadline it was given still stands
    # A clock set backwards must not buy time: stop on whichever measure runs out first.
    latest = monotonic() + max(0.0, (deadline - now()).total_seconds())
    while now() < deadline and monotonic() < latest:
        sleep(min(1.0, max(0.0, latest - monotonic())))
    while True:
        try:
            api.delete_self(pod)
            return
        except (ProviderUnavailable, ValueError):
            sleep(2.0)


# Same pinned Linux x86_64 uv artifact used by the former private image.
GUARDIAN_MARKER = Path("/run/etude/guardian-started")
UV_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.8.22/uv-x86_64-unknown-linux-gnu.tar.gz"
)
UV_SHA256 = "741ff1f5742c5a4a25d2f829e8395355e43f7a5ae2ebc6368e9ae2df0efb69cf"


def ensure_uv() -> None:
    """Supply uv when a stock image has only Python; verify before extraction."""
    if shutil.which("uv") is not None:
        return
    with urlopen(UV_URL, timeout=min(60, startup_timeout() or 60)) as response:
        code = response.read()
    if hashlib.sha256(code).hexdigest() != UV_SHA256:
        raise ValueError("uv download checksum does not match")
    with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as archive:
        member = archive.extractfile("uv-x86_64-unknown-linux-gnu/uv")
        if member is None:
            raise ValueError("uv archive contains no executable")
        path = Path("/usr/local/bin/uv")
        path.write_bytes(member.read())
        path.chmod(0o755)


def _start_ssh() -> None:
    """Enable key-only inspection without making SSH a requirement for training."""
    keys = os.environ.get("PUBLIC_KEY", "").strip()
    if not keys:
        print(
            "SSH unavailable: add a public key to your RunPod account before renting.", flush=True
        )
        return
    try:
        # Our entrypoint replaces the image's start.sh, including its SSH setup.
        directory = Path.home() / ".ssh"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        authorized = directory / "authorized_keys"
        existing = authorized.read_text() if authorized.exists() else ""
        lines = list(dict.fromkeys([*existing.splitlines(), *keys.splitlines()]))
        authorized.write_text("\n".join(lines) + "\n")
        authorized.chmod(0o600)
        Path("/run/sshd").mkdir(mode=0o755, parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-A"],
            check=True,
            capture_output=True,
            timeout=min(30, startup_timeout() or 30),
        )
        subprocess.run(
            [
                "/usr/sbin/sshd",
                "-o",
                "PasswordAuthentication=no",
                "-o",
                "KbdInteractiveAuthentication=no",
                "-o",
                "PermitRootLogin=prohibit-password",
            ],
            check=True,
            capture_output=True,
            timeout=min(30, startup_timeout() or 30),
        )
    except (OSError, subprocess.SubprocessError) as error:
        # Custom training images may omit OpenSSH. Never dump command output or keys.
        print(
            f"SSH unavailable ({type(error).__name__}): check OpenSSH in the image; "
            "continuing worker startup.",
            flush=True,
        )
        return
    print("SSH ready: use the pod's Connect tab in RunPod.", flush=True)


def main(argv: list[str]) -> int:
    pod = os.environ["RUNPOD_POD_ID"]
    api = GuardianAPI(pod_key())

    def delete_self() -> None:
        while True:
            try:
                api.delete_self(pod)
                return
            except ProviderUnavailable:
                time.sleep(2.0)

    if argv[1] == "delete":
        delete_self()
    elif argv[1] == "boot":
        # A readiness handshake makes the guardian precede every download/install.
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[4])}
        marker = GUARDIAN_MARKER
        marker.unlink(missing_ok=True)
        timer = subprocess.Popen(
            [sys.executable, "-m", "etude.launch.providers.runpod.pod", "guard"], env=env
        )
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
                latest = time.monotonic() + min(30, startup_timeout() or 30)
                while not marker.exists():
                    if timer.poll() is not None or time.monotonic() >= latest:
                        raise RuntimeError("guardian did not start; refusing client installation")
                    time.sleep(0.05)
                write_log("guardian", "ready; independent deadline active")
                startup_timeout()
                _start_ssh()
                write_log("package download", "ensuring uv")
                ensure_uv()
                write_log("package download", "fetching client")
                with urlopen(
                    os.environ["ETUDE_PACKAGE_URL"], timeout=min(60, startup_timeout() or 60)
                ) as response:
                    code = response.read()
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
                write_log("cleanup", "requesting machine deletion")
                write_log("bootstrap", CLOSED)
                publishing.set()
                # At most two seconds here; the guardian still deletes independently.
                publish(log, url)
                delete_self()
    elif argv[1] == "guard":
        marker = GUARDIAN_MARKER
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(_now().isoformat())
        deadline = datetime.fromisoformat(os.environ["ETUDE_DEADLINE"])
        guard(api, pod, deadline, Decimal(os.environ["ETUDE_MAX_USD"]))
    else:
        raise ValueError("expected guard, boot or delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
