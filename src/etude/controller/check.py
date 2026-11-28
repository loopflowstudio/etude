"""Validate a Python experiment's declarations against offline state.

Each declaration stage runs once. Training and evaluation use the runtime's
readers with explicit state, so the launcher and its storage stay outside
this path.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import typer
from typer._click.exceptions import UsageError

from etude.call import changed_config, config_type
from etude.controller.declarations import (
    asked,
    declared_report,
    read_experiment,
    read_training_jobs,
    require_jobs,
    with_queued_runs,
)
from etude.controller.diagnostics import declaration, locate
from etude.errors import EtudeError, LauncherError, sanitize
from etude.jobs import ExperimentState
from etude.records import Record


class Issue(Record):
    file: Path
    line: int
    message: str
    fix: str | None = None


class Check(Record):
    file: Path
    issues: tuple[Issue, ...] = ()
    local_recipe: bool = False
    eval_skipped: bool = False


def _issue(error: EtudeError | LauncherError, file: Path) -> Issue:
    location = locate(error, file)
    return Issue(file=location.file, line=location.line, message=str(error), fix=error.fix)


def check_file(file: Path, given: Sequence[str] = (), *, experiment: str | None = None) -> Check:
    """Read declarations, validate them independently, and prepare no work."""
    try:
        definition = read_experiment(file, experiment=experiment)
        if all(each is None for each in (definition.train, definition.report)):
            require_jobs(definition)
    except (EtudeError, LauncherError) as error:
        return Check(file=file, issues=(_issue(error, file),))

    if given and definition.training is None:
        raise typer.BadParameter("file has no training_jobs() options")
    issues: list[Issue] = []
    state = ExperimentState(experiment=definition.name)
    recipe = definition.train
    local_recipe = recipe is not None and definition.training is None and definition.eval is None
    try:
        if definition.training is not None:
            requests = read_training_jobs(
                file, given, experiment=definition.name, state=state, function=definition.training
            )
            state = with_queued_runs(state, requests)
        elif recipe is not None and local_recipe:
            with declaration(file, recipe):
                config = config_type(recipe)
                default = changed_config(config, "{}")
                config.model_validate_json(default.model_dump_json(round_trip=True))
    except (EtudeError, LauncherError) as error:
        issues.append(_issue(error, file))

    eval_skipped = False
    if definition.eval is not None:
        try:
            with declaration(file, definition.eval):
                for job in asked(definition.eval, state):
                    job.lower(state, lines=state.lines)
        except EtudeError as error:
            if issues and error.code in {"run_not_found", "policy_not_found"}:
                eval_skipped = True
            else:
                issues.append(_issue(error, file))
        except LauncherError as error:
            issues.append(_issue(error, file))
    try:
        declared_report(definition)
    except (EtudeError, LauncherError) as error:
        issues.append(_issue(error, file))
    return Check(
        file=file, issues=tuple(issues), local_recipe=local_recipe, eval_skipped=eval_skipped
    )


def show_check(file: Path, given: Sequence[str], experiment: str | None) -> int:
    if file.suffix != ".py" or not file.is_file():
        typer.echo(f"{file}: expected an existing Python (.py) file", err=True)
        return 2
    try:
        checked = check_file(file, given, experiment=experiment)
    except UsageError as error:
        typer.echo(f"etude check: {error.format_message()}", err=True)
        return 2
    for issue in checked.issues:
        try:
            path = issue.file.resolve().relative_to(Path.cwd())
        except ValueError:
            path = issue.file
        print(f"{path}:{issue.line}: error: {sanitize(issue.message)}")
        if issue.fix:
            print(f"  fix: {sanitize(issue.fix)}")
    if checked.eval_skipped:
        print("eval_jobs: not checked until training_jobs passes")
    if checked.issues:
        count = len(checked.issues)
        print(f"{file}: {count} {'error' if count == 1 else 'errors'}")
        return 1
    if checked.local_recipe:
        print(f"{file}: ok, local recipe: etude train {file}; nothing to launch")
    else:
        print(f"{file}: ok, as a first launch sees it (no stored runs)")
    return 0
