"""Per-writer indexes and lazy blocks, shared by directory and bucket readers.

A manifest duplicates block headers so opening a run needs no metric data.
Each writer has two (`write_manifest`): one for permanent blocks, rewritten
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
    Array,
    Header,
    Number,
    Reduction,
    Span,
    Table,
    decode,
    encode_number,
    header,
    reduce,
)

type Endpoints = tuple[Number, Number]
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

    def array(self, span: Span, dtype: str, width: int = 1) -> Array:
        start = self.description.start + span.offset
        data = (
            self.storage.get_range(self.key, start, span.length)
            if self.body is None
            else self.body[start : start + span.length]
        )
        if len(data) != span.length or zlib.crc32(data) != span.crc32:
            raise EtudeError("block_array_damaged", 422)
        array = np.frombuffer(data, dtype=dtype)
        return array if width == 1 else array.reshape(-1, width)

    def table(self) -> Table:
        return decode(self.storage.get(self.key) if self.body is None else self.body)


def fetch(storage: Storage, prefix: str, key: str) -> StoredBlock:
    """A small block no manifest describes, in one request, header and arrays together."""
    body = storage.get(prefix + key)
    described, start = header(body)
    description = BlockDescription(key=key, header=described, start=start)
    return StoredBlock(storage, prefix + key, description, body)


def describe(storage: Storage, prefix: str, key: str) -> BlockDescription:
    """Read a header in two small ranges when no manifest describes a file."""
    front = storage.get_range(prefix + key, 0, 16)
    length = int.from_bytes(front[12:16], "little")
    described, start = header(front + storage.get_range(prefix + key, 16, length))
    return BlockDescription(key=key, header=described, start=start)


def _described(storage: Storage, files: Iterable[BlockFile]) -> list[BlockDescription]:
    """Each block's header and its coarsest row groups."""
    blocks: list[BlockDescription] = []
    for file in files:
        description = describe(storage, "", file.key)
        stored = StoredBlock(storage, file.key, description)
        level = next((level for level in stored.header.rollups if level.factor == 4096), None)
        overview = (
            reduce(stored.table(), 4096)
            if level is None
            else Reduction(
                clocks={c.name: stored.array(c.data, "<f8", 2) for c in level.clocks},
                series={c.name: stored.array(c.data, "<f8", 7) for c in level.series},
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
        blocks.append(
            BlockDescription(
                key=file.key, header=description.header, start=description.start, overview=inline
            )
        )
    return blocks


def _latest(blocks: Iterable[BlockDescription]) -> dict[str, Number | None]:
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


def write_manifest(root: Path, writer: str, *, permanent: bool = True) -> None:
    """Publish this writer's indexes after the blocks they describe exist.

    There are two files, split by how often they change, so that what is
    rewritten and uploaded at every fold stays small however long the run is.
    `metrics/WRITER.permanent.json` describes the permanent blocks and is
    rewritten only when one is added (`permanent`). `metrics/WRITER.index.json`
    carries the latest value of every metric and nothing per block: recent
    blocks are small, so a reader fetches each whole and needs no index for it.
    """
    storage = DirectoryStorage(root)
    files = select(
        file
        for path in root.glob(f"metrics/*-{writer}.blk")
        if (file := parse(path.relative_to(root).as_posix())) is not None
    )
    lasting_key = f"metrics/{writer}.permanent.json"
    if permanent:
        described = _described(storage, (file for file in files if file.level >= 2))
        lasting = Manifest(writer=writer, blocks=tuple(described), latest=_latest(described))
        storage.put(lasting_key, lasting.model_dump_json().encode())
    elif (root / lasting_key).is_file():
        lasting = Manifest.model_validate_json(storage.get(lasting_key))
    else:
        lasting = Manifest(writer=writer, blocks=(), latest={})
    recent = [describe(storage, "", file.key) for file in files if file.level < 2]
    manifest = Manifest(writer=writer, blocks=(), latest={**lasting.latest, **_latest(recent)})
    storage.put(f"metrics/{writer}.index.json", manifest.model_dump_json().encode())
