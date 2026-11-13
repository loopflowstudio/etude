"""Read runs back from their files, in a directory or a bucket, with no server.

The files are the record, so this reader is the reference for what they mean.
A run's rows are one set across the processes that wrote them. For one metric
against one clock: a row counts when it gives that clock; an earlier process's
rows count strictly before a later attempt's first logged value on that clock.
Historical attempts with checkpoint-based resume coordinates keep their old
inclusive cutoff. An attempt with no rows and no recorded cutoff hides nothing.
A later write at the same clock value replaces an earlier one.
Superseded rows stay on disk. Buckets keep their extremes.

Rows are read by column (`etude.train.block`), so a chart costs two arrays.
`read` holds a whole run; `read_batches` streams selected metrics a
stretch of overlapping blocks at a time, planned from headers (`_stretches`),
so it holds one stretch's arrays plus a few numbers per block.
`RunData.points` is the same plan read all at once.
A run's records (declaration, state, attempts, checkpoints) are read when it
is read; its metric blocks only the first time `RunData.blocks` is used, so a
reader of checkpoints, or of an eval run, reads no metric data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict, PrivateAttr, SkipValidation, ValidationError

from etude.carriage import checkpoint_key
from etude.tracking.block_file import BlockFile, parse, select
from etude.tracking.checkpoints import CheckpointRecord
from etude.tracking.errors import EtudeError
from etude.tracking.listing import as_storage, declared, run_folders, under
from etude.tracking.records import (
    HOURS_SINCE_START,
    RUNNING_HOURS,
    Attempt,
    Clock,
    Record,
    RunFile,
    RunState,
)
from etude.tracking.storage import Storage, run_prefix
from etude.train.block import (
    COUNT,
    DTYPES,
    FINITE,
    FIRST,
    LAST,
    MAX,
    MIN,
    STATISTICS,
    SUM,
    Columns,
    SeriesColumn,
)
from etude.train.manifest import BlockDescription, Manifest, StoredBlock, describe, fetch
from etude.train.records import RowTable, TablePoint

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
    # In ordinal order, one per ordinal: a later attempt continues its run's ordinals.
    checkpoints: tuple[CheckpointRecord, ...]
    # Every process that wrote to this run, in start order.
    attempts: tuple[Attempt, ...]
    _read_blocks: Callable[[], tuple[StoredBlock, ...]] = PrivateAttr()

    @cached_property
    def blocks(self) -> tuple[StoredBlock, ...]:
        """The stored blocks of every process that wrote rows, in the order the processes started.

        Read from storage the first time it is used, keeping the metrics `read_all` was given.
        """
        return self._read_blocks()

    @cached_property
    def columns(self) -> tuple[Columns, ...]:
        """Each block's rows by column. Raw inspection loads whole blocks; chart reads do not."""
        return tuple(block.columns() for block in self.blocks)

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
    def writer_starts(self) -> dict[str, int]:
        """When each process that wrote to this run started, in milliseconds since the epoch.

        Its attempt's `started_ms`, else, for a process that recorded no
        attempt, the earliest wall time its blocks give.
        """
        return _writer_starts(
            self.attempts, ((b.header.writer, b.header.wall_first) for b in self.blocks)
        )

    @property
    def incomplete(self) -> int:
        """Rows that did not give every declared clock."""
        return sum(block.header.incomplete for block in self.blocks)

    @property
    def reached(self) -> dict[str, float]:
        """Latest position per clock, with later attempts superseding earlier ones.

        Historical resume coordinates count even before a row. An empty new
        attempt leaves the previous position intact.
        """
        positions = {attempt.writer: dict(attempt.resumed_at) for attempt in self.attempts}
        starts = self.writer_starts
        for block in self.blocks:
            position = positions.setdefault(block.header.writer, {})
            for column in block.header.clocks:
                if column.last is not None:
                    position[column.name] = column.last
        reached: dict[str, float] = {}
        for writer in sorted(positions, key=lambda writer: starts[writer]):
            reached.update(positions[writer])
        return reached

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

    def points(self, metric: str, clock: str = HOURS_SINCE_START) -> Points:
        """The metric's values on the clock, by the reading rules in this module's note."""
        located = {block.key: block for block in self.blocks}
        placed = [_place(block, clock) for block in self.blocks]
        parts = [
            _batch(stretch, located.__getitem__, (metric,), clock).points[metric]
            for stretch in _stretches(placed, self.attempts, clock)
        ]
        return _latest_writes([p.x for p in parts], [p.y for p in parts])

    def read_table(self, name: str, clock: str) -> tuple[TablePoint, ...]:
        """Read selected JSON tables, in clock order; missing or damaged artifacts raise.

        Resolve only after supersession, using the selected block's own storage
        and run prefix. Unreferenced artifacts never participate in history.
        """
        located = {block.key: block for block in self.blocks}
        placed = [_place(block, clock) for block in self.blocks]
        points: list[TablePoint] = []
        for stretch in _stretches(placed, self.attempts, clock):
            # Counted rows in write order: their clock values, and each one's block and digest.
            xs: list[Floats] = []
            tables: list[tuple[StoredBlock, str | None]] = []
            for extent in stretch:
                block = located[extent.key]
                column = next(
                    (c for c in block.header.series if c.kind == "text" and c.name == name), None
                )
                if column is None:
                    continue
                x, keep = _counted(block, column, _clock(block, clock), extent)
                codes = block.array(column.data, DTYPES["text"])[keep]
                xs.append(x[keep])
                tables.extend((block, column.labels[int(code)]) for code in codes)
            if not xs:
                continue
            x = np.concatenate(xs)
            for index in _last_indices(x):
                points.append(TablePoint(x=float(x[index]), rows=_table(*tables[index]).rows))
        return tuple(points)

    def chart(
        self,
        metric: str,
        clock: str = HOURS_SINCE_START,
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
        located = {block.key: block for block in self.blocks}
        stretches = _stretches([_place(b, clock) for b in self.blocks], self.attempts, clock)
        # Blocks whose counted rows overlap repeat clock values, and only the
        # exact path picks the latest write among them.
        if any(len(stretch) > 1 for stretch in stretches):
            return buckets(self.points(metric, clock), limit, span=span)
        extents = [stretch[0] for stretch in stretches]
        # For zooms use the window's fraction of the run to select a finer level.
        fraction = 1.0
        if span is not None and extents:
            width = extents[-1].high - extents[0].low
            fraction = min(1.0, max(0.0, span[1] - span[0]) / width) if width else 1.0
        target = max(1, int(self.rows * fraction) // (limit * 4))
        # An inline overview must have enough groups to fill the requested chart.
        inline_groups = sum(
            len(block.description.overview.series.get(metric, ()))
            for block in self.blocks
            if block.description.overview is not None
        )
        groups: list[_Groups] = []
        for extent in extents:
            block = located[extent.key]
            h = block.header
            column = next((c for c in h.series if c.name == metric and c.kind == "metric"), None)
            if column is None:
                continue
            bounds = _clock_range(block, clock)
            increasing = next(c.increasing for c in h.clocks if c.name == clock)
            if not increasing or bounds is None or column.rows is not None:
                return buckets(self.points(metric, clock), limit, span=span)
            # A block the cutoff falls inside is read raw, so its roll-up groups
            # are never split; blocks wholly before it keep their roll-ups.
            cut = extent.cutoff if extent.cutoff is not None and bounds[1] > extent.cutoff else None
            if span is not None and (extent.high < span[0] or extent.low > span[1]):
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
                statistics = np.asarray(inline.series[metric], dtype="<f8")
                groups.append(_Groups(endpoints[:, 0], endpoints[:, 1], statistics))
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
                statistics = block.array(m.data, "<f8", STATISTICS)
                groups.append(_Groups(endpoints[:, 0], endpoints[:, 1], statistics))
            else:
                # Raw is still column-selective: never fetch other metrics.
                x = _clock(block, clock)
                y = block.array(column.data, "<f8")
                if cut is not None:
                    x, y = x[x <= cut], y[x <= cut]
                if span is not None:
                    inside = (x >= span[0]) & (x <= span[1])
                    x, y = x[inside], y[inside]
                if not len(x):
                    continue
                # Each row is a group of one.
                finite = np.isfinite(y)
                statistics = np.empty((len(x), STATISTICS))
                statistics[:, MIN] = statistics[:, MAX] = np.where(finite, y, np.nan)
                statistics[:, SUM] = np.where(finite, y, 0)
                statistics[:, FINITE] = finite
                statistics[:, FIRST] = statistics[:, LAST] = y
                statistics[:, COUNT] = 1
                groups.append(_Groups(x, x, statistics))
        return _chart(groups, limit, span)


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


@dataclass(frozen=True, slots=True)
class _Groups:
    """Consecutive row groups of one metric: each group's clock endpoints and statistics."""

    x_first: Floats
    x_last: Floats
    # One row per group, of `etude.train.block`'s `STATISTICS` columns.
    statistics: Floats


def _chart(
    groups: Sequence[_Groups],
    limit: int,
    span: tuple[float, float] | None = None,
) -> Buckets:
    if not groups:
        return buckets(Points(x=np.empty(0), y=np.empty(0)), limit)
    first_x = np.concatenate([each.x_first for each in groups])
    last_x = np.concatenate([each.x_last for each in groups])
    stats = np.concatenate([each.statistics for each in groups])
    index = _bucket_indices(first_x, float(last_x[-1]), limit, span)
    starts = np.flatnonzero(np.r_[True, index[1:] != index[:-1]])
    ends = np.r_[starts[1:], len(first_x)] - 1
    finite = np.add.reduceat(stats[:, FINITE], starts)
    total = np.add.reduceat(stats[:, COUNT], starts).astype(np.int64)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.add.reduceat(stats[:, SUM], starts) / finite
    return Buckets(
        x_first=first_x[starts],
        x_last=last_x[ends],
        first=stats[starts, FIRST],
        last=stats[ends, LAST],
        min=np.fmin.reduceat(stats[:, MIN], starts),
        max=np.fmax.reduceat(stats[:, MAX], starts),
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
    column = next((c for c in block.header.clocks if c.name == clock), None)
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
    return block.array(next(c.data for c in h.clocks if c.name == clock), "<f8")


def _place(block: StoredBlock, clock: str) -> _Placed:
    """An increasing column's header gives its range; any other column is read once."""
    h = block.header
    given: _Given | None = None
    column = next((c for c in h.clocks if c.name == clock), None)
    if column is None:
        return _Placed(block.key, h.writer, h.first, h.wall_first, None)
    if column.increasing and column.first is not None and column.last is not None:
        given = _Given(column.first, column.last, column.first)
    else:
        x = _clock(block, clock)
        x = x[~np.isnan(x)]
        if len(x):
            given = _Given(float(x.min()), float(x.max()), float(x[0]))
    return _Placed(block.key, h.writer, h.first, h.wall_first, given)


def _writer_starts(
    attempts: Sequence[Attempt], blocks: Iterable[tuple[str, int]]
) -> dict[str, int]:
    """When each writer started: its attempt's `started_ms`, else its blocks' earliest wall time.

    `blocks` gives each block's writer and first wall time. A block's place in
    write order is its writer's start, then its sequence.
    """
    unrecorded: dict[str, int] = {}
    for writer, wall_first in blocks:
        unrecorded[writer] = min(unrecorded.get(writer, wall_first), wall_first)
    return unrecorded | {attempt.writer: attempt.started_ms for attempt in attempts}


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
    # Historical attempts may have a cutoff even without any logged rows.
    starts = _writer_starts(attempts, ((block.writer, block.wall_first) for block in placed))
    ordered = sorted(writers, key=starts.__getitem__)
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
            # Old checkpoint cuts retained the coordinate itself. New first-row
            # cuts replace it even when that row omits a particular metric.
            if record is not None and record.first_row:
                resumed = float(np.nextafter(resumed, -np.inf))
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
            x, keep = _counted(block, column, logged, extent, after)
            xs[column.name].append(x[keep])
            ys[column.name].append(block.array(column.data, "<f8")[keep])
    return Batch(points={metric: _latest_writes(xs[metric], ys[metric]) for metric in xs})


def _latest_writes(xs: Sequence[Floats], ys: Sequence[Floats]) -> Points:
    """Points from parts in write order, keeping the latest write of each clock value."""
    x = np.concatenate(xs) if xs else np.empty(0)
    y = np.concatenate(ys) if ys else np.empty(0)
    selected = _last_indices(x)
    return Points(x=x[selected], y=y[selected])


def _last_indices(x: Floats) -> npt.NDArray[np.int64]:
    """Indices of `x` in ascending order, keeping the last of each repeated value."""
    if len(x) < 2 or np.all(x[1:] > x[:-1]):
        return np.arange(len(x), dtype=np.int64)
    order = np.argsort(x, kind="stable")  # equal values stay in write order
    ordered = x[order]
    return order[np.r_[ordered[1:] != ordered[:-1], True]]


def _counted(
    block: StoredBlock,
    column: SeriesColumn,
    logged: Floats,
    extent: _Extent,
    after: float | None = None,
) -> tuple[Floats, npt.NDArray[np.bool_]]:
    """A column's clock values, and which of its rows count in the extent (above `after`)."""
    x = logged if column.rows is None else logged[block.array(column.rows, "<u4")]
    keep = ~np.isnan(x)
    if extent.cutoff is not None:
        keep &= x <= extent.cutoff
    if after is not None:
        keep &= x > after
    return x, keep


def _table(block: StoredBlock, digest: str | None) -> RowTable:
    """The JSON table a text row names by digest, stored beside the block's run."""
    if digest is None or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise EtudeError("table_reference_invalid", 422) from ValueError(digest)
    prefix = block.key.removesuffix(block.description.key)
    key = f"{prefix}artifacts/{digest}.json"
    body = block.storage.get(key)
    if sha256(body).hexdigest() != digest:
        raise EtudeError("table_artifact_damaged", 422) from ValueError(key)
    return RowTable.model_validate_json(body)


def _order(record: CheckpointRecord) -> tuple[int, int]:
    return record.ordinal, record.wall_ms or 0


def _hours(
    records: Iterable[CheckpointRecord], attempts: Sequence[Attempt], *, continued: bool
) -> tuple[CheckpointRecord, ...]:
    """Records in ordinal order, where one saved before Etude's clocks gets them from `wall_ms`.

    `hours_since_start` counts from the run's first attempt; `running_hours`
    adds the time since the attempt that saved it began to where that attempt
    resumed. Either stays absent where the line began in a parent, or the
    checkpoint an attempt resumed from is unknown.
    """
    done: list[CheckpointRecord] = []
    for record in records:
        if record.wall_ms is None:
            done.append(record)
            continue
        at = dict(record.at)
        began = [a for a in attempts if a.started_ms <= record.wall_ms]
        if began and not continued:
            at.setdefault(HOURS_SINCE_START, (record.wall_ms - attempts[0].started_ms) / 3_600_000)
        if began and RUNNING_HOURS not in at:
            attempt = began[-1]
            if not attempt.resumed_at:
                base = None if continued else 0.0
            else:
                resumed = next(
                    (
                        d
                        for d in reversed(done)
                        if all(d.at.get(k) == v for k, v in attempt.resumed_at.items())
                    ),
                    None,
                )
                base = None if resumed is None else resumed.at.get(RUNNING_HOURS)
            if base is not None:
                at[RUNNING_HOURS] = base + (record.wall_ms - attempt.started_ms) / 3_600_000
        done.append(record if at == record.at else record.model_copy(update={"at": at}))
    return tuple(done)


def _here(
    record: CheckpointRecord, storage: Storage, folder: str, keys: set[tuple[str, str]]
) -> CheckpointRecord:
    """The record with its file's address in this storage, where the file sits beside it.

    The record names the file where it was written, which may be a machine
    that no longer exists. A missing file leaves the record as it is.
    """
    name = record.file or checkpoint_key(record.artifact.uri)
    present = (
        all((folder, name + "/" + member) in keys for member in record.artifact.members)
        if record.artifact.members
        else (folder, name) in keys
    )
    if record.artifact.sha256 is None or not present:
        return record
    artifact = record.artifact.model_copy(update={"uri": storage.uri(folder + name)})
    return record.model_copy(update={"artifact": artifact})


def lines(data: Sequence[RunData]) -> dict[str, str]:
    """Each run's learner line: the first run of the chain of continuations that led to it,
    as far as `data` knows. A run and the runs continuing it are one learner, one seed."""
    parents = {
        run.name: run.file.declaration.continues.parent.source_id
        for run in data
        if run.file is not None and run.file.declaration.continues is not None
    }
    found = {}
    for run in data:
        first, seen = run.name, {run.name}
        while first in parents and parents[first] not in seen:
            first = parents[first]
            seen.add(first)
        found[run.name] = first
    return found


def read_all(
    source: Storage | Path | str,
    experiment: str | None = None,
    *,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
) -> list[RunData]:
    """Every run under a source, listing it once.

    With `metrics`, retain only these keys and `prefixes`, reading each
    immutable block once instead of the cumulative metric indexes.
    """
    storage = as_storage(source)
    keys = sorted(storage.keys(""))
    listed = set(keys)
    data = []
    for run, folders in sorted(run_folders(keys).items()):
        file = declared(storage, folders, listed)
        if file is not None and experiment in (None, file.experiment):
            data.append(
                _read(
                    storage,
                    file.experiment,
                    run,
                    folders,
                    keys,
                    run_file=file,
                    metrics=metrics,
                    prefixes=prefixes,
                )
            )
    return sorted(data, key=lambda one: (one.experiment, one.name))


def read(source: Storage | Path | str, experiment: str, run: str) -> RunData:
    """One run's declaration, state, rows and checkpoints. Rows not yet sealed are not read."""
    storage = as_storage(source)
    keys = sorted(storage.keys(""))
    folders = run_folders(keys).get(run, [run_prefix(run)])
    file = declared(storage, folders, set(keys))
    return _read(storage, experiment, run, folders, keys, run_file=file)


def read_batches(
    source: Storage | Path | str,
    experiment: str,
    run: str,
    *,
    clock: str = HOURS_SINCE_START,
    metrics: Sequence[str],
) -> Iterator[Batch]:
    """A run's history on one clock, one stretch at a time.

    Memory is one stretch's selected arrays plus a few numbers per block.

    One metric's points concatenated across the batches are exactly
    `read(...).points(metric, clock)`. A first pass reads each block and keeps
    those numbers; each batch reads its blocks again for their selected arrays. No metric index is loaded. A block a compaction
    replaced is found again from a fresh listing, resuming after the last batch
    yielded; a block still missing after fresh listings, a sequence gap or a
    damaged array raises during iteration. A writer's newest blocks missing
    from storage cannot be told from a run still being delivered.

    `metrics` is a list of names: a name no block logged as a metric raises
    `EtudeError("metric_not_logged")` before the first batch, so a misspelt
    name is not mistaken for an empty history.
    """
    if isinstance(metrics, str):
        raise TypeError(f"metrics is a list of names; pass [{metrics!r}]")
    storage = as_storage(source)
    folders = run_folders(sorted(storage.keys(""))).get(run, [run_prefix(run)])
    selected = frozenset(metrics)

    def block(key: str) -> StoredBlock:
        return StoredBlock(storage, key, describe(storage, "", key, metrics=selected))

    done: float | None = None  # the highest clock value already yielded
    failures = 0  # fresh listings in a row that yielded nothing new
    while True:
        try:
            keys = _listing(storage, folders)
            attempts = _records(storage, keys).attempts
            logged: set[str] = set()
            placed: list[_Placed] = []
            for chosen in _selected(storage, keys, indexes=False):
                # The whole header plans the block and says which metrics it logged.
                key = chosen.file.key
                whole = StoredBlock(
                    storage, chosen.folder + key, describe(storage, chosen.folder, key)
                )
                logged.update(c.name for c in whole.header.series if c.kind == "metric")
                placed.append(_place(whole, clock))
            if missing := sorted(selected - logged):
                raise EtudeError("metric_not_logged", 404) from KeyError(", ".join(missing))
            starts = _writer_starts(attempts, ((p.writer, p.wall_first) for p in placed))
            placed.sort(key=lambda p: (starts[p.writer], p.sequence))
            for stretch in _stretches(placed, attempts, clock):
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


def _named(keys: Sequence[str], folders: Sequence[str]) -> list[tuple[str, str]]:
    """Every (folder, name under it) of a sorted listing, first folder first."""
    return [(folder, key[len(folder) :]) for folder in folders for key in under(keys, folder)]


def _listing(storage: Storage, folders: Sequence[str]) -> list[tuple[str, str]]:
    """Every (folder, name under it), listed afresh, first folder first."""
    return _named(sorted(k for f in folders for k in storage.keys(f)), folders)


@dataclass(frozen=True)
class _Records:
    """A training run's records, from every folder of it."""

    checkpoints: list[CheckpointRecord]
    # Every process that wrote to the run, once each, in start order.
    attempts: tuple[Attempt, ...]
    # By writer, the folder its attempt record was first found in.
    folder_of: dict[str, str]


def _records(storage: Storage, keys: Sequence[tuple[str, str]]) -> _Records:
    """The run's checkpoint records and its attempts, with their folders."""
    present = set(keys)
    checkpoints: list[CheckpointRecord] = []
    attempts: dict[str, Attempt] = {}
    folder_of: dict[str, str] = {}
    for folder, name in keys:
        kind, _, leaf = name.partition("/")
        # A training run's records: `metrics/WRITER.json`, an attempt, and
        # `checkpoints/NAME.json`. Nothing else, such as an eval run's files, is read.
        if kind not in ("metrics", "checkpoints") or "/" in leaf or not leaf.endswith(".json"):
            continue
        if kind == "metrics" and leaf.endswith((".index.json", ".permanent.json")):
            continue  # an index, read with the blocks
        body = storage.get(folder + name)
        try:
            if kind == "checkpoints":
                checkpoints.append(
                    _here(CheckpointRecord.model_validate_json(body), storage, folder, present)
                )
            else:
                attempt = Attempt.model_validate_json(body)
                attempts.setdefault(attempt.writer, attempt)
                folder_of.setdefault(attempt.writer, folder)
        except ValidationError:
            continue
    ordered = tuple(sorted(attempts.values(), key=lambda attempt: attempt.started_ms))
    return _Records(checkpoints=checkpoints, attempts=ordered, folder_of=folder_of)


@dataclass(frozen=True)
class _Selected:
    """A block that holds some of the run's rows, in the folder it was found in."""

    folder: str
    file: BlockFile
    # Its description from a writer's index, when one was read.
    description: BlockDescription | None


def _selected(
    storage: Storage, keys: Sequence[tuple[str, str]], *, indexes: bool
) -> list[_Selected]:
    """The blocks that hold the run's rows, each with its folder and any index description.

    Raises FileNotFoundError for a gap in a writer's sequence, which a fresh
    listing may close.
    """
    descriptions: dict[str, BlockDescription] = {}
    # A writer is one process on one machine, so a block's name is in one folder.
    folder_of: dict[str, str] = {}
    for folder, name in keys:
        if name.startswith("metrics/") and name.endswith((".index.json", ".permanent.json")):
            if not indexes:
                continue
            manifest = Manifest.model_validate_json(storage.get(folder + name))
            for described in manifest.blocks:
                descriptions[described.key] = described
                folder_of.setdefault(described.key, folder)
        elif parse(name) is not None:
            folder_of.setdefault(name, folder)
    candidates = [file for name in folder_of if (file := parse(name)) is not None]
    expected: dict[str, int] = {}
    selected = []
    for file in select([*candidates, *(d.file() for d in descriptions.values())]):
        if file.first != expected.get(file.writer, 0):
            raise FileNotFoundError("block sequence gap")
        expected[file.writer] = file.last + 1
        selected.append(_Selected(folder_of[file.key], file, descriptions.get(file.key)))
    return selected


def _read(
    storage: Storage,
    experiment: str,
    run: str,
    folders: Sequence[str],
    listing: Sequence[str],
    *,
    run_file: RunFile | None,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
) -> RunData:
    """The run's records from `listing`, the source's sorted keys; its blocks once used."""
    keys = _named(listing, folders)
    present = set(keys)
    records = _records(storage, keys)
    attempts = records.attempts
    # The state the latest attempt left, where it left one.
    # `state.json` is the historical name; where both exist `outcome.json` wins.
    ended = {
        folder: name
        for folder in folders
        for name in ("state.json", "outcome.json")
        if (folder, name) in present
    }
    left = (records.folder_of[attempt.writer] for attempt in reversed(attempts))
    latest = next((folder for folder in left if folder in ended), None)
    state = latest or (next(reversed(ended)) if ended else None)
    run_state = RunState.model_validate_json(storage.get(state + ended[state])) if state else None
    if run_state is not None and attempts:
        # A completion belongs to one writer, not every later resume. Older
        # evidence without attribution is conclusive only for a single attempt.
        current = run_state.writer == attempts[-1].writer
        unattributed = run_state.writer is None and len(attempts) == 1
        if not current and not unattributed:
            run_state = None
    data = RunData(
        experiment=experiment,
        name=run,
        file=run_file,
        state=run_state,
        # A later attempt continues its run's ordinals; each is kept once.
        checkpoints=_hours(
            {r.ordinal: r for r in sorted(records.checkpoints, key=_order)}.values(),
            attempts,
            continued=run_file is not None and run_file.declaration.continues is not None,
        ),
        attempts=attempts,
    )
    data._read_blocks = lambda: _blocks(storage, folders, keys, attempts, metrics, prefixes)
    return data


def _blocks(
    storage: Storage,
    folders: Sequence[str],
    keys: list[tuple[str, str]],
    attempts: Sequence[Attempt],
    metrics: frozenset[str] | None,
    prefixes: tuple[str, ...],
) -> tuple[StoredBlock, ...]:
    """The run's metric blocks in write order, listing again when compaction removed one."""
    blocks: list[StoredBlock] = []
    for retry in range(3):
        try:
            blocks = []
            for chosen in _selected(storage, keys, indexes=metrics is None):
                folder, file, description = chosen.folder, chosen.file, chosen.description
                if description is None and file.level < 2 and metrics is None:
                    # A recent block: small, and in no manifest, so take it whole.
                    blocks.append(fetch(storage, folder, file.key))
                    continue
                if description is None:
                    description = describe(
                        storage, folder, file.key, metrics=metrics, prefixes=prefixes
                    )
                blocks.append(StoredBlock(storage, folder + file.key, description))
            break
        except FileNotFoundError:
            if retry == 2:
                raise
            keys = _listing(storage, folders)
    starts = _writer_starts(attempts, ((b.header.writer, b.header.wall_first) for b in blocks))
    blocks.sort(key=lambda block: (starts[block.header.writer], block.header.first))
    return tuple(blocks)
