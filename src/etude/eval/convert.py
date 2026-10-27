"""Converting an eval run of the per-file layout into blocks, and deleting its files after.

Older eval runs kept a file per fact: `matches/KEY.json`, `tables/TABLE_FILE`
per match or merge, `.removed` notes, and every author file under
`artifacts/`. `convert` reads them once and writes blocks
(`etude.eval.block`) beside them that hold the same match records, each
table's rows of a match from the latest file holding them, and every
artifact as a blob (named by no match). Each block lists the old keys it
replaces, so conversion is incremental: a second pass converts only what an
old writer added since. `delete_files` deletes exactly the replaced keys, and
only once nothing of the old layout is left unconverted in that store.
Readers other than `convert` never read the per-file layout.
"""

from __future__ import annotations

import secrets
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from etude.carriage import EVAL_BLOCK_INDEX, EVAL_BLOCK_PACK
from etude.eval.block import (
    SEAL_BYTES,
    Block,
    file_key,
    per_file,
    quote,
    read,
    sealed,
    write,
)
from etude.eval.records import TABLE_FILE, MatchRecord
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, run_folders
from etude.run.records import Digest, Record
from etude.run.storage import Storage

if TYPE_CHECKING:
    import duckdb

# Reads of a bucket's small files run this many at once.
_READERS = 32


class Conversion(Record):
    """What `convert` wrote for an eval run, over every folder of it in one store."""

    run: str
    blocks: int
    matches: int
    blobs: int
    # Old keys the new blocks replace.
    replaced: int


def convert(run: str, source: Storage | Path | str) -> Conversion:
    """Write blocks holding every per-file record of eval run `run` in `source` not yet held.

    Every folder of the run in `source` (a runs directory or bucket) is
    converted in place; the old files stay until `delete_files`.
    """
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    storage = as_storage(source)
    blocks: list[Block] = []
    db = duckdb.connect()
    try:
        for folder, names in _folders(storage, run).items():
            with tempfile.TemporaryDirectory(prefix="etude-convert-") as scratch:
                blocks += _convert(storage, folder, names, db, Path(scratch))
    finally:
        db.close()
    return Conversion(
        run=run,
        blocks=len(blocks),
        matches=sum(len(block.matches) for block in blocks),
        blobs=sum(len(block.blobs) for block in blocks),
        replaced=sum(len(block.replaces) for block in blocks),
    )


def delete_files(run: str, source: Storage | Path | str) -> int:
    """Delete the per-file records of eval run `run` in `source` that its blocks there replace.

    Refuses with `EtudeError("eval_files_not_converted")`, deleting nothing,
    while any folder of the run in `source` keeps an old file no block there
    replaces: convert, and deliver the blocks, first. Returns how many were deleted.
    """
    storage = as_storage(source)
    doomed: list[str] = []
    for folder, names in _folders(storage, run).items():
        replaced = _replaced(storage, folder, names)
        left = [name for name in names if _old(name) and name not in replaced]
        if left:
            raise EtudeError("eval_files_not_converted", 409) from ValueError(
                f"{folder}: {len(left)} files no block replaces, such as {left[0]}"
            )
        doomed += [folder + name for name in names if name in replaced]
    for each in doomed:
        storage.delete(each)
    return len(doomed)


def _folders(storage: Storage, run: str) -> dict[str, list[str]]:
    """Each folder of the run in `storage`, with the names of its files."""
    keys = storage.keys("")
    return {
        folder: [each[len(folder) :] for each in keys if each.startswith(folder)]
        for folder in run_folders(keys).get(run, [])
    }


def _replaced(storage: Storage, folder: str, names: Sequence[str]) -> set[str]:
    """The old files the folder's blocks replace."""
    return {name for each in sealed(names) for name in read(storage, folder, each).replaces}


def _old(name: str) -> bool:
    """A file of the per-file layout: a record, a table file, a `.removed` note or an artifact."""
    return (
        per_file(name)
        or (name.startswith("tables/") and name.endswith(".removed"))
        or name.startswith("artifacts/")
    )


def _convert(
    storage: Storage,
    folder: str,
    names: Sequence[str],
    db: duckdb.DuckDBPyConnection,
    scratch: Path,
) -> list[Block]:
    """Blocks for the folder's old files no block replaces yet, delivered to it."""
    replaced = _replaced(storage, folder, names)
    old = sorted(name for name in names if _old(name) and name not in replaced)
    if not old:
        return []
    copies = scratch / "old"

    def fetch(name: str) -> None:
        target = copies / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(storage.get(folder + name))

    with ThreadPoolExecutor(_READERS) as readers:
        list(readers.map(fetch, old))
    retired = {
        line
        for name in old
        if name.endswith(".removed")
        for line in (copies / name).read_text().split("\n")
        if line
    }
    records: dict[Digest, MatchRecord] = {}
    for name in old:
        if name.startswith("matches/") and name.endswith(".json"):
            record = MatchRecord.model_validate_json((copies / name).read_bytes())
            if record.match.key not in records or record.state == "done":
                records[record.match.key] = record
    tables: dict[str, list[Path]] = {}
    for name in old:
        if (parsed := TABLE_FILE.match(name)) and name not in retired:
            tables.setdefault(parsed[1], []).append(copies / name)
    latest: dict[str, Sequence[Path]] = {
        table: [_latest(db, table, paths, scratch)] for table, paths in tables.items()
    }
    artifacts = [name for name in old if name.startswith("artifacts/")]
    packs = _packs(copies, artifacts)
    root = scratch / "run"
    blocks: list[Block] = []
    for index, (blobs, held) in enumerate(packs or [({}, [])]):
        first = index == 0
        replaces = [name for name in old if not name.startswith("artifacts/")] if first else []
        block = write(
            root,
            secrets.token_hex(6),
            sorted(records.values(), key=lambda r: (r.wall_ms, r.match.key)) if first else [],
            latest if first else {},
            blobs,
            db,
            replaces=[*replaces, *held],
        )
        _deliver(storage, folder, root, block)
        blocks.append(block)
    return blocks


def _latest(
    db: duckdb.DuckDBPyConnection, table: str, paths: Sequence[Path], scratch: Path
) -> Path:
    """One file of a table's rows, each match's from the latest file holding it.

    Every file of a table lies in one folder, so their paths order as their
    names do: by write time.
    """
    target = scratch / "latest" / f"{table}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    files = ", ".join(quote(path) for path in paths)
    db.execute(
        f"copy (select * exclude (filename) from read_parquet([{files}], union_by_name = true,"
        " filename = true) qualify filename = max(filename) over (partition by match))"
        f" to {quote(target)} (format parquet)"
    )
    return target


def _packs(copies: Path, artifacts: Sequence[str]) -> list[tuple[dict[Digest, Path], list[str]]]:
    """The artifacts as blobs, in packs of at most `SEAL_BYTES` each, with the names each holds."""
    packs: list[tuple[dict[Digest, Path], list[str]]] = []
    size = 0
    for name in artifacts:
        path = copies / name
        length = path.stat().st_size
        if not packs or (size + length > SEAL_BYTES and packs[-1][0]):
            packs.append(({}, []))
            size = 0
        blobs, held = packs[-1]
        blobs.setdefault(sha256(path.read_bytes()).hexdigest(), path)
        held.append(name)
        size += length
    return packs


def _deliver(storage: Storage, folder: str, root: Path, block: Block) -> None:
    """Send a written block to the run's folder: its pack, its tables, then its index."""
    files = [
        *([EVAL_BLOCK_PACK] if block.pack else []),
        *(f"{table}.parquet" for table in block.tables),
    ]
    for file in [*files, EVAL_BLOCK_INDEX]:
        storage.put_file(folder + file_key(block.name, file), root / file_key(block.name, file))
