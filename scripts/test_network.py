"""Run a command with loopback available and every outside network address refused.

The client's tests mock buckets, providers and SSH; this proves none of them
reaches a real one. Usage: `uv run python scripts/test_network.py uv run pytest`.
On Linux it needs passwordless sudo, `unshare` and `ip`, as GitHub's runners have;
on macOS it uses `sandbox-exec`.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

_MACOS_RULES = """(version 1)(allow default)
(deny network*)
(allow network-outbound (remote ip "localhost:*") (remote unix-socket))
(allow network-inbound (local ip "localhost:*") (local unix-socket))
(allow network-bind)
"""


def _probe() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            connection, _ = server.accept()
            connection.close()
    with socket.socket() as client:
        client.settimeout(1)
        try:
            client.connect(("1.1.1.1", 443))
        except OSError:
            return
    raise RuntimeError("the outside network is reachable inside test isolation")


def _isolated(arguments: list[str]) -> None:
    """Inside the isolation: drop back to the calling user, prove it holds, then run."""
    if platform.system() == "Linux":
        subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
        if user := os.environ.get("SUDO_USER"):
            gid = int(os.environ["SUDO_GID"])
            os.setgroups(os.getgrouplist(user, gid))
            os.setgid(gid)
            os.setuid(int(os.environ["SUDO_UID"]))
    _probe()
    # Environments a test builds (a stand-in controller's) come from uv's cache,
    # which `uv sync` filled before isolation.
    os.environ["UV_OFFLINE"] = "1"
    os.environ["GIT_ALLOW_PROTOCOL"] = "file"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.execvp(arguments[0], arguments)


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["--isolated"]:
        _isolated(arguments[1:])
    if not arguments:
        raise SystemExit("usage: test_network.py COMMAND [ARGS...]")
    child = [sys.executable, str(Path(__file__).resolve()), "--isolated", *arguments]
    if platform.system() == "Darwin":
        command = ["sandbox-exec", "-p", _MACOS_RULES, *child]
    elif platform.system() == "Linux" and shutil.which("unshare") and shutil.which("ip"):
        # Root makes the network namespace; the child returns to this user at once.
        environment = [f"PATH={os.environ['PATH']}", f"HOME={os.environ['HOME']}"]
        command = ["sudo", "-n", "-E", "env", *environment, "unshare", "--net", *child]
    else:
        raise SystemExit("needs macOS sandbox-exec, or Linux sudo, unshare and ip")
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
