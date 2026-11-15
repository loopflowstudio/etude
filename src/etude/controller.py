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

from etude.declarations import (
    Experiment,
    asked,
    find_training_run,
    format_known_run,
    read_training_jobs,
    select_runs,
)
from etude.errors import EtudeError, LauncherError
from etude.eval.records import (
    Line,
    Match,
    TournamentRun,
    digest,
    folder_name,
)
from etude.eval.tournament import StoredRuns, played
from etude.experiment import observe, read_tournament_lines
from etude.jobs import (
    Checkpoints,
    Evaluation,
    ExperimentState,
    TrainingRunSummary,
    evaluation_spec,
    fetch_checkpoints,
    hold_evaluation,
    with_experiment,
    with_matches,
)
from etude.launch import client
from etude.launch.listing import show_quote
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
    state: Literal["queued", "running", "would launch", "not queued"] | None = None
    reason: str | None = None
    stopped: tuple[str, ...] = ()


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
                tournament=(spec := evaluation_spec(job)).tournament,
                checkpoints=spec.held,
            )
            for job in jobs
        ],
        keys=listed,
        declarations=declarations,
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
    taken = {f"{experiment.name}/{run.name}" for run in state.eval_runs}
    decisions: list[TournamentDecision] = []
    stopped_runs: set[str] = set()
    stored = (
        StoredRuns.read(experiment.name, source, keys=listed, declarations=declarations)
        if jobs
        else None
    )
    for executable in jobs:
        spec = evaluation_spec(executable)
        found = played(
            spec.tournament,
            experiment=experiment.name,
            checkpoints=spec.held,
            enter=spec.entrants,
            runs=stored,
            lines=policy_lines,
        )
        wanted = [*found.new, *(found.failed if retry_failed else ())]
        pending = [match for match in wanted if match.key not in running]
        done = TournamentDecision(
            tournament=spec.tournament.name,
            new=len(pending),
            running=len(wanted) - len(pending),
            failed=0 if retry_failed else len(found.failed),
        )
        if not retry_failed:
            # A failed run's undelivered matches wait for an explicit retry.
            stopped = {failed[m.key] for m in pending if m.key in failed}
            stopped_runs.update(stopped)
            done.stopped = tuple(sorted(stopped))
            pending = [match for match in pending if match.key not in failed]
        if pending and mode == "local":
            done.played = _play_here(
                spec, executable, pending, state, policy_lines, source, directory
            )
            running.update(m.key for m in pending)
        elif pending:
            executable = with_matches(executable, pending, state, policy_lines)
            if _fits(executable, committed, max_usd):
                stem = f"{experiment.name}/{folder_name(spec.tournament.name)}-{digest(sorted(m.key for m in pending))[:12]}"
                done.launched = stem
                number = 2
                while done.launched in taken:
                    done.launched = f"{stem}-{number}"
                    number += 1
                planned.append(RunRequest(id=done.launched, job=executable, smoke=smoke))
                running.update(m.key for m in pending)
                taken.add(done.launched)
                committed += executable.max_usd or Decimal(0)
            else:
                done.state = "not queued"
                done.reason = f"--max-usd {max_usd} (${committed:.2f} committed)"
        decisions.append(done)
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
    spec: Evaluation,
    executable: Job,
    pending: Sequence[Match],
    state: ExperimentState,
    lines: tuple[Line, ...],
    source: Storage,
    directory: Path | str | None,
) -> int:
    """Play with the same verified artifacts a launched run receives as inputs."""
    with TemporaryDirectory(prefix="etude-eval-") as folder:
        here = fetch_checkpoints(pending, state, spec.checkpoints, Path(folder))
        held = hold_evaluation(
            spec.model_copy(update={"matches": tuple(pending), "lines": lines}),
            files=here,
            checkout=executable.repo,
            directory=directory,
            storages=[source],
        )
        return held.played


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
