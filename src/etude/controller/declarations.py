"""Execute experiment files and validate their typed training and eval declarations.

The file supplies the experiment's default name and expands all declarations
before job/seed selection. An explicit experiment override wins. Explicit
RunRequests preserve their ids; Jobs expand to full experiment/job@seed addresses.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import typer

from etude.controller.diagnostics import declaration
from etude.errors import EtudeError, LauncherError
from etude.experiment import observe
from etude.experiment_file import execute_file, git_checkout, refuse_renamed
from etude.identity import run_address
from etude.jobs import EvalJob, ExperimentState, TrainingJob, TrainingRunSummary, evaluation_group
from etude.report import Report
from etude.spec import Job, RunRequest, declared_address, expand_runs
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
    if function is None:
        function = read_experiment(path).training
    if not callable(function):
        raise LauncherError(
            f"{path.name}: define training_jobs() returning jobs or run requests",
            fix="def training_jobs():\n    return TrainingJob(...)",
        )
    with declaration(path, function):
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
        "format",
        "resume",
        "until",
        "job",
        "seed",
    }
    arguments: dict[str, Any] = {}
    values: list[Any] = []

    def invoke(**kwargs: Any) -> None:
        with declaration(path, function):
            arguments.update(kwargs)
            observed = state
            if "state" in signature.parameters and observed is None:
                observed = observe(default_root(), experiment or path.stem)
            values.append(
                function(
                    **kwargs, **({"state": observed} if "state" in signature.parameters else {})
                )
            )

    parameters = []
    for name, param in signature.parameters.items():
        if name == "state":
            continue
        if name in reserved or "no_" + name in reserved:
            raise LauncherError(
                f"{path.name}: training_jobs() parameter {name!r} clashes with launcher option --{name.replace('_', '-')}",
                fix=f"rename the parameter to train_{name}",
                at=function,
            )
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise LauncherError(
                f"{path.name}: training_jobs() parameter {name!r} must be a named parameter",
                fix=f"declare {name} as a named parameter",
                at=function,
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
        raise LauncherError(
            f"{path.name}: training_jobs() must return TrainingJob",
            fix="return TrainingJob(name='base', train=train, config=Config(), max_hours=1)",
            at=function,
        )
    with declaration(path, function):
        plain = [each.plain() if isinstance(each, Job) else each.job for each in returned]
    jobs: list[Job] = []
    for candidate in plain:
        if not local:
            with declaration(path, function):
                candidate.require_hours()
            require_checkout(candidate, function)
        if candidate not in jobs:
            jobs.append(candidate)
    names = [job.name for job in jobs]
    if len(names) != len(set(names)):
        raise LauncherError(
            f"{path.name}: jobs need distinct names",
            fix="give each TrainingJob a different name=",
            at=function,
        )
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    normalized: list[Job | RunRequest] = []
    for each, job in zip(returned, plain, strict=True):
        if not job.name:
            raise LauncherError(
                f"{path.name}: TrainingJob requires name=", fix='name="base"', at=function
            )
        label = experiment or job.experiment or path.stem
        named = job.model_copy(
            update={
                "repo": str(Path(job.repo).resolve()) if job.repo else None,
                "experiment": label,
            }
        )
        if isinstance(each, Job):
            normalized.append(named)
            continue
        seed = each.seed if each.seed is not None else named.seeds[0]
        address = (
            run_address(each.id, label) if each.id else declared_address(label, named.name, seed)
        )
        normalized.append(
            each.model_copy(
                update={"job": named, "params": each.params or params, "id": address, "seed": seed}
            )
        )
    return expand_runs(normalized, params=params)


def require_checkout(job: Job, function: Callable[..., object]) -> None:
    if job.repo is not None and git_checkout(Path(job.repo)) is None:
        raise LauncherError(
            f"{job.repo} is in no git checkout",
            fix="put the experiment in a Git checkout or set repo= to its checkout",
            at=function,
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


def with_queued_runs(state: ExperimentState, requests: Sequence[RunRequest]) -> ExperimentState:
    """Make this pass's planned training runs visible to eval_jobs, before any save."""
    queued = []
    for request in requests:
        assert request.id is not None  # Declarations have assigned addresses.
        name = request.id.removeprefix(state.experiment + "/")
        queued.append(
            TrainingRunSummary.queued(name, state.experiment, request.job.name, request.seed)
        )
    return replace(state, runs=(*state.runs, *queued))


type EvalFunction = Callable[[ExperimentState], object]


@dataclass(frozen=True)
class Experiment:
    name: str
    eval: EvalFunction | None
    file: Path | None = None
    training: Callable[..., object] | None = None
    report: Callable[[], object] | None = None
    train: Callable[..., object] | None = None


def read_experiment(path: Path, *, experiment: str | None = None) -> Experiment:
    if path.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422) from ValueError(path)
    if not path.is_file():
        raise EtudeError("experiment_file_not_found", 404) from FileNotFoundError(path)
    with declaration(path):
        refuse_renamed(path)
        namespace = execute_file(path)
        found = namespace.get("eval_jobs")
        training = namespace.get("training_jobs")
        report = namespace.get("report")
        train = namespace.get("train")
        for name, value, signature in (
            ("training_jobs", training, "training_jobs()"),
            ("eval_jobs", found, "eval_jobs(state)"),
            ("train", train, "train(config: Config) -> None"),
        ):
            if value is not None and not callable(value):
                raise LauncherError(
                    f"{path.name}: {name} must be a function", fix=f"def {signature}:"
                )
        if report is not None and not callable(report):
            raise EtudeError(
                "experiment_report_is_not_a_function", 422, fix="def report() -> Report:"
            )
    return Experiment(
        name=experiment or path.stem,
        eval=cast(EvalFunction | None, found),
        file=path,
        training=training if callable(training) else None,
        report=report if callable(report) else None,
        train=train if callable(train) else None,
    )


def declared_report(experiment: Experiment) -> Report | None:
    if experiment.report is None:
        return None
    assert experiment.file is not None
    with declaration(experiment.file, experiment.report):
        report = experiment.report()
        if not isinstance(report, Report):
            raise EtudeError(
                "experiment_report_returns_a_report",
                422,
                fix="return Report()",
            ) from TypeError(f"{experiment.file.name}: report() returned a {type(report).__name__}")
    return report


def require_jobs(experiment: Experiment) -> None:
    if experiment.training is None and experiment.eval is None:
        assert experiment.file is not None
        raise LauncherError(
            f"{experiment.file.name}: define training_jobs() or eval_jobs(state)",
            fix="def training_jobs():\n    return TrainingJob(...)",
        )


def asked(function: EvalFunction, state: ExperimentState) -> list[EvalJob]:
    defining = function if inspect.isroutine(function) else type(function)
    path = Path(inspect.getfile(inspect.unwrap(defining)))
    with declaration(path, function):
        returned = function(state)
    given = list(returned) if isinstance(returned, (list, tuple)) else [returned]
    jobs: list[EvalJob] = []
    for each in given:
        if not isinstance(each, EvalJob):
            raise LauncherError(
                f"{path.name}: eval_jobs() must return EvalJob",
                fix="return EvalJob(tournament=tournament, play=play, max_hours=1)",
                at=function,
            )
        jobs.append(each)
    return jobs
