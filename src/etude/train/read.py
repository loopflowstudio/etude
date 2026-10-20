"""Read runs back from their files, in a directory or a bucket, with no server.

The files are the record, so this reader is the reference for what they mean.
A run's rows are one set across the processes that wrote them. For one metric
against one clock: a row counts when it gives that clock; an earlier process's
rows count up to and including the checkpoint coordinate a later one resumed
from on that clock, or the later one's first logged value when no checkpoint
coordinate is known, and not beyond it.
A later write at the same clock value replaces an earlier one.
Superseded rows stay on disk. Buckets keep their extremes.

Rows are read by column (`etude.train.block`), so a chart costs two arrays.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict, SkipValidation, ValidationError

from etude.runs.block_file import parse, select
from etude.runs.listing import RUN_FOLDERS, as_storage, declared, places
from etude.runs.records import WALL_CLOCK, Attempt, Clock, Record, RunFile, RunState
from etude.runs.storage import Storage, run_prefix
from etude.train.block import Table
from etude.train.manifest import BlockDescription, Manifest, StoredBlock, describe, fetch
from etude.train.records import CheckpointRecord

type Floats = npt.NDArray[np.float64]


class Arrays(Record):
    """Records whose fields are NumPy arrays, which Pydantic carries as given."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


class Points(Arrays):
    """One metric against one clock: `x` ascending, one `y` per `x`. `y` may be NaN or infinite."""

    x: SkipValidation[Floats]
    y: SkipValidation[Floats]

    def __len__(self) -> int:
        return len(self.x)


class RunData(Arrays):
    """One run as written: every row, including superseded ones, by column."""

    experiment: str
    name: str
    file: RunFile | None
    state: RunState | None
    # One table per process that wrote rows, in the order the processes started.
    blocks: SkipValidation[tuple[StoredBlock, ...]]
    checkpoints: tuple[CheckpointRecord, ...]
    # Every process that wrote to this run, in start order.
    attempts: tuple[Attempt, ...]

    @cached_property
    def tables(self) -> tuple[Table, ...]:
        """Raw inspection loads whole blocks; chart reads do not."""
        return tuple(block.table() for block in self.blocks)

    @property
    def clocks(self) -> tuple[Clock, ...]:
        return self.file.declaration.clocks if self.file else ()

    @property
    def job(self) -> str | None:
        return self.file.declaration.job if self.file else None

    @property
    def rows(self) -> int:
        return sum(block.header.rows for block in self.blocks)

    @property
    def incomplete(self) -> int:
        """Rows that did not give every declared clock."""
        return sum(block.header.incomplete for block in self.blocks)

    def metrics(self) -> list[str]:
        """Names logged as numbers, in first-seen order."""
        return list(
            dict.fromkeys(
                column.name
                for block in self.blocks
                for column in block.header.series
                if column.kind == "metric"
            )
        )

    def points(self, metric: str, clock: str = WALL_CLOCK) -> Points:
        """The metric's values on the clock, by the reading rules in this module's note."""
        parts: list[tuple[Floats, Floats]] = []
        writers: dict[str, list[StoredBlock]] = {}
        for block in self.blocks:
            writers.setdefault(block.header.writer, []).append(block)
        attempts = {attempt.writer: attempt for attempt in self.attempts}
        for attempt in self.attempts:
            writers.setdefault(attempt.writer, [])
        # A resumed attempt cuts history even before it has logged any rows.
        ordered = sorted(
            writers,
            key=lambda writer: (
                attempts[writer].started_ms
                if writer in attempts
                else writers[writer][0].header.wall_first
            ),
        )
        # Work backwards so each writer sees the earliest cutoff of all later attempts.
        cutoff: float | None = None
        for writer in reversed(ordered):
            group = writers[writer]
            xs: list[Floats] = []
            ys: list[Floats] = []
            recorded = attempts.get(writer)
            resumed = recorded.resumed_at.get(clock) if recorded is not None else None
            for block in group:
                h = block.header
                if clock == WALL_CLOCK:
                    x = block.array(h.wall_ms, "<i8") / 1000
                else:
                    c = next((c for c in h.clocks if c.name == clock), None)
                    if c is None:
                        continue
                    x = block.array(c.data, "<f8")
                given = ~np.isnan(x)
                if resumed is None and given.any():
                    resumed = float(x[given][0])
                column = next(
                    (c for c in h.series if c.name == metric and c.kind == "metric"), None
                )
                if column is None:
                    continue
                if column.rows is not None:
                    x = x[block.array(column.rows, "<u4")]
                given = ~np.isnan(x)
                if cutoff is not None:
                    given &= x <= cutoff
                xs.append(x[given])
                ys.append(block.array(column.data, "<f8")[given])
            parts.append(
                (
                    np.concatenate(xs) if xs else np.empty(0),
                    np.concatenate(ys) if ys else np.empty(0),
                )
            )
            if resumed is not None:
                cutoff = resumed if cutoff is None else min(cutoff, resumed)
        if not parts:
            return Points(x=np.empty(0), y=np.empty(0))
        parts.reverse()  # Equal coordinates must still stay in write order.
        x, y = np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
        if len(x) > 1 and not np.all(x[1:] > x[:-1]):
            order = np.argsort(x, kind="stable")  # equal values stay in write order
            x, y = x[order], y[order]
            last = np.r_[x[1:] != x[:-1], True]  # the latest write of each value
            x, y = x[last], y[last]
        return Points(x=x, y=y)

    def chart(
        self,
        metric: str,
        clock: str = WALL_CLOCK,
        limit: int = 1000,
        *,
        span: tuple[float, float] | None = None,
    ) -> Buckets:
        """Read one chart using the finest affordable in-block roll-up.

        Group endpoints describe complete extents. Extremes, sums and counts
        are preserved; a bucket boundary can differ from raw by one group.
        Sparse metrics and repeated clocks use the exact raw path.
        """
        if limit < 1:
            raise ValueError("chart limit must be positive")
        chunks: list[tuple[Floats, Floats, np.ndarray[Any, Any]]] = []
        ranges = [_clock_range(block, clock) for block in self.blocks]
        # Where each writer's rows stop counting because a later attempt resumed.
        cutoffs = self._cutoffs(clock, ranges)
        if cutoffs is None:
            return buckets(self.points(metric, clock), limit, span=span)
        # For zooms use the window's fraction of the run to select a finer level.
        present = [bounds for bounds in ranges if bounds is not None]
        fraction = 1.0
        if span is not None and present:
            width = max(b[1] for b in present) - min(b[0] for b in present)
            fraction = min(1.0, max(0.0, span[1] - span[0]) / width) if width else 1.0
        target = max(1, int(self.rows * fraction) // (limit * 4))
        # An inline overview must have enough groups to fill the requested chart.
        inline_groups = sum(
            len(block.description.overview.series.get(metric, ()))
            for block in self.blocks
            if block.description.overview is not None
        )
        previous: float | None = None
        for block, bounds in zip(self.blocks, ranges):
            h = block.header
            column = next((c for c in h.series if c.name == metric and c.kind == "metric"), None)
            if column is None:
                continue
            if bounds is None:
                return buckets(self.points(metric, clock), limit, span=span)
            increasing = (
                h.wall_increasing
                if clock == WALL_CLOCK
                else next(c.increasing for c in h.clocks if c.name == clock)
            )
            cutoff = cutoffs[h.writer]
            if cutoff is not None and bounds[0] > cutoff:
                continue  # Every row here was superseded by a later attempt.
            # A block the cutoff falls inside is read raw, so its roll-up groups
            # are never split; blocks wholly before it keep their roll-ups.
            cut = cutoff if cutoff is not None and bounds[1] > cutoff else None
            if (
                not increasing
                or column.rows is not None
                or (previous is not None and bounds[0] <= previous)
            ):
                return buckets(self.points(metric, clock), limit, span=span)
            previous = bounds[1] if cut is None else cut
            if span is not None and (bounds[1] < span[0] or bounds[0] > span[1]):
                continue
            inline = block.description.overview
            if (
                span is None
                and cut is None
                and inline_groups >= limit
                and inline is not None
                and metric in inline.series
            ):
                endpoints = np.asarray(inline.clocks[clock], dtype="<f8")
                chunks.append(
                    (
                        endpoints[:, 0],
                        endpoints[:, 1],
                        np.asarray(inline.series[metric], dtype="<f8"),
                    )
                )
                continue
            levels = [level for level in h.rollups if level.factor <= target]
            if (
                levels
                and cut is None
                and (span is None or span[0] <= bounds[0] and bounds[1] <= span[1])
            ):
                level = levels[-1]
                c = next(c for c in level.clocks if c.name == clock)
                m = next(c for c in level.series if c.name == metric)
                endpoints = block.array(c.data, "<f8", 2)
                chunks.append((endpoints[:, 0], endpoints[:, 1], block.array(m.data, "<f8", 7)))
            else:
                # Raw is still column-selective: never fetch other metrics.
                x = (
                    block.array(h.wall_ms, "<i8") / 1000
                    if clock == WALL_CLOCK
                    else block.array(next(c.data for c in h.clocks if c.name == clock), "<f8")
                )
                y = block.array(column.data, "<f8")
                if cut is not None:
                    x, y = x[x <= cut], y[x <= cut]
                if span is not None:
                    inside = (x >= span[0]) & (x <= span[1])
                    x, y = x[inside], y[inside]
                if not len(x):
                    continue
                finite = np.isfinite(y)
                stats = np.column_stack(
                    (y, y, np.where(finite, y, 0), finite, y, y, np.ones(len(x)))
                )
                stats[~finite, :2] = np.nan
                chunks.append((x, x, stats))
        return _chart(chunks, limit, span)

    def _cutoffs(
        self, clock: str, ranges: Sequence[tuple[float, float] | None]
    ) -> dict[str, float | None] | None:
        """For each writer, the clock value from which a later attempt's rows replace its own.

        The rule is `points`'s, decided from block headers and attempt records
        alone: a later attempt takes over from where it resumed, or from its
        first row when it recorded no resume point. None when a header cannot
        say where a writer began, which sends the caller to the exact path.
        """
        began: dict[str, float] = {}
        started: dict[str, int] = {}
        for block, bounds in zip(self.blocks, ranges):
            writer = block.header.writer
            if writer not in started:
                started[writer] = block.header.wall_first
                if bounds is None:
                    return None
                began[writer] = bounds[0]
        for attempt in self.attempts:
            started[attempt.writer] = attempt.started_ms
            resumed = attempt.resumed_at.get(clock)
            if resumed is not None:
                began[attempt.writer] = resumed
        cutoffs: dict[str, float | None] = {}
        cutoff: float | None = None
        for writer in sorted(started, key=lambda writer: started[writer], reverse=True):
            cutoffs[writer] = cutoff
            if writer in began:
                cutoff = began[writer] if cutoff is None else min(cutoff, began[writer])
        return cutoffs


class Buckets(Arrays):
    """Points reduced to at most a fixed number of buckets, one entry per bucket.

    `min`, `max` and `mean` ignore non-finite values and are NaN for a bucket
    that has none finite. `first` and `last` are the values as logged.
    """

    x_first: SkipValidation[Floats]
    x_last: SkipValidation[Floats]
    first: SkipValidation[Floats]
    last: SkipValidation[Floats]
    min: SkipValidation[Floats]
    max: SkipValidation[Floats]
    mean: SkipValidation[Floats]
    count: SkipValidation[npt.NDArray[np.int64]]
    nonfinite: SkipValidation[npt.NDArray[np.int64]]
    # Which of the equal-width buckets over the span each entry is.
    index: SkipValidation[npt.NDArray[np.int64]]

    def __len__(self) -> int:
        return len(self.count)


def buckets(
    points: Points, limit: int = 1000, *, span: tuple[float, float] | None = None
) -> Buckets:
    """At most `limit` equal-width buckets over `span` (default: the points' range).

    With no span, points that already fit within `limit` are returned one per
    bucket, untouched.
    """
    x, y = points.x, points.y
    if span is not None:
        inside = (x >= span[0]) & (x <= span[1])
        x, y = x[inside], y[inside]
    if len(x) == 0:
        none = np.empty(0, dtype=np.float64)
        whole = np.empty(0, dtype=np.int64)
        return Buckets(
            x_first=none, x_last=none, first=none, last=none, min=none, max=none, mean=none,
            count=whole, nonfinite=whole, index=whole,
        )  # fmt: skip
    index = _bucket_indices(x, float(x[-1]), limit, span)
    starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
    ends = np.r_[starts[1:], len(x)] - 1
    finite = np.isfinite(y)
    counted = np.add.reduceat(finite.astype(np.int64), starts)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.add.reduceat(np.where(finite, y, 0.0), starts) / counted
    low_y = np.minimum.reduceat(np.where(finite, y, np.inf), starts)
    high_y = np.maximum.reduceat(np.where(finite, y, -np.inf), starts)
    count = ends - starts + 1
    return Buckets(
        x_first=x[starts],
        x_last=x[ends],
        first=y[starts],
        last=y[ends],
        min=np.where(counted > 0, low_y, np.nan),
        max=np.where(counted > 0, high_y, np.nan),
        mean=mean,
        count=count,
        nonfinite=count - counted,
        index=index[starts],
    )


def _chart(
    chunks: Sequence[tuple[Floats, Floats, np.ndarray[Any, Any]]],
    limit: int,
    span: tuple[float, float] | None = None,
) -> Buckets:
    if not chunks:
        return buckets(Points(x=np.empty(0), y=np.empty(0)), limit)
    first_x = np.concatenate([chunk[0] for chunk in chunks])
    last_x = np.concatenate([chunk[1] for chunk in chunks])
    stats = np.concatenate([chunk[2] for chunk in chunks])
    index = _bucket_indices(first_x, float(last_x[-1]), limit, span)
    starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
    ends = np.r_[starts[1:], len(first_x)] - 1
    finite = np.add.reduceat(stats[:, 3], starts)
    total = np.add.reduceat(stats[:, 6], starts).astype(np.int64)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.add.reduceat(stats[:, 2], starts) / finite
    return Buckets(
        x_first=first_x[starts],
        x_last=last_x[ends],
        first=stats[starts, 4],
        last=stats[ends, 5],
        min=np.fmin.reduceat(stats[:, 0], starts),
        max=np.fmax.reduceat(stats[:, 1], starts),
        mean=mean,
        count=total,
        nonfinite=total - finite.astype(np.int64),
        index=index[starts],
    )


def _bucket_indices(
    first: Floats, last: float, limit: int, span: tuple[float, float] | None
) -> npt.NDArray[np.int64]:
    """Use the requested span so curves from different runs share bucket coordinates."""
    if span is None and len(first) <= limit:
        return np.arange(len(first), dtype=np.int64)
    low, high = span or (float(first[0]), last)
    scale = limit / (high - low) if high > low else 0.0
    return np.minimum(limit - 1, ((first - low) * scale).astype(np.int64))


def _clock_range(block: StoredBlock, clock: str) -> tuple[float, float] | None:
    h = block.header
    if clock == WALL_CLOCK:
        return h.wall_first / 1000, h.wall_last / 1000
    column = next((c for c in h.clocks if c.name == clock), None)
    if column is None or column.first is None or column.last is None:
        return None
    return column.first, column.last


def read_all(source: Storage | Path | str, experiment: str | None = None) -> list[RunData]:
    """Every run under a source, or every run of one experiment, listing the source once."""
    storage = as_storage(source)
    data = []
    for run, folders in sorted(places(storage).items()):
        file = declared(storage, folders)
        if file is not None and experiment in (None, file.experiment):
            data.append(_read(storage, file.experiment, run, folders, run_file=file))
    return sorted(data, key=lambda one: (one.experiment, one.name))


def read(source: Storage | Path | str, experiment: str, run: str) -> RunData:
    """One run's declaration, state, rows and checkpoints. Rows not yet sealed are not read."""
    storage = as_storage(source)
    return _read(storage, experiment, run, places(storage).get(run, [run_prefix(run)]))


def _read(
    storage: Storage,
    experiment: str,
    run: str,
    places: Sequence[str],
    *,
    run_file: RunFile | None = None,
) -> RunData:
    def listing() -> list[tuple[str, str]]:
        """Every (folder, name under it), first folder first."""
        return [(place, key[len(place) :]) for place in places for key in storage.keys(place)]

    keys = listing()
    checkpoints: list[CheckpointRecord] = []
    attempts: list[tuple[Attempt, str]] = []
    for place, name in keys:
        kind, _, leaf = name.partition("/")
        if not leaf.endswith(".json") or "/" in leaf or kind not in RUN_FOLDERS:
            continue
        if kind == "metrics" and leaf.endswith((".index.json", ".permanent.json")):
            continue  # an index, read below
        body = storage.get(place + name)
        try:
            if kind == "checkpoints":
                checkpoints.append(CheckpointRecord.model_validate_json(body))
            else:
                attempts.append((Attempt.model_validate_json(body), place))
        except ValidationError:
            continue
    attempts.sort(key=lambda pair: pair[0].started_ms)
    blocks: list[StoredBlock] = []
    for retry in range(3):
        try:
            descriptions: dict[str, BlockDescription] = {}
            # A writer is one process on one machine, so a block's name is in one folder.
            folder: dict[str, str] = {}
            for place, name in keys:
                if name.startswith("metrics/") and name.endswith(
                    (".index.json", ".permanent.json")
                ):
                    manifest = Manifest.model_validate_json(storage.get(place + name))
                    for block in manifest.blocks:
                        descriptions[block.key] = block
                        folder.setdefault(block.key, place)
                elif parse(name) is not None:
                    folder.setdefault(name, place)
            candidates = [file for name in folder if (file := parse(name)) is not None]
            selected = select([*candidates, *(d.file() for d in descriptions.values())])
            expected: dict[str, int] = {}
            blocks = []
            for file in selected:
                if file.first != expected.get(file.writer, 0):
                    raise FileNotFoundError("block sequence gap")
                expected[file.writer] = file.last + 1
                place = folder[file.key]
                description = descriptions.get(file.key)
                if description is None and file.level < 2:
                    # A recent block: small, and in no manifest, so take it whole.
                    blocks.append(fetch(storage, place, file.key))
                    continue
                if description is None:
                    description = describe(storage, place, file.key)
                blocks.append(StoredBlock(storage, place + file.key, description))
            break
        except FileNotFoundError:
            if retry == 2:
                raise
            keys = listing()
    started = {attempt.writer: attempt.started_ms for attempt, _ in attempts}
    blocks.sort(
        key=lambda block: (
            started.get(block.header.writer, block.header.wall_first),
            block.header.first,
        )
    )
    names = {(place, name) for place, name in keys}
    declared = next((place for place in places if (place, "run.json") in names), None)
    # The state the latest attempt left, where it left one.
    ended = [place for place in places if (place, "state.json") in names]
    latest = next((place for _, place in reversed(attempts) if place in ended), None)
    state = latest or (ended[-1] if ended else None)
    run_state = RunState.model_validate_json(storage.get(state + "state.json")) if state else None
    if run_state is not None and attempts:
        # A completion belongs to one writer, not every later resume. Older
        # evidence without attribution is conclusive only for a single attempt.
        current = run_state.writer == attempts[-1][0].writer
        unattributed = run_state.writer is None and len(attempts) == 1
        if not current and not unattributed:
            run_state = None
    return RunData(
        experiment=experiment,
        name=run,
        file=run_file
        if run_file is not None
        else RunFile.model_validate_json(storage.get(declared + "run.json"))
        if declared
        else None,
        state=run_state,
        blocks=tuple(blocks),
        # By time: each machine numbers its own checkpoints from 1.
        checkpoints=tuple(sorted(checkpoints, key=lambda record: (record.wall_ms, record.ordinal))),
        attempts=tuple(attempt for attempt, _ in attempts),
    )
