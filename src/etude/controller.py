"""Reconcile an experiment file against its stored results and scheduler records.

`advance` reads one state, declares training then evaluation, and hands down
new runs. The scheduler's public records are the only memory of a hand-down.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from etude.declarations import (
    Experiment,
    asked,
    find_training_run,
    format_known_run,
    read_training_jobs,
    select_runs,
)
from etude.errors import EtudeError, LauncherError
from etude.eval.records import Line, TournamentRun, digest
from etude.eval.resources import available_cpus
from etude.eval.result import EvalResult
from etude.eval.tournament import StoredRuns, played
from etude.experiment import observe, read_tournament_lines
from etude.jobs import (
    Checkpoints,
    EvalGroup,
    Evaluation,
    ExperimentState,
    TrainingRunSummary,
    _group_key,
    _relocated,
    evaluation_group,
    fetch_checkpoints,
    group_evaluations,
    hold_group,
    with_experiment,
)
from etude.launch import client
from etude.launch.commands import DEFAULT_MIN_EVAL_HOURS
from etude.launch.listing import show_quote
from etude.launch.records import CPU, GPU
from etude.launch.server import selected
from etude.run import located_runs
from etude.smoke import smoke_file
from etude.spec import Job, RunRequest, Smoke, Until
from etude.storage import Storage, default_root


@dataclass
class TournamentDecision:
    """What one pass found and did for a tournament; filled in as the pass decides."""

    tournament: str
    new: int
    running: int
    failed: int = 0
    played: int = 0
    launched: str | None = None
    state: Literal["queued", "running", "would launch", "not queued", "waiting"] | None = None
    reason: str | None = None
    stopped: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PendingEvaluation:
    job: Job
    spec: Evaluation
    decision: TournamentDecision


def source_for(pool: str | None, rent: str | None, source: str | None = None) -> str:
    """The target's delivery storage; ambiguous targets need an explicit source."""
    if source is not None:
        return source
    if selected():
        raise LauncherError("a server launch needs --source")
    scheduler = client.default()
    target = scheduler.default_target() if pool is None and rent is None else None
    pool = pool if target is None else target.pool
    rent = rent if target is None else target.rent
    names: set[str | None] = set()
    if pool is not None:
        names.update(m.storage for m in scheduler.machines(pool))
    if rent is not None:
        ondemand = next((r for r in scheduler.ondemands() if r.name == rent), None)
        if ondemand is None:
            raise LauncherError(f"no on-demand {rent!r}")
        names.add(ondemand.storage)
    uris = {
        default_root()
        if name is None
        else next(r for r in scheduler.storages() if r.name == name).uri
        for name in names
    }
    if len(uris) > 1:
        raise LauncherError("target has several storages; give --source")
    return str(next(iter(uris), default_root()))


def _fits(job: Job, committed: Decimal, max_usd: Decimal | None) -> bool:
    return max_usd is None or committed + (job.max_usd or Decimal(0)) <= max_usd


def _resumed(
    request: RunRequest, experiment: str, until: Until | None
) -> tuple[RunRequest, Decimal]:
    """The request as a resume under its standing record's id, and the dollars that record releases."""
    request = request.model_copy(
        update={
            "resume": True,
            "job": request.job.model_copy(update={"until": until or request.job.until}),
        }
    )
    for item in client.standing(experiment):
        if item.record.launch.spec.name == request.job.name and item.record.seed == request.seed:
            return request.model_copy(update={"id": item.record.id}), item.committed_usd
    return request, Decimal(0)


def _eval_cpus(pool: str | None, rent: str | None) -> int | None:
    """Known target capacity, from local records only; unknown targets stay undivided."""
    if selected():
        return None
    scheduler = client.default()
    if pool is None and rent is None:
        target = scheduler.default_target()
        pool, rent = target.pool, target.rent
    counts: list[int | None] = []
    if pool is not None:
        for machine in scheduler.machines(pool):
            hardware = machine.hardware
            counts.append(
                available_cpus()
                if machine.kind == "local"
                else hardware.vcpus
                if isinstance(hardware, CPU)
                else hardware.min_vcpus
                if isinstance(hardware, GPU)
                else None
            )
    if rent is not None:
        counts.append(
            next((r.template.vcpus for r in scheduler.ondemands() if r.name == rent), None)
        )
    return min(c for c in counts if c is not None) if counts and all(c for c in counts) else None


def _print_lines(lines: dict[str | None, str]) -> None:
    """Print the training lines in declared order, once, and forget them."""
    for line in lines.values():
        print(line)
    lines.clear()


def _queued(request: RunRequest, experiment: str) -> TrainingRunSummary:
    assert request.id is not None
    name = request.id.removeprefix(experiment + "/")
    return TrainingRunSummary(
        name=name,
        experiment=experiment,
        job=request.job.name,
        launch=request.job.name,
        line=name,
        state="queued",
        checkpoints=Checkpoints(),
        seed=request.seed,
    )


def advance(
    experiment: Experiment,
    *,
    source: Storage,
    pool: str | None = None,
    rent: str | None = None,
    directory: Path | str | None = None,
    retry_failed: bool = False,
    min_eval_hours: float = DEFAULT_MIN_EVAL_HOURS,
    smoke: Smoke = Smoke(),
    mode: Literal["local", "launch", "queue"] = "local",
    max_usd: Decimal | None = None,
    quote: bool = False,
    smoke_file_first: bool = False,
    given: Sequence[str] = (),
    job: str | None = None,
    seed: int | None = None,
    resume: bool = False,
    until: Until | None = None,
) -> list[TournamentDecision]:
    """One pass. Existing addresses stay put; explicit retries preserve delivered matches."""
    if max_usd is not None and (not max_usd.is_finite() or max_usd < 0):
        raise LauncherError("--max-usd must be a finite, nonnegative dollar limit")
    if not math.isfinite(min_eval_hours) or min_eval_hours < 0:
        raise LauncherError("--min-eval-hours must be finite and nonnegative")
    listed = source.keys("")
    declarations = located_runs(source, listed)
    state = observe(source, experiment.name, keys=listed, declarations=declarations)
    committed = state.committed_usd
    print(
        f"{experiment.name}: state from {source.uri('run.json').removesuffix('/run.json')}: "
        f"{_count(len(state.runs), 'training run')}, {_count(len(state.eval_runs), 'eval run')}"
        + (f"; ${committed:.2f} of ${max_usd:.2f} committed" if max_usd is not None else "")
    )
    # A training selection or resume is a pass over those runs alone.
    evaluate = job is None and seed is None and not resume and until is None
    new: list[RunRequest] = []
    requests: list[RunRequest] = []
    # One line per declared run, keyed by address; a new run's line waits for its placement.
    lines: dict[str | None, str] = {}
    if mode != "local" and callable(experiment.training):
        assert experiment.file is not None
        requests = read_training_jobs(
            experiment.file,
            given,
            experiment=experiment.name,
            state=state,
            function=experiment.training,
        )
        if job is not None or seed is not None:
            requests = select_runs(requests, job=job, seed=seed)
        if resume and len(requests) != 1:
            raise LauncherError("--resume needs one run; select --job and --seed")
        for request in requests:
            if resume:
                request, released = _resumed(request, experiment.name, until)
                committed -= released
            elif known := format_known_run(request, find_training_run(request, state)):
                lines[request.id] = known
                continue
            if not _fits(request.job, committed, max_usd):
                lines[request.id] = (
                    f"{request.id}  new, not queued: --max-usd {max_usd} (${committed:.2f} committed)"
                )
                continue
            new.append(request)
            lines[request.id] = f"{request.id}  new"
            committed += request.job.max_usd or Decimal(0)
    jobs: list[Job] = []
    if experiment.eval is not None and evaluate:
        # This pass's new runs have no checkpoints yet, but their policies already exist
        # for eval_jobs. Selection/resume passes skip evaluation, so every run here is new.
        asking = replace(state, runs=(*state.runs, *(_queued(r, experiment.name) for r in new)))
        try:
            jobs = [with_experiment(j, experiment.name) for j in asked(experiment.eval, asking)]
        except EtudeError:
            # A policy of a run this pass left out (`--max-usd`) is missing; say which and why.
            _print_lines(lines)
            raise
    policy_lines = read_tournament_lines(
        source,
        state,
        [
            TournamentRun(
                tournament=(spec := evaluation_group(job).evaluations[0]).tournament,
                checkpoints=spec.held,
            )
            for job in jobs
        ],
        keys=listed,
        declarations=declarations,
    )
    if smoke_file_first and not quote:
        assert experiment.file is not None
        before = (
            smoke_file(experiment.file, requests)
            if callable(experiment.training)
            else smoke_file(experiment.file, steps=("eval", "report"), source=source)
        )
        new = before.stamp(new)
        smoke = Smoke(steps=before.rest)
    planned = list(new)
    running = {
        key for run in state.eval_runs if run.state in {"queued", "running"} for key in run.matches
    }
    failed = {
        key: f"{experiment.name}/{run.name}"
        for run in state.eval_runs
        if run.state == "failed"
        for key in run.matches
    }
    taken = {f"{experiment.name}/{run.name}" for run in state.eval_runs}
    decisions: list[TournamentDecision] = []
    stopped_runs: set[str] = set()
    stored = (
        StoredRuns.read(experiment.name, source, keys=listed, declarations=declarations)
        if jobs
        else None
    )
    groups: dict[str, list[_PendingEvaluation]] = {}
    for executable in jobs:
        spec = evaluation_group(executable).evaluations[0]
        found = played(
            spec.tournament,
            experiment=experiment.name,
            checkpoints=spec.held,
            enter=spec.entrants,
            runs=stored,
            lines=policy_lines,
        )
        new_matches = list(
            {m.key: m for m in (*found.new, *(found.failed if retry_failed else ()))}.values()
        )
        pending = [match for match in new_matches if match.key not in running]
        done = TournamentDecision(
            tournament=spec.tournament.name,
            new=len(pending),
            running=len(new_matches) - len(pending),
            # Retryable failures are replayed with the new matches, not counted twice.
            failed=0 if retry_failed else len(found.failed) - len(found.retry),
        )
        retryable = {m.key for m in found.retry}
        stopped = {failed[m.key] for m in pending if m.key in failed and m.key not in retryable}
        if stopped and not retry_failed:
            stopped_runs.update(stopped)
            done.stopped = tuple(sorted(stopped))
            pending = [
                match for match in pending if match.key not in failed or match.key in retryable
            ]
        if pending:
            groups.setdefault(_group_key(executable, spec), []).append(
                _PendingEvaluation(
                    job=executable,
                    spec=spec.model_copy(update={"matches": tuple(pending), "lines": policy_lines}),
                    decision=done,
                )
            )
            running.update(m.key for m in pending)
        decisions.append(done)
    training_active = bool(new) or any(r.state in {"queued", "running"} for r in state.runs)
    cpu_capacity = _eval_cpus(pool, rent) if mode != "local" and groups else None
    for group in groups.values():
        members = [each.spec for each in group]
        executables = [each.job for each in group]
        if mode == "local":
            held = _play_here(members, executables[0], state, policy_lines, source, directory)
            for each, result in zip(group, held, strict=True):
                each.decision.played = result.played
            continue
        executable = group_evaluations(executables, members, state)
        keys = sorted(m.key for spec in members for m in spec.matches or ())
        stem = f"{experiment.name}/evals-{digest(keys)[:12]}"
        identity = stem
        number = 2
        while identity in taken:
            identity = f"{stem}-{number}"
            number += 1
        request = RunRequest(id=identity, job=executable, smoke=smoke)
        estimates = (
            [stored.estimate_seconds(spec.tournament, spec.matches or ()) for spec in members]
            if stored
            else []
        )
        hours = None
        if estimates and all(seconds is not None for seconds in estimates):
            hours = sum(seconds or 0 for seconds in estimates) / (cpu_capacity or 1) / 3600
        held_back: Literal["not queued", "waiting"] | None = None
        reason: str | None = None
        if not _fits(executable, committed, max_usd):
            held_back = "not queued"
            reason = f"--max-usd {max_usd} (${committed:.2f} committed)"
        elif (
            training_active
            and hours is not None
            and hours < min_eval_hours
            and identity not in client.room([*planned, request], pool, rent=rent)
        ):
            held_back = "waiting"
            reason = f"{len(keys)} eval matches, estimated {hours:.2f} machine hours below --min-eval-hours {min_eval_hours:g}; no machine has room and training is running"
        else:
            planned.append(request)
            taken.add(identity)
            committed += executable.max_usd or Decimal(0)
        for each in group:
            each.decision.state, each.decision.reason = held_back, reason
            each.decision.launched = identity if held_back is None else None
    if planned:
        if quote:
            for request in new:
                lines[request.id] = f"{request.id}  new, would launch"
            _print_lines(lines)
            show_quote(client.quote(planned, pool, rent=rent))
            for decision in decisions:
                if decision.launched:
                    decision.state = "would launch"
        else:
            submit = client.queue if mode == "queue" else client.launch
            submit(planned, pool, rent=rent)
            placed: dict[str | None, Literal["queued", "running"]] = {
                item.record.id: item.placement for item in client.standing(experiment.name)
            }
            for request in new:
                lines[request.id] = f"{request.id}  new, {placed[request.id]}"
            for decision in decisions:
                if decision.launched:
                    decision.state = placed[decision.launched]
    _print_lines(lines)
    for decision in decisions:
        _show_tournament(decision)
    if stopped_runs and not quote:
        raise EtudeError("launched_pass_failed", 409, detail=", ".join(sorted(stopped_runs)))
    return decisions


def _play_here(
    specs: Sequence[Evaluation],
    executable: Job,
    state: ExperimentState,
    lines: tuple[Line, ...],
    source: Storage,
    directory: Path | str | None,
) -> tuple[EvalResult, ...]:
    """Play with the same verified artifacts a launched run receives as inputs."""
    with TemporaryDirectory(prefix="etude-eval-") as folder:
        here = fetch_checkpoints(
            tuple(match for spec in specs for match in spec.matches or ()),
            state,
            tuple(checkpoint for spec in specs for checkpoint in spec.checkpoints),
            Path(folder),
        )
        return hold_group(
            EvalGroup(
                evaluations=tuple(
                    spec.model_copy(
                        update={
                            "matches": tuple(_relocated(m, here) for m in spec.matches or ()),
                            "lines": lines,
                        }
                    )
                    for spec in specs
                )
            ),
            checkout=executable.repo,
            directory=directory,
            storages=[source],
        )


def _show_tournament(done: TournamentDecision) -> None:
    if not (done.new or done.running or done.failed):
        print(f"eval {done.tournament}: nothing to play")
        return
    line = f"eval {done.tournament}: {done.new} new, {done.running} running, {done.failed} failed"
    if done.launched:
        line += f", {done.state} {done.launched}"
    elif done.played:
        line += f", {done.played} played"
    elif done.state:
        line += f", {done.state}"
        if done.reason:
            line += f": {done.reason}"
    if done.stopped:
        line += f"; failed runs {', '.join(done.stopped)} (--retry-failed retries them)"
    print(line, flush=True)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
