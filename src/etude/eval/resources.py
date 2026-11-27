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
from collections.abc import Iterator, Sequence
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
    """Process topology, stream sharing, and resources per inference owner."""

    bus: Literal["local", "socket"] | None
    processes: int
    game_threads: int
    agent_threads: int
    games_per_process: int | None  # `play_many` requests its own slots.
    share_streams: bool
    checkpoint_bytes: int
    reason: str

    def describe(self, tournaments: Sequence[str]) -> str | None:
        """Describe the placement of one evaluation phase."""
        if not self.processes or (self.bus is None and self.games_per_process is not None):
            return None
        process = "game process" if self.processes == 1 else "game processes"
        game = "game" if self.games_per_process == 1 else "games"
        capacity = (
            "slots requested by play_many"
            if self.games_per_process is None
            else f"{self.games_per_process} {game} each"
        )
        games = f"{self.processes} {process}, {capacity}"
        if self.bus is None:
            return f"etude: {games}"
        mode = "shared" if self.bus == "socket" else "direct"
        threads = self.agent_threads if self.bus == "socket" else self.game_threads
        unit = "thread" if threads == 1 else "threads"
        each = " each" if self.bus == "local" and self.processes > 1 else ""
        return (
            f"etude: agents: {mode} inference for {', '.join(tournaments)} ({self.reason}); "
            f"{games}; {threads} inference {unit}{each}"
        )


def place(
    pending: Sequence[int],
    *,
    streaming: bool = False,
    agents: bool,
    max_batch: int = 32,
    memory_gb: float | None = None,
    match_bytes: int = 0,
) -> Placement:
    """Allocate the host to tournaments using the same kind of play function.

    `pending` gives the match count per tournament. `match_bytes` is the largest
    known total of distinct checkpoints seated in a pending match. A streaming
    phase serves every tournament in one process and skips the GPU probe.
    """
    queues = tuple(count for count in pending if count > 0)
    if not queues:
        return Placement(
            bus=None,
            processes=0,
            game_threads=1,
            agent_threads=0,
            games_per_process=None,
            share_streams=False,
            checkpoint_bytes=0,
            reason="no pending matches",
        )
    cpus = available_cpus()
    # One reservation covers the shared cache or all direct caches combined.
    host_bytes = memory_bytes() if not streaming or (agents and memory_gb is None) else 0
    held = 0
    if agents:
        held = int(memory_gb * 1024**3) if memory_gb is not None else host_bytes // 2
    if streaming:
        return Placement(
            bus="local" if agents else None,
            processes=1,
            game_threads=max(1, cpus - 1) if agents else cpus,
            agent_threads=0,
            games_per_process=None,
            share_streams=True,
            checkpoint_bytes=held,
            reason="play_many" if agents else "no checkpoints to load",
        )
    if not agents:
        cap = min(cpus, max(1, host_bytes // GAME_BYTES))
        return Placement(
            bus=None,
            processes=min(cap, sum(queues)),
            game_threads=1,
            agent_threads=0,
            games_per_process=1,
            share_streams=False,
            checkpoint_bytes=0,
            reason="no checkpoints to load",
        )
    gpu = _has_gpu()
    share_streams = not gpu
    memory_processes = max(1, (host_bytes - held) // GAME_BYTES)
    cap = min(max(1, cpus - 1), memory_processes) if gpu else min(cpus, memory_processes)
    if share_streams and match_bytes > 0:
        # Each direct process needs room for a match in its own checkpoint cache.
        cap = min(cap, max(1, held // match_bytes))
    largest = max(queues)
    # Direct processes share every queue and can reuse their checkpoint cache.
    if share_streams:
        queues = (largest,)
    games_per_process = min(math.ceil(max_batch / min(cap, sum(queues))), largest)
    processes = min(cap, sum(math.ceil(count / games_per_process) for count in queues))
    return Placement(
        bus="socket" if gpu else "local",
        processes=processes,
        game_threads=1 if gpu else max(1, (cpus - processes) // processes),
        agent_threads=max(1, cpus - processes) if gpu else 0,
        games_per_process=games_per_process,
        share_streams=share_streams,
        checkpoint_bytes=held if gpu else held // processes,
        reason="NVIDIA GPU visible" if gpu else "no NVIDIA GPU visible",
    )


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
