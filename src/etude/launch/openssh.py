"""Shared OpenSSH argv builder with liveness defaults that honor `~/.ssh/config`.

`ssh -G` reads the user's effective OpenSSH configuration locally without
touching the network. Defaults are added only where the configuration leaves
them unset (`connecttimeout none`, `serveraliveinterval 0`). `ServerAliveCountMax`
is left to OpenSSH (default 3) because `ssh -G` does not distinguish unset from 3.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from etude.deadline import remaining

CONNECT_TIMEOUT = 30
SERVER_ALIVE_INTERVAL = 20

_config_cache: dict[tuple[str, str, str], tuple[int | None, tuple[str, ...]]] = {}
_config_lock = threading.Lock()


def _default_options() -> tuple[str, ...]:
    return (
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
    )


def _config_stamp() -> tuple[str, int | None]:
    home = os.environ.get("HOME", "")
    config = Path(home) / ".ssh" / "config" if home else Path.home() / ".ssh" / "config"
    try:
        mtime: int | None = config.stat().st_mtime_ns
    except OSError:
        mtime = None
    return home, mtime


def _liveness_options(address: str) -> tuple[str, ...]:
    """Effective liveness `-o` flags for `address`, cached until `HOME` or `~/.ssh/config` changes."""
    home, mtime = _config_stamp()
    key = (address, os.environ.get("PATH", ""), home)
    with _config_lock:
        cached = _config_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        result = subprocess.run(
            ["ssh", "-G", "--", address],
            capture_output=True,
            timeout=remaining(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _default_options()
    if result.returncode:
        return _default_options()
    raw = result.stdout
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else (raw or "")
    settings: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.strip().partition(" ")
        if sep:
            settings[name.lower()] = value.strip().lower()
    options: list[str] = []
    if settings.get("connecttimeout", "none") == "none":
        options.extend(["-o", f"ConnectTimeout={CONNECT_TIMEOUT}"])
    if settings.get("serveraliveinterval", "0") == "0":
        options.extend(["-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}"])
    resolved = tuple(options)
    with _config_lock:
        _config_cache[key] = (mtime, resolved)
    return resolved


def ssh_command(address: str, remote: str, *, batch: bool = False) -> list[str]:
    """Build an `ssh` argv for `address` running `remote`, preserving user liveness overrides."""
    argv = ["ssh"]
    if batch:
        argv.extend(["-o", "BatchMode=yes"])
    argv.extend(_liveness_options(address))
    argv.extend(["--", address, remote])
    return argv
