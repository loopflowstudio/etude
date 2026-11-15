"""Merged SQL fingerprints carried by wheels and immutable server archives.

Source checkouts derive the frontier from origin/main, or from main where a
checkout (such as uv's for a `@main` pin) has no remote-tracking branch.
Installed programs use only their embedded evidence; unavailable Git cannot
authorize new SQL.
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

    def git(*args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, timeout=allowance()
        ).stdout

    try:
        # uv's checkout of a `@main` pin has refs/heads/main but no origin/main.
        main = next(
            ref
            for ref in ("origin/main", "main")
            if subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                capture_output=True,
                timeout=allowance(),
            ).returncode
            == 0
        )
        # `root` is src/etude; the prefix is repository-relative.
        listed = git("ls-tree", "-r", "--full-tree", "--name-only", main, "--", prefix)
        for file in listed.decode().splitlines():
            if not file.endswith(".sql"):
                continue
            body = git("show", f"{main}:{file}")
            local = folder / Path(file).name
            if local.exists() and body == local.read_bytes():
                found[local.stem] = hashlib.sha256(body).hexdigest()
    except (OSError, StopIteration, subprocess.CalledProcessError):
        pass  # Absence of provenance grants no upgrade authority.
    return json.dumps(found, sort_keys=True).encode()
