"""`etude smoke`: an experiment file's training, evaluation and report, tiny, in order, here.

`smoke_file` is the whole chain, as `etude smoke` runs it and `etude launch
--smoke` and `etude queue --smoke` run it before submitting:

1. Train: each distinct job `training_jobs()` returns, run once with its declared tiny
   form on a throwaway local launcher (`etude.launch.smoke.smoke_train`).
2. Eval: the file's `eval_jobs(state)` on what training stored, each tournament it
   returns cut to one game per match, every condition kept, and held here, through
   `etude.jobs.hold_group` as `etude eval` holds it.
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
from tempfile import TemporaryDirectory
from typing import Literal

from etude.controller.declarations import (
    Experiment,
    asked,
    read_experiment,
    read_training_jobs,
    require_training_options,
)
from etude.controller.diagnostics import causes, declaration
from etude.errors import EtudeError, LauncherError
from etude.eval.records import Benchmark, RoundRobin
from etude.eval.tournament import played
from etude.experiment import observe, read_tournament_lines
from etude.jobs import (
    EvalGroup,
    _group_key,
    evaluation_group,
    fetch_checkpoints,
    hold_group,
)
from etude.launch.smoke import Smoked, show, smoke_home, smoke_train
from etude.page import build, render, write
from etude.run import RunListing
from etude.spec import Job, RunRequest, SmokeStep
from etude.storage import DirectoryStorage, Storage, default_root

type Step = Literal["train", "eval", "report"]
STEPS: tuple[Step, ...] = ("train", "eval", "report")


def smoke_file(
    file: Path | Experiment,
    runs: Sequence[RunRequest] | None = None,
    *,
    given: Sequence[str] = (),
    steps: Collection[Step] = STEPS,
    source: Storage | None = None,
    keep: bool = False,
) -> Smoked:
    """Smoke the experiment file's `steps`, in order, and return what each did.

    `runs` are what its `training_jobs()` returned, as `read_training_jobs` read them; read
    with its defaults when not given. Without the train step, `source` holds
    the checkpoints to evaluate and the runs to report on; resolve it before
    calling, since the temporary home moves the default store. Raises
    `LauncherError` at the first failure, after printing its output.
    """
    definition = read_experiment(file) if isinstance(file, Path) else file
    path = definition.file
    assert path is not None
    require_training_options(given, definition, read_training="train" in steps and runs is None)
    if definition.training is None:
        steps = tuple(step for step in steps if step != "train")
        source = source or DirectoryStorage(default_root())
    if "train" in steps and source is not None:
        raise LauncherError("--source is for smoking eval and report without the train step")
    if "train" not in steps and source is None and set(steps) & {"eval", "report"}:
        raise LauncherError("smoking eval or report without train needs --source")
    if "train" in steps and runs is None:
        runs = read_training_jobs(
            path, given, experiment=definition.name, function=definition.training
        )
    train: dict[str, SmokeStep] = {}
    done: list[SmokeStep] = []
    with smoke_home(keep=keep) as folder:
        store = DirectoryStorage(folder / "track")
        if "train" in steps:
            for trained in smoke_train(runs or (), folder):
                step = trained.step
                if trained.run is not None:
                    step = step.model_copy(
                        update={"detail": _saved(store, definition.name, trained.run)}
                    )
                show(step)
                train[trained.job] = step
        # What the temporary store holds a run of, so eval and report have something to read.
        stored = any(each.outcome == "passed" for each in train.values())
        if "eval" in steps:
            for step in _evaluate(definition, source or store, store):
                show(step)
                done.append(step)
                stored = stored or step.outcome == "passed"
        if "report" in steps:
            step = _report(definition, store if stored else source, store)
            show(step)
            done.append(step)
    return Smoked(train=train, rest=tuple(done))


def _saved(store: Storage, experiment: str, run: str) -> str:
    found = [
        each
        for each in observe(store, experiment).runs
        if each.name == run.removeprefix(experiment + "/")
    ]
    count = len(found[0].checkpoints) if found else 0
    return f"{count} checkpoint{'' if count == 1 else 's'}"


def _evaluate(definition: Experiment, source: Storage, store: DirectoryStorage) -> list[SmokeStep]:
    """Each evaluation `eval_jobs(state)` asks for on `source`, held tiny into `store`."""
    function = definition.eval
    if function is None:
        return [
            SmokeStep(step="eval", name="eval", outcome="skipped", detail="no eval_jobs(state)")
        ]
    listing = RunListing.read(source)
    experiment = definition.name
    state = observe(listing, experiment)
    jobs = _try_step("eval", "eval", lambda: asked(function, state))
    if not jobs:
        return [
            SmokeStep(step="eval", name="eval", outcome="not_smoked", detail="eval returned none")
        ]
    steps: list[SmokeStep] = []
    runnable = []
    for job in jobs:
        outside = (
            job.tournament.experiments - {experiment}
            if isinstance(job.tournament, Benchmark)
            else frozenset()
        )
        if outside:
            steps.append(
                SmokeStep(
                    step="eval",
                    name=job.tournament.name,
                    outcome="not_smoked",
                    detail="cross-experiment seats: " + ", ".join(sorted(outside)),
                )
            )
        else:
            runnable.append(job)
    lines = read_tournament_lines(
        listing,
        state,
        [job.recorded() for job in runnable],
    )
    batches: dict[str, list[Job]] = {}
    path = definition.file
    assert path is not None
    for job in runnable:
        tiny = job.model_copy(update={"tournament": _tiny_tournament(job.tournament)})
        recorded = tiny.recorded()
        pending = played(
            tiny.tournament,
            experiment=experiment,
            checkpoints=recorded.checkpoints,
            enter=recorded.entrants,
            lines=lines,
            storages=[store],
        ).new
        with declaration(path, function):
            lowered = tiny.lower(state, pending, lines)
        spec = evaluation_group(lowered).evaluations[0]
        batches.setdefault(_group_key(lowered, spec), []).append(lowered)
    for batch in batches.values():
        started = time.monotonic()
        tiny_specs = tuple(spec for job in batch for spec in evaluation_group(job).evaluations)
        name = tiny_specs[0].definition.name if len(tiny_specs) == 1 else "eval"
        with TemporaryDirectory(prefix="etude-smoke-inputs-") as folder:
            files = _try_step(
                "eval",
                name,
                lambda: fetch_checkpoints(
                    tuple(match for spec in tiny_specs for match in spec.matches),
                    state,
                    (),
                    Path(folder),
                ),
            )
            held = _try_step(
                "eval",
                name,
                lambda: hold_group(
                    EvalGroup(evaluations=tuple(spec.relocate(files) for spec in tiny_specs)),
                    checkout=batch[0].repo,
                    storages=[store],
                ),
            )
        for spec, result in zip(tiny_specs, held, strict=True):
            steps.append(
                SmokeStep(
                    step="eval",
                    name=spec.definition.name if result.played else "eval",
                    outcome="passed" if result.played else "not_smoked",
                    seconds=time.monotonic() - started,
                    detail=f"{result.played} match{'' if result.played == 1 else 'es'}, 1 game each"
                    if result.played
                    else "no checkpoints",
                )
            )
    return steps


def _tiny_tournament(tournament: Benchmark | RoundRobin) -> Benchmark | RoundRobin:
    """The tournament with each of its seatings, or itself, at one game per match.

    Every condition is kept: a game that breaks under one deal or seating is
    the failure a rented evaluation would otherwise find after training paid.
    """
    if isinstance(tournament, RoundRobin):
        return tournament.model_copy(update={"games": 1})
    seatings = tuple(each.model_copy(update={"games": 1}) for each in tournament.seatings)
    return tournament.model_copy(update={"seatings": seatings})


def _report(definition: Experiment, source: Storage | None, store: DirectoryStorage) -> SmokeStep:
    """`report()` drawn from `source`, written beside the temporary store."""
    if all(each is None for each in (definition.training, definition.eval, definition.report)):
        return SmokeStep(
            step="report", name="report", outcome="skipped", detail="not an experiment file"
        )
    if source is None:
        return SmokeStep(step="report", name="report", outcome="not_smoked", detail="no runs")
    started = time.monotonic()
    page = _try_step("report", "report", lambda: build(source, definition))
    write(render(page), store.root.parent / "reports" / f"{definition.name}.html")
    return SmokeStep(
        step="report", name="report", outcome="passed", seconds=time.monotonic() - started
    )


def _try_step[T](step: Step, name: str, work: Callable[[], T]) -> T:
    """`work()`, or its traceback printed and a `LauncherError` naming the step."""
    started = time.monotonic()
    try:
        return work()
    except Exception as error:
        location = None
        if isinstance(error, (LauncherError, EtudeError)):
            if error.fix is not None:
                raise
            location = error.location
        if location is None:
            traceback.print_exc()
        # Name the first error, not a wrapper such as eval's `match_failed`.
        first: BaseException = error
        for cause in causes(error):
            first = cause
        raise LauncherError(
            f"smoke {step}{'' if name == step else ' ' + name}: failed after"
            f" {time.monotonic() - started:.1f} s ({type(first).__name__}: {first});"
            " nothing was submitted",
            at=location,
        ) from error
