"""Process environment for evaluation callers and serving code.

`resources` divides the CPUs available to this container, supplies temporary
fast scratch, and restores the caller's environment when the group finishes.
Receipts are kept in the run's durable artifact folder by the group owner.
"""

import math
import os
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


@contextmanager
def resources(workers: int, receipts: Path) -> Iterator[None]:
    """Give serving and play the same scratch and per-worker thread budget."""
    receipts.mkdir(parents=True, exist_ok=True)
    threads = str(max(1, available_cpus() // max(1, workers)))
    fast = Path("/dev/shm")
    directory = fast if fast.is_dir() and os.access(fast, os.W_OK) else None
    with TemporaryDirectory(prefix="etude-eval-", dir=directory) as scratch:
        values = {name: threads for name in THREAD_VARIABLES}
        values.update(
            TMPDIR=scratch, ETUDE_SCRATCH_DIR=scratch, ETUDE_RECEIPTS_DIR=str(receipts.resolve())
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
