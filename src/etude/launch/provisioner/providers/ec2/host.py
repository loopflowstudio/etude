"""EC2 user data for GPU hosts configured to terminate on guest power-off."""

from datetime import datetime
from shlex import quote


def user_data(deadline: datetime, url: str, sha256: str) -> str:
    """Arm the absolute paid deadline before fetching the session's boot script."""
    if deadline.utcoffset() is None:
        raise ValueError("EC2 rental deadline needs a timezone")
    return f"""#!/bin/bash
set -eu
umask 077
end() {{
    until systemctl --no-block poweroff; do sleep 1; done
}}
trap end EXIT
seconds=$(({int(deadline.timestamp())} - $(date +%s)))
(( seconds > 0 )) || exit 0
systemd-run --unit=etude-deadline --on-active="${{seconds}}s" --timer-property=AccuracySec=1s systemctl --no-block poweroff
cd "$(mktemp -d /run/etude-boot.XXXXXXXX)"
curl --fail --silent --show-error --output boot {quote(url)}
printf '%s  boot\\n' {quote(sha256)} | sha256sum -c /dev/stdin
/bin/bash boot
"""
