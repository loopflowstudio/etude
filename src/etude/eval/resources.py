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
    games_per_process: int
    share_streams: bool
    checkpoint_bytes: int
    reason: str

    def describe(self, tournaments: Sequence[str], live: int, *, streaming: bool) -> str | None:
        """Describe the placement of the games that started."""
        if not live or (self.bus is None and not streaming):
            return None
        process = "game process" if live == 1 else "game processes"
        game = "game" if self.games_per_process == 1 else "games"
        capacity = f"{self.games_per_process} {game} each"
        if streaming:
            slots = "slots requested by play_many"
            capacity = f"{slots}; play holds up to {capacity}" if self.games_per_process else slots
        games = f"{live} {process}, {capacity}"
        if self.bus is None:
            return f"etude: {games}"
        mode = "shared" if self.bus == "socket" else "direct"
        threads = self.agent_threads if self.bus == "socket" else self.game_threads
        unit = "thread" if threads == 1 else "threads"
        each = " each" if self.bus == "local" and live > 1 else ""
        return (
            f"etude: agents: {mode} inference for {', '.join(tournaments)} ({self.reason}); "
            f"{games}; {threads} inference {unit}{each}"
        )


def place(
    play: Sequence[int] = (),
    streaming: int = 0,
    *,
    agents: bool,
    max_batch: int = 32,
    memory_gb: float | None = None,
) -> Placement:
    """Choose inference transport, exact game processes, thread budgets, concurrency and grouping.

    `play` gives pending match counts per active `play` tournament; `streaming`
    counts active `play_many` tournaments.

    An all-`play_many` group runs sequentially in one game process on every
    host and skips the GPU probe. With a loader, it uses a direct inference
    thread with the host inference thread budget
    (`max(1, cpus - 1)`); code-only runs use no bus and give the single process
    the host's `cpus` BLAS budget. Any group with `play` skips the GPU probe
    when code-only, uses the shared agent process on visible NVIDIA hardware
    (`agent_threads = max(1, cpus - processes)`), and otherwise runs direct
    inference splitting remaining host cores across game processes
    (`game_threads = max(1, (cpus - processes) // processes)`). With direct
    inference, processes serve tournaments of the same kind in turn. One
    process serves the `play_many` streams and the largest `play` queue sizes
    the `play` processes. Socket and code-only mixed groups use separate
    processes for each tournament.
    """
    active_play = tuple(count for count in play if count > 0)
    active_streaming = max(0, streaming)
    if not active_play and active_streaming <= 0:
        return Placement(
            bus=None,
            processes=0,
            game_threads=1,
            agent_threads=0,
            games_per_process=0,
            share_streams=False,
            checkpoint_bytes=0,
            reason="no pending matches",
        )
    cpus = available_cpus()
    # One reservation covers the shared cache or all direct caches combined.
    host_bytes = memory_bytes() if active_play or (agents and memory_gb is None) else 0
    held = 0
    if agents:
        held = int(memory_gb * 1024**3) if memory_gb is not None else host_bytes // 2
    if not active_play:
        return Placement(
            bus="local" if agents else None,
            processes=1,
            game_threads=max(1, cpus - 1) if agents else cpus,
            agent_threads=0,
            games_per_process=0,
            share_streams=True,
            checkpoint_bytes=held,
            reason="all tournaments use play_many" if agents else "no checkpoints to load",
        )
    if not agents:
        cap = min(cpus, max(1, host_bytes // GAME_BYTES))
        return Placement(
            bus=None,
            processes=min(cap, active_streaming + sum(active_play)),
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
    largest_play = max(active_play)
    play_queues = (largest_play,) if share_streams else active_play
    streaming_slots = min(1, active_streaming) if share_streams else active_streaming
    play_cap = min(max(1, cap - streaming_slots), sum(play_queues))
    games_per_process = min(math.ceil(max_batch / play_cap), largest_play)
    play_needed = sum(math.ceil(count / games_per_process) for count in play_queues)
    processes = min(cap, streaming_slots + play_needed)
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
