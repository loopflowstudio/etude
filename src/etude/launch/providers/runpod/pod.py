"""RunPod self-scoped credentials, budget facts and SSH startup. Standard library only."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etude.launch.environment import startup_timeout
from etude.launch.providers.bootstrap import run
from etude.launch.providers.runpod.http import PodHTTP


class GuardianAPI(PodHTTP):
    """Read only the two financial facts needed to shorten this pod's timer."""

    def budget_deadline(self, pod: str, max_usd: Decimal) -> datetime | None:
        raw = self.ask("GET", f"pods/{pod}", absent_ok=True)
        if raw is None:
            return None
        price = Decimal(str(raw["cost"]))
        return (
            (datetime.fromisoformat(raw["createdAt"]) + timedelta(hours=float(max_usd / price)))
            if price > 0
            else None
        )


def pod_key() -> str:
    """The key RunPod gives a pod for itself. It cannot create or touch other pods."""
    value = os.environ.get("RUNPOD_API_KEY")
    if value:
        return value
    config = tomllib.loads((Path.home() / ".runpod" / "config.toml").read_text())
    return str(config["apikey"])


def start_ssh() -> None:
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
            timeout=startup_timeout(),
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
            timeout=startup_timeout(),
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
    return run(
        argv[1],
        api=GuardianAPI(pod_key()),
        machine=os.environ["RUNPOD_POD_ID"],
        module="etude.launch.providers.runpod.pod",
        start_ssh=start_ssh,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
