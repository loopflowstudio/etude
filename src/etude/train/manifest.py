"""Per-writer indexes and lazy blocks, shared by directory and bucket readers.

A manifest duplicates block headers so opening a run needs no metric data.
Each writer has two (`Index.write`): one for permanent blocks, rewritten
rarely, and a small one for recent blocks, rewritten at every fold.
StoredBlock fetches only requested arrays and verifies their CRCs. Missing
compacted objects cause the reader to refresh its listing.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from etude.run.block_file import BlockFile, parse, select
from etude.run.errors import EtudeError
from etude.run.records import Record
from etude.run.storage import DirectoryStorage, Storage
from etude.train.block import (
    STATISTICS,
    Array,
    ArrayLocation,
    Columns,
    Header,
    Number,
    Reduction,
    decode,
    encode_number,
    header,
    reduce,
)

type Endpoints = tuple[Number, Number]
# One row group's `etude.train.block` statistics, `MIN` to `COUNT`.
type Statistics = tuple[Number, Number, Number, Number, Number, Number, Number]


class Overview(Record):
    """The coarsest row groups, carried inline for a one-object overview."""

    clocks: dict[str, tuple[Endpoints, ...]]
    series: dict[str, tuple[Statistics, ...]]


class BlockDescription(Record):
    key: str
    header: Header
    start: int
    overview: Overview | None = None

    def file(self) -> BlockFile:
        file = parse(self.key)
        if file is None:
            raise EtudeError("block_not_recognised", 422)
        return file


class Manifest(Record):
    writer: str
    blocks: tuple[BlockDescription, ...]
    latest: dict[str, Number | None]


@dataclass(frozen=True)
class StoredBlock:
    storage: Storage
    key: str
    description: BlockDescription
    # The whole file, when it was fetched in one piece; arrays then come from it.
    body: bytes | None = None

    @property
    def header(self) -> Header:
        return self.description.header

    def array(self, location: ArrayLocation, dtype: str, width: int = 1) -> Array:
        start = self.description.start + location.offset
        data = (
            self.storage.get_range(self.key, start, location.length)
            if self.body is None
            else self.body[start : start + location.length]
        )
        if len(data) != location.length or zlib.crc32(data) != location.crc32:
            raise EtudeError("block_array_damaged", 422)
        array = np.frombuffer(data, dtype=dtype)
        return array if width == 1 else array.reshape(-1, width)

    def columns(self) -> Columns:
        return decode(self.storage.get(self.key) if self.body is None else self.body)


def fetch(storage: Storage, prefix: str, key: str) -> StoredBlock:
    """A small block no manifest describes, in one request, header and arrays together."""
    body = storage.get(prefix + key)
    described, start = header(body)
    description = BlockDescription(key=key, header=described, start=start)
    return StoredBlock(storage, prefix + key, description, body)


def describe(
    storage: Storage,
    prefix: str,
    key: str,
    *,
    metrics: frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
) -> BlockDescription:
    """Read one block's header, retaining only requested numeric columns if given.

    The block is read whole, once: it never changes, so a bucket's copy is kept
    in Etude's cache and its arrays are later read from there, not as one
    request per array. Projection leaves every clock and original array
    offset/CRC intact. Peak metadata is one full block plus the retained
    columns of previous blocks.
    """
    described, start = header(storage.get(prefix + key))
    if metrics is not None:
        series = tuple(
            c
            for c in described.series
            if c.kind == "metric" and (c.name in metrics or c.name.startswith(prefixes))
        )
        names = {c.name for c in series}
        described = described.model_copy(
            update={
                "series": series,
                "rollups": tuple(
                    level.model_copy(
                        update={"series": tuple(c for c in level.series if c.name in names)}
                    )
                    for level in described.rollups
                ),
            }
        )
    return BlockDescription(key=key, header=described, start=start)


def _describe_permanent(storage: Storage, file: BlockFile) -> BlockDescription:
    """One permanent block's header and coarsest row groups."""
    description = describe(storage, "", file.key)
    stored = StoredBlock(storage, file.key, description)
    level = next((level for level in stored.header.rollups if level.factor == 4096), None)
    overview = (
        reduce(stored.columns(), 4096)
        if level is None
        else Reduction(
            clocks={c.name: stored.array(c.data, "<f8", 2) for c in level.clocks},
            series={c.name: stored.array(c.data, "<f8", STATISTICS) for c in level.series},
        )
    )
    inline = Overview.model_validate(
        {
            "clocks": {
                name: [[encode_number(float(value)) for value in row] for row in values]
                for name, values in overview.clocks.items()
            },
            "series": {
                name: [[encode_number(float(value)) for value in row] for row in values]
                for name, values in overview.series.items()
            },
        }
    )
    return BlockDescription(
        key=file.key, header=description.header, start=description.start, overview=inline
    )


def _latest_values(blocks: Iterable[BlockDescription]) -> dict[str, Number | None]:
    latest: dict[str, Number | None] = {}
    for block in sorted(blocks, key=lambda block: block.header.last):
        latest.update(
            {
                column.name: column.latest
                for column in block.header.series
                if column.kind == "metric"
            }
        )
    return latest


class Index:
    """Writer-owned descriptions; rebuild once, then add only newly sealed blocks.

    Recent refreshes use bounded recent descriptions and cached permanent latest
    values. Publishing a permanent manifest still serializes its full history,
    but never reads or describes those earlier blocks again.
    """

    def __init__(self, root: Path, writer: str) -> None:
        self.storage: Storage = DirectoryStorage(root)
        self.writer = writer
        self._permanent: dict[str, BlockDescription] = {}
        self._recent: dict[str, BlockDescription] = {}
        self._permanent_latest: dict[str, Number | None] = {}
        self._dirty = False
        files = select(
            file
            for path in root.glob(f"metrics/*-{writer}.blk")
            if (file := parse(path.relative_to(root).as_posix())) is not None
        )
        for file in files:
            self.add(file.key)

    def add(self, key: str) -> None:
        """Remember a sealed block, retiring recent descriptions it covers."""
        file = parse(key)
        if file is None or file.writer != self.writer or file.level < 1:
            raise ValueError("index requires a sealed block from its writer")
        if key in self._permanent or key in self._recent:
            return
        if file.level >= 2:
            described = _describe_permanent(self.storage, file)
            self._permanent[key] = described
            self._permanent_latest.update(_latest_values([described]))
            self._dirty = True
        else:
            described = describe(self.storage, "", key)
        self._recent = {
            key: block
            for key, block in self._recent.items()
            if not (file.first <= block.header.first and block.header.last <= file.last)
        }
        if file.level < 2:
            self._recent[key] = described

    def write(self) -> None:
        """Publish indexes after their blocks exist; only permanent additions rewrite history."""
        if self._dirty:
            lasting = Manifest(
                writer=self.writer,
                blocks=tuple(self._permanent.values()),
                latest=self._permanent_latest,
            )
            self.storage.put(
                f"metrics/{self.writer}.permanent.json", lasting.model_dump_json().encode()
            )
            self._dirty = False
        recent = Manifest(
            writer=self.writer,
            blocks=(),
            latest={**self._permanent_latest, **_latest_values(self._recent.values())},
        )
        self.storage.put(f"metrics/{self.writer}.index.json", recent.model_dump_json().encode())


def metric_names(root: Path) -> set[str]:
    """Numeric names from previous attempts, recovered once when tracking starts."""
    names: set[str] = set()
    indexed: set[str] = set()
    for path in root.glob("metrics/*.index.json"):
        manifest = Manifest.model_validate_json(path.read_bytes())
        names.update(manifest.latest)
        indexed.add(manifest.writer)
    storage = DirectoryStorage(root)
    for path in (root / "metrics").glob("*"):
        file = parse(path.relative_to(root).as_posix())
        if file is not None and (file.level == 0 or file.writer not in indexed):
            names.update(
                column.name
                for column in describe(storage, "", file.key).header.series
                if column.kind == "metric"
            )
    return names
