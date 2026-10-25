"""Read a Python job file and validate its runs() flags before submission.

A job file that is also an experiment file, defining `eval(state)` or
`report()` at its top level, names the experiment its runs record under for
its stem (`experiment_named`), so `ppo.py` launches runs of experiment `ppo`.
"""

from __future__ import annotations

import ast
import inspect
import runpy
import sys
import typing
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from etude.launch.errors import LauncherError
from etude.launch.records import Job, Run

# What makes a Python file an experiment's: a top-level eval(state) or report().
EXPERIMENT_FUNCTIONS = ("eval", "report")


def experiment_named(path: Path) -> str | None:
    """The experiment a file is, its stem, when it defines one of `EXPERIMENT_FUNCTIONS`.

    Recognised by its text, without running it; a file that does not parse is no experiment.
    """
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except SyntaxError:
        return None
    defined = {
        each.name for each in tree.body if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return path.stem if defined & set(EXPERIMENT_FUNCTIONS) else None


def read_job_file(path: Path, given: Sequence[str] = (), *, help: bool = False) -> list[Run]:
    """Let Typer validate runs()'s signature, then invoke it once with typed flags.

    An experiment file's runs are given its experiment; a job naming another is refused.
    """

    if path.suffix != ".py":
        raise LauncherError("job files must be Python (.py), defining runs()")
    # As `python job.py` would, so the file can import its neighbours: `from train import train`.
    folder = str(path.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    function = runpy.run_path(str(path.resolve())).get("runs")
    if not callable(function):
        raise LauncherError(f"{path.name}: define runs() returning a Job, a Run, or a list of Runs")
    signature = inspect.signature(function)
    hints = typing.get_type_hints(function)
    reserved = {"pool", "cohort", "parallel", "max_usd", "help"}
    arguments: dict[str, Any] = {}
    values: list[Any] = []

    def invoke(**kwargs: Any) -> None:
        arguments.update(kwargs)
        try:
            values.append(function(**kwargs))
        except Exception as error:
            hint = (
                ". Records take named fields, such as Run(job=..., seed=1)"
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
    if isinstance(value, Job):
        runs = [Run(job=value)]
    elif isinstance(value, Run):
        runs = [value]
    elif isinstance(value, list) and value and all(isinstance(run, Run) for run in value):
        runs = value
    else:
        raise LauncherError(
            f"{path.name}: runs() must return a Job, a Run, or a nonempty list of Runs"
        )
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    experiment = experiment_named(path)
    for run in runs:
        if experiment is not None and run.job.experiment not in (None, experiment):
            raise LauncherError(
                f"{path.name} is experiment {experiment!r}; its runs() returned a job of"
                f" experiment {run.job.experiment!r}"
            )
    return [
        run.model_copy(
            update={
                "params": run.params or params,
                "job": run.job.model_copy(
                    update={
                        "repo": str(Path(run.job.repo).resolve()) if run.job.repo else None,
                        "experiment": experiment or run.job.experiment,
                    }
                ),
            }
        )
        for run in runs
    ]
