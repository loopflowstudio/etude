"""Process environment for game and agent processes.

`resources` divides the CPUs available to this container, supplies temporary
fast scratch, and restores the caller's environment when the group finishes.
Observations are kept in the run's durable artifact folder by the group owner.
"""

import math
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)


def available_cpus() -> int:
    """Respect affinity and Linux cgroup quotas rather than the host's CPU count."""
    affinity = getattr(os, "sched_getaffinity", None)
    count = len(affinity(0)) if affinity is not None else os.cpu_count() or 1
    # A cgroup's ancestors may impose tighter limits than its own cpu.max.
    root = Path("/sys/fs/cgroup")
    membership = Path("/proc/self/cgroup")
    paths = [root]
    if membership.is_file():
        for line in membership.read_text().splitlines():
            if line.startswith("0::"):
                leaf = root / line.split("::", 1)[1].lstrip("/")
                if leaf.is_relative_to(root):
                    paths = [leaf, *(p for p in leaf.parents if p.is_relative_to(root))]
    for folder in paths:
        quota_file = folder / "cpu.max"
        if quota_file.is_file():
            quota, period = quota_file.read_text().split()
            if quota != "max":
                count = min(count, max(1, math.floor(int(quota) / int(period))))
    for mount in (root / "cpu", root / "cpu,cpuacct"):
        v1_paths = [mount]
        if membership.is_file():
            for line in membership.read_text().splitlines():
                _, controllers, location = line.split(":", 2)
                if "cpu" in controllers.split(","):
                    leaf = mount / location.lstrip("/")
                    if leaf.is_relative_to(mount):
                        v1_paths = [leaf, *(p for p in leaf.parents if p.is_relative_to(mount))]
        for folder in v1_paths:
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
    root = Path("/sys/fs/cgroup")
    membership = Path("/proc/self/cgroup")
    paths = [root]
    if membership.is_file():
        for line in membership.read_text().splitlines():
            if line.startswith("0::"):
                leaf = root / line.split("::", 1)[1].lstrip("/")
                if leaf.is_relative_to(root):
                    paths = [leaf, *(p for p in leaf.parents if p.is_relative_to(root))]
    for folder in paths:
        limit = folder / "memory.max"
        if limit.is_file() and (value := limit.read_text().strip()) != "max":
            physical = min(physical, int(value))
    mount = root / "memory"
    paths = [mount]
    if membership.is_file():
        for line in membership.read_text().splitlines():
            _, controllers, location = line.split(":", 2)
            if "memory" in controllers.split(","):
                leaf = mount / location.lstrip("/")
                if leaf.is_relative_to(mount):
                    paths = [leaf, *(p for p in leaf.parents if p.is_relative_to(mount))]
    for folder in paths:
        limit = folder / "memory.limit_in_bytes"
        if limit.is_file():
            physical = min(physical, int(limit.read_text()))
    return physical


def active_games(pending: int, *, agents: bool, memory_gb: float | None = None) -> int:
    memory = memory_bytes()
    held = int(memory_gb * 1024**3) if memory_gb is not None else memory // 2 if agents else 0
    return min(
        pending, max(1, available_cpus() - int(agents)), max(1, (memory - held) // GAME_BYTES)
    )


@contextmanager
def resources(artifacts: Path) -> Iterator[None]:
    """Give game processes one thread each and private temporary scratch."""
    artifacts.mkdir(parents=True, exist_ok=True)
    fast = Path("/dev/shm")
    directory = fast if fast.is_dir() and os.access(fast, os.W_OK) else None
    with TemporaryDirectory(prefix="etude-eval-", dir=directory) as scratch:
        values = {name: "1" for name in THREAD_VARIABLES}
        values.update(
            TMPDIR=scratch, ETUDE_SCRATCH_DIR=scratch, ETUDE_AGENTS_DIR=str(artifacts.resolve())
        )
        previous = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
