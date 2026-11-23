"""Read an experiment's stored and scheduler state without keeping pass memory.

`observe` reads a source once and merges the launcher's public `standing`;
`state_of` does the same for a page's already-read data, using only local records.
Author-facing types are defined in `etude.jobs`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from graphlib import TopologicalSorter
from pathlib import Path

import etude.jobs as jobs
from etude.checkpoints import PolicyKey
from etude.errors import EtudeError
from etude.eval.records import Benchmark, Line, Match, TournamentRun
from etude.identity import run_address
from etude.launch import client
from etude.launch.records import Standing
from etude.records import RunRecord
from etude.run import LocatedRun, RunListing
from etude.spec import Job, declared_address
from etude.storage import Storage
from etude.train.read import RunData, lines, read_all
from etude.train.report import job_names

__all__ = ("observe",)


def observe(
    source: Storage | Path | str,
    experiment: str,
    *,
    keys: Sequence[str] | None = None,
    declarations: Sequence[LocatedRun] | None = None,
) -> jobs.ExperimentState:
    """The experiment's training runs under a directory or bucket and their stored checkpoints.

    A checkpoint whose file Etude did not store, so no digest, is left out:
    nothing can be evaluated without its bytes. Runs' records alone are read:
    no metric block or index.
    """
    return state_of(
        read_all(source, experiment, metrics=frozenset(), keys=keys, declarations=declarations),
        experiment,
        retained=client.standing(experiment),
    )


def read_tournament_lines(
    source: Storage,
    state: jobs.ExperimentState,
    tournaments: Sequence[TournamentRun],
    *,
    listing: RunListing,
) -> tuple[Line, ...]:
    """Read the lines reachable by these tournaments, sharing one source discovery."""
    named: set[str] = set()
    for tournament in tournaments:
        benchmark = tournament.tournament
        if not isinstance(benchmark, Benchmark) or not any(s.relative for s in benchmark.seatings):
            continue
        named.update(benchmark.experiments)
        named.update(
            c.policy.experiment
            for c in tournament.checkpoints
            if c.policy is not None and c.policy.experiment is not None
        )
    found = list(state.lines)
    for name in sorted(named - {state.experiment}):
        found.extend(
            line.seen_from(state.experiment)
            for line in observe(
                source, name, keys=listing.keys, declarations=listing.declarations
            ).lines
        )
    return tuple(found)


class TrainingRuns:
    """Resolve submitted addresses and unambiguous older spellings within one experiment."""

    def __init__(
        self, runs: Iterable[jobs.TrainingRunSummary], *, requested: Iterable[str | None]
    ) -> None:
        claimed = set(requested)
        self._addresses: dict[str, jobs.TrainingRunSummary] = {}
        self._aliases: dict[str, jobs.TrainingRunSummary | None] = {}
        for run in runs:
            self._addresses[run.address] = run
            data = run.stored
            if data is None or data.file is None:
                continue
            self._addresses[data.address] = run
            if (
                run.status is None
                and data.address not in claimed
                and data.file.continues is None
                and run.seed is not None
                and (data.file.job_name or data.file.job)
            ):
                alias = declared_address(run.experiment, run.launch, run.seed)
                # Two unclaimed roots leave the declared spelling unresolved.
                self._aliases[alias] = None if alias in self._aliases else run

    def find(
        self, address: str | None, job: Job, seed: int | None
    ) -> jobs.TrainingRunSummary | None:
        if address is None:
            return None
        if exact := self._addresses.get(address):
            return exact
        root = self._aliases.get(address)
        if root and root.launch == job.name and root.seed == seed and job.continuation_json is None:
            return root
        return None


def state_of(
    every: Sequence[RunData], experiment: str, *, retained: tuple[Standing, ...] | None = None
) -> jobs.ExperimentState:
    """The experiment's training runs among runs already read, as `observe` finds them.

    A run that recorded no end has failed once this computer's launcher says
    so, and so ends at its last stored checkpoint.
    """
    ours = [
        data
        for data in every
        if data.file is not None
        and data.experiment == experiment
        and data.file.kind != "evaluation"
    ]
    line = lines(ours)
    job = job_names(ours)
    by_name = {data.name: data for data in ours}
    retained = client.standing(experiment, local=True) if retained is None else retained
    runs: dict[str, jobs.TrainingRunSummary] = {}
    # Parent records precede continuations even when their names sort the other way.
    parents = {
        data.name: (data.file.continues.parent.source_id,)
        if data.file is not None
        and data.file.continues is not None
        and data.file.continues.parent.source_id in by_name
        else ()
        for data in ours
    }
    for name in TopologicalSorter(parents).static_order():
        data = by_name[name]
        first = by_name.get(line[data.name], data)
        saved = jobs.Checkpoints(
            record.model_copy(
                update={
                    "run": data.name,
                    "experiment": data.experiment,
                    "line": line[data.name],
                    "policy": PolicyKey(
                        experiment=data.experiment,
                        job=job[first.name].name,
                        seed=first.file.seed if first.file else None,
                        role=record.role,
                    ),
                }
            )
            for record in data.checkpoints
            if record.artifact.sha256 is not None and record.artifact.size_bytes is not None
        )
        runs[data.address] = jobs.TrainingRunSummary(
            name=data.name,
            experiment=data.experiment,
            job=job[data.name].name,
            launch=job[data.name].launch,
            line=line[data.name],
            state=data.stored_state,
            checkpoints=saved,
            seed=data.file.seed if data.file else None,
            stored=data,
            last_write=data.last_write,
        )
    launches = find_eval_groups(retained)
    evals = _evaluations(every, experiment, launches)
    training_runs = TrainingRuns(runs.values(), requested=(item.record.id for item in retained))
    committed = Decimal(0)
    for item in retained:
        record = item.record
        committed += item.committed_usd
        name = record.id.removeprefix(experiment + "/")
        if (group := launches.get(record.id)) is not None:
            matches = tuple(
                Match.model_validate(m.model_dump(), context={"experiment": experiment}).key
                for spec in group.evaluations
                for m in spec.matches
            )
            summary = evals.get(record.id) or jobs.EvalRunSummary(name=name, state="queued")
            evals[record.id] = _join_allocation(replace(summary, matches=matches), item)
            continue
        spec = record.launch.spec
        training = training_runs.find(record.id, spec, record.seed)
        address = training.address if training else record.id
        if training is None:
            training = jobs.TrainingRunSummary.queued(name, experiment, spec.name, record.seed)
        runs[address] = _join_allocation(training, item)
    return jobs.ExperimentState(
        experiment=experiment,
        runs=tuple(runs.values()),
        eval_runs=tuple(evals.values()),
        committed_usd=committed,
    )


def find_eval_groups(retained: Sequence[Standing]) -> dict[str, jobs.EvalGroup]:
    launches = {}
    for item in retained:
        try:
            launches[item.record.id] = jobs.evaluation_group(item.record.launch.spec)
        except EtudeError as error:
            if error.code != "not_an_evaluation_job":
                raise
    return launches


def locate_evaluation_members(
    folders: dict[str, RunRecord], launches: dict[str, jobs.EvalGroup]
) -> dict[str, str]:
    """Map physical tournament folders to the launch that owns their results."""
    owners = {
        folder + member + "/": record.id
        for folder, record in folders.items()
        for member in record.members or ()
    }
    legacy = {
        (address, spec.address)
        for address, group in launches.items()
        for spec in group.evaluations
        if spec.experiment is not None
    }
    for folder, record in folders.items():
        if folder in owners or record.members is not None:
            continue
        # A retained launch alone cannot claim a standalone tournament of the same name.
        member = run_address(record.id, record.experiment)
        suffix = "/" + member + "/"
        if not folder.endswith(suffix):
            continue
        parent = folder.removesuffix(suffix)
        parent_record = folders.get(parent + "/")
        if parent_record and parent_record.members is not None:
            continue  # A portable group declares its entire membership.
        parts = parent.split("/")
        for candidate in ("/".join(parts[-2:]), parts[-1]):
            if (candidate, member) in legacy:
                owners[folder] = candidate
                break
    return owners


def _evaluations(
    every: Sequence[RunData], experiment: str, launches: dict[str, jobs.EvalGroup]
) -> dict[str, jobs.EvalRunSummary]:
    copies = [
        d for d in every if d.file and d.file.kind == "evaluation" and d.experiment == experiment
    ]
    owners = locate_evaluation_members(
        {d.eval_folder: d.file for d in copies if d.eval_folder and d.file}, launches
    )
    inputs: dict[str, list[RunData]] = defaultdict(list)
    for data in copies:
        owner = owners.get(data.eval_folder or "")
        inputs[owner or data.address].append(data)

    summaries: dict[str, jobs.EvalRunSummary] = {}
    for address, files in inputs.items():
        # A copied launch may have a newer writer or a later delivery of that writer's end.
        root = max(
            (data for data in files if data.eval_folder not in owners),
            key=lambda d: (
                d.writers[-1].started_ms if d.writers else -1,
                d.outcome is not None,
                d.last_write.timestamp() if d.last_write else 0,
            ),
            default=None,
        )
        state = root.stored_state if root else "running"
        summaries[address] = jobs.EvalRunSummary(
            name=address.removeprefix(experiment + "/"),
            state=state,
            last_write=max((d.last_write for d in files if d.last_write), default=None),
            stored=root,
            file_state=state,
        )
    return summaries


def _join_allocation[Run: (jobs.TrainingRunSummary, jobs.EvalRunSummary)](
    run: Run, item: Standing
) -> Run:
    """Join the accepted file outcome to the allocation that could have written it."""
    status = item.status
    data = run.stored
    writers = data.writers if data else ()
    latest = writers[-1] if writers else None
    outcome = data.outcome if data else None
    # read_located accepts the latest writer's end, or an anonymous legacy end.
    # After an explicit resume, only an end attributed to the new allocation counts.
    ended_here = False
    if outcome and latest and status.attempt is not None:
        if latest.attempt == status.attempt:
            ended_here = outcome.writer == latest.writer
        elif latest.attempt and status.attempt:
            outcome = None
    if data and data.file and data.file.members is not None:
        if status.phase == "queued" or (status.attempt is not None and not ended_here):
            outcome = None
    if item.record.launch.resume_after and not ended_here:
        outcome = None

    phase = status.phase
    state: jobs.RunState
    allocation_phase: jobs.AllocationPhase | None = None
    if outcome and (outcome.state != "stopped" or (status.done and status.outcome == "succeeded")):
        state = outcome.state
    elif status.done:
        state = "finished" if status.outcome == "succeeded" else "failed"
    elif ended_here and phase != "queued":
        # The writer stopped, but the launcher still owns continuation.
        state, allocation_phase = item.placement, "stopped"
    elif (
        phase in ("renting", "setting_up")
        and status.attempt is not None
        and any(w.attempt == status.attempt for w in writers)
    ):
        state = "running"
    else:
        state = item.placement
        allocation_phase = phase if phase in ("renting", "setting_up", "stopping") else None
    return replace(run, state=state, standing=item, allocation_phase=allocation_phase)
