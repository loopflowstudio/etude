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
from typing import cast

import typer

from etude.controller.diagnostics import declaration
from etude.errors import EtudeError, LauncherError
from etude.experiment import observe
from etude.experiment_file import execute_file, git_checkout
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
        job_app = _training_command(path, function, signature)
    parsed = job_app(
        args=["--help"] if help else list(given),
        prog_name=f"{path.name} parameters",
        standalone_mode=False,
    )
    if help:
        return []
    # Outside help, Typer returns the options callback's typed arguments.
    arguments = cast(dict[str, object], parsed)
    params = {
        name: value
        for name, value in arguments.items()
        if isinstance(value, (str, int, float, bool))
    }
    with declaration(path, function):
        if "state" in signature.parameters:
            arguments["state"] = (
                state if state is not None else observe(default_root(), experiment or path.stem)
            )
        returned = function(**arguments)
        items = returned if isinstance(returned, (list, tuple)) else [returned]
        normalized: list[Job | RunRequest] = []
        jobs: dict[str, Job] = {}
        for each in items:
            job = _training_job(each, path, function)
            if not local:
                job.require_hours()
                require_checkout(job, function)
            if jobs.setdefault(job.name, job) != job:
                raise LauncherError(
                    f"{path.name}: jobs need distinct names",
                    fix="give each TrainingJob a different name=",
                    at=function,
                )
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
            if not isinstance(each, RunRequest):
                normalized.append(named)
                continue
            seed = each.seed if each.seed is not None else named.seeds[0]
            address = (
                run_address(each.id, label)
                if each.id
                else declared_address(label, named.name, seed)
            )
            normalized.append(
                each.model_copy(
                    update={
                        "job": named,
                        "params": each.params or params,
                        "id": address,
                        "seed": seed,
                    }
                )
            )
        return expand_runs(normalized, params=params)


def _training_command(
    path: Path, function: Callable[..., object], signature: inspect.Signature
) -> typer.Typer:
    """Parse the authored options with Typer. The caller supplies experiment state separately."""
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

    def options(**kwargs: object) -> dict[str, object]:
        return kwargs

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
        options, "__signature__", signature.replace(parameters=parameters, return_annotation=None)
    )
    options.__doc__ = function.__doc__
    job_app = typer.Typer(
        rich_markup_mode=None, pretty_exceptions_enable=False, add_completion=False
    )
    job_app.command()(options)
    return job_app


def _training_job(value: object, path: Path, function: Callable[..., object]) -> Job:
    if isinstance(value, TrainingJob):
        return value.plain()
    if isinstance(value, RunRequest) and " -m etude.call " in value.job.command:
        try:
            evaluation_group(value.job)
        except EtudeError:
            return value.job
    raise LauncherError(
        f"{path.name}: training_jobs() must return TrainingJob",
        fix="return TrainingJob(name='base', train=train, config=Config(), max_hours=1)",
        at=function,
    )


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
    """Read declarations, using top-level `train` only as a standalone training recipe."""
    if path.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422, detail=str(path))
    if not path.is_file():
        raise EtudeError("experiment_file_not_found", 404, detail=str(path))
    with declaration(path):
        namespace = execute_file(path)
        evaluate = namespace.get("eval_jobs")
        training = namespace.get("training_jobs")
        report = namespace.get("report")
        train = namespace.get("train") if training is None else None
        for name, value, signature in (
            ("training_jobs", training, "training_jobs()"),
            ("eval_jobs", evaluate, "eval_jobs(state)"),
        ):
            if value is not None and not callable(value):
                raise LauncherError(
                    f"{path.name}: {name} must be a function", fix=f"def {signature}:"
                )
        if report is not None and not callable(report):
            raise EtudeError(
                "experiment_report_is_not_a_function",
                422,
                detail=f"report has type {type(report).__name__}",
                fix="def report() -> Report:",
            )
        if train is not None and not callable(train) and evaluate is None and report is None:
            raise LauncherError(
                f"{path.name}: train must be a function",
                fix="def train(config: Config) -> None:",
            )
    return Experiment(
        name=experiment or path.stem,
        eval=cast(EvalFunction | None, evaluate),
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
                detail=f"report() returned {type(report).__name__}",
                fix="return Report()",
            )
    return report


def require_training_options(
    given: Sequence[str], experiment: Experiment, *, read_training: bool = True
) -> None:
    """File options must reach the training declaration for this invocation."""
    if not given:
        return
    if experiment.training is None:
        raise typer.BadParameter("file has no training_jobs() options", param_hint=given[0])
    if not read_training:
        raise typer.BadParameter("this invocation skips training_jobs()", param_hint=given[0])


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
