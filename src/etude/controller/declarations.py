"""Execute experiment files and validate their typed training and eval declarations.

The file supplies the experiment's default name and expands all declarations
before job/seed selection. An explicit experiment override wins. Explicit
RunRequests preserve their ids; Jobs expand to full experiment/job@seed addresses.
"""

from __future__ import annotations

import inspect
import runpy
import sys
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import typer

from etude.errors import EtudeError, LauncherError
from etude.experiment import observe
from etude.experiment_file import refuse_renamed
from etude.identity import run_address
from etude.jobs import EvalJob, ExperimentState, TrainingJob, TrainingRunSummary, evaluation_group
from etude.spec import Job, RunRequest, expand_runs
from etude.storage import default_root


def read_training_jobs(
    path: Path,
    given: Sequence[str] = (),
    *,
    help: bool = False,
    experiment: str | None = None,
    local: bool = False,
    state: ExperimentState | None = None,
    function: Callable[..., object] | None = None,
) -> list[RunRequest]:
    """Let Typer validate training_jobs()'s signature, then invoke it once with typed flags.

    Validate named typed jobs before qualifying them and expanding seeds.
    `local` reads declarations for training here, without rental or git requirements.
    """

    if path.suffix != ".py":
        raise LauncherError("experiment files must be Python (.py), defining training_jobs()")
    refuse_renamed(path)
    if function is None:
        found = _namespace(path).get("training_jobs")
        function = cast(Callable[..., object], found) if callable(found) else None
    if not callable(function):
        raise LauncherError(f"{path.name}: define training_jobs() returning jobs or run requests")
    signature = inspect.signature(function)
    hints = typing.get_type_hints(function)
    reserved = {
        "pool",
        "rent",
        "max_usd",
        "source",
        "watch",
        "retry_failed",
        "smoke",
        "quote",
        "help",
        "experiment",
        "resume",
        "until",
        "job",
        "seed",
    }
    arguments: dict[str, Any] = {}
    values: list[Any] = []

    def invoke(**kwargs: Any) -> None:
        arguments.update(kwargs)
        observed = state
        if "state" in signature.parameters and observed is None:
            observed = observe(default_root(), experiment or path.stem)
        try:
            values.append(
                function(
                    **kwargs, **({"state": observed} if "state" in signature.parameters else {})
                )
            )
        except Exception as error:
            hint = (
                ". Records take named fields, such as RunRequest(job=..., seed=1)"
                if isinstance(error, TypeError) and "positional argument" in str(error)
                else ""
            )
            raise LauncherError(
                f"{path.name}: {type(error).__name__}: {' '.join(str(error).splitlines())}{hint}"
            ) from error

    parameters = []
    for name, param in signature.parameters.items():
        if name == "state":
            continue
        if name in reserved or "no_" + name in reserved:
            raise LauncherError(
                f"{path.name}: training_jobs() parameter {name!r} clashes with launcher option --{name.replace('_', '-')}"
            )
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise LauncherError(
                f"{path.name}: training_jobs() parameter {name!r} must be a named parameter"
            )
        kind = hints.get(
            name, str if param.default is inspect.Parameter.empty else type(param.default)
        )
        default_value = ... if param.default is inspect.Parameter.empty else param.default
        parameters.append(
            param.replace(
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=kind,
                default=typer.Option(default_value),
            )
        )
    setattr(
        invoke, "__signature__", signature.replace(parameters=parameters, return_annotation=None)
    )
    invoke.__doc__ = function.__doc__
    job_app = typer.Typer(
        rich_markup_mode=None, pretty_exceptions_enable=False, add_completion=False
    )
    job_app.command()(invoke)
    job_app(
        args=["--help"] if help else list(given),
        prog_name=f"{path.name} parameters",
        standalone_mode=False,
    )
    if help:
        return []
    returned = values[0]
    returned = list(returned) if isinstance(returned, (list, tuple)) else [returned]
    for each in returned:
        if isinstance(each, TrainingJob):
            continue
        if isinstance(each, RunRequest) and " -m etude.call " in each.job.command:
            try:
                evaluation_group(each.job)
            except EtudeError:
                continue
        raise LauncherError(f"{path.name}: training_jobs() must return TrainingJob")
    plain = [each.plain() if isinstance(each, Job) else each.job for each in returned]
    jobs: list[Job] = []
    for candidate in plain:
        if not local:
            candidate.require_hours()
            if candidate.repo is not None and not any(
                (folder / ".git").exists()
                for folder in (Path(candidate.repo), *Path(candidate.repo).parents)
            ):
                raise LauncherError(f"{candidate.repo} is in no git checkout")
        if candidate not in jobs:
            jobs.append(candidate)
    names = [job.name for job in jobs]
    if len(names) != len(set(names)):
        raise LauncherError(f"{path.name}: jobs need distinct names")
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    normalized: list[Job | RunRequest] = []
    for each, job in zip(returned, plain, strict=True):
        if not job.name:
            raise LauncherError(f"{path.name}: TrainingJob requires name=")
        label = experiment or job.experiment or path.stem
        named = job.model_copy(
            update={
                "repo": str(Path(job.repo).resolve()) if job.repo else None,
                "experiment": label,
            }
        )
        normalized.append(
            named
            if isinstance(each, Job)
            else each.model_copy(
                update={
                    "job": named,
                    "params": each.params or params,
                    "id": run_address(each.id, label)
                    if each.id
                    else f"{label}/{named.name}@{each.seed if each.seed is not None else named.seeds[0]}",
                    "seed": each.seed if each.seed is not None else named.seeds[0],
                }
            )
        )
    return expand_runs(normalized, params=params)


def find_training_run(request: RunRequest, state: ExperimentState) -> TrainingRunSummary | None:
    """Find a declared address, including stored compact job/seed spellings."""
    return next(
        (
            run
            for run in state.runs
            if (
                request.id is not None
                and run.name == request.id.removeprefix(state.experiment + "/")
            )
            or (run.launch == request.job.name and run.seed == request.seed)
        ),
        None,
    )


def format_known_run(request: RunRequest, prior: TrainingRunSummary) -> str:
    """The pass's line for a known run."""
    line = f"{request.id}  {prior.state}"
    if prior.state in {"failed", "stopped"}:
        line += f"; --job {request.job.name} --seed {request.seed} --resume"
    return line


def select_runs(
    runs: Sequence[RunRequest], *, job: str | None = None, seed: int | None = None
) -> list[RunRequest]:
    """Select declared jobs and seeds without changing their addresses."""
    selected = [
        run
        for run in runs
        if (job is None or run.job.name == job) and (seed is None or run.seed == seed)
    ]
    if not selected:
        raise LauncherError(
            "no runs match --job and --seed; select a job and seed defined in training_jobs()"
        )
    return selected


type EvalFunction = Callable[[ExperimentState], object]


@dataclass(frozen=True)
class Experiment:
    name: str
    eval: EvalFunction | None
    file: Path | None = None
    training: Callable[..., object] | None = None


def _namespace(path: Path) -> dict[str, Any]:
    # As `python job.py` would, so the file can import its neighbours: `from train import train`.
    folder = str(path.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return runpy.run_path(str(path.resolve()))


def read_experiment(path: Path, *, experiment: str | None = None) -> Experiment:
    if path.suffix != ".py":
        raise LauncherError("experiment files must be Python (.py)")
    refuse_renamed(path)
    namespace = _namespace(path)
    found = namespace.get("eval_jobs")
    if found is not None and not callable(found):
        raise LauncherError(f"{path.name}: eval_jobs must be a function")
    if found is None and not callable(namespace.get("training_jobs")):
        raise LauncherError(f"{path.name}: define training_jobs() or eval_jobs(state)")
    return Experiment(
        name=experiment or path.stem,
        eval=cast(EvalFunction | None, found),
        file=path,
        training=cast(Callable[..., object] | None, namespace.get("training_jobs")),
    )


def asked(function: EvalFunction, state: ExperimentState) -> list[EvalJob]:
    returned = function(state)
    given = list(returned) if isinstance(returned, (list, tuple)) else [returned]
    jobs: list[EvalJob] = []
    for each in given:
        if not isinstance(each, EvalJob):
            defining = function if inspect.isroutine(function) else type(function)
            raise LauncherError(
                f"{Path(inspect.getfile(defining)).name}: eval_jobs() must return EvalJob"
            )
        jobs.append(each)
    return jobs
