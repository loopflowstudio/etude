"""Reconcile an experiment file against its stored results and scheduler records.

`advance` reads one state, declares training then evaluation, and hands down
new runs. The scheduler's public records are the only memory of a hand-down.
"""

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal

from etude.declarations import Experiment, asked, read_job_file, select_runs
from etude.errors import EtudeError, LauncherError
from etude.eval.records import digest, folder_name
from etude.eval.tournament import StoredRuns, played
from etude.experiment import observe
from etude.jobs import (
    ExperimentState,
    evaluation_spec,
    hold_evaluation,
    with_experiment,
    with_matches,
)
from etude.launch import client
from etude.launch.listing import show_quote
from etude.launch.server import selected
from etude.records import Record
from etude.smoke import smoke_file
from etude.spec import Job, RunRequest, Smoke, Until
from etude.storage import Storage, default_root


class Pass(Record):
    tournament: str
    new: int
    running: int
    failed: int = 0
    played: int = 0
    launched: str | None = None
    state: str | None = None


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
        names.add(next(r for r in scheduler.ondemands() if r.name == rent).storage)
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


def _show_training(request: RunRequest, state: ExperimentState) -> bool:
    prior = next(
        (
            r
            for r in state.runs
            if r.name == request.id or (r.launch == request.job.name and r.seed == request.seed)
        ),
        None,
    )
    if prior is None:
        return False
    line = f"{request.id}  {prior.state}"
    if prior.state == "failed":
        line += f"; --job {request.job.name} --seed {request.seed} --resume"
    print(line)
    return True


def advance(
    experiment: Experiment,
    *,
    source: Storage,
    pool: str | None = None,
    rent: str | None = None,
    directory: Path | str | None = None,
    retry_failed: bool = False,
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
) -> list[Pass]:
    """One pass. Existing addresses stay put; explicit retries preserve delivered matches."""
    listed = source.keys("")
    state = observe(source, experiment.name, keys=listed)
    committed = state.committed_usd
    print(
        f"{experiment.name}: state from {source.uri('run.json').removesuffix('/run.json')}: {len(state.runs)} training runs, {len(state.eval_runs)} eval runs"
        + (f"; ${committed:.2f} of ${max_usd:.2f} committed" if max_usd is not None else "")
    )
    evaluate = (
        experiment.eval is not None
        and job is None
        and seed is None
        and not resume
        and until is None
    )
    jobs = (
        [with_experiment(j, experiment.name) for j in asked(experiment.eval, state)]
        if evaluate and experiment.eval is not None
        else []
    )
    before = None
    if mode != "local" and callable(experiment.training):
        assert experiment.file is not None
        requests = read_job_file(
            experiment.file,
            given,
            experiment=experiment.name,
            state=state,
            function=experiment.training,
        )
        if job is not None or seed is not None:
            requests = select_runs(requests, job=job, seed=seed)
        new: list[RunRequest] = []
        for request in requests:
            if not resume and _show_training(request, state):
                continue
            if not _fits(request.job, committed, max_usd):
                print(
                    f"{request.id}  new, not queued: --max-usd {max_usd} (${committed:.2f} committed)"
                )
                continue
            if resume:
                if len(requests) != 1:
                    raise LauncherError("--resume needs one run; select --job and --seed")
                request = request.model_copy(
                    update={
                        "resume": True,
                        "job": request.job.model_copy(update={"until": until or request.job.until}),
                    }
                )
            new.append(request)
            committed += request.job.max_usd or Decimal(0)
        if smoke_file_first and not quote:
            assert experiment.file is not None
            before = smoke_file(experiment.file, requests)
            new = before.stamp(new)
            smoke = Smoke(steps=before.rest)
        if new:
            if quote:
                for request in new:
                    print(f"{request.id}  new, would launch")
                show_quote(client.quote(new, pool, rent=rent))
            else:
                submit = client.queue if mode == "queue" else client.launch
                ids = submit(new, pool, rent=rent)
                statuses = {
                    item.record.id: item.status for item in client.standing(experiment.name)
                }
                for identity in ids:
                    status = statuses[identity]
                    print(f"{identity}  new, {'queued' if status.phase == 'queued' else 'running'}")
    if (
        experiment.eval is None
        or job is not None
        or seed is not None
        or resume
        or until is not None
    ):
        return []
    running = {
        key for run in state.eval_runs if run.state in {"queued", "running"} for key in run.matches
    }
    failed = {
        key: run.name for run in state.eval_runs if run.state == "failed" for key in run.matches
    }
    taken = {run.name for run in state.eval_runs}
    passes: list[Pass] = []
    if smoke_file_first and before is None and not quote:
        assert experiment.file is not None
        before = smoke_file(experiment.file, ())
        smoke = Smoke(steps=before.rest)
    stored = StoredRuns.read(experiment.name, source, keys=listed) if jobs else None
    for executable in jobs:
        spec = evaluation_spec(executable)
        found = played(
            spec.tournament,
            experiment=experiment.name,
            checkpoints=tuple(each.ref for each in spec.checkpoints),
            runs=stored,
            lines=state.lines,
        )
        new_matches = [*found.new, *(found.failed if retry_failed else ())]
        pending = [match for match in new_matches if match.key not in running]
        done = Pass(
            tournament=spec.tournament.name,
            new=len(new_matches),
            running=len(new_matches) - len(pending),
            failed=0 if retry_failed else len(found.failed),
        )
        stopped = {failed[m.key] for m in pending if m.key in failed}
        if stopped and not retry_failed:
            raise EtudeError("launched_pass_failed", 409, detail=", ".join(sorted(stopped)))
        if pending:
            if mode == "local":
                held = hold_evaluation(
                    spec.model_copy(update={"matches": tuple(pending), "lines": state.lines}),
                    checkout=executable.repo,
                    directory=directory,
                    storages=[source],
                )
                done = done.model_copy(update={"played": held.played})
                running.update(m.key for m in pending)
            else:
                executable = with_matches(executable, pending, state)
                if _fits(executable, committed, max_usd):
                    stem = f"{experiment.name}/{folder_name(spec.tournament.name)}-{digest(sorted(m.key for m in pending))[:12]}"
                    identity = stem
                    number = 2
                    while identity in taken:
                        identity = f"{stem}-{number}"
                        number += 1
                    if quote:
                        done = done.model_copy(
                            update={"launched": identity, "state": "would launch"}
                        )
                        show_quote(
                            client.quote([RunRequest(id=identity, job=executable)], pool, rent=rent)
                        )
                    else:
                        submit = client.queue if mode == "queue" else client.launch
                        (identity,) = submit(
                            RunRequest(id=identity, job=executable, smoke=smoke), pool, rent=rent
                        )
                        done = done.model_copy(
                            update={
                                "launched": identity,
                                "state": "queued" if mode == "queue" else "running",
                            }
                        )
                    running.update(m.key for m in pending)
                    taken.add(identity)
                    committed += executable.max_usd or Decimal(0)
                else:
                    done = done.model_copy(
                        update={
                            "state": f"not queued: --max-usd {max_usd} (${committed:.2f} committed)"
                        }
                    )
        passes.append(done)
    return passes
