"""Process environment for game and agent processes.

`place` returns `Placement`, choosing the inference bus, game processes and
inference thread budget. `resources` owns temporary fast scratch;
`Resources.configure` sets child-only environment before author imports,
leaving the calling process untouched. Observations are kept in the run's
durable artifact folder by the group owner.
"""

import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from etude.errors import RetryRequested

if TYPE_CHECKING:
    from etude.eval.loader import AgentLoader

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


type Accelerator = Literal["cuda", "metal"]
type Device = Literal["cuda", "metal", "cpu"]
type _Unavailable = Literal[
    "no accelerator detected", "CUDA_VISIBLE_DEVICES hides GPUs", "nvidia-smi failed"
]
type Inference = (
    Accelerator
    | _Unavailable
    | Literal[
        "no loader",
        "no checkpoints",
        "cpu-only loader",
        "loader does not declare cuda",
        "loader does not declare metal",
    ]
)


def _detect_accelerator() -> Accelerator | _Unavailable:
    """Find visible hardware or explain its absence without importing a model library."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "metal"
    command = shutil.which("nvidia-smi")
    if command is None:
        return "no accelerator detected"
    if os.environ.get("CUDA_VISIBLE_DEVICES") in ("", "-1"):
        return "CUDA_VISIBLE_DEVICES hides GPUs"
    try:
        listed = subprocess.check_output(
            [command, "--query-gpu=index", "--format=csv,noheader"],
            text=True,
            timeout=seconds_left(),
        )
    except subprocess.CalledProcessError:
        # Driver unloaded or mismatched: play direct, but name the fault at startup.
        return "nvidia-smi failed"
    except subprocess.TimeoutExpired as error:
        raise RetryRequested("nvidia-smi did not answer before the deadline") from error
    return "cuda" if listed.strip() else "no accelerator detected"


@dataclass(frozen=True)
class TournamentQueue:
    """Pending match demand for one tournament."""

    matches: int
    streaming: bool = False

    def process_demand(self, streaming_batch: int) -> int:
        return math.ceil(self.matches / streaming_batch) if self.streaming else self.matches


@dataclass(frozen=True)
class Placement:
    """Process topology, stream sharing, and resources per inference owner."""

    bus: Literal["local", "socket"] | None
    processes: int
    game_threads: int
    agent_threads: int
    games_per_process: int | None  # Concurrency of async play, if present.
    share_streams: bool
    checkpoint_bytes: int
    reason: Inference | Literal["no pending matches"]

    def process_shares(self, queues: Sequence[TournamentQueue], max_batch: int) -> tuple[int, ...]:
        """Share the available processes among unfinished tournament queues."""
        demand = [queue.process_demand(max_batch if self.share_streams else 1) for queue in queues]
        if self.share_streams:
            return tuple(min(count, self.processes) for count in demand)
        return _allocate_processes(demand, min(sum(demand), self.processes))

    def describe(self, tournaments: Sequence[str], *, streaming: bool) -> str | None:
        """Describe the placement of one evaluation group."""
        if not self.processes or (self.bus is None and not streaming):
            return None
        process = "game process" if self.processes == 1 else "game processes"
        game = "game" if self.games_per_process == 1 else "games"
        capacity = []
        if self.games_per_process is not None:
            capacity.append(f"{self.games_per_process} {game} each")
        if streaming:
            capacity.append("slots requested by play_many")
        games = ", ".join((f"{self.processes} {process}", *capacity))
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


def _select_inference(loader: "AgentLoader", checkpoints: bool) -> Inference:
    if loader.load is None:
        return "no loader"
    if not checkpoints:
        return "no checkpoints"
    if loader.devices == ("cpu",):
        return "cpu-only loader"
    match _detect_accelerator():
        case "cuda":
            return "cuda" if "cuda" in loader.devices else "loader does not declare cuda"
        case "metal":
            return "metal" if "metal" in loader.devices else "loader does not declare metal"
        case unavailable:
            return unavailable


def _allocate_processes(demand: Sequence[int], processes: int) -> tuple[int, ...]:
    """Share the host in tournament order, leaving one slot for queues that start later."""
    allocated = [0] * len(demand)
    eligible = deque(index for index, count in enumerate(demand) if count)
    for _ in range(processes):
        index = eligible.popleft()
        allocated[index] += 1
        if allocated[index] < demand[index]:
            eligible.append(index)
    return tuple(
        max(1, slots) if count else 0 for slots, count in zip(allocated, demand, strict=True)
    )


def place(
    queues: Sequence[TournamentQueue],
    *,
    loader: "AgentLoader",
    checkpoints: bool,
    match_bytes: int = 0,
) -> Placement:
    """Allocate one host to the pending async and streaming match queues.

    `match_bytes` is the largest known total of distinct checkpoints seated in
    a pending match.
    """
    total = sum(queue.matches for queue in queues)
    if not total:
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
    inference = _select_inference(loader, checkpoints)
    cpus = available_cpus()
    host_bytes = memory_bytes()
    largest_play = max((queue.matches for queue in queues if not queue.streaming), default=0)
    if inference in ("no loader", "no checkpoints"):
        processes = min(cpus, max(1, host_bytes // GAME_BYTES), total)
        return Placement(
            bus=None,
            processes=processes,
            game_threads=1,
            agent_threads=0,
            games_per_process=1 if largest_play else None,
            share_streams=False,
            checkpoint_bytes=0,
            reason=inference,
        )
    # One reservation covers the shared cache or all direct caches combined.
    memory_gb = loader.memory_gb
    held = int(memory_gb * 1024**3) if memory_gb is not None else host_bytes // 2
    accelerator = inference in ("cuda", "metal")
    memory_processes = max(1, (host_bytes - held) // GAME_BYTES)
    cap = min(max(1, cpus - 1) if accelerator else cpus, memory_processes)
    if accelerator:
        cap = min(cap, loader.max_batch)
    elif match_bytes > 0:
        # Each direct process needs room for a match in its own checkpoint cache.
        cap = min(cap, max(1, held // match_bytes))

    demand = [queue.process_demand(1 if accelerator else loader.max_batch) for queue in queues]
    # A direct process can serve multiple queues in turn.
    processes = min(cap, sum(demand) if accelerator else max(demand))
    shared = accelerator and processes > 1
    games = min(math.ceil(loader.max_batch / processes), math.ceil(largest_play / processes))
    return Placement(
        bus="socket" if shared else "local",
        processes=processes,
        game_threads=1 if shared else max(1, (cpus - processes) // processes),
        agent_threads=max(1, cpus - processes) if shared else 0,
        games_per_process=games or None,
        share_streams=not shared,
        checkpoint_bytes=held if shared else held // processes,
        reason=inference,
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
