"""Merged SQL fingerprints carried by wheels and immutable server archives.

Source checkouts derive the frontier from origin/main. Installed programs use
only their embedded evidence; unavailable Git cannot authorize new SQL.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path


def frontier(root: Path, *, timeout: float | None = None) -> bytes:
    """Serialize merged names/checksums without importing client dependencies."""
    embedded = root / "launch" / "migrations" / "merged.json"
    if embedded.exists():
        return embedded.read_bytes()
    folder = root / "launch" / "migrations"
    prefix = "src/etude/launch/migrations/"
    found: dict[str, str] = {}
    deadline = None if timeout is None else time.monotonic() + timeout

    def allowance() -> float | None:
        if deadline is None:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("migration provenance allowance expired")
        return left

    try:
        listed = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-tree",
                    "-r",
                    "--name-only",
                    "origin/main",
                    "--",
                    prefix,
                ],
                check=True,
                capture_output=True,
                timeout=allowance(),
            )
            .stdout.decode()
            .splitlines()
        )
        for file in listed:
            if not file.endswith(".sql"):
                continue
            body = subprocess.run(
                ["git", "-C", str(root), "show", f"origin/main:{file}"],
                check=True,
                capture_output=True,
                timeout=allowance(),
            ).stdout
            local = folder / Path(file).name
            if local.exists() and body == local.read_bytes():
                found[local.stem] = hashlib.sha256(body).hexdigest()
    except (OSError, subprocess.CalledProcessError):
        pass  # Absence of provenance grants no upgrade authority.
    return json.dumps(found, sort_keys=True).encode()
