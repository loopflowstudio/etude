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

from etude.errors import LauncherError
from etude.identity import run_address
from etude.spec import Job, RunRequest


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
        raise LauncherError(f"{path.name}: define runs() returning jobs or run requests")
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
        "job",
        "seed",
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
    returned = values[0]
    returned = list(returned) if isinstance(returned, (list, tuple)) else [returned]
    if not returned or not all(isinstance(each, (Job, RunRequest)) for each in returned):
        raise LauncherError(f"{path.name}: runs() must return jobs or run requests")
    jobs = [each if isinstance(each, Job) else each.job for each in returned]
    names = [job.name or path.stem for job in jobs]
    if len(names) != len(set(names)):
        raise LauncherError(f"{path.name}: jobs need distinct names")
    runs: list[RunRequest] = []
    for each in returned:
        if isinstance(each, RunRequest):
            runs.append(each)
        else:
            # Cardinality belongs to the complete file, before any selection.
            name = (experiment or each.experiment or path.stem) if len(jobs) == 1 else each.name
            if not name:
                raise LauncherError(f"{path.name}: name each job when defining several")
            runs.extend(each.model_copy(update={"name": name}).runs())
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    return [
        run.model_copy(
            update={
                "params": run.params or params,
                "id": run_address(run.id, experiment or run.job.experiment or path.stem)
                if run.id
                else None,
                "job": run.job.model_copy(
                    update={
                        "repo": str(Path(run.job.repo).resolve()) if run.job.repo else None,
                        "experiment": experiment or run.job.experiment or path.stem,
                        "name": jobs[0].name or path.stem if len(jobs) == 1 else run.job.name,
                    }
                ),
            }
        )
        for run in runs
    ]


def select_runs(
    runs: Sequence[RunRequest], *, job: str | None = None, seed: int | None = None
) -> list[RunRequest]:
    """Select declared jobs and seeds without changing their addresses."""
    selected = [run for run in runs if (job is None or run.job.name == job)
                and (seed is None or run.seed == seed)]
    if not selected:
        raise LauncherError("no runs match --job and --seed; select a job and seed defined in runs()")
    return selected
