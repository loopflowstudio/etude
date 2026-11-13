"""The training half of an experiment's report: jobs, metric traces, checkpoints and time.

`training` turns an experiment's training runs (`etude.train.read.RunData`)
into `Training`, everything the page draws from their files, already
bucketed. Runs are grouped into jobs (`RunDeclaration.job`); a job with
several seeds is drawn as the mean of its seeds' bucket means, banded by
their range. Checkpoints are kept at their exact coordinates, with the wall
time to each and the pace of every step clock since the previous one.
Timers are summed from the metrics logged under a prefix (`etude.report.Flame`).

Every run is drawn on its declared clocks and Etude's two in hours
(`etude.run.records.BUILT_IN_CLOCKS`). Attempt spans and checkpoint `elapsed`
are seconds since the run first started: its first attempt, or its first row
when no attempt was recorded.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Literal

import numpy as np

from etude.run.records import BUILT_IN_CLOCKS, BUILT_IN_NAMES, HOURS_SINCE_START, Clock, Record
from etude.train.read import Floats, RunData, lines

BUCKETS = 120


class Job(Record):
    """Runs that differ only by provenance: seed, device and commit.

    `launch` is the name it was launched under; `name` adds the config digest
    when that launch name has jobs with several configs ("ppo 1a2b3c").
    """

    name: str
    launch: str
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
    # Latest recorded position, including a continuation before its first row.
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
    jobs: tuple[Job, ...]
    runs: tuple[RunSummary, ...]
    checkpoints: tuple[Mark, ...]
    metrics: tuple[str, ...]  # every metric logged, in first-seen order
    # By clock, then metric: each run's trace, and each several-seed job's mean.
    series: dict[str, dict[str, tuple[Trace, ...]]]
    bands: dict[str, dict[str, tuple[Trace, ...]]]
    timers: tuple[Timers, ...]


def round_value(value: float | None, figures: int = 5) -> float | None:
    """Significant figures for the page; NaN and infinities become gaps."""
    if value is None or not math.isfinite(value):
        return None
    return float(f"{value:.{figures}g}")


def rounded(values: Floats, figures: int = 5) -> list[float | None]:
    return [round_value(value, figures) for value in values.tolist()]


def started(run: RunData) -> float:
    """Seconds since the epoch when the run first started."""
    starts = [attempt.started_ms for attempt in run.attempts]
    starts += [block.header.wall_first for block in run.blocks]
    return min(starts) / 1000 if starts else 0.0


def run_trace(run: RunData, metric: str, clock: str) -> Trace | None:
    reduced = run.chart(metric, clock, BUCKETS)
    if not len(reduced):
        return None
    return Trace(
        owner=run.name,
        x=rounded((reduced.x_first + reduced.x_last) / 2, 7),
        mean=rounded(reduced.mean),
        nonfinite=int(reduced.nonfinite.sum()),
    )


def job_band(job: str, members: Sequence[RunData], metric: str, clock: str) -> Trace | None:
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
        x=[round_value(low + (index + 0.5) * width, 7) for index in filled.tolist()],
        mean=[round_value(statistics.fmean(seeds)) for seeds in values],
        low=[round_value(min(seeds)) for seeds in values],
        high=[round_value(max(seeds)) for seeds in values],
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
        declaration = run.file.declaration if run.file else None
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


def launched_jobs(data: Sequence[RunData]) -> tuple[Job, ...]:
    """The runs' jobs (`job_names`), in launch order. A job's seeds are its learner lines
    (`etude.train.read.lines`), so a continuation is no extra seed."""
    names = job_names(data)
    line = lines(data)
    members: dict[str, list[RunData]] = {}
    # In launch order: a job's colour follows when it started, never its rank.
    for run in sorted(data, key=started):
        members.setdefault(names[run.name].name, []).append(run)
    found = []
    for name, runs in members.items():
        count = len({line[r.name] for r in runs})
        seeds = f"{count} seed{'s' if count > 1 else ''}"
        found.append(
            Job(
                name=name,
                launch=names[runs[0].name].launch,
                label=f"{name} · {seeds}",
                runs=tuple(r.name for r in runs),
            )
        )
    return tuple(found)


def summary(run: RunData) -> RunSummary:
    start = started(run)
    last: dict[str, int] = {}
    # A continuation may discard a previous attempt's overrun. Its recorded
    # position, including a resume before the first row, supersedes that attempt.
    positions = {attempt.writer: dict(attempt.resumed_at) for attempt in run.attempts}
    starts = {attempt.writer: attempt.started_ms for attempt in run.attempts}
    for block in run.blocks:
        writer = block.header.writer
        last[writer] = max(last.get(writer, 0), block.header.wall_last)
        starts.setdefault(writer, block.header.wall_first)
        position = positions.setdefault(writer, {})
        for column in block.header.clocks:
            if column.last is not None:
                position[column.name] = column.last
    reached: dict[str, float] = {}
    for writer in sorted(positions, key=lambda writer: starts[writer]):
        reached.update(positions[writer])
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


def run_marks(run: RunData) -> list[Mark]:
    start = started(run)
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
                round_value((here - there) / seconds)
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


def timers(run: RunData, prefix: str) -> Timers | None:
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
    jobs = launched_jobs(data)
    members = {job.name: [run for run in data if run.name in job.runs] for job in jobs}
    series: dict[str, dict[str, tuple[Trace, ...]]] = {}
    bands: dict[str, dict[str, tuple[Trace, ...]]] = {}
    for clock in clocks:
        for metric in metrics:
            traces = [
                trace
                for run in data
                if clock in BUILT_IN_NAMES or clock in {c.name for c in run.clocks}
                if (trace := run_trace(run, metric, clock)) is not None
            ]
            if traces:
                series.setdefault(clock, {})[metric] = tuple(traces)
            means = [
                band
                for job, runs in members.items()
                if (band := job_band(job, runs, metric, clock)) is not None
            ]
            if means:
                bands.setdefault(clock, {})[metric] = tuple(means)
    return Training(
        clocks=tuple(clocks.values()),
        jobs=jobs,
        runs=tuple(summary(run) for run in data),
        checkpoints=tuple(mark for run in data for mark in run_marks(run)),
        metrics=tuple(metrics),
        series=series,
        bands=bands,
        timers=tuple(
            found
            for prefix in dict.fromkeys(prefixes)
            for run in data
            if (found := timers(run, prefix)) is not None
        ),
    )
