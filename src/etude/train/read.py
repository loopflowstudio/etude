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
`read` holds a whole run; `read_batches` streams selected metrics a
stretch of overlapping blocks at a time, planned from headers (`_stretches`),
so it holds one stretch's arrays plus a few numbers per block.
`RunData.points` is the same plan read all at once.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict, SkipValidation, ValidationError

from etude.run.block_file import BlockFile, parse, select
from etude.run.listing import RUN_FOLDERS, as_storage, declared, places
from etude.run.records import WALL_CLOCK, Attempt, Clock, Record, RunFile, RunState
from etude.run.storage import Storage, run_prefix
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


class Batch(Arrays):
    """One stretch of a run's history on one clock: each selected metric's points in it."""

    points: dict[str, Points]


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
        located = {block.key: block for block in self.blocks}
        placed = [_place(block, clock) for block in self.blocks]
        parts = [
            _batch(stretch, located.__getitem__, (metric,), clock).points[metric]
            for stretch in _stretches(placed, self.attempts, clock)
        ]
        return _latest([p.x for p in parts], [p.y for p in parts])

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


class _Given(NamedTuple):
    """The lowest, highest and first clock value a block's rows give."""

    low: float
    high: float
    first: float


@dataclass(frozen=True, slots=True)
class _Placed:
    """What planning needs of one block on one clock, small so a long run's plan stays small."""

    key: str  # the block's location in its storage
    writer: str
    sequence: int
    wall_first: int
    given: _Given | None  # None when no row gives the clock


@dataclass(frozen=True, slots=True)
class _Extent:
    """The rows of one block that count on one clock, whose values lie from `low` to `high`."""

    key: str
    # Write order across the run: the writer's place, then the block's.
    order: tuple[int, int]
    low: float
    high: float
    # Rows above this value belong to a later attempt.
    cutoff: float | None


def _clock(block: StoredBlock, clock: str) -> Floats:
    h = block.header
    if clock == WALL_CLOCK:
        return block.array(h.wall_ms, "<i8") / 1000
    return block.array(next(c.data for c in h.clocks if c.name == clock), "<f8")


def _place(block: StoredBlock, clock: str) -> _Placed:
    """An increasing column's header gives its range; any other column is read once."""
    h = block.header
    given: _Given | None = None
    if clock == WALL_CLOCK:
        if h.rows and h.wall_increasing:
            given = _Given(h.wall_first / 1000, h.wall_last / 1000, h.wall_first / 1000)
        readable = h.rows > 0
    else:
        column = next((c for c in h.clocks if c.name == clock), None)
        first, last = (column.first, column.last) if column is not None else (None, None)
        if column is not None and column.increasing and first is not None and last is not None:
            given = _Given(first, last, first)
        readable = column is not None
    if given is None and readable:
        x = _clock(block, clock)
        x = x[~np.isnan(x)]
        if len(x):
            given = _Given(float(x.min()), float(x.max()), float(x[0]))
    return _Placed(block.key, h.writer, h.first, h.wall_first, given)


def _position(
    writer: str, sequence: int, wall_first: int, started: dict[str, int]
) -> tuple[int, int]:
    """A block's place in write order: its process's start, then its sequence."""
    return started.get(writer, wall_first), sequence


def _stretches(
    placed: Sequence[_Placed], attempts: Sequence[Attempt], clock: str
) -> list[list[_Extent]]:
    """The blocks whose rows count on the clock, ascending, grouped where their ranges overlap.

    `placed` is in write order. Each group keeps it, so the latest write of a
    clock value wins within the group, and no value appears in two groups.
    """
    writers: dict[str, list[_Placed]] = {}
    for block in placed:
        writers.setdefault(block.writer, []).append(block)
    recorded = {attempt.writer: attempt for attempt in attempts}
    for attempt in attempts:
        writers.setdefault(attempt.writer, [])
    # A resumed attempt cuts history even before it has logged any rows.
    ordered = sorted(
        writers,
        key=lambda writer: (
            recorded[writer].started_ms if writer in recorded else writers[writer][0].wall_first
        ),
    )
    # Work backwards so each writer sees the earliest cutoff of all later attempts.
    cutoffs: dict[str, float | None] = {}
    cutoff: float | None = None
    for writer in reversed(ordered):
        cutoffs[writer] = cutoff
        record = recorded.get(writer)
        resumed = record.resumed_at.get(clock) if record is not None else None
        if resumed is None:
            resumed = next((b.given.first for b in writers[writer] if b.given is not None), None)
        if resumed is not None:
            cutoff = resumed if cutoff is None else min(cutoff, resumed)
    rank = {writer: place for place, writer in enumerate(ordered)}
    extents: list[_Extent] = []
    for index, block in enumerate(placed):
        if block.given is None:
            continue
        low, high = block.given.low, block.given.high
        cut = cutoffs[block.writer]
        if cut is not None:
            if low > cut:
                continue  # Every row here was superseded by a later attempt.
            high = min(high, cut)
        extents.append(_Extent(block.key, (rank[block.writer], index), low, high, cut))
    stretches: list[list[_Extent]] = []
    reach = -np.inf
    for extent in sorted(extents, key=lambda extent: extent.low):
        if stretches and extent.low <= reach:
            stretches[-1].append(extent)
            reach = max(reach, extent.high)
        else:
            stretches.append([extent])
            reach = extent.high
    return [sorted(stretch, key=lambda extent: extent.order) for stretch in stretches]


def _batch(
    stretch: Sequence[_Extent],
    blocks: Callable[[str], StoredBlock],
    metrics: Sequence[str],
    clock: str,
    after: float | None = None,
) -> Batch:
    """The metrics' points within one stretch, above `after` when given."""
    xs: dict[str, list[Floats]] = {metric: [] for metric in metrics}
    ys: dict[str, list[Floats]] = {metric: [] for metric in metrics}
    for extent in stretch:
        block = blocks(extent.key)
        columns = [
            column
            for column in block.header.series
            if column.kind == "metric" and column.name in xs
        ]
        if not columns:
            continue
        logged = _clock(block, clock)
        for column in columns:
            x = logged if column.rows is None else logged[block.array(column.rows, "<u4")]
            keep = ~np.isnan(x)
            if extent.cutoff is not None:
                keep &= x <= extent.cutoff
            if after is not None:
                keep &= x > after
            xs[column.name].append(x[keep])
            ys[column.name].append(block.array(column.data, "<f8")[keep])
    return Batch(points={metric: _latest(xs[metric], ys[metric]) for metric in xs})


def _latest(xs: Sequence[Floats], ys: Sequence[Floats]) -> Points:
    """Points from parts in write order, keeping the latest write of each clock value."""
    x = np.concatenate(xs) if xs else np.empty(0)
    y = np.concatenate(ys) if ys else np.empty(0)
    if len(x) > 1 and not np.all(x[1:] > x[:-1]):
        order = np.argsort(x, kind="stable")  # equal values stay in write order
        x, y = x[order], y[order]
        last = np.r_[x[1:] != x[:-1], True]
        x, y = x[last], y[last]
    return Points(x=x, y=y)


def _order(record: CheckpointRecord) -> tuple[int, int]:
    return record.ordinal, record.wall_ms


def _here(
    record: CheckpointRecord, storage: Storage, place: str, keys: set[tuple[str, str]]
) -> CheckpointRecord:
    """The record with its file's address in this storage, where the file sits beside it.

    The record names the file where it was written, which may be a machine
    that no longer exists. A missing file leaves the record as it is.
    """
    name = "checkpoints/" + record.artifact.uri.rpartition("/")[2]
    if record.artifact.sha256 is None or (place, name) not in keys:
        return record
    artifact = record.artifact.model_copy(update={"uri": storage.uri(place + name)})
    return record.model_copy(update={"artifact": artifact})


def read_all(
    source: Storage | Path | str,
    experiment: str | None = None,
    *,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
) -> list[RunData]:
    """Every run under a source, listing it once.

    With `metrics`, retain only these keys and `prefixes`, reading immutable
    headers one at a time instead of the cumulative metric indexes.
    """
    storage = as_storage(source)
    data = []
    for run, folders in sorted(places(storage).items()):
        file = declared(storage, folders)
        if file is not None and experiment in (None, file.experiment):
            data.append(
                _read(
                    storage,
                    file.experiment,
                    run,
                    folders,
                    run_file=file,
                    metrics=metrics,
                    prefixes=prefixes,
                )
            )
    return sorted(data, key=lambda one: (one.experiment, one.name))


def read(source: Storage | Path | str, experiment: str, run: str) -> RunData:
    """One run's declaration, state, rows and checkpoints. Rows not yet sealed are not read."""
    storage = as_storage(source)
    return _read(storage, experiment, run, places(storage).get(run, [run_prefix(run)]))


def read_batches(
    source: Storage | Path | str,
    experiment: str,
    run: str,
    *,
    clock: str = WALL_CLOCK,
    metrics: Sequence[str],
) -> Iterator[Batch]:
    """A run's history on one clock, one stretch at a time.

    Memory is one stretch's selected arrays plus a few numbers per block.

    One metric's points concatenated across the batches are exactly
    `read(...).points(metric, clock)`. A first pass reads each header and keeps
    those numbers; each batch reads its blocks' headers again with
    their selected arrays. No metric index is loaded. A block a compaction
    replaced is found again from a fresh listing, resuming after the last batch
    yielded; a block still missing after fresh listings, a sequence gap or a
    damaged array raises during iteration. A writer's newest blocks missing
    from storage cannot be told from a run still being delivered.
    """
    storage = as_storage(source)
    folders = places(storage).get(run, [run_prefix(run)])
    selected = frozenset(metrics)

    def block(key: str) -> StoredBlock:
        return StoredBlock(storage, key, describe(storage, "", key, metrics=selected))

    done: float | None = None  # the highest clock value already yielded
    failures = 0  # fresh listings in a row that yielded nothing new
    while True:
        try:
            keys = _listing(storage, folders)
            _, attempts = _records(storage, keys)
            started = {attempt.writer: attempt.started_ms for attempt, _ in attempts}
            placed = sorted(
                (
                    _place(block(place + file.key), clock)
                    for place, file, _ in _selected(storage, keys, indexes=False)
                ),
                key=lambda p: _position(p.writer, p.sequence, p.wall_first, started),
            )
            for stretch in _stretches(placed, [attempt for attempt, _ in attempts], clock):
                high = max(extent.high for extent in stretch)
                if done is not None and high <= done:
                    continue
                yield _batch(stretch, block, metrics, clock, after=done)
                done = high
                failures = 0
            return
        except FileNotFoundError:
            # A live writer replaces blocks every few seconds, so a slow
            # consumer may meet several; only a listing that cannot make
            # progress three times is a missing block.
            failures += 1
            if failures == 3:
                raise


def _listing(storage: Storage, folders: Sequence[str]) -> list[tuple[str, str]]:
    """Every (folder, name under it), first folder first."""
    return [(place, key[len(place) :]) for place in folders for key in storage.keys(place)]


def _records(
    storage: Storage, keys: Sequence[tuple[str, str]]
) -> tuple[list[CheckpointRecord], list[tuple[Attempt, str]]]:
    """The run's checkpoint records, and its attempts in start order with their folders."""
    present = set(keys)
    checkpoints: list[CheckpointRecord] = []
    attempts: list[tuple[Attempt, str]] = []
    for place, name in keys:
        kind, _, leaf = name.partition("/")
        if not leaf.endswith(".json") or "/" in leaf or kind not in RUN_FOLDERS:
            continue
        if kind == "metrics" and leaf.endswith((".index.json", ".permanent.json")):
            continue  # an index, read with the blocks
        body = storage.get(place + name)
        try:
            if kind == "checkpoints":
                checkpoints.append(
                    _here(CheckpointRecord.model_validate_json(body), storage, place, present)
                )
            else:
                attempts.append((Attempt.model_validate_json(body), place))
        except ValidationError:
            continue
    attempts.sort(key=lambda pair: pair[0].started_ms)
    return checkpoints, attempts


def _selected(
    storage: Storage, keys: Sequence[tuple[str, str]], *, indexes: bool
) -> list[tuple[str, BlockFile, BlockDescription | None]]:
    """The blocks that hold the run's rows, each with its folder and any index description.

    Raises FileNotFoundError for a gap in a writer's sequence, which a fresh
    listing may close.
    """
    descriptions: dict[str, BlockDescription] = {}
    # A writer is one process on one machine, so a block's name is in one folder.
    folder: dict[str, str] = {}
    for place, name in keys:
        if name.startswith("metrics/") and name.endswith((".index.json", ".permanent.json")):
            if not indexes:
                continue
            manifest = Manifest.model_validate_json(storage.get(place + name))
            for described in manifest.blocks:
                descriptions[described.key] = described
                folder.setdefault(described.key, place)
        elif parse(name) is not None:
            folder.setdefault(name, place)
    candidates = [file for name in folder if (file := parse(name)) is not None]
    expected: dict[str, int] = {}
    selected = []
    for file in select([*candidates, *(d.file() for d in descriptions.values())]):
        if file.first != expected.get(file.writer, 0):
            raise FileNotFoundError("block sequence gap")
        expected[file.writer] = file.last + 1
        selected.append((folder[file.key], file, descriptions.get(file.key)))
    return selected


def _read(
    storage: Storage,
    experiment: str,
    run: str,
    folders: Sequence[str],
    *,
    run_file: RunFile | None = None,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
) -> RunData:
    keys = _listing(storage, folders)
    checkpoints, attempts = _records(storage, keys)
    blocks: list[StoredBlock] = []
    for retry in range(3):
        try:
            blocks = []
            for place, file, description in _selected(storage, keys, indexes=metrics is None):
                if description is None and file.level < 2 and metrics is None:
                    # A recent block: small, and in no manifest, so take it whole.
                    blocks.append(fetch(storage, place, file.key))
                    continue
                if description is None:
                    description = describe(
                        storage, place, file.key, metrics=metrics, prefixes=prefixes
                    )
                blocks.append(StoredBlock(storage, place + file.key, description))
            break
        except FileNotFoundError:
            if retry == 2:
                raise
            keys = _listing(storage, folders)
    started = {attempt.writer: attempt.started_ms for attempt, _ in attempts}
    blocks.sort(
        key=lambda block: _position(
            block.header.writer, block.header.first, block.header.wall_first, started
        )
    )
    names = {(place, name) for place, name in keys}
    if run_file is None:
        declaring = next((place for place in folders if (place, "run.json") in names), None)
        if declaring is not None:
            run_file = RunFile.model_validate_json(storage.get(declaring + "run.json"))
    # The state the latest attempt left, where it left one.
    ended = [place for place in folders if (place, "state.json") in names]
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
        file=run_file,
        state=run_state,
        blocks=tuple(blocks),
        # A later attempt continues its run's ordinals; each is kept once.
        checkpoints=tuple({r.ordinal: r for r in sorted(checkpoints, key=_order)}.values()),
        attempts=tuple(attempt for attempt, _ in attempts),
    )
