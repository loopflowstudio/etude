"""Reconcile an experiment file against its stored results and scheduler records.

`advance` reads one state, declares training then evaluation, and hands down
new runs. The scheduler's public records are the only memory of a hand-down.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from etude.controller.declarations import (
    Experiment,
    asked,
    format_known_run,
    read_training_jobs,
    select_runs,
)
from etude.controller.smoke import smoke_file
from etude.errors import EtudeError, LauncherError
from etude.eval.records import digest
from etude.eval.result import EvalResult
from etude.eval.tournament import StoredRuns, played
from etude.experiment import TrainingRuns, observe, read_tournament_lines
from etude.jobs import (
    Checkpoints,
    EvalGroup,
    EvalJob,
    Evaluation,
    ExperimentState,
    TrainingRunSummary,
    _group_key,
    evaluation_group,
    fetch_checkpoints,
    group_evaluations,
    hold_group,
)
from etude.launch import client
from etude.launch.listing import show_quote
from etude.launch.server import selected
from etude.run import RunListing
from etude.spec import Job, RunRequest, Smoke, Until
from etude.storage import Storage, default_root
from etude.view_link import print_view


@dataclass
class TournamentDecision:
    """What one pass found and did for a tournament; filled in as the pass decides."""

    tournament: str
    new: int
    running: int
    failed: int = 0
    played: int = 0
    launched: str | None = None
    state: Literal["queued", "running", "would launch", "would queue", "not queued"] | None = None
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
    """Resume this address and release its previous budget commitment."""
    request = request.model_copy(
        update={
            "resume": True,
            "job": request.job.model_copy(update={"until": until or request.job.until}),
        }
    )
    for item in client.standing(experiment):
        if item.record.id == request.id:
            return request, item.committed_usd
    return request, Decimal(0)


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
    listing = RunListing.read(source)
    state = observe(source, experiment.name, keys=listing.keys, declarations=listing.declarations)
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
    lines: dict[str | None, tuple[str, str | None]] = {}
    if mode != "local" and callable(experiment.training):
        assert experiment.file is not None
        requests = read_training_jobs(
            experiment.file,
            given,
            experiment=experiment.name,
            state=state,
            function=experiment.training,
        )
        known = TrainingRuns(state.runs, requested=(r.id for r in requests))
        if job is not None or seed is not None:
            requests = select_runs(requests, job=job, seed=seed)
        if resume and len(requests) != 1:
            raise LauncherError("--resume needs one run; select --job and --seed")
        for request in requests:
            prior = known.find(request.id, request.job, request.seed)
            if resume:
                if prior and prior.status:
                    request = request.model_copy(update={"id": prior.status.run})
                request, released = _resumed(request, experiment.name, until)
                committed -= released
            elif prior is not None:
                lines[request.id] = format_known_run(request, prior), prior.address
                continue
            if not _fits(request.job, committed, max_usd):
                lines[request.id] = (
                    f"{request.id}  new, not queued: --max-usd {max_usd} (${committed:.2f} committed)",
                    None,
                )
                continue
            new.append(request)
            lines[request.id] = f"{request.id}  new", None
            committed += request.job.max_usd or Decimal(0)
    jobs: list[EvalJob] = []
    if experiment.eval is not None and evaluate:
        # This pass's new runs have no checkpoints yet, but their policies already exist
        # for eval_jobs. Selection/resume passes skip evaluation, so every run here is new.
        asking = replace(state, runs=(*state.runs, *(_queued(r, experiment.name) for r in new)))
        try:
            jobs = asked(experiment.eval, asking)
        except EtudeError:
            # A policy of a run this pass left out (`--max-usd`) is missing; say which and why.
            for line, _ in lines.values():
                print(line)
            raise
    policy_lines = read_tournament_lines(
        source,
        state,
        [job.recorded() for job in jobs],
        listing=listing,
    )
    smoke = Smoke()
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
    taken = {run.address for run in state.eval_runs}
    decisions: list[TournamentDecision] = []
    stopped_runs: set[str] = set()
    stored = StoredRuns.read(experiment.name, source, listing=listing) if jobs else None
    groups: dict[str, list[_PendingEvaluation]] = {}
    for declaration in jobs:
        recorded = declaration.recorded()
        found = played(
            declaration.tournament,
            experiment=experiment.name,
            checkpoints=recorded.checkpoints,
            enter=recorded.entrants,
            runs=stored,
            lines=policy_lines,
        )
        new_matches = list(
            {m.key: m for m in (*found.new, *(found.failed if retry_failed else ()))}.values()
        )
        pending = [match for match in new_matches if match.key not in running]
        done = TournamentDecision(
            tournament=declaration.tournament.name,
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
            executable = declaration.lower(state, pending, policy_lines)
            spec = evaluation_group(executable).evaluations[0]
            groups.setdefault(_group_key(executable, spec), []).append(
                _PendingEvaluation(
                    job=executable,
                    spec=spec,
                    decision=done,
                )
            )
            running.update(m.key for m in pending)
        decisions.append(done)
    for group in groups.values():
        members = [each.spec for each in group]
        executables = [each.job for each in group]
        if mode == "local":
            held = _play_here(members, executables[0], state, source, directory)
            for each, result in zip(group, held, strict=True):
                each.decision.played = result.played
            continue
        executable = group_evaluations(executables, members)
        keys = sorted(m.key for spec in members for m in spec.matches)
        stem = f"{experiment.name}/evals-{digest(keys)[:12]}"
        identity = stem
        number = 2
        while identity in taken:
            identity = f"{stem}-{number}"
            number += 1
        request = RunRequest(id=identity, job=executable, smoke=smoke)
        held_back: Literal["not queued"] | None = None
        reason: str | None = None
        if not _fits(executable, committed, max_usd):
            held_back = "not queued"
            reason = f"--max-usd {max_usd} (${committed:.2f} committed)"
        else:
            planned.append(request)
            taken.add(identity)
            committed += executable.max_usd or Decimal(0)
        for each in group:
            each.decision.state, each.decision.reason = held_back, reason
            each.decision.launched = identity if held_back is None else None
    quoted = None
    if planned:
        if quote:
            quoted = client.quote(planned, pool, rent=rent)
            # A run outside the first wave would wait in the queue, as launch places it.
            wave = {machine.run for machine in quoted.machines}
            for request in new:
                verb = "launch" if request.id in wave else "queue"
                lines[request.id] = f"{request.id}  new, would {verb}", None
            for decision in decisions:
                if decision.launched:
                    decision.state = "would launch" if decision.launched in wave else "would queue"
        else:
            submit = client.queue if mode == "queue" else client.launch
            submit(planned, pool, rent=rent)
            placed: dict[str | None, Literal["queued", "running"]] = {
                item.record.id: item.placement for item in client.standing(experiment.name)
            }
            for request in new:
                lines[request.id] = f"{request.id}  new, {placed[request.id]}", request.id
            for decision in decisions:
                if decision.launched:
                    decision.state = placed[decision.launched]
    for line, address in lines.values():
        print(line)
        if not quote and address is not None:
            print_view(address)
    if quoted is not None:
        show_quote(quoted)
    for decision in decisions:
        _show_tournament(decision)
    if not quote:
        for address in dict.fromkeys(d.launched for d in decisions if d.launched is not None):
            print_view(address)
    if stopped_runs and not quote:
        raise EtudeError("launched_pass_failed", 409, detail=", ".join(sorted(stopped_runs)))
    return decisions


def _play_here(
    specs: Sequence[Evaluation],
    executable: Job,
    state: ExperimentState,
    source: Storage,
    directory: Path | str | None,
) -> tuple[EvalResult, ...]:
    """Play with the same verified artifacts a launched run receives as inputs."""
    with TemporaryDirectory(prefix="etude-eval-") as folder:
        here = fetch_checkpoints(
            tuple(match for spec in specs for match in spec.matches),
            state,
            (),
            Path(folder),
        )
        return hold_group(
            EvalGroup(evaluations=tuple(spec.relocate(here) for spec in specs)),
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
