"""Read a Python job file and validate its runs() flags before submission.

The file supplies the experiment's default name and expands all declarations
before job/seed selection. An explicit experiment override wins. Explicit
RunRequests preserve their ids; Jobs use the shared compact address expansion.
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
from etude.spec import Job, RunRequest, expand_runs


def read_job_file(
    path: Path, given: Sequence[str] = (), *, help: bool = False, experiment: str | None = None
) -> list[RunRequest]:
    """Let Typer validate runs()'s signature, then invoke it once with typed flags.

    Default unnamed jobs to the file's stem; qualify them before expanding seeds.
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
    jobs: list[Job] = []
    for each in returned:
        candidate = each if isinstance(each, Job) else each.job
        if candidate not in jobs:
            jobs.append(candidate)
    names = [job.name or path.stem for job in jobs]
    if len(names) != len(set(names)):
        raise LauncherError(f"{path.name}: jobs need distinct names")
    params = {
        name: each for name, each in arguments.items() if isinstance(each, (str, int, float, bool))
    }
    normalized: list[Job | RunRequest] = []
    for each in returned:
        job = each if isinstance(each, Job) else each.job
        if len(jobs) > 1 and not job.name:
            raise LauncherError(f"{path.name}: name each job when defining several")
        label = experiment or job.experiment or path.stem
        named = job.model_copy(
            update={
                "repo": str(Path(job.repo).resolve()) if job.repo else None,
                "experiment": label,
                "name": job.name or path.stem,
            }
        )
        normalized.append(
            named
            if isinstance(each, Job)
            else each.model_copy(
                update={
                    "job": named,
                    "params": each.params or params,
                    "id": run_address(each.id, label) if each.id else None,
                }
            )
        )
    return expand_runs(normalized, params=params)


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
            "no runs match --job and --seed; select a job and seed defined in runs()"
        )
    return selected
