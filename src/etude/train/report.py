"""The training half of an experiment's report: jobs, metric traces, checkpoints and time.

`training` turns an experiment's training runs (`etude.train.read.RunData`)
into `Training`, everything the page draws from their files, already
bucketed. Runs are grouped into jobs (`RunRecord.job`); a job with
several seeds is drawn as the mean of its seeds' bucket means, banded by
their range. Checkpoints are kept at their exact coordinates, with the wall
time to each and the pace of every step clock since the previous one.
Timers are summed from the metrics logged under a prefix (`etude.report.Flame`).

Every run is drawn on its declared clocks and Etude's two in hours
(`etude.records.BUILT_IN_CLOCKS`). Attempts' times and checkpoint
`elapsed` are seconds since the run first started: its first writer's start
(`etude.train.read.RunData.writer_starts`).
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Literal

import numpy as np

from etude.checkpoints import CheckpointRecord
from etude.records import BUILT_IN_CLOCKS, BUILT_IN_NAMES, HOURS_SINCE_START, Clock, Record
from etude.train.read import Floats, RunData, lines

BUCKETS = 120


class JobGroup(Record):
    """Runs drawn together as one job: they differ only by provenance, seed, device and commit.

    `launch` is the name it was launched under; `name` adds the config digest
    when that launch name has jobs with several configs ("ppo 1a2b3c").
    """

    name: str
    launch: str
    label: str  # the name and its seed count, as every chart's key shows it
    runs: tuple[str, ...]


class WriterSummary(Record):
    """One writer, in seconds since its run first started."""

    host: str
    pod: str | None
    started: float
    last_row: float | None  # None when the attempt logged no row


class RunSummary(Record):
    name: str
    # Seconds since the epoch when it first started; every other time here is since then.
    started: float
    seed: int | None
    state: str
    rows: int
    incomplete: int
    dropped: int | None
    writers: tuple[WriterSummary, ...]
    # Where each clock last stood across attempts (`etude.train.read.RunData.reached`).
    at: dict[str, float]


class Mark(Record):
    """A recorded checkpoint as the page draws it.

    Seeds of a job align on `align`, where it stood on the run's first
    declared clock. `pace` is each step clock per wall second since the
    previous checkpoint, None where an attempt began in between.
    """

    run: str
    ordinal: int
    at: dict[str, float]
    align: float
    elapsed: float
    pace: dict[str, float | None]


class Trace(Record):
    """One run's bucket means, or a job's mean over seeds, on one clock.

    A job's trace has `low` and `high`, the range of its seeds' bucket means,
    and `seeds`, how many seeds each bucket has.
    """

    owner: str  # a run's or a job's name
    x: list[float | None]
    mean: list[float | None]
    low: list[float | None] | None = None
    high: list[float | None] | None = None
    nonfinite: int = 0
    seeds: list[int] | None = None


class Timer(Record):
    name: str
    seconds: float
    # recorded: a logged timer; summed: its children's total, with no timer of
    # its own; unattributed: a parent's seconds its children leave; untimed:
    # active wall time no top-level timer covers.
    status: Literal["recorded", "summed", "unattributed", "untimed"]
    children: tuple[Timer, ...] = ()


class Timers(Record):
    run: str
    prefix: str
    tree: Timer


class Training(Record):
    clocks: tuple[Clock, ...]
    jobs: tuple[JobGroup, ...]
    runs: tuple[RunSummary, ...]
    checkpoints: tuple[Mark, ...]
    metrics: tuple[str, ...]  # every metric logged, in first-seen order
    # By clock, then metric: each run's trace, and each several-seed job's mean.
    series: dict[str, dict[str, tuple[Trace, ...]]]
    bands: dict[str, dict[str, tuple[Trace, ...]]]
    timers: tuple[Timers, ...]


def _round(value: float | None, figures: int = 5) -> float | None:
    """Significant figures for the page; NaN and infinities become gaps."""
    if value is None or not math.isfinite(value):
        return None
    return float(f"{value:.{figures}g}")


def _rounded(values: Floats, figures: int = 5) -> list[float | None]:
    return [_round(value, figures) for value in values.tolist()]


def _started(run: RunData) -> float:
    """Seconds since the epoch when the run first started."""
    starts = run.writer_starts
    return min(starts.values()) / 1000 if starts else 0.0


def _trace(run: RunData, metric: str, clock: str) -> Trace | None:
    reduced = run.chart(metric, clock, BUCKETS)
    if not len(reduced):
        return None
    return Trace(
        owner=run.name,
        x=_rounded((reduced.x_first + reduced.x_last) / 2, 7),
        mean=_rounded(reduced.mean),
        nonfinite=int(reduced.nonfinite.sum()),
    )


def _band(job: str, members: Sequence[RunData], metric: str, clock: str) -> Trace | None:
    """The mean of the seeds' bucket means over a shared span, banded by their range."""
    present = [(run, chart) for run in members if len(chart := run.chart(metric, clock, BUCKETS))]
    if len(present) < 2:
        return None
    low = min(float(chart.x_first[0]) for _, chart in present)
    high = max(float(chart.x_last[-1]) for _, chart in present)
    width = (high - low) / BUCKETS
    means = np.full((len(present), BUCKETS), np.nan)
    for row, (run, _) in enumerate(present):
        reduced = run.chart(metric, clock, BUCKETS, span=(low, high))
        means[row, reduced.index] = reduced.mean
    count = (~np.isnan(means)).sum(axis=0)
    filled = np.flatnonzero(count)
    values = [means[:, index][~np.isnan(means[:, index])].tolist() for index in filled.tolist()]
    return Trace(
        owner=job,
        x=[_round(low + (index + 0.5) * width, 7) for index in filled.tolist()],
        mean=[_round(statistics.fmean(seeds)) for seeds in values],
        low=[_round(min(seeds)) for seeds in values],
        high=[_round(max(seeds)) for seeds in values],
        seeds=count[filled].tolist(),
    )


class JobName(Record):
    """A job's names: `name`, its full name, and `launch`, the name it was launched under."""

    name: str
    launch: str


def job_names(data: Sequence[RunData]) -> dict[str, JobName]:
    """Each run's job, by run: the name its author launched it under (else its key, else
    the run) and its config. Reads only the runs' declarations.

    The launcher's job key includes the commit, so grouping by name pools a
    job's runs across commits and devices with its seeds: these are provenance,
    and a report addresses jobs by name. A config is scientific: a name launched
    with several configs becomes one job per config, "ppo 1a2b3c"; a report
    naming "ppo" addresses all of them. `eval(state)` names jobs by this too.
    """
    keys: dict[str, tuple[str, str | None]] = {}
    for run in data:
        declaration = run.file
        named = declaration.job_name if declaration else None
        config = declaration.config_sha256 if declaration else None
        keys[run.name] = (named or run.job or run.name, config)
    configs = Counter(named for named, _ in set(keys.values()))
    return {
        run: JobName(
            name=named if configs[named] == 1 else f"{named} {(config or 'none')[:6]}",
            launch=named,
        )
        for run, (named, config) in keys.items()
    }


def _jobs(data: Sequence[RunData]) -> tuple[JobGroup, ...]:
    """The runs' jobs (`job_names`), in launch order. A job's seeds are its learner lines
    (`etude.train.read.lines`), so a continuation is no extra seed."""
    names = job_names(data)
    line = lines(data)
    members: dict[str, list[RunData]] = {}
    # In launch order: a job's colour follows when it started, never its rank.
    for run in sorted(data, key=_started):
        members.setdefault(names[run.name].name, []).append(run)
    found = []
    for name, runs in members.items():
        count = len({line[r.name] for r in runs})
        seeds = f"{count} seed{'s' if count > 1 else ''}"
        found.append(
            JobGroup(
                name=name,
                launch=names[runs[0].name].launch,
                label=f"{name} · {seeds}",
                runs=tuple(r.name for r in runs),
            )
        )
    return tuple(found)


def _summary(run: RunData) -> RunSummary:
    start = _started(run)
    last: dict[str, int] = {}
    for block in run.blocks:
        writer = block.header.writer
        last[writer] = max(last.get(writer, 0), block.header.wall_last)
    declaration = run.file
    return RunSummary(
        name=run.name,
        started=start,
        seed=declaration.seed if declaration else None,
        state=run.outcome.state
        if run.outcome and run.outcome.state != "stopped"
        else "running or unfinished",
        rows=run.rows,
        incomplete=run.incomplete,
        dropped=run.outcome.counters.dropped if run.outcome else None,
        writers=tuple(
            WriterSummary(
                host=writer.host,
                pod=writer.pod,
                started=writer.started_ms / 1000 - start,
                last_row=last[writer.writer] / 1000 - start if writer.writer in last else None,
            )
            for writer in run.writers
        ),
        at=run.reached,
    )


def _marks(run: RunData) -> list[Mark]:
    start = _started(run)
    steps = [clock.name for clock in run.clocks if clock.kind == "step"]
    first = run.clocks[0].name if run.clocks else None
    marks: list[Mark] = []
    previous: CheckpointRecord | None = None
    for checkpoint in run.checkpoints:
        if checkpoint.wall_ms is None:
            continue
        previous_wall = previous.wall_ms if previous is not None else None
        restarted = any(
            (previous_wall is None or previous_wall < writer.started_ms)
            and writer.started_ms <= checkpoint.wall_ms
            for writer in run.writers
        )
        seconds = (checkpoint.wall_ms - previous_wall) / 1000 if previous_wall is not None else 0
        # A restart breaks the interval: resumed coordinates do not measure
        # work across the two processes or the time spent between them.
        pace: dict[str, float | None] = {}
        for clock in steps:
            here = checkpoint.at.get(clock)
            there = previous.at.get(clock) if previous is not None else None
            pace[clock] = (
                _round((here - there) / seconds)
                if not restarted and here is not None and there is not None and seconds > 0
                else None
            )
        marks.append(
            Mark(
                run=run.name,
                ordinal=checkpoint.ordinal,
                at=checkpoint.at,
                align=checkpoint.at.get(first, float(checkpoint.ordinal))
                if first
                else float(checkpoint.ordinal),
                elapsed=checkpoint.wall_ms / 1000 - start,
                pace=pace,
            )
        )
        previous = checkpoint
    return marks


def _timers(run: RunData, prefix: str) -> Timers | None:
    """The run's icicle under `prefix`, or None when it logged no timer there."""
    totals: dict[tuple[str, ...], float] = {}
    # Any clock every row gives; a declared one also covers rows logged before Etude's.
    clock = run.clocks[0].name if run.clocks else HOURS_SINCE_START
    for metric in run.metrics():
        if metric.startswith(prefix) and len(metric) > len(prefix):
            reduced = run.chart(metric, clock, 1)
            finite = reduced.count - reduced.nonfinite
            totals[tuple(metric[len(prefix) :].split("/"))] = float(
                np.nansum(reduced.mean * finite)
            )
    if not totals:
        return None

    def node(path: tuple[str, ...]) -> Timer:
        below = sorted({p[: len(path) + 1] for p in totals if p[: len(path)] == path and p != path})
        children = sorted((node(p) for p in below), key=lambda t: -t.seconds)
        covered = sum(child.seconds for child in children)
        seconds = totals.get(path, covered)
        rest = seconds - covered
        if children and rest > 1e-9 * max(seconds, 1.0):
            children.append(Timer(name="unattributed", seconds=rest, status="unattributed"))
        status: Literal["recorded", "summed"] = "recorded" if path in totals else "summed"
        return Timer(name=path[-1], seconds=seconds, status=status, children=tuple(children))

    tops = sorted({path[:1] for path in totals})
    children = sorted((node(path) for path in tops), key=lambda t: -t.seconds)
    spans: dict[str, tuple[int, int]] = {}
    for block in run.blocks:
        first, last = spans.get(block.header.writer, (block.header.wall_first, 0))
        spans[block.header.writer] = (
            min(first, block.header.wall_first),
            max(last, block.header.wall_last),
        )
    active = sum(last - first for first, last in spans.values()) / 1000
    covered = sum(child.seconds for child in children)
    if active > covered:
        children.append(Timer(name="untimed", seconds=active - covered, status="untimed"))
    tree = Timer(
        name="active wall time",
        seconds=max(active, covered),
        status="recorded" if active >= covered else "summed",
        children=tuple(children),
    )
    return Timers(run=run.name, prefix=prefix, tree=tree)


def training(data: Sequence[RunData], *, prefixes: Sequence[str] = ()) -> Training:
    """What the page draws from these runs; `prefixes` name the timer trees to sum."""
    clocks: dict[str, Clock] = {}
    for run in data:
        for declared in run.clocks:
            clocks.setdefault(declared.name, declared)
    clocks.update({clock.name: clock for clock in BUILT_IN_CLOCKS})
    metrics: dict[str, None] = {}
    for run in data:
        metrics.update(dict.fromkeys(run.metrics()))
    jobs = _jobs(data)
    members = {job.name: [run for run in data if run.name in job.runs] for job in jobs}
    series: dict[str, dict[str, tuple[Trace, ...]]] = {}
    bands: dict[str, dict[str, tuple[Trace, ...]]] = {}
    for clock in clocks:
        for metric in metrics:
            traces = [
                trace
                for run in data
                if clock in BUILT_IN_NAMES or clock in {c.name for c in run.clocks}
                if (trace := _trace(run, metric, clock)) is not None
            ]
            if traces:
                series.setdefault(clock, {})[metric] = tuple(traces)
            means = [
                band
                for job, runs in members.items()
                if (band := _band(job, runs, metric, clock)) is not None
            ]
            if means:
                bands.setdefault(clock, {})[metric] = tuple(means)
    return Training(
        clocks=tuple(clocks.values()),
        jobs=jobs,
        runs=tuple(_summary(run) for run in data),
        checkpoints=tuple(mark for run in data for mark in _marks(run)),
        metrics=tuple(metrics),
        series=series,
        bands=bands,
        timers=tuple(
            found
            for prefix in dict.fromkeys(prefixes)
            for run in data
            if (found := _timers(run, prefix)) is not None
        ),
    )
