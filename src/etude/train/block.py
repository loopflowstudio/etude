"""Columnar blocks: how a run's logged rows are stored and read.

`Columns` is rows held by column: wall time, one array per declared clock, and
one `Series` per logged name. A block is one `Columns` written to one
immutable file. `Builder` turns rows into columns as they are logged; `concat`
joins a writer's consecutive columns when small blocks are merged into large
ones. These are not the JSON row tables of `log_table` (`RowTable`).

File layout, all little-endian; readers depend on it, so a change needs a new VERSION:

    0   8   magic b"ETUDEBLK"
    8   4   format version, u32
    12  4   header length H, u32
    16  H   header, UTF-8 JSON (`Header`)
    ...     zero padding to a 64-byte boundary: the data start
    ...     arrays, each on a 64-byte boundary, at the offsets the header gives
            relative to the data start
    -8  4   CRC-32 of every byte before it
    -4  4   magic b"KLB1"

The header comes first so one read of the front of a file says where every
column is. A reader takes only the arrays it wants. Row order is write order.
A clock a row did not give is NaN in that clock's array. A name logged on only
some rows carries the row numbers where it is present.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from pydantic import ConfigDict

from etude.run.errors import EtudeError
from etude.run.records import Record

MAGIC, END, VERSION, ALIGN = b"ETUDEBLK", b"KLB1", 1, 64

type Json = float | int | str | bool | None
type Number = float | Literal["NaN", "Infinity", "-Infinity"]
# A number is charted. A flag is a logged bool. Text is anything else, kept as written.
type Kind = Literal["metric", "flag", "text"]
# One-dimensional; float64, int64, uint32 or uint8 according to what the column holds.
type Array = npt.NDArray[Any]
DTYPES: dict[str, str] = {"metric": "<f8", "flag": "|u1", "text": "<u4"}

# The columns of a metric's statistics over one row group, in stored order: the
# finite minimum, maximum and sum, how many values are finite, the first and
# last values as logged, and how many there are.
MIN, MAX, SUM, FINITE, FIRST, LAST, COUNT = range(7)
STATISTICS = 7


class LoggedRow(Record):
    """One row as it was logged: when, where each clock stood, and every other value.

    A clock the row did not give is absent from `clocks`. A value may be a
    non-finite number, as logged.
    """

    model_config = ConfigDict(
        defer_build=True,
        frozen=True,
        extra="forbid",
        allow_inf_nan=True,
        ser_json_inf_nan="constants",
    )

    wall_ms: int
    clocks: dict[str, float]
    values: dict[str, Json]


@dataclass(frozen=True)
class Series:
    """One logged name of one kind within a block's `Columns`."""

    # Row numbers where it is present, ascending; None when it is on every row.
    rows: Array | None
    data: Array
    # Text only: `data` holds positions in this tuple.
    labels: tuple[str | None, ...] = ()


@dataclass(frozen=True)
class Reduction:
    """Consecutive row groups: clock endpoints and metric statistics.

    Each metric is one row per group of the `STATISTICS` columns (`MIN` to `COUNT`).
    Sparse metrics stay on the raw read path because their clock endpoints differ.
    """

    clocks: dict[str, Array]
    series: dict[str, Array]


@dataclass(frozen=True)
class Columns:
    """Rows by column, from one writer, covering sequences `first` to `last` inclusive."""

    writer: str
    first: int
    last: int
    wall_ms: Array
    clocks: dict[str, Array]
    series: dict[tuple[str, Kind], Series]
    reductions: dict[int, Reduction] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return len(self.wall_ms)

    def row(self, index: int) -> LoggedRow:
        """One row as it was logged. For inspection and export, not for charts."""
        values: dict[str, Json] = {}
        for (name, kind), series in self.series.items():
            if series.rows is None:
                at = index
            else:
                at = int(np.searchsorted(series.rows, index))
                if at >= len(series.rows) or series.rows[at] != index:
                    continue
            item = series.data[at]
            values[name] = (
                float(item)
                if kind == "metric"
                else bool(item)
                if kind == "flag"
                else series.labels[int(item)]
            )
        clocks = {
            name: float(a[index]) for name, a in self.clocks.items() if not np.isnan(a[index])
        }
        return LoggedRow(wall_ms=int(self.wall_ms[index]), clocks=clocks, values=values)


class Builder:
    """Collects rows by column as they are logged."""

    def __init__(self, clocks: Sequence[str]) -> None:
        self._clocks: dict[str, list[float]] = {name: [] for name in clocks}
        self._wall: list[int] = []
        self._rows: dict[tuple[str, Kind], list[int]] = {}
        self._data: dict[tuple[str, Kind], list[float | int]] = {}
        self._labels: dict[str, dict[str | None, int]] = {}

    def __len__(self) -> int:
        return len(self._wall)

    def add(self, wall_ms: int, clocks: Mapping[str, float], values: Mapping[str, object]) -> None:
        row = len(self._wall)
        self._wall.append(wall_ms)
        for name, column in self._clocks.items():
            column.append(clocks.get(name, np.nan))
        for name, value in values.items():
            item: float | int
            if isinstance(value, bool):
                key: tuple[str, Kind] = (name, "flag")
                item = int(value)
            elif isinstance(value, (int, float)):
                key, item = (name, "metric"), float(value)
            else:
                key = (name, "text")
                labels = self._labels.setdefault(name, {})
                text = None if value is None else str(value)
                item = labels.setdefault(text, len(labels))
            self._rows.setdefault(key, []).append(row)
            self._data.setdefault(key, []).append(item)

    def columns(self, writer: str, sequence: int) -> Columns:
        count = len(self._wall)
        series: dict[tuple[str, Kind], Series] = {}
        for key, data in self._data.items():
            series[key] = Series(
                rows=None if len(data) == count else np.asarray(self._rows[key], dtype="<u4"),
                data=np.asarray(data, dtype=DTYPES[key[1]]),
                labels=tuple(self._labels.get(key[0], {})) if key[1] == "text" else (),
            )
        return Columns(
            writer=writer,
            first=sequence,
            last=sequence,
            wall_ms=np.asarray(self._wall, dtype="<i8"),
            clocks={name: np.asarray(column, dtype="<f8") for name, column in self._clocks.items()},
            series=series,
        )


def concat(pieces: Sequence[Columns]) -> Columns:
    """One `Columns` from a writer's consecutive ones, in the order given."""
    starts = np.cumsum([0, *(columns.rows for columns in pieces)])
    names = dict.fromkeys(name for columns in pieces for name in columns.clocks)
    clocks = {
        name: np.concatenate(
            [columns.clocks.get(name, np.full(columns.rows, np.nan)) for columns in pieces]
        )
        for name in names
    }
    series: dict[tuple[str, Kind], Series] = {}
    for key in dict.fromkeys(key for columns in pieces for key in columns.series):
        parts = [
            (columns.series.get(key), int(start), columns.rows)
            for columns, start in zip(pieces, starts)
        ]
        labels: dict[str | None, int] = {}
        data: list[Array] = []
        for part, _, _ in parts:
            if part is None:
                continue
            if key[1] == "text":
                codes = np.asarray([labels.setdefault(label, len(labels)) for label in part.labels])
                data.append(codes.astype("<u4")[part.data] if len(part.data) else part.data)
            else:
                data.append(part.data)
        everywhere = all(part is not None and part.rows is None for part, _, _ in parts)
        series[key] = Series(
            rows=None
            if everywhere
            else np.concatenate(
                [
                    (np.arange(count, dtype="<u4") if part.rows is None else part.rows) + start
                    for part, start, count in parts
                    if part is not None
                ]
            ).astype("<u4"),
            data=np.concatenate(data),
            labels=tuple(labels),
        )
    return Columns(
        writer=pieces[0].writer,
        first=min(columns.first for columns in pieces),
        last=max(columns.last for columns in pieces),
        wall_ms=np.concatenate([columns.wall_ms for columns in pieces]),
        clocks=clocks,
        series=series,
    )


class ArrayLocation(Record):
    """Where one array sits, relative to the data start."""

    offset: int
    length: int  # bytes
    crc32: int


class ClockColumn(Record):
    name: str
    data: ArrayLocation
    # True when every row gives it and each value is above the last.
    increasing: bool
    first: float | None = None
    last: float | None = None
    nonfinite: int = 0


class SeriesColumn(Record):
    name: str
    kind: Kind
    count: int
    rows: ArrayLocation | None
    data: ArrayLocation
    labels: tuple[str | None, ...] = ()
    latest: Number | None = None


class RollupColumn(Record):
    factor: int
    count: int
    clocks: tuple[ClockColumn, ...]
    series: tuple[SeriesColumn, ...]


class Header(Record):
    """What a block holds and where. Unknown fields are ignored, so it can grow."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    writer: str
    first: int
    last: int
    rows: int
    wall_ms: ArrayLocation
    clocks: tuple[ClockColumn, ...]
    series: tuple[SeriesColumn, ...]
    rollups: tuple[RollupColumn, ...] = ()
    wall_first: int = 0
    wall_last: int = 0
    wall_increasing: bool = False
    incomplete: int = 0


@dataclass
class _Layout:
    """Arrays placed one after another on aligned boundaries."""

    chunks: list[bytes] = field(default_factory=list)
    size: int = 0

    def place(self, array: Array) -> ArrayLocation:
        body = array.tobytes()
        location = ArrayLocation(offset=self.size, length=len(body), crc32=zlib.crc32(body))
        padded = body + bytes(-len(body) % ALIGN)
        self.chunks.append(padded)
        self.size += len(padded)
        return location


def encode_number(value: float) -> Number:
    """A number in strict JSON, retaining which non-finite value was logged."""
    if np.isfinite(value):
        return value
    return "NaN" if np.isnan(value) else "Infinity" if value > 0 else "-Infinity"


def reduce(columns: Columns, factor: int) -> Reduction:
    """Reduce by row order, keeping finite sums and the logged first/last values."""
    starts = np.arange(0, columns.rows, factor)
    ends = np.minimum(starts + factor, columns.rows) - 1
    clocks = {
        name: np.column_stack((data[starts], data[ends])) for name, data in columns.clocks.items()
    }
    series: dict[str, Array] = {}
    for (name, kind), column in columns.series.items():
        if kind != "metric":
            continue
        data = column.data
        row_numbers = np.arange(columns.rows) if column.rows is None else column.rows
        groups = row_numbers // factor
        metric_starts = (
            np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]])
            if len(data)
            else np.empty(0, dtype=np.int64)
        )
        metric_ends = np.r_[metric_starts[1:], len(data)] - 1
        stats = np.full((len(starts), STATISTICS), np.nan)
        stats[:, [SUM, FINITE, COUNT]] = 0
        if len(data):
            finite = np.isfinite(data)
            count = np.add.reduceat(finite.astype("<i8"), metric_starts)
            present = groups[metric_starts]
            stats[present, MIN] = np.where(
                count, np.minimum.reduceat(np.where(finite, data, np.inf), metric_starts), np.nan
            )
            stats[present, MAX] = np.where(
                count, np.maximum.reduceat(np.where(finite, data, -np.inf), metric_starts), np.nan
            )
            stats[present, SUM] = np.add.reduceat(np.where(finite, data, 0), metric_starts)
            stats[present, FINITE] = count
            stats[present, FIRST] = data[metric_starts]
            stats[present, LAST] = data[metric_ends]
            stats[present, COUNT] = metric_ends - metric_starts + 1
        series[name] = stats
    return Reduction(clocks=clocks, series=series)


def encode(columns: Columns) -> bytes:
    """The columns as one block file."""
    layout = _Layout()
    rollups: list[RollupColumn] = []
    for factor in (16, 256, 4096):
        if columns.rows < factor:
            continue
        reduced = reduce(columns, factor)
        rollups.append(
            RollupColumn(
                factor=factor,
                count=(columns.rows + factor - 1) // factor,
                clocks=tuple(
                    ClockColumn(name=name, data=layout.place(data), increasing=False)
                    for name, data in reduced.clocks.items()
                ),
                series=tuple(
                    SeriesColumn(
                        name=name,
                        kind="metric",
                        count=len(data),
                        rows=None,
                        data=layout.place(data),
                    )
                    for name, data in reduced.series.items()
                ),
            )
        )
    header = Header(
        writer=columns.writer,
        first=columns.first,
        last=columns.last,
        rows=columns.rows,
        rollups=tuple(rollups),
        wall_first=int(columns.wall_ms[0]) if columns.rows else 0,
        wall_last=int(columns.wall_ms[-1]) if columns.rows else 0,
        wall_increasing=bool(np.all(columns.wall_ms[1:] > columns.wall_ms[:-1])),
        incomplete=int(np.isnan(np.stack(list(columns.clocks.values()))).any(axis=0).sum())
        if columns.clocks
        else 0,
        wall_ms=layout.place(columns.wall_ms),
        clocks=tuple(
            ClockColumn(
                name=name,
                data=layout.place(column),
                first=float(column[0]) if len(column) and np.isfinite(column[0]) else None,
                last=float(column[-1]) if len(column) and np.isfinite(column[-1]) else None,
                nonfinite=int((~np.isfinite(column)).sum()),
                increasing=bool(len(column) < 2 or np.all(column[1:] > column[:-1]))
                and not bool(np.isnan(column).any()),
            )
            for name, column in columns.clocks.items()
        ),
        series=tuple(
            SeriesColumn(
                name=name,
                kind=kind,
                count=len(series.data),
                rows=None if series.rows is None else layout.place(series.rows),
                data=layout.place(series.data),
                labels=series.labels,
                latest=encode_number(float(series.data[-1]))
                if kind == "metric" and len(series.data)
                else None,
            )
            for (name, kind), series in columns.series.items()
        ),
    )
    described = header.model_dump_json().encode()
    front = MAGIC + VERSION.to_bytes(4, "little") + len(described).to_bytes(4, "little") + described
    body = front + bytes(-len(front) % ALIGN) + b"".join(layout.chunks)
    return body + zlib.crc32(body).to_bytes(4, "little") + END


def header(block: bytes) -> tuple[Header, int]:
    """A block's header and the position its arrays are measured from."""
    if block[:8] != MAGIC or int.from_bytes(block[8:12], "little") != VERSION:
        raise EtudeError("block_not_recognised", 422)
    length = int.from_bytes(block[12:16], "little")
    described = Header.model_validate_json(block[16 : 16 + length])
    return described, 16 + length + (-(16 + length) % ALIGN)


def decode(block: bytes) -> Columns:
    """A whole block as its `Columns`. A truncated or altered file is refused."""
    if block[-4:] != END or zlib.crc32(block[:-8]) != int.from_bytes(block[-8:-4], "little"):
        raise EtudeError("block_damaged", 422)
    described, start = header(block)

    def array(location: ArrayLocation, dtype: str) -> Array:
        size = np.dtype(dtype).itemsize
        return np.frombuffer(
            block, dtype=dtype, count=location.length // size, offset=start + location.offset
        )

    return Columns(
        writer=described.writer,
        first=described.first,
        last=described.last,
        wall_ms=array(described.wall_ms, "<i8"),
        clocks={clock.name: array(clock.data, "<f8") for clock in described.clocks},
        reductions={
            level.factor: Reduction(
                clocks={c.name: array(c.data, "<f8").reshape(-1, 2) for c in level.clocks},
                series={c.name: array(c.data, "<f8").reshape(-1, STATISTICS) for c in level.series},
            )
            for level in described.rollups
        },
        series={
            (column.name, column.kind): Series(
                rows=None if column.rows is None else array(column.rows, "<u4"),
                data=array(column.data, DTYPES[column.kind]),
                labels=column.labels,
            )
            for column in described.series
        },
    )


def rows(columns: Columns) -> Iterator[LoggedRow]:
    """Every row as logged, in write order."""
    return (columns.row(index) for index in range(columns.rows))
