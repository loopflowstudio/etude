"""The training half of an experiment's report: jobs, metric lines, checkpoints and time.

`training` turns an experiment's training runs (`etude.train.read.RunData`)
into `Training`, everything the page draws from their files, already
bucketed. Runs are grouped into jobs (`RunDeclaration.job`); a job with
several seeds is drawn as the mean of its seeds' bucket means, banded by
their range. Checkpoints are kept at their exact coordinates, with the wall
time to each and the pace of every step clock since the previous one.
Timers are summed from the metrics logged under a prefix (`etude.report.Flame`).

Wall time on the page is seconds since the run first started: its first
attempt, or its first row when no attempt was recorded.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Literal

import numpy as np

from etude.run.records import WALL_CLOCK, Clock, Record
from etude.train.read import Floats, RunData

BUCKETS = 120
WALL_MEANING = "wall time since the run first started"


class Job(Record):
    """Runs that differ only by seed. `name` is what its author called it, else its key."""

    key: str
    name: str
    label: str  # the name and its seed count, as every chart's key shows it
    runs: tuple[str, ...]


class Span(Record):
    """One attempt, in seconds since its run first started."""

    host: str
    pod: str | None
    started: float
    last_row: float | None  # None when the attempt logged no row


class RunSummary(Record):
    name: str
    # Seconds since the epoch when it first started; every other time here is since then.
    started: float
    job: str
    seed: int | None
    state: str
    rows: int
    incomplete: int
    dropped: int | None
    attempts: tuple[Span, ...]
    # Where each clock last stood in the rows read, for per-step normalisation.
    reached: dict[str, float]


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


class Line(Record):
    """One run's bucket means, or a job's mean over seeds, on one clock.

    A job's line has `low` and `high`, the range of its seeds' bucket means,
    and `seeds`, how many seeds each bucket has.
    """

    owner: str  # a run's name, or a job's key
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
    jobs: tuple[Job, ...]
    runs: tuple[RunSummary, ...]
    checkpoints: tuple[Mark, ...]
    metrics: tuple[str, ...]  # every metric logged, in first-seen order
    # By clock, then metric: each run's line, and each several-seed job's mean.
    series: dict[str, dict[str, tuple[Line, ...]]]
    bands: dict[str, dict[str, tuple[Line, ...]]]
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
    starts = [attempt.started_ms for attempt in run.attempts]
    starts += [block.header.wall_first for block in run.blocks]
    return min(starts) / 1000 if starts else 0.0


def _origin(run: RunData, clock: str) -> float:
    return _started(run) if clock == WALL_CLOCK else 0.0


def _line(run: RunData, metric: str, clock: str) -> Line | None:
    reduced = run.chart(metric, clock, BUCKETS)
    if not len(reduced):
        return None
    return Line(
        owner=run.name,
        x=_rounded((reduced.x_first + reduced.x_last) / 2 - _origin(run, clock), 7),
        mean=_rounded(reduced.mean),
        nonfinite=int(reduced.nonfinite.sum()),
    )


def _band(job: str, members: Sequence[RunData], metric: str, clock: str) -> Line | None:
    """The mean of the seeds' bucket means over a shared span, banded by their range."""
    present = [run for run in members if len(run.chart(metric, clock, BUCKETS))]
    if len(present) < 2:
        return None
    charts = [(run, run.chart(metric, clock, BUCKETS), _origin(run, clock)) for run in present]
    low = min(float(chart.x_first[0]) - origin for _, chart, origin in charts)
    high = max(float(chart.x_last[-1]) - origin for _, chart, origin in charts)
    width = (high - low) / BUCKETS
    means = np.full((len(present), BUCKETS), np.nan)
    for row, (run, _, origin) in enumerate(charts):
        reduced = run.chart(metric, clock, BUCKETS, span=(low + origin, high + origin))
        means[row, reduced.index] = reduced.mean
    count = (~np.isnan(means)).sum(axis=0)
    filled = np.flatnonzero(count)
    values = [means[:, index][~np.isnan(means[:, index])].tolist() for index in filled.tolist()]
    return Line(
        owner=job,
        x=[_round(low + (index + 0.5) * width, 7) for index in filled.tolist()],
        mean=[_round(statistics.fmean(seeds)) for seeds in values],
        low=[_round(min(seeds)) for seeds in values],
        high=[_round(max(seeds)) for seeds in values],
        seeds=count[filled].tolist(),
    )


def _jobs(data: Sequence[RunData]) -> tuple[Job, ...]:
    """Each run's job: its declared one, or the run alone. Named as its author named it."""
    members: dict[str, list[RunData]] = {}
    # In launch order: a job's colour follows when it started, never its rank.
    for run in sorted(data, key=_started):
        members.setdefault(run.job or run.name, []).append(run)
    named = {
        key: next(
            (r.file.declaration.job_name for r in runs if r.file and r.file.declaration.job_name),
            key,
        )
        for key, runs in members.items()
    }
    names = list(named.values())
    jobs = []
    for key, runs in members.items():
        name = named[key] if names.count(named[key]) == 1 else f"{named[key]} {key[:6]}"
        seeds = f"{len(runs)} seed{'s' if len(runs) > 1 else ''}"
        jobs.append(
            Job(key=key, name=name, label=f"{name} · {seeds}", runs=tuple(r.name for r in runs))
        )
    return tuple(jobs)


def _summary(run: RunData) -> RunSummary:
    start = _started(run)
    last: dict[str, int] = {}
    for block in run.blocks:
        writer = block.header.writer
        last[writer] = max(last.get(writer, 0), block.header.wall_last)
    reached: dict[str, float] = {}
    for block in run.blocks:
        for column in block.header.clocks:
            if column.last is not None:
                reached[column.name] = max(reached.get(column.name, -math.inf), column.last)
    declaration = run.file.declaration if run.file else None
    return RunSummary(
        name=run.name,
        started=start,
        job=run.job or run.name,
        seed=declaration.seed if declaration else None,
        state=run.state.state if run.state else "running or unfinished",
        rows=run.rows,
        incomplete=run.incomplete,
        dropped=run.state.counters.dropped if run.state else None,
        attempts=tuple(
            Span(
                host=attempt.host,
                pod=attempt.pod,
                started=attempt.started_ms / 1000 - start,
                last_row=last[attempt.writer] / 1000 - start if attempt.writer in last else None,
            )
            for attempt in run.attempts
        ),
        reached=reached,
    )


def _marks(run: RunData) -> list[Mark]:
    start = _started(run)
    steps = [clock.name for clock in run.clocks if clock.kind == "step"]
    first = run.clocks[0].name if run.clocks else None
    marks: list[Mark] = []
    previous = None
    for checkpoint in sorted(run.checkpoints, key=lambda c: (c.ordinal, c.wall_ms)):
        began = [
            a for a in run.attempts if (previous.wall_ms if previous else -1) < a.started_ms
            and a.started_ms <= checkpoint.wall_ms
        ]  # fmt: skip
        # Pace is measured within one attempt: from the previous checkpoint, or
        # from where the attempt that recorded this one resumed.
        since = None if began else previous
        resumed = began[-1] if began else None
        pace: dict[str, float | None] = {}
        for clock in steps:
            here = checkpoint.at.get(clock)
            if since is not None:
                there, then = since.at.get(clock), since.wall_ms
            elif resumed is not None:
                there, then = resumed.resumed_at.get(clock), resumed.started_ms
            else:
                there, then = None, 0
            seconds = (checkpoint.wall_ms - then) / 1000
            pace[clock] = (
                _round((here - there) / seconds)
                if here is not None and there is not None and seconds > 0
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
    for metric in run.metrics():
        if metric.startswith(prefix) and len(metric) > len(prefix):
            reduced = run.chart(metric, WALL_CLOCK, 1)
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
    metrics: dict[str, None] = {}
    for run in data:
        metrics.update(dict.fromkeys(run.metrics()))
    jobs = _jobs(data)
    members = {job.key: [run for run in data if run.name in job.runs] for job in jobs}
    series: dict[str, dict[str, tuple[Line, ...]]] = {}
    bands: dict[str, dict[str, tuple[Line, ...]]] = {}
    for clock in [*clocks, WALL_CLOCK]:
        for metric in metrics:
            lines = [
                line
                for run in data
                if clock == WALL_CLOCK or clock in {c.name for c in run.clocks}
                if (line := _line(run, metric, clock)) is not None
            ]
            if lines:
                series.setdefault(clock, {})[metric] = tuple(lines)
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
