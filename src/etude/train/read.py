"""Read runs back from their files, in a directory or a bucket, with no server.

The files are the record, so this reader is the reference for what they mean.
A run's rows are one set across the processes that wrote them. For one metric
against one clock: a row counts when it gives that clock; an earlier process's
rows count strictly before a later attempt's first logged value on that clock.
Historical writers with checkpoint-based resume coordinates keep their old
inclusive cutoff. An attempt with no rows and no recorded cutoff hides nothing.
A later write at the same clock value replaces an earlier one.
Superseded rows stay on disk. Buckets keep their extremes.

Rows are read by column (`etude.train.block`), so a chart costs two arrays.
`read` holds a whole run; `RunData.points` reads a metric one stretch of
overlapping blocks at a time, planned from headers (`_stretches`).
A run's records (declaration, outcome, writers, checkpoints) are read when it
is read; its metric blocks only the first time `RunData.blocks` is used, so a
reader of checkpoints, or of an eval run, reads no metric data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from hashlib import sha256
from pathlib import Path
from typing import Literal, NamedTuple

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict, PrivateAttr, SkipValidation, ValidationError

from etude.block_file import BlockFile, parse, select
from etude.carriage import checkpoint_key
from etude.checkpoints import CheckpointRecord
from etude.errors import EtudeError
from etude.identity import run_address, stored_owner
from etude.out import MEMBERSHIP, OutFiles, OutMember
from etude.records import (
    HOURS_SINCE_START,
    RUNNING_HOURS,
    Clock,
    Record,
    RunOutcome,
    RunRecord,
    RunWriter,
)
from etude.run import LocatedRun, RunListing, as_storage, located_runs, under
from etude.storage import Storage, run_prefix
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

__all__ = (
    "read",
    "read_all",
    "RunData",
    "Points",
    "buckets",
    "Buckets",
    "lines",
)


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
    file: RunRecord | None
    outcome: RunOutcome | None
    # In ordinal order, one per ordinal: a later attempt continues its run's ordinals.
    checkpoints: tuple[CheckpointRecord, ...]
    # Every process that wrote to this run, in start order.
    writers: tuple[RunWriter, ...]
    # Latest modification in this run's listed folders, when the source supplied dates.
    last_write: datetime | None = None
    _read_blocks: Callable[[], tuple[StoredBlock, ...]] = PrivateAttr()
    _read_out: Callable[[], tuple[OutMember, ...]] = PrivateAttr(default=tuple)

    @property
    def address(self) -> str:
        """Preserve a declared address's spelling, including older compact runs."""
        return run_address(self.file.id if self.file else self.name, self.experiment)

    @property
    def stored_state(self) -> Literal["running", "stopped", "finished", "failed"]:
        return self.outcome.state if self.outcome else "running"

    @cached_property
    def out(self) -> tuple[OutMember, ...]:
        """Stored membership read once, without reading any out bytes."""
        return self._read_out()

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
        return self.file.clocks if self.file else ()

    @property
    def job(self) -> str | None:
        return self.file.job if self.file else None

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
            self.writers, ((b.header.writer, b.header.wall_first) for b in self.blocks)
        )

    @property
    def incomplete(self) -> int:
        """Rows that did not give every declared clock."""
        return sum(block.header.incomplete for block in self.blocks)

    @property
    def reached(self) -> dict[str, float]:
        """Latest position per clock, with later writers superseding earlier ones.

        Historical resume coordinates count even before a row. An empty new
        attempt leaves the previous position intact.
        """
        positions = {attempt.writer: dict(attempt.resumed_at) for attempt in self.writers}
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
            _stretch_points(stretch, located.__getitem__, metric, clock)
            for stretch in _stretches(placed, self.writers, clock)
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
        for stretch in _stretches(placed, self.writers, clock):
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
        stretches = _stretches([_place(b, clock) for b in self.blocks], self.writers, clock)
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
    writers: Sequence[RunWriter], blocks: Iterable[tuple[str, int]]
) -> dict[str, int]:
    """When each writer started: its attempt's `started_ms`, else its blocks' earliest wall time.

    `blocks` gives each block's writer and first wall time. A block's place in
    write order is its writer's start, then its sequence.
    """
    unrecorded: dict[str, int] = {}
    for writer, wall_first in blocks:
        unrecorded[writer] = min(unrecorded.get(writer, wall_first), wall_first)
    return unrecorded | {attempt.writer: attempt.started_ms for attempt in writers}


def _stretches(
    placed: Sequence[_Placed], writers: Sequence[RunWriter], clock: str
) -> list[list[_Extent]]:
    """The blocks whose rows count on the clock, ascending, grouped where their ranges overlap.

    `placed` is in write order. Each group keeps it, so the latest write of a
    clock value wins within the group, and no value appears in two groups.
    """
    blocks_by_writer: dict[str, list[_Placed]] = {}
    for block in placed:
        blocks_by_writer.setdefault(block.writer, []).append(block)
    recorded = {attempt.writer: attempt for attempt in writers}
    for attempt in writers:
        blocks_by_writer.setdefault(attempt.writer, [])
    # Historical writers may have a cutoff even without any logged rows.
    starts = _writer_starts(writers, ((block.writer, block.wall_first) for block in placed))
    ordered = sorted(blocks_by_writer, key=starts.__getitem__)
    # Work backwards so each writer sees the earliest cutoff of all later writers.
    cutoffs: dict[str, float | None] = {}
    cutoff: float | None = None
    for writer in reversed(ordered):
        cutoffs[writer] = cutoff
        record = recorded.get(writer)
        resumed = record.resumed_at.get(clock) if record is not None else None
        if resumed is None:
            resumed = next(
                (b.given.first for b in blocks_by_writer[writer] if b.given is not None), None
            )
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


def _stretch_points(
    stretch: Sequence[_Extent], blocks: Callable[[str], StoredBlock], metric: str, clock: str
) -> Points:
    """One metric's points within one stretch."""
    xs: list[Floats] = []
    ys: list[Floats] = []
    for extent in stretch:
        block = blocks(extent.key)
        logged: Floats | None = None
        for column in block.header.series:
            if column.kind == "metric" and column.name == metric:
                logged = _clock(block, clock) if logged is None else logged
                x, keep = _counted(block, column, logged, extent)
                xs.append(x[keep])
                ys.append(block.array(column.data, "<f8")[keep])
    return _latest_writes(xs, ys)


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
) -> tuple[Floats, npt.NDArray[np.bool_]]:
    """A column's clock values, and which of its rows count in the extent."""
    x = logged if column.rows is None else logged[block.array(column.rows, "<u4")]
    keep = ~np.isnan(x)
    if extent.cutoff is not None:
        keep &= x <= extent.cutoff
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
    records: Iterable[CheckpointRecord], writers: Sequence[RunWriter], *, continued: bool
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
        began = [a for a in writers if a.started_ms <= record.wall_ms]
        if began and not continued:
            at.setdefault(HOURS_SINCE_START, (record.wall_ms - writers[0].started_ms) / 3_600_000)
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
    return record.model_copy(update={"artifact": artifact, "folder": folder.rstrip("/")})


def lines(data: Sequence[RunData]) -> dict[str, str]:
    """Each run's learner line: the first run of the chain of continuations that led to it,
    as far as `data` knows. A run and the runs continuing it are one learner, one seed."""
    parents = {
        run.name: run.file.continues.parent.source_id
        for run in data
        if run.file is not None and run.file.continues is not None
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
    keys: Sequence[str] | None = None,
    declarations: Sequence[LocatedRun] | None = None,
) -> list[RunData]:
    """Every run under a source, listing it once.

    With `metrics`, retain only these keys and `prefixes`, reading each
    immutable block once instead of the cumulative metric indexes.
    A caller supplying its own keys and declarations also supplies any write dates.
    """
    storage = as_storage(source)
    if keys is None and declarations is None:
        listing = RunListing.read(storage)
        keys, declarations = listing.keys, listing.declarations
    else:
        keys = sorted(storage.versions("") if keys is None else keys)
        if declarations is None:
            declarations = located_runs(storage, keys)
    data = []
    for located in declarations:
        file = located.file
        if experiment in (None, file.experiment):
            data.append(read_located(storage, located, keys, metrics=metrics, prefixes=prefixes))
    return sorted(data, key=lambda one: (one.experiment, one.name))


def read_located(
    storage: Storage,
    run: LocatedRun,
    keys: Sequence[str],
    *,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
    whole_blocks: bool = False,
) -> RunData:
    """Read a declaration's copies from a sorted listing. A whole-run chart
    build traverses every numeric column, so it may acquire each block whole."""
    return _read(
        storage,
        run.file.experiment,
        run.name,
        run.folders,
        keys,
        declaration=run,
        metrics=metrics,
        prefixes=prefixes,
        whole_blocks=whole_blocks,
    )


def read(source: Storage | Path | str, experiment: str, run: str) -> RunData:
    """One run's declaration, state, rows and checkpoints. Rows not yet sealed are not read."""
    storage = as_storage(source)
    run = run.removeprefix(experiment + "/")
    listing = RunListing.read(storage, run=run)
    for located in listing.declarations:
        if located.file.experiment == experiment:
            return read_located(storage, located, listing.keys)
    # Only a name nobody declared can hold historical rows without a declaration.
    folders = [] if listing.declarations else listing.folders.get(run, [run_prefix(run)])
    return _read(storage, experiment, run, folders, listing.keys, declaration=None)


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
    writers: tuple[RunWriter, ...]
    # By writer, the folder its attempt record was first found in.
    folder_of: dict[str, str]


def _records(storage: Storage, keys: Sequence[tuple[str, str]]) -> _Records:
    """The run's checkpoint records and its writers, with their folders."""
    present = set(keys)
    checkpoints: list[CheckpointRecord] = []
    writers: dict[str, RunWriter] = {}
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
                attempt = RunWriter.model_validate_json(body)
                writers.setdefault(attempt.writer, attempt)
                folder_of.setdefault(attempt.writer, folder)
        except ValidationError:
            continue
    ordered = tuple(sorted(writers.values(), key=lambda attempt: attempt.started_ms))
    return _Records(checkpoints=checkpoints, writers=ordered, folder_of=folder_of)


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
    declaration: LocatedRun | None,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
    whole_blocks: bool = False,
) -> RunData:
    """The run's records from `listing`, the source's sorted keys; its blocks once used."""
    run_file = declaration.file if declaration else None
    keys = _named(listing, folders)
    present = set(keys)
    records = _records(storage, keys)
    writers = records.writers
    # The outcome the latest attempt left, where it left one.
    # `state.json` is the historical name; where both exist `outcome.json` wins.
    ended = {
        folder: name
        for folder in folders
        for name in ("state.json", "outcome.json")
        if (folder, name) in present
    }
    left = (records.folder_of[attempt.writer] for attempt in reversed(writers))
    latest = next((folder for folder in left if folder in ended), None)
    folder = latest or (next(reversed(ended)) if ended else None)
    outcome = (
        RunOutcome.model_validate_json(storage.get(folder + ended[folder])) if folder else None
    )
    if outcome is not None and writers:
        # A completion belongs to one writer, not every later resume. Older
        # evidence without attribution is conclusive only for a single attempt.
        current = outcome.writer == writers[-1].writer
        unattributed = outcome.writer is None and len(writers) == 1
        if not current and not unattributed:
            outcome = None
    data = RunData(
        experiment=experiment,
        name=run,
        file=run_file,
        last_write=declaration.last_write if declaration else None,
        outcome=outcome,
        # A later attempt continues its run's ordinals; each is kept once.
        checkpoints=_hours(
            {r.ordinal: r for r in sorted(records.checkpoints, key=_order)}.values(),
            writers,
            continued=run_file is not None and run_file.continues is not None,
        ),
        writers=writers,
    )
    data._read_blocks = lambda: _blocks(
        storage, folders, keys, writers, metrics, prefixes, whole_blocks
    )
    data._read_out = lambda: _read_out(storage, experiment, run, folders, present)
    return data


def _read_out(
    storage: Storage,
    experiment: str,
    run: str,
    folders: Sequence[str],
    present: set[tuple[str, str]],
) -> tuple[OutMember, ...]:
    for folder in folders:
        if (folder, MEMBERSHIP) not in present:
            continue
        membership = OutFiles.model_validate_json(storage.get(folder + MEMBERSHIP))
        owner = stored_owner(run, experiment, folder.rstrip("/"))
        return tuple(
            OutMember(
                run=owner,
                key=member.key,
                sha256=member.sha256,
                size_bytes=member.size_bytes,
                uri=storage.uri(folder + member.key),
            )
            for member in membership.files
            if (folder, member.key) in present
        )
    return ()


def _blocks(
    storage: Storage,
    folders: Sequence[str],
    keys: list[tuple[str, str]],
    writers: Sequence[RunWriter],
    metrics: frozenset[str] | None,
    prefixes: tuple[str, ...],
    whole_blocks: bool,
) -> tuple[StoredBlock, ...]:
    """The run's metric blocks in write order, listing again when compaction removed one."""
    blocks: list[StoredBlock] = []
    for retry in range(3):
        try:
            blocks = []
            for chosen in _selected(storage, keys, indexes=metrics is None and not whole_blocks):
                folder, file, description = chosen.folder, chosen.file, chosen.description
                if whole_blocks or (description is None and file.level < 2 and metrics is None):
                    # All columns are needed, or this small recent block has no manifest.
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
    starts = _writer_starts(writers, ((b.header.writer, b.header.wall_first) for b in blocks))
    blocks.sort(key=lambda block: (starts[block.header.writer], block.header.first))
    return tuple(blocks)
