"""Read a Python job file and validate its runs() flags before submission.

Unnamed jobs record under the file's stem, so `ppo.py` launches runs of
experiment `ppo`. Plain job files preserve explicit job experiments; files
defining `eval(state)` or `report()` require their own experiment identity.
"""

from __future__ import annotations

import inspect
import runpy
import sys
import typing
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from etude.experiment_file import experiment_named
from etude.launch.errors import LauncherError
from etude.launch.records import Job, RunRequest


def read_job_file(
    path: Path, given: Sequence[str] = (), *, help: bool = False, experiment: str | None = None
) -> list[RunRequest]:
    """Let Typer validate runs()'s signature, then invoke it once with typed flags.

    Default unnamed jobs to the file's stem. Experiment files refuse other identities.
    """

    if path.suffix != ".py":
        raise LauncherError("job files must be Python (.py), defining runs()")
    # As `python job.py` would, so the file can import its neighbours: `from train import train`.
    folder = str(path.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    function = runpy.run_path(str(path.resolve())).get("runs")
    if not callable(function):
        raise LauncherError(
            f"{path.name}: define runs() returning a Job, a RunRequest, or a list of Runs"
        )
    signature = inspect.signature(function)
    hints = typing.get_type_hints(function)
    reserved = {
        "pool",
        "rent",
        "cohort",
        "parallel",
        "max_usd",
        "help",
        "experiment",
        "resume",
        "until",
    }
    arguments: dict[str, Any] = {}
    values: list[Any] = []

    def invoke(**kwargs: Any) -> None:
        arguments.update(kwargs)
        try:
            values.append(function(**kwargs))
        except Exception as error:
            hint = (
                ". Records take named fields, such as RunRequest(job=..., seed=1)"
                if isinstance(error, TypeError) and "positional argument" in str(error)
                else ""
            )
            raise LauncherError(f"{path.name}: {type(error).__name__}: {error}{hint}") from error

    parameters = []
    for name, param in signature.parameters.items():
        if name in reserved or "no_" + name in reserved:
            raise LauncherError(
                f"{path.name}: runs() parameter {name!r} clashes with launcher option --{name.replace('_', '-')}"
            )
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise LauncherError(f"{path.name}: runs() parameter {name!r} must be a named parameter")
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
    value = values[0]
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if not values or not all(isinstance(each, (Job, RunRequest)) for each in values):
        raise LauncherError(f"{path.name}: runs() must return jobs or run requests")
    runs = [
        run
        for each in values
        for run in (
            [
                RunRequest(job=each, seed=seed, id=f"{each.name or path.stem}-seed-{seed}")
                for seed in each.seeds
            ]
            if isinstance(each, Job)
            else [each]
        )
    ]
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    required = experiment_named(path)
    if required is not None and experiment not in (None, required):
        raise LauncherError(f"{path.name} is experiment {required!r}")
    for run in runs:
        if required is not None and run.job.experiment not in (None, required):
            raise LauncherError(
                f"{path.name} is experiment {required!r}; its runs() returned a job of"
                f" experiment {run.job.experiment!r}"
            )
    return [
        run.model_copy(
            update={
                "params": run.params or params,
                "job": run.job.model_copy(
                    update={
                        "repo": str(Path(run.job.repo).resolve()) if run.job.repo else None,
                        "experiment": experiment or run.job.experiment or path.stem,
                        "name": run.job.name or path.stem,
                    }
                ),
            }
        )
        for run in runs
    ]
