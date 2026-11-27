"""Process environment for game and agent processes.

`place` returns `Placement`, choosing the inference bus, game processes and
inference thread budget. `resources` owns temporary fast scratch;
`Resources.configure` sets child-only environment before author imports,
leaving the calling process untouched. Observations are kept in the run's
durable artifact folder by the group owner.
"""

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

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


def _has_gpu() -> bool:
    """Read visible NVIDIA hardware without importing the author's model library."""
    if os.environ.get("CUDA_VISIBLE_DEVICES") in ("", "-1"):
        return False
    command = shutil.which("nvidia-smi")
    if command is None:
        return False
    return bool(
        subprocess.check_output(
            [command, "--query-gpu=index", "--format=csv,noheader"],
            text=True,
            timeout=seconds_left(),
        ).strip()
    )


@dataclass(frozen=True)
class Placement:
    """Inference transport, game-process budget, inference threads and grouping."""

    bus: Literal["local", "socket"] | None
    processes: int
    threads: int
    grouped: bool = False

    def games(self, max_batch: int) -> int:
        """Concurrent `play` matches per game process."""
        if self.processes <= 0:
            return 0
        return math.ceil(max_batch / self.processes) if self.bus is not None else 1


def place(
    pending: int,
    *,
    agents: bool,
    streaming: bool = False,
    memory_gb: float | None = None,
) -> Placement:
    """Choose inference transport, game processes, inference threads and grouping.

    An all-`play_many` group (`streaming=True`) runs sequentially in one game
    process (`grouped=True`) on every host without probing GPU hardware: with a
    loader it uses a direct inference thread with the host inference thread
    budget (`max(1, cpus - 1)`); code-only runs use no bus. Any group with
    `play` skips the GPU probe when code-only, uses the shared agent process on
    visible NVIDIA hardware, and otherwise runs direct inference with one
    model-library thread per game process.
    """
    if pending <= 0:
        return Placement(bus=None, processes=0, threads=0)
    cpus = available_cpus()
    if streaming:
        if not agents:
            return Placement(bus=None, processes=1, threads=0, grouped=True)
        return Placement(bus="local", processes=1, threads=max(1, cpus - 1), grouped=True)
    if not agents:
        memory_processes = max(1, memory_bytes() // GAME_BYTES)
        return Placement(bus=None, processes=min(pending, cpus, memory_processes), threads=0)
    gpu = _has_gpu()
    held = agent_bytes(memory_gb)
    memory_processes = max(1, (memory_bytes() - held) // GAME_BYTES)
    if gpu:
        cpu_processes = max(1, cpus - 1)
        processes = min(pending, cpu_processes, memory_processes)
        return Placement(bus="socket", processes=processes, threads=max(1, cpus - processes))
    processes = min(pending, cpus, memory_processes)
    return Placement(bus="local", processes=processes, threads=1)


@dataclass(frozen=True)
class Resources:
    """One eval's scratch lifetime and durable observation destination."""

    scratch: Path
    artifacts: Path

    def configure(self, threads: int) -> None:
        """Configure a dedicated child before loading author code."""
        os.environ.update({name: str(threads) for name in THREAD_VARIABLES})
        os.environ.update(
            TMPDIR=str(self.scratch),
            ETUDE_SCRATCH_DIR=str(self.scratch),
            ETUDE_AGENTS_DIR=str(self.artifacts),
        )
        # Libraries using tempfile must share this child's scratch lifetime.
        tempfile.tempdir = str(self.scratch)


@contextmanager
def resources(artifacts: Path) -> Iterator[Resources]:
    """Own scratch until all game and agent children have exited.

    Parent environment and tempfile defaults are process-wide, so overlapping
    evals must pass their paths to child initialization instead of changing them.
    """
    artifacts.mkdir(parents=True, exist_ok=True)
    fast = Path("/dev/shm")
    directory = fast if fast.is_dir() and os.access(fast, os.W_OK) else None
    with tempfile.TemporaryDirectory(prefix="etude-eval-", dir=directory) as scratch:
        yield Resources(scratch=Path(scratch), artifacts=artifacts.resolve())
