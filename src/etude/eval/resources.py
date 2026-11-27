"""Process environment for game and agent processes.

`plan_execution` chooses shared or direct inference and returns game, thread
and per-owner checkpoint-byte budgets for startup and dispatch. `resources`
owns temporary fast scratch; `Resources.configure` sets child-only environment
before author imports, leaving the calling process untouched. Observations are
kept in the run's durable artifact folder by the group owner.
"""

import os
import platform
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


type Gpu = Literal["nvidia", "apple"]

type Reason = (
    Gpu | Literal["shared=True", "shared=False", "ETUDE_EVAL_GPU=none", "no GPU", "no checkpoints"]
)
"""Why a plan shares or splits inference: the GPU found, the author's or host's choice, or none.

`"no GPU"` means the probe ran and found nothing; every other non-GPU reason skipped it.
"""


def _gpu() -> Gpu | None:
    """Read visible NVIDIA or Apple Silicon hardware without importing a model library."""
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "apple"
    if os.environ.get("CUDA_VISIBLE_DEVICES") in ("", "-1"):
        return None
    command = shutil.which("nvidia-smi")
    if command is None:
        return None
    try:
        listed = subprocess.check_output(
            [command, "--query-gpu=index", "--format=csv,noheader"],
            text=True,
            timeout=seconds_left(),
        )
    except subprocess.CalledProcessError:
        # nvidia-smi on PATH but failing (driver unloaded or mismatched): no usable GPU.
        return None
    return "nvidia" if listed.strip() else None


def _gpu_disabled() -> bool:
    """`ETUDE_EVAL_GPU=none` hides every GPU from eval sizing, as `CUDA_VISIBLE_DEVICES=""` does for NVIDIA."""
    match os.environ.get("ETUDE_EVAL_GPU", ""):
        case "":
            return False
        case "none":
            return True
        case other:
            raise ValueError(f"ETUDE_EVAL_GPU must be 'none' or unset, not {other!r}")


@dataclass(frozen=True)
class ExecutionPlan:
    """Process topology and per-owner checkpoint-byte budget for one eval group.

    `threads` is each inference owner's thread budget: the shared agent process's
    in shared mode, and each game process's (always one) in direct mode.
    """

    games: int
    threads: int
    shared: bool
    reason: Reason
    checkpoint_bytes: int

    def describe(self, tournaments: Sequence[str]) -> str:
        """One startup line naming the mode, why it was chosen, and its sizing."""
        games = "game" if self.games == 1 else "games"
        if self.shared:
            unit = "thread" if self.threads == 1 else "threads"
            inference = f"{self.threads} inference {unit}"
        else:
            inference = "1 inference thread each"
        mode = "shared" if self.shared else "direct"
        return (
            f"etude: agents: {mode} inference for {', '.join(tournaments)} ({self.reason}); "
            f"{self.games} {games} at a time; {inference}"
        )


def plan_execution(
    pending: int,
    *,
    needs_loading: bool,
    memory_gb: float | None = None,
    batch_inputs: int = 32,
    shared: bool | None = None,
) -> ExecutionPlan:
    """Choose shared or direct inference and size games, threads and checkpoint bytes.

    NVIDIA games fill one shared batch. Apple Silicon games leave a core for the
    shared agent process. Forced sharing without a detected GPU reserves at
    least half the cores for inference. Direct games split the checkpoint-byte allowance
    across game processes. Memory and pending work may reduce games further.
    Code-only plays (`needs_loading=False`) and `shared=False` never probe.
    """
    if pending < 1:
        raise ValueError("an execution plan needs pending matches")
    cpus = available_cpus()
    gpu: Gpu | None = None
    reason: Reason
    if not needs_loading:
        reason = "no checkpoints"
    elif shared is False:
        reason = "shared=False"
    elif _gpu_disabled():
        reason = "shared=True" if shared else "ETUDE_EVAL_GPU=none"
    elif (gpu := _gpu()) is not None:
        reason = gpu
    else:
        reason = "shared=True" if shared else "no GPU"
    use_shared = needs_loading and (shared is True or gpu is not None)
    total_memory = memory_bytes()
    held = 0
    if needs_loading:
        held = total_memory // 2 if memory_gb is None else int(memory_gb * 1024**3)
    if not use_shared:
        capacity = cpus
    elif gpu == "nvidia":
        capacity = batch_inputs
    elif gpu == "apple":
        capacity = max(1, cpus - 1)
    else:
        capacity = max(1, cpus // 2)
    games = min(
        pending,
        capacity,
        max(1, (total_memory - held) // GAME_BYTES),
    )
    return ExecutionPlan(
        games=games,
        threads=max(1, cpus - games) if use_shared else 1,
        shared=use_shared,
        reason=reason,
        checkpoint_bytes=held if use_shared else held // games,
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
