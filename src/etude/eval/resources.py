"""Process environment for game and agent processes.

`divide_cpus` returns CPU counts for agent startup and game dispatch.
`resources` supplies temporary fast scratch and restores the caller's
environment when the group finishes.
Observations are kept in the run's durable artifact folder by the group owner.
"""

import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)


def _cgroups(controller: str) -> Iterator[Path]:
    """The container's cgroup and ancestors, for unified and v1 controller mounts."""
    root = Path("/sys/fs/cgroup")
    membership = Path("/proc/self/cgroup")
    lines = membership.read_text().splitlines() if membership.is_file() else []
    mounts = [(root, ""), (root / controller, controller)]
    if controller == "cpu":
        mounts.append((root / "cpu,cpuacct", controller))
    for mount, name in mounts:
        location = next(
            (line.split(":", 2)[2] for line in lines if name in line.split(":", 2)[1].split(",")),
            "/",
        )
        leaf = mount / location.lstrip("/")
        # Ancestors can impose tighter limits; never walk above the controller mount.
        if leaf.is_relative_to(mount):
            yield leaf
            yield from (p for p in leaf.parents if p.is_relative_to(mount))
        else:
            yield mount


def available_cpus() -> int:
    """Respect affinity and Linux cgroup quotas rather than the host's CPU count."""
    affinity = getattr(os, "sched_getaffinity", None)
    count = len(affinity(0)) if affinity is not None else os.cpu_count() or 1
    for folder in _cgroups("cpu"):
        quota_file = folder / "cpu.max"
        if quota_file.is_file():
            quota, period = quota_file.read_text().split()
            if quota != "max":
                count = min(count, max(1, int(quota) // int(period)))
        quota_file, period_file = folder / "cpu.cfs_quota_us", folder / "cpu.cfs_period_us"
        if quota_file.is_file() and period_file.is_file():
            quota_v1, period_v1 = int(quota_file.read_text()), int(period_file.read_text())
            if quota_v1 > 0:
                count = min(count, max(1, quota_v1 // period_v1))
    return max(1, count)


GAME_BYTES = 256 * 1024**2


def memory_bytes() -> int:
    """Host memory capped by the container's inherited memory limits."""
    physical = (
        os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") if sys.platform != "darwin" else 0
    )
    if not physical:
        # macOS exposes physical memory through sysctl, not SC_PHYS_PAGES.
        physical = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True))
    for folder in _cgroups("memory"):
        limit = folder / "memory.max"
        if limit.is_file() and (value := limit.read_text().strip()) != "max":
            physical = min(physical, int(value))
        limit = folder / "memory.limit_in_bytes"
        if limit.is_file():
            physical = min(physical, int(limit.read_text()))
    return physical


def agent_bytes(memory_gb: float | None) -> int:
    """Loaded-checkpoint bytes the agent process may hold: declared, or half of memory."""
    return int(memory_gb * 1024**3) if memory_gb is not None else memory_bytes() // 2


def seconds_left() -> float | None:
    """Seconds before the run's `ETUDE_DEADLINE`, never negative; None without one."""
    deadline = os.environ.get("ETUDE_DEADLINE")
    if not deadline:
        return None
    return max(0.0, datetime.fromisoformat(deadline).timestamp() - time.time())


@dataclass(frozen=True)
class Cpus:
    games: int
    threads: int


def divide_cpus(pending: int, *, agents: bool, memory_gb: float | None = None) -> Cpus:
    """Reserve one eighth of CPUs for inference; small game sets return unused CPUs.

    A single CPU is shared by one game and inference. Memory and pending work
    may reduce games further, but never leave queued work with zero capacity.
    """
    cpus = available_cpus()
    held = agent_bytes(memory_gb) if agents else 0
    games = min(
        pending,
        max(1, cpus - max(1, (cpus + 7) // 8)) if agents else cpus,
        max(1, (memory_bytes() - held) // GAME_BYTES),
    )
    return Cpus(games=games, threads=max(1, cpus - games) if agents else 0)


@contextmanager
def resources(artifacts: Path) -> Iterator[None]:
    """Give game processes one thread each and private temporary scratch."""
    artifacts.mkdir(parents=True, exist_ok=True)
    fast = Path("/dev/shm")
    directory = fast if fast.is_dir() and os.access(fast, os.W_OK) else None
    previous_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="etude-eval-", dir=directory) as scratch:
        values = {name: "1" for name in THREAD_VARIABLES}
        values.update(
            TMPDIR=scratch, ETUDE_SCRATCH_DIR=scratch, ETUDE_AGENTS_DIR=str(artifacts.resolve())
        )
        previous = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        # tempfile caches TMPDIR on first use, including calls made by libraries.
        # Keep that cache within the same lifetime as the scratch environment.
        tempfile.tempdir = None
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            tempfile.tempdir = previous_tempdir
