"""Read the declarations of one experiment without loading the launcher or evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from etude.controller.diagnostics import declaration
from etude.errors import EtudeError, LauncherError, SourceLocation
from etude.experiment_file import execute_file
from etude.report import Report

if TYPE_CHECKING:
    from etude.jobs import ExperimentState


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


def require_jobs(experiment: Experiment) -> None:
    if experiment.training is None and experiment.eval is None:
        assert experiment.file is not None
        raise LauncherError(
            "define training_jobs() or eval_jobs(state)",
            fix="def training_jobs():\n    return TrainingJob(...)",
            at=SourceLocation(experiment.file, 1),
        )
