"""Run jobs tiny on this computer, in a throwaway launcher, before anything is submitted.

A smoke runs in `smoke_home`: a temporary `ETUDE_HOME` holding its own
launcher database, sessions and tracked runs, so nothing reaches the real
records, checkpoints or artifact cache. Prepared environments and installed
programs are content-addressed and lock-guarded, so the real ones are shared
and a second smoke does not build them again.

`smoke_train` is the train step: each distinct job's smoke form (`Job.smoke`,
applied by `tiny`) launched on that launcher's `local` pool, through the same
path a real local run takes. `etude.smoke` adds the experiment file's
evaluation and report; the Python client calls `smoke_train` alone. `Smoked`
stamps each run with its own job's train step and the file's other steps, so
a job without a smoke form is never recorded as passed beside one that has it.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from etude.errors import LauncherError
from etude.launch.database import Database, launcher_file
from etude.launch.executor import local
from etude.launch.logs import render
from etude.launch.records import Machine, Pool
from etude.launch.scheduler import Scheduler
from etude.launch.source import home
from etude.spec import Job, RunRequest, Smoke, SmokeStep, job_id

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
    """The job with its smoke form's overrides, or None when it declares none."""
    if job.smoke is None:
        return None
    # The form's own values, not a dump of them: model_copy does not validate.
    overrides = {name: value for name, value in job.smoke if value is not None}
    return job.model_copy(update={**overrides, "smoke": None})


@dataclass(frozen=True)
class Trained:
    """A job's train step, with its tiny run while the smoke's home still exists."""

    job: str  # `job_id`
    step: SmokeStep
    run: str | None = None  # in the smoke's own launcher, which is deleted with it


@dataclass(frozen=True)
class Smoked:
    """What a smoke did: each job's train step, by `job_id`, and the file's other steps."""

    train: Mapping[str, SmokeStep] = field(default_factory=dict)
    rest: tuple[SmokeStep, ...] = ()

    def stamp(self, runs: Sequence[RunRequest]) -> list[RunRequest]:
        """`runs`, each with its own job's train step and the rest as its smoke."""
        stamped = []
        for run in runs:
            own = self.train.get(job_id(run.job))
            steps = [*self.rest] if own is None else [own, *self.rest]
            stamped.append(run.model_copy(update={"smoke": Smoke(steps=tuple(steps))}))
        return stamped


def smoke_train(runs: Sequence[RunRequest], folder: Path) -> list[Trained]:
    """Each distinct job of `runs` (seeds dropped) run tiny here, one after another.

    `folder` is a `smoke_home`. A job without a smoke form is `not_smoked`. The
    first that does not succeed prints its job's output and raises
    `LauncherError` naming it.
    """
    distinct: dict[str, RunRequest] = {}
    for run in runs:
        distinct.setdefault(job_id(run.job), run)
    store = Database(launcher_file(folder))
    try:
        service = local(folder / "sessions")
        scheduler = Scheduler(store, {"local": lambda machine: service})
        scheduler.add_pool(Pool(name="local"))
        store.add_machine(Machine(name="local", pool="local", kind="local"))
        return [_train(scheduler, key, run) for key, run in distinct.items()]
    finally:
        store.close()


def _train(scheduler: Scheduler, key: str, run: RunRequest) -> Trained:
    name = run.job.name or "job"
    job = tiny(run.job)
    if job is None:
        return Trained(
            job=key,
            step=SmokeStep(step="train", name=name, outcome="not_smoked", detail="no smoke form"),
        )
    started = time.monotonic()
    launched = scheduler.launch(
        RunRequest(job=job, seed=run.seed, params=dict(run.params)), "local"
    )
    (status,) = scheduler.wait(launched, every=0.1)
    # Until its sessions end, so no worker still writes into the smoke's home.
    while not scheduler.logs(run=launched).settled:
        scheduler.tick()
        time.sleep(0.05)
    seconds = time.monotonic() - started
    if status.outcome != "succeeded":
        sys.stderr.write(render(scheduler.logs(run=launched)))
        cause = f" ({status.cause})" if status.cause else ""
        raise LauncherError(
            f"smoke train {name}: {status.outcome}{cause} after {seconds:.1f} s;"
            " nothing was submitted"
        )
    step = SmokeStep(step="train", name=name, outcome="passed", seconds=seconds)
    return Trained(job=key, step=step, run=launched)


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
