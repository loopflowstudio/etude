"""Validate a Python experiment's declarations against offline state.

Each declaration stage runs once. Training and evaluation use the runtime's
readers with explicit state, so the launcher and its storage stay outside
this path.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from contextlib import nullcontext, redirect_stdout
from decimal import Decimal
from pathlib import Path
from typing import Literal

import typer
from pydantic import computed_field

from etude.call import changed_config, config_type
from etude.controller.declarations import (
    asked,
    declared_report,
    read_experiment,
    read_training_jobs,
    require_jobs,
    with_queued_runs,
)
from etude.controller.diagnostics import Issue, declaration
from etude.controller.game_imports import GameImport, check_game_imports
from etude.errors import EtudeError, LauncherError, SourceLocation, function_location
from etude.eval.records import RoundRobin
from etude.experiment import TrainingRuns, read_tournament_lines, state_of
from etude.jobs import Evaluation, ExperimentState, evaluation_group
from etude.records import Record
from etude.run import RunListing
from etude.spec import Job
from etude.storage import DirectoryStorage
from etude.train.read import read_all


class PlannedRun(Record):
    address: str
    max_hours: float
    max_usd: Decimal | None


class PlannedTournament(Record):
    name: str
    kind: Literal["round_robin", "benchmark"]
    entrants: int | None = None
    checkpoints: int | None = None
    matches: int
    games: int
    max_hours: float
    max_usd: Decimal | None


class Check(Record):
    file: Path
    experiment: str
    issues: tuple[Issue, ...] = ()
    training: tuple[PlannedRun, ...] = ()
    evals: tuple[PlannedTournament, ...] = ()
    source: Path | None = None
    local_recipe: bool = False
    eval_skipped: bool = False

    @computed_field  # type: ignore[prop-decorator]  # Pydantic serializes this property.
    @property
    def ok(self) -> bool:
        return all(issue.severity != "error" for issue in self.issues)


def check_file(
    file: Path,
    given: Sequence[str] = (),
    *,
    experiment: str | None = None,
    source: Path | None = None,
) -> Check:
    """Read declarations, validate them independently, and prepare no work."""
    name = experiment or file.stem
    try:
        definition = read_experiment(file, experiment=name)
        if all(each is None for each in (definition.train, definition.report)):
            require_jobs(definition)
    except (EtudeError, LauncherError) as error:
        return Check(
            file=file, experiment=name, source=source, issues=(Issue.from_error(error, file),)
        )

    if given and definition.training is None:
        raise typer.BadParameter("file has no training_jobs() options")
    issues: list[Issue] = []
    training: list[PlannedRun] = []
    evals: list[PlannedTournament] = []
    listing = RunListing.read(DirectoryStorage(source)) if source is not None else None
    state = (
        ExperimentState(experiment=name)
        if listing is None
        else state_of(read_all(listing, name, metrics=frozenset()), name, retained=())
    )
    recipe = definition.train
    local_recipe = recipe is not None and definition.training is None and definition.eval is None
    try:
        if definition.training is not None:
            requests = read_training_jobs(
                file, given, experiment=definition.name, state=state, function=definition.training
            )
            known = TrainingRuns(state.runs, requested=(request.id for request in requests))
            requests = [
                request
                for request in requests
                if known.find(request.id, request.job, request.seed) is None
            ]
            for request in requests:
                assert request.id is not None  # Declarations have assigned addresses.
                training.append(
                    PlannedRun(
                        address=request.id,
                        max_hours=request.job.require_hours(),
                        max_usd=request.job.max_usd,
                    )
                )
            state = with_queued_runs(state, requests)
        elif recipe is not None and local_recipe:
            with declaration(file, recipe):
                config = config_type(recipe)
                default = changed_config(config, "{}")
                config.model_validate_json(default.model_dump_json(round_trip=True))
    except (EtudeError, LauncherError) as error:
        issues.append(Issue.from_error(error, file))

    games: list[GameImport] = []
    eval_skipped = False
    if definition.eval is not None:
        try:
            with declaration(file, definition.eval):
                jobs = asked(definition.eval, state)
                lines = (
                    state.lines
                    if listing is None
                    else read_tournament_lines(
                        listing, state, (job.recorded() for job in jobs), include_standing=False
                    )
                )
                for job in jobs:
                    lowered = job.lower(state, lines=lines)
                    (spec,) = evaluation_group(lowered).evaluations
                    evals.append(_plan_tournament(lowered, spec))
                    if spec.agents.load is not None and spec.agents.devices != ("cpu",):
                        function = job.play or job.play_many
                        assert function is not None  # A lowered job has one game function.
                        games.append(
                            GameImport(
                                file=file,
                                location=function_location(function) or SourceLocation(file, 1),
                                checkout=lowered.repo,
                                play=spec.play,
                                build=spec.agents.build,
                                streaming=spec.streaming,
                            )
                        )
        except EtudeError as error:
            if issues and error.code in {"run_not_found", "policy_not_found"}:
                eval_skipped = True
            else:
                issues.append(Issue.from_error(error, file))
        except LauncherError as error:
            issues.append(Issue.from_error(error, file))
    try:
        declared_report(definition)
    except (EtudeError, LauncherError) as error:
        issues.append(Issue.from_error(error, file))
    for issue in check_game_imports(games):
        if issue not in issues:
            issues.append(issue)
    return Check(
        file=file,
        experiment=name,
        issues=tuple(issues),
        training=tuple(training),
        evals=tuple(evals),
        source=source,
        local_recipe=local_recipe,
        eval_skipped=eval_skipped,
    )


def _plan_tournament(job: Job, spec: Evaluation) -> PlannedTournament:
    held = spec.tournament
    # Several seatings can describe the same match.
    matches = {match.key: match for match in spec.matches}
    robin = isinstance(held.tournament, RoundRobin)
    return PlannedTournament(
        name=held.tournament.name,
        kind="round_robin" if robin else "benchmark",
        entrants=len({player.key for player in held.entrants}) if robin else None,
        checkpoints=None if robin else len({ref.key for ref in held.checkpoints}),
        matches=len(matches),
        games=sum(match.games for match in matches.values()),
        max_hours=job.require_hours(),
        max_usd=job.max_usd,
    )


def show_check(
    file: Path,
    given: Sequence[str],
    *,
    experiment: str | None,
    source: str | None,
    format: Literal["text", "json"],
) -> int:
    if file.suffix != ".py" or not file.is_file():
        typer.echo(f"{file}: expected an existing Python (.py) file", err=True)
        return 2
    directory = None
    if source is not None:
        if "://" in source:
            typer.echo(
                f"etude check: --source needs a local directory. "
                f"Use etude launch {shlex.quote(str(file))} --quote for bucket storage.",
                err=True,
            )
            return 2
        directory = Path(source)
        if not directory.is_dir():
            typer.echo(f"{directory}: --source needs an existing directory", err=True)
            return 2
    with redirect_stdout(sys.stderr) if format == "json" else nullcontext():
        checked = check_file(file, given, experiment=experiment, source=directory)
    if format == "json":
        print(checked.model_dump_json())
    else:
        _show_text(checked)
    return 0 if checked.ok else 1


def _show_text(checked: Check) -> None:
    file = checked.file
    for issue in checked.issues:
        try:
            path = issue.file.resolve().relative_to(Path.cwd())
        except ValueError:
            path = issue.file
        print(f"{path}:{issue.line}: {issue.severity}: {issue.message}")
        if issue.fix:
            print(f"  fix: {issue.fix}")
    if checked.eval_skipped:
        print("eval_jobs: not checked until training_jobs passes")
    if checked.issues:
        errors = sum(issue.severity == "error" for issue in checked.issues)
        warnings = len(checked.issues) - errors
        counts = [_count(n, label) for n, label in ((errors, "error"), (warnings, "warning")) if n]
        print(f"{file}: {', '.join(counts)}")
    if not checked.ok:
        return
    if checked.local_recipe:
        print(f"{file}: ok, local recipe: etude train {file}; nothing to launch")
        return
    if checked.source is not None:
        print(f"{file}: ok, from local runs in {checked.source}")
    else:
        print(f"{file}: ok, as a first launch sees it (no stored runs)")
    if checked.training:
        hours = sum(run.max_hours for run in checked.training)
        dollars = sum((run.max_usd or Decimal(0) for run in checked.training), Decimal(0))
        covered = sum(run.max_usd is not None for run in checked.training)
        total = f"training  {_count(len(checked.training), 'run')}, up to {hours:g} machine-hours"
        if covered:
            total += f", ${dollars:.2f}"
            if covered < len(checked.training):
                total += f" across {_count(covered, 'run')} with max_usd"
        print(total)
        for run in checked.training:
            print(f"  {run.address}  {_budget(run.max_hours, run.max_usd)}")
    if checked.evals:
        print("eval")
        for tournament in checked.evals:
            selected = (
                f"round robin  {_count(tournament.entrants, 'entrant')}"
                if tournament.entrants is not None
                else f"benchmark  {_count(tournament.checkpoints or 0, 'checkpoint')}"
            )
            schedule = _count(tournament.matches, "match", plural="matches")
            if tournament.matches:
                schedule += f"  {_count(tournament.games, 'game')}"
            elif tournament.checkpoints == 0:
                schedule += "  (none stored yet)"
            print(
                f"  {tournament.name}  {selected}  {schedule}"
                f"  {_budget(tournament.max_hours, tournament.max_usd)}"
            )
    if checked.training or checked.evals:
        command = ["etude", "launch", str(file), "--quote"]
        if checked.experiment != file.stem:
            command.extend(("--experiment", checked.experiment))
        print(f"next: {shlex.join(command)}")


def _budget(hours: float, usd: Decimal | None) -> str:
    cap = f"max_usd {usd:.2f}" if usd is not None else "no max_usd: owned pools only"
    return f"max_hours {hours:g}  {cap}"


def _count(number: int, noun: str, *, plural: str | None = None) -> str:
    name = noun if number == 1 else plural or noun + "s"
    return f"{number} {name}"
