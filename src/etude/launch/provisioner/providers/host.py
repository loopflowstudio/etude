"""GPU host boot sequence. The host owns deletion independently of the container."""

import shlex
from datetime import datetime

from etude.launch.provisioner.providers.program import boot_source


def container_boot(image: str, env: dict[str, str]) -> str:
    """Pass capabilities through the environment. Docker argv contains only their names."""
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
    command = ["docker", "run", "--rm", "--init", "--gpus", "all"]
    for key in env:
        command.extend(["--env", key])
    command.extend(
        [
            "--entrypoint",
            "python3.12",
            image,
            "-c",
            boot_source("etude.launch.provisioner.providers.watchdog"),
        ]
    )
    return f"set -eu\n{exports}\nexec {shlex.join(command)}\n"


def user_data(deadline: datetime, end: str, boot: str, setup: str = "") -> str:
    """Arm deletion before boot. The parent shell retains its exit trap across a container exec."""
    if deadline.utcoffset() is None:
        raise ValueError("GPU rental deadline needs a timezone")
    return f"""#!/bin/bash
set -eu
umask 077
{setup}
end() {{
    until {end}; do sleep 1; done
}}
trap end EXIT
seconds=$(({int(deadline.timestamp())} - $(date +%s)))
(( seconds > 0 )) || exit 0
systemd-run --unit=etude-deadline --on-active="${{seconds}}s" --timer-property=AccuracySec=1s /bin/bash -c 'until {end}; do sleep 1; done'
(
{boot}
)
"""


def fetch_boot(url: str, sha256: str) -> str:
    return f"""cd "$(mktemp -d /run/etude-boot.XXXXXXXX)"
curl --fail --silent --show-error --output boot {shlex.quote(url)}
printf '%s  boot\\n' {shlex.quote(sha256)} | sha256sum -c /dev/stdin
/bin/bash boot
"""
