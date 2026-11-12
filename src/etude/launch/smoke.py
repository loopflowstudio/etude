"""Run jobs tiny on this computer, in a throwaway launcher, before anything is submitted.

A smoke runs in `smoke_home`: a temporary `ETUDE_HOME` holding its own
launcher database, sessions and tracked runs, so nothing reaches the real
records, checkpoints or artifact cache. Prepared environments and installed
programs are content-addressed and lock-guarded, so the real ones are shared
and a second smoke does not build them again.

`smoke_train` is the train step: each distinct job's tiny form (`Job.smoke`,
applied by `tiny`) launched on that launcher's `local` pool, through the same
path a real local run takes. `etude.smoke` adds the experiment file's
evaluation and report; the Python client calls `smoke_train` alone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from etude.launch.database import Database
from etude.launch.errors import LauncherError
from etude.launch.executor import local
from etude.launch.logs import render
from etude.launch.records import Job, Machine, Pool, Run, Smoke, SmokeStep, job_identity
from etude.launch.scheduler import Scheduler
from etude.launch.source import home

# Kept under ETUDE_HOME by content key with their own locks: shared with the real home.
_SHARED = ("prepared", "programs")
_MOVED = ("ETUDE_HOME", "ETUDE_TRACK_DIR")


@contextmanager
def smoke_home(*, keep: bool = False) -> Iterator[Path]:
    """A temporary ETUDE_HOME, with ETUDE_TRACK_DIR inside it, for the duration.

    Both are set in this process's environment, so the workers it starts and
    every reader of the default store use it. Removed afterwards unless `keep`.
    """
    real = home()
    folder = Path(tempfile.mkdtemp(prefix="etude-smoke-"))
    for name in _SHARED:
        (real / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        (folder / name).symlink_to(real / name, target_is_directory=True)
    saved = {name: os.environ.get(name) for name in _MOVED}
    os.environ["ETUDE_HOME"] = str(folder)
    os.environ["ETUDE_TRACK_DIR"] = str(folder / "track")
    try:
        yield folder
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if keep:
            print(f"smoke: kept {folder}", file=sys.stderr, flush=True)
        else:
            shutil.rmtree(folder, ignore_errors=True)  # removes the links, not what they share


def tiny(job: Job) -> Job | None:
    """The job with its tiny form's overrides, or None when it declares none."""
    if job.smoke is None:
        return None
    return job.model_copy(update={**job.smoke.model_dump(exclude_none=True), "smoke": None})


def smoke_train(runs: Sequence[Run], folder: Path) -> list[SmokeStep]:
    """Each distinct job of `runs` (seeds dropped) run tiny here, one after another.

    `folder` is a `smoke_home`. A job without a tiny form is `not_smoked`. The
    first that does not succeed prints its job's output and raises
    `LauncherError` naming it.
    """
    distinct: dict[str, Run] = {}
    for run in runs:
        distinct.setdefault(json.dumps(job_identity(run.job), sort_keys=True), run)
    store = Database(folder / "launcher.db")
    try:
        service = local(folder / "sessions")
        scheduler = Scheduler(store, {"local": lambda machine: service})
        scheduler.add_pool(Pool(name="local"))
        store.add_machine(Machine(name="local", pool="local", kind="local"))
        return [_train(scheduler, run) for run in distinct.values()]
    finally:
        store.close()


def _train(scheduler: Scheduler, run: Run) -> SmokeStep:
    name = run.job.name or "job"
    job = tiny(run.job)
    if job is None:
        return SmokeStep(step="train", name=name, outcome="not_smoked", detail="no tiny form")
    started = time.monotonic()
    launched = scheduler.launch(Run(job=job, seed=run.seed, params=dict(run.params)), "local")
    (status,) = scheduler.wait(launched, every=0.1)
    # Until its sessions end, so no worker still writes into the smoke's home.
    while not scheduler.logs(run=launched).settled:
        scheduler.tick()
        time.sleep(0.05)
    seconds = time.monotonic() - started
    if status.outcome != "succeeded":
        sys.stderr.write(render(scheduler.logs(run=launched)))
        raise LauncherError(
            f"smoke train {name}: {status} after {seconds:.1f} s; nothing was submitted"
        )
    return SmokeStep(step="train", name=name, outcome="passed", seconds=seconds, run=launched)


def show(step: SmokeStep) -> None:
    """One line on stderr for a step, such as `smoke train ppo: passed in 4.1 s`.

    On stderr, so a submission's standard output stays the list of its runs.
    """
    said = f"smoke {step.step}" + (f" {step.name}" if step.name != step.step else "")
    if step.outcome == "passed":
        said += f": passed in {step.seconds or 0:.1f} s"
    else:
        said += ": not smoked" if step.outcome == "not_smoked" else ": skipped"
    print(said + (f" ({step.detail})" if step.detail else ""), file=sys.stderr, flush=True)


def smoked(steps: Sequence[SmokeStep]) -> Smoke:
    """The smoke `steps` make: passed when any step ran and passed, else none."""
    passed = any(each.outcome == "passed" for each in steps)
    return Smoke(outcome="passed" if passed else "none", steps=tuple(steps))
