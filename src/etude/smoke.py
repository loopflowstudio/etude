"""`etude smoke`: an experiment file's training, evaluation and report, tiny, in order, here.

`smoke_file` is the whole chain, as `etude smoke` runs it and `etude launch
--smoke` and `etude queue --smoke` run it before submitting:

1. Train: each distinct job `runs()` returns, run once with its declared tiny
   form on a throwaway local launcher (`etude.launch.smoke.smoke_train`).
2. Eval: the file's `eval(state)` on what training stored, each tournament it
   returns cut to one game per match, every condition kept, and held here, through
   `etude.jobs.hold_evaluation` as `etude eval` holds it.
3. Report: `report()`, or the default, drawn from those results by `etude.page`.

Everything goes to a temporary ETUDE_HOME (`smoke_home`). Without the train
step, eval and report read checkpoints from a named source and still deliver
only to the temporary store. The first failure prints what it can, raises
`LauncherError` naming the step, and stops the chain.

This module joins the launcher with evaluation, tracking and the report, as
`etude.cli` does, so the launcher itself never imports it.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import Literal

from etude.eval.records import Benchmark, RoundRobin
from etude.experiment import ExperimentState, asked, observe, read_experiment
from etude.experiment_file import defined_functions, experiment_named
from etude.jobs import evaluation_spec, hold_evaluation, with_experiment
from etude.launch.errors import LauncherError
from etude.launch.job_file import read_job_file
from etude.launch.records import Job, Run, SmokeStep
from etude.launch.smoke import Smoked, show, smoke_home, smoke_train
from etude.page import build, render, write
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage, Storage

type Step = Literal["train", "eval", "report"]
STEPS: tuple[Step, ...] = ("train", "eval", "report")


def smoke_file(
    path: Path,
    runs: Sequence[Run] | None = None,
    *,
    steps: Collection[Step] = STEPS,
    source: Storage | None = None,
    keep: bool = False,
) -> Smoked:
    """Smoke the experiment file's `steps`, in order, and return what each did.

    `runs` are what its `runs()` returned, as `read_job_file` read them; read
    with its defaults when not given. Without the train step, `source` holds
    the checkpoints to evaluate and the runs to report on; resolve it before
    calling, since the temporary home moves the default store. Raises
    `LauncherError` at the first failure, after printing its output.
    """
    if "train" in steps and source is not None:
        raise LauncherError("--source is for smoking eval and report without the train step")
    if "train" not in steps and source is None and set(steps) & {"eval", "report"}:
        raise LauncherError("smoking eval or report without train needs --source")
    if "train" in steps and runs is None:
        runs = read_job_file(path)
    experiment = experiment_named(path)
    train: dict[str, SmokeStep] = {}
    done: list[SmokeStep] = []
    with smoke_home(keep=keep) as folder:
        store = DirectoryStorage(folder / "track")
        if "train" in steps:
            for trained in smoke_train(runs or (), folder):
                step = trained.step
                if trained.run is not None and experiment is not None:
                    step = step.model_copy(
                        update={"detail": _saved(store, experiment, trained.run)}
                    )
                show(step)
                train[trained.job] = step
        # What the temporary store holds a run of, so eval and report have something to read.
        stored = any(each.outcome == "passed" for each in train.values())
        if "eval" in steps:
            for step in _evaluate(path, experiment, source or store, store):
                show(step)
                done.append(step)
                stored = stored or step.outcome == "passed"
        if "report" in steps:
            step = _report(path, experiment, store if stored else source, store)
            show(step)
            done.append(step)
    return Smoked(train=train, rest=tuple(done))


def _saved(store: Storage, experiment: str, run: str) -> str:
    found = [each for each in observe(store, experiment).runs if each.name == run]
    count = len(found[0].checkpoints) if found else 0
    return f"{count} checkpoint{'' if count == 1 else 's'}"


def _evaluate(
    path: Path, experiment: str | None, source: Storage, store: DirectoryStorage
) -> list[SmokeStep]:
    """Each evaluation `eval(state)` asks for on `source`, held tiny into `store`."""
    if experiment is None or "eval" not in defined_functions(path):
        return [SmokeStep(step="eval", name="eval", outcome="skipped", detail="no eval(state)")]
    state = observe(source, experiment)
    if not any(run.checkpoints for run in state.runs):
        return [SmokeStep(step="eval", name="eval", outcome="not_smoked", detail="no checkpoints")]
    jobs = _try_step("eval", "eval", lambda: asked(read_experiment(path).eval, state))
    if not jobs:
        return [
            SmokeStep(step="eval", name="eval", outcome="not_smoked", detail="eval returned none")
        ]
    return [_smoke_evaluation(job, experiment, store, state) for job in jobs]


def _smoke_evaluation(
    job: Job, experiment: str, store: DirectoryStorage, state: ExperimentState
) -> SmokeStep:
    """One evaluation job `eval(state)` returned, held tiny into `store`."""
    spec = evaluation_spec(job)
    name = spec.tournament.name
    try:
        job = with_experiment(job, experiment)
    except EtudeError as error:
        raise LauncherError(
            f"smoke eval {name}: a job of experiment {spec.experiment!r}"
        ) from error
    tiny = evaluation_spec(job).model_copy(
        update={"tournament": _tiny_tournament(spec.tournament), "lines": state.lines}
    )
    started = time.monotonic()
    held = _try_step(
        "eval", name, lambda: hold_evaluation(tiny, checkout=job.repo, storages=[store])
    )
    plural = "" if held.played == 1 else "es"
    return SmokeStep(
        step="eval",
        name=name,
        outcome="passed",
        seconds=time.monotonic() - started,
        detail=f"{held.played} match{plural}, 1 game each",
    )


def _tiny_tournament(tournament: Benchmark | RoundRobin) -> Benchmark | RoundRobin:
    """The tournament with each of its seatings, or itself, at one game per match.

    Every condition is kept: a game that breaks under one deal or seating is
    the failure a rented evaluation would otherwise find after training paid.
    """
    if isinstance(tournament, RoundRobin):
        return tournament.model_copy(update={"games": 1})
    seatings = tuple(each.model_copy(update={"games": 1}) for each in tournament.seatings)
    return tournament.model_copy(update={"seatings": seatings})


def _report(
    path: Path, experiment: str | None, source: Storage | None, store: DirectoryStorage
) -> SmokeStep:
    """`report()` drawn from `source`, written beside the temporary store."""
    if experiment is None:
        return SmokeStep(
            step="report", name="report", outcome="skipped", detail="not an experiment file"
        )
    if source is None:
        return SmokeStep(step="report", name="report", outcome="not_smoked", detail="no runs")
    started = time.monotonic()
    page = _try_step("report", "report", lambda: build(source, path))
    write(render(page), store.root.parent / "reports" / f"{experiment}.html")
    return SmokeStep(
        step="report", name="report", outcome="passed", seconds=time.monotonic() - started
    )


def _try_step[T](step: Step, name: str, work: Callable[[], T]) -> T:
    """`work()`, or its traceback printed and a `LauncherError` naming the step."""
    started = time.monotonic()
    try:
        return work()
    except Exception as error:
        traceback.print_exc()
        # Name the first error, not a wrapper such as eval's `match_failed`.
        first: BaseException = error
        while first.__cause__ is not None:
            first = first.__cause__
        raise LauncherError(
            f"smoke {step}{'' if name == step else ' ' + name}: failed after"
            f" {time.monotonic() - started:.1f} s ({type(first).__name__}: {first});"
            " nothing was submitted"
        ) from error
