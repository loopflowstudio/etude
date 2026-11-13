"""An eval run's sealed blocks: what `evaluate` writes and every reader of an eval run reads.

A writer stages each stored match under `blocks/.WRITER/` (`Staging`; a dot
folder, never delivered) and seals the staged segment into a block, one
folder that is never rewritten:

    blocks/WRITTEN.WRITER/blobs.zip      the matches' blobs, stored members named SHA256
    blocks/WRITTEN.WRITER/TABLE.parquet  one table's rows of the block's matches
    blocks/WRITTEN.WRITER/ROLLUP.parquet each match's partial totals of a `Rollup`
    blocks/WRITTEN.WRITER/block.json     the `Block`: its match records, tables and blobs

They are written and delivered in that order (`etude.carriage._rank`), so a
reader that finds `block.json` finds the rest. WRITTEN is the seal's time in
zero-padded nanoseconds, so block names order by when they were sealed and a
match's rows come from the latest block holding them. Plain DuckDB reads a
run's tables without Etude (`read_parquet('RUN/blocks/*/plays.parquet')`), and
any unzip tool yields its blobs; a blob's stored bytes are one range of its pack.
A table's rows are sorted by match, and by game where rows name one, in small
row groups, so a read filtered to a few matches skips most of a file, locally
or by range reads from a bucket.

A rollup's partial totals are taken per match as it is stored (`partial_totals`,
so a rollup that cannot be summed refuses the match) and merged into the
block's file when it is sealed, so a reader adds them up without the raw
rows, and a match held by two blocks (a block sealed again after a crash)
counts once, from the later.

A staging folder holds a lock while its writer lives. One whose writer died
is sealed by the next writer in the run's folder (`seal_abandoned`), so a
crash loses nothing that reached the disk.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue, TypeAdapter

from etude.cache import Content
from etude.carriage import EVAL_BLOCK_INDEX, EVAL_BLOCK_PACK, EVAL_BLOCKS
from etude.eval.records import (
    ADDED,
    ROLLUP_ROWS,
    ROLLUP_SEATED,
    TABLE_FILE,
    Match,
    MatchRecord,
    Rollup,
)
from etude.run.errors import EtudeError
from etude.run.records import Digest, Name, Record
from etude.run.storage import Storage

if TYPE_CHECKING:
    import duckdb

# `blocks/WRITTEN.WRITER/block.json`, relative to the run's folder.
BLOCK_FILE = re.compile(rf"^{EVAL_BLOCKS}/(\d{{20}}\.[0-9a-f]+)/block\.json$")
# A staged segment is sealed once this old or holding this many blob bytes.
SEAL_SECONDS = 300.0
SEAL_BYTES = 256 * 2**20
# Rows per Parquet row group: the unit a filtered read keeps or skips.
ROW_GROUP_ROWS = 8192
# A staging folder's rollups, so a dead writer's matches are sealed with its own.
_ROLLUPS = "rollups.json"
_ROLLUP_LIST: TypeAdapter[tuple[Rollup, ...]] = TypeAdapter(tuple[Rollup, ...])
# What a staging folder holds, each in its own folder.
_PARTS = ("tables", "rollups", "blobs", "records")
# Columns a rollup joins tables on, where both have them.
_JOINED = ("run", "match", "seat", "game")
# Types a sum totals as whole numbers. A column no row gave is a null, INTEGER.
_WHOLE = frozenset(
    ["BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT",
     "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "HUGEINT", "UHUGEINT"]
)  # fmt: skip
# A ZIP local file header's fixed part, before the member's name and extra field.
_LOCAL_HEADER = 30


class Span(Record):
    """Where a blob's bytes lie in its block's pack."""

    offset: int = Field(ge=0)
    size_bytes: int = Field(ge=0)


class Block(Record):
    """A sealed block's index: `blocks/NAME/block.json`."""

    writer: str
    written_ns: int = Field(ge=0)
    matches: tuple[MatchRecord, ...]
    # Tables with a `TABLE.parquet` in the block.
    tables: tuple[Name, ...] = ()
    # The pack, `blobs.zip`, when the block keeps blobs; each blob by its SHA256.
    pack: Content | None = None
    blobs: dict[Digest, Span] = {}
    # Keys of the per-file layout (relative to the run) a converted block holds.
    replaces: tuple[str, ...] = ()
    # The run's rollups, and those with a `ROLLUP.parquet` of partial totals in the block.
    rollups: tuple[Rollup, ...] = ()
    totals: tuple[Name, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.written_ns:020d}.{self.writer}"

    @property
    def summed(self) -> tuple[Rollup, ...]:
        """The rollups with partial totals in this block."""
        return tuple(rollup for rollup in self.rollups if rollup.name in self.totals)

    @property
    def files(self) -> list[str]:
        """The block's files before its index, in the order they are delivered."""
        return [
            *([EVAL_BLOCK_PACK] if self.pack else []),
            *(f"{name}.parquet" for name in [*self.tables, *(r.name for r in self.summed)]),
        ]


def file_key(name: str, file: str) -> str:
    """A block's file, relative to the run's folder."""
    return f"{EVAL_BLOCKS}/{name}/{file}"


def sealed(names: Iterable[str]) -> list[str]:
    """The sealed blocks among a run's file names, oldest first."""
    return sorted(found[1] for name in names if (found := BLOCK_FILE.match(name)))


def read(storage: Storage, folder: str, name: str) -> Block:
    """Block `name` of the run in `folder` (a prefix of `storage`)."""
    return Block.model_validate_json(storage.get(folder + file_key(name, EVAL_BLOCK_INDEX)))


def run_blocks(
    storage: Storage, folder: str, run: str, names: Iterable[str], known: dict[str, Block]
) -> list[Block]:
    """The sealed blocks of eval run `run` in `folder`, given the names of its files.

    A block is never rewritten, so one read of its index serves every place
    holding it: `known` keeps what was read. Raises `eval_run_not_converted`
    when the folder keeps per-file records no block read so far replaces.
    """
    names = list(names)
    found = sealed(names)
    for name in found:
        if name not in known:
            known[name] = read(storage, folder, name)
    refuse_unconverted(run, names, (each for block in known.values() for each in block.replaces))
    return [known[name] for name in found]


def per_file(name: str) -> bool:
    """Whether a run's file name belongs to the per-file layout older eval runs kept."""
    return name.startswith("matches/") or TABLE_FILE.match(name) is not None


def refuse_unconverted(run: str, names: Iterable[str], replaced: Iterable[str]) -> None:
    """Raise `eval_run_not_converted` when the run holds per-file records no block replaces."""
    replaced = set(replaced)
    left = [name for name in names if per_file(name) and name not in replaced]
    if left:
        raise EtudeError("eval_run_not_converted", 409) from ValueError(
            f"eval run {run!r} keeps {len(left)} per-file records;"
            f" convert it with `etude convert-eval {run} --source URI`"
        )


class Staging:
    """One writer's stored matches not yet sealed, in `blocks/.WRITER/` of the run's folder.

    A match's tables and blobs are written before its record, and sealing
    takes only matches with a record, so a crash mid-match stages nothing of it.
    Raises `BlockingIOError` when another live process holds the folder.
    """

    def __init__(self, root: Path, writer: str, rollups: Sequence[Rollup] = ()) -> None:
        self.root, self.writer, self.rollups = root, writer, tuple(rollups)
        self.path = root / EVAL_BLOCKS / f".{writer}"
        for part in _PARTS:
            (self.path / part).mkdir(parents=True, exist_ok=True)
        # When the open segment's first match was staged here, and its blob bytes.
        self._opened: float | None = None
        self._bytes = 0
        self._lock = os.open(self.path / "lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock)
            raise
        _write(self.path / _ROLLUPS, _ROLLUP_LIST.dump_json(self.rollups))

    def table(self, table: str, match: Digest) -> Path:
        """Where a match's rows of `table` are written, as Parquet."""
        folder = self.path / "tables" / table
        folder.mkdir(exist_ok=True)
        return folder / f"{match}.parquet"

    def partial(self, rollup: str, match: Digest) -> Path:
        """Where a match's partial totals of a rollup are written, as Parquet."""
        folder = self.path / "rollups" / rollup
        folder.mkdir(exist_ok=True)
        return folder / f"{match}.parquet"

    def stage(self, record: MatchRecord, blobs: Sequence[bytes] = ()) -> None:
        """Keep a match's blobs, then its record: the match is staged."""
        for body in blobs:
            _write(self.path / "blobs" / hashlib.sha256(body).hexdigest(), body)
        _write(
            self.path / "records" / f"{record.match.key}.json", record.model_dump_json().encode()
        )
        if self._opened is None:
            self._opened = time.monotonic()
        self._bytes += sum(len(body) for body in blobs)

    def due(self) -> bool:
        """Whether the segment staged here should be sealed: old enough or large enough."""
        return self._opened is not None and (
            time.monotonic() - self._opened >= SEAL_SECONDS or self._bytes >= SEAL_BYTES
        )

    def seal(self, db: duckdb.DuckDBPyConnection) -> Block | None:
        """Seal what is staged into a block in the run's folder and empty the staging.

        None when nothing is staged.
        """
        records = sorted(
            (
                MatchRecord.model_validate_json(path.read_bytes())
                for path in (self.path / "records").glob("*.json")
            ),
            key=lambda record: (record.wall_ms, record.match.key),
        )
        if not records:
            return None
        # A failed match keeps no rows, though a rollup that refused it found them staged.
        staged = {record.match.key for record in records if record.state == "done"}
        tables, partials = (
            {
                folder.name: paths
                for folder in sorted((self.path / part).iterdir())
                if (paths := [path for path in sorted(folder.iterdir()) if path.stem in staged])
            }
            for part in ("tables", "rollups")
        )
        blobs = {
            digest: self.path / "blobs" / digest
            for record in records
            for digest in record.blobs
        }  # fmt: skip
        block = write(
            self.root,
            self.writer,
            records,
            tables,
            blobs,
            db,
            rollups=self.rollups,
            partials=partials,
        )
        for part in _PARTS:
            shutil.rmtree(self.path / part)
            (self.path / part).mkdir()
        self._opened, self._bytes = None, 0
        return block

    def close(self) -> None:
        """Release the folder; an empty one is removed."""
        if not any((self.path / "records").iterdir()):
            shutil.rmtree(self.path, ignore_errors=True)
        os.close(self._lock)


def seal_abandoned(root: Path, db: duckdb.DuckDBPyConnection) -> None:
    """Seal and remove every staging folder in the run's folder whose writer has died."""
    folder = root / EVAL_BLOCKS
    if not folder.is_dir():
        return
    for path in sorted(folder.iterdir()):
        if not (path.is_dir() and path.name.startswith(".")):
            continue
        kept = path / _ROLLUPS
        rollups = _ROLLUP_LIST.validate_json(kept.read_bytes()) if kept.is_file() else ()
        try:
            staging = Staging(root, path.name[1:], rollups)
        except BlockingIOError:
            continue  # its writer lives
        staging.seal(db)
        staging.close()


def write(
    root: Path,
    writer: str,
    records: Sequence[MatchRecord],
    tables: Mapping[str, Sequence[Path]],
    blobs: Mapping[Digest, Path],
    db: duckdb.DuckDBPyConnection,
    *,
    replaces: Sequence[str] = (),
    rollups: Sequence[Rollup] = (),
    partials: Mapping[str, Sequence[Path]] = {},
) -> Block:
    """Write one block to the run's folder `root`: its pack, each table merged, each
    rollup's partial totals (`partial_totals` files, by rollup) merged, then its index.

    Each table's Parquet files are merged into one; a column given whole
    numbers in one file and fractions in another becomes fractions.
    """
    written = time.time_ns()
    name = f"{written:020d}.{writer}"
    folder = root / EVAL_BLOCKS / name
    folder.mkdir(parents=True)
    pack, spans = (None, {}) if not blobs else _pack(folder / EVAL_BLOCK_PACK, blobs)
    for table, paths in tables.items():
        merged = _parquet(paths)
        order = ", ".join(_quoted(c for c in ("match", "game") if c in _columns(db, merged)))
        _copy(db, f"select * from {merged} order by {order}", folder / f"{table}.parquet")
    totals = [rollup for rollup in rollups if partials.get(rollup.name)]
    for rollup in totals:
        order = ", ".join(_quoted(["run", "match", *rollup.by]))
        merged = _parquet(partials[rollup.name])
        _copy(db, f"select * from {merged} order by {order}", folder / f"{rollup.name}.parquet")
    block = Block(
        writer=writer,
        written_ns=written,
        matches=tuple(records),
        tables=tuple(tables),
        pack=pack,
        blobs=spans,
        replaces=tuple(replaces),
        rollups=tuple(rollups),
        totals=tuple(rollup.name for rollup in totals),
    )
    _write(folder / EVAL_BLOCK_INDEX, block.model_dump_json().encode())
    return block


def partial_totals(
    db: duckdb.DuckDBPyConnection,
    rollup: Rollup,
    tables: Mapping[str, Sequence[Path]],
    matches: Sequence[Match],
    target: Path,
) -> bool:
    """Write each match's partial totals of `rollup` over `tables`' rows to `target`.

    False, writing nothing, when the rollup's table or one it joins has no
    rows here. Whole numbers and booleans sum to whole numbers, fractions to
    fractions. Raises `rollup_refused` for an expression DuckDB cannot
    evaluate here, `rollup_sum_not_numeric` for a sum of text, and
    `rollup_needs_seat` for `player` or `opponents` over rows naming no seat.
    """
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    if not all(tables.get(table) for table in (rollup.table, *rollup.join)):
        return False
    try:
        source = _joined(db, rollup, tables, matches)
        held = _columns(db, source)
        expressions = [*rollup.sums.values(), *([rollup.where] if rollup.where else [])]
        named = {c for each in expressions for c in _referenced(db, rollup, each)}
        if absent := sorted(c for c in {*rollup.by, *named} if c not in held):
            nulls = ", ".join(f"null as {c}" for c in _quoted(absent))
            source = f"(select *, {nulls} from {source})"
        sums = []
        for column, expression in rollup.sums.items():
            kind = _sum_type(db, rollup, column, f"select ({expression}) from {source}")
            total = f"coalesce(sum(cast(({expression}) as {kind})), 0)"
            sums.append(f'cast({total} as {kind}) as "{column}"')
        where = f" where ({rollup.where})" if rollup.where else ""
        keys = ", ".join(_quoted(["run", "match", *rollup.by]))
        _copy(
            db,
            f"select {keys}, count(*) as {ROLLUP_ROWS}{''.join(', ' + each for each in sums)}"
            f" from {source}{where} group by {keys} order by {keys}",
            target,
        )
    except duckdb.Error as error:
        raise EtudeError("rollup_refused", 422) from ValueError(f"rollup {rollup.name!r}: {error}")
    return True


def _joined(
    db: duckdb.DuckDBPyConnection,
    rollup: Rollup,
    tables: Mapping[str, Sequence[Path]],
    matches: Sequence[Match],
) -> str:
    """A rollup's rows: its table joined to the others, with each seat's players when `by` names them."""
    source = _parquet(tables[rollup.table])
    for table in rollup.join:
        held, other = _columns(db, source), _columns(db, _parquet(tables[table]))
        keys = [c for c in _JOINED if c in held and c in other]
        shared = [c for c in other if c in held and c not in keys]
        dropped = f" exclude ({', '.join(_quoted(shared))})" if shared else ""
        source = (
            f"(select * from {source} join (select *{dropped} from {_parquet(tables[table])})"
            f" using ({', '.join(_quoted(keys))}))"
        )
    if not set(rollup.by) & set(ROLLUP_SEATED):
        return source
    held = _columns(db, source)
    if "seat" not in held:
        raise EtudeError("rollup_needs_seat", 422) from ValueError(rollup.name)
    if set(held) & set(ROLLUP_SEATED):
        raise EtudeError("rollup_column_reserved", 422) from ValueError(rollup.name)
    seated, values = unnest(
        {"match": "VARCHAR", "seat": "BIGINT", "player": "VARCHAR", "opponents": "VARCHAR"},
        [
            [match.key, seat, player.key,
             ",".join(other.key for at, other in enumerate(match.players) if at != seat)]
            for match in matches
            for seat, player in enumerate(match.players)
        ],
    )  # fmt: skip
    db.execute(f"create or replace temp table _seated as {seated}", values)
    return f"(select * from {source} left join _seated using (match, seat))"


def unnest(
    columns: Mapping[str, str], rows: Sequence[Sequence[object]]
) -> tuple[str, list[list[object]]]:
    """A select of `rows` with `columns` (name: DuckDB type), and its parameters.

    Each column is one list parameter, unnested beside the others, so any
    number of rows, none included, is one statement.
    """
    select = ", ".join(f'unnest(?::{kind}[]) as "{name}"' for name, kind in columns.items())
    return f"select {select}", [[row[at] for row in rows] for at in range(len(columns))]


def _parquet(paths: Sequence[Path]) -> str:
    files = ", ".join(quote(path) for path in paths)
    return f"read_parquet([{files}], union_by_name = true)"


def _quoted(columns: Iterable[str]) -> list[str]:
    return [f'"{c}"' for c in columns]


def _sum_type(db: duckdb.DuckDBPyConnection, rollup: Rollup, column: str, query: str) -> str:
    """BIGINT or DOUBLE: what a sum's expression totals as."""
    ((_, kind, *_),) = db.execute(f"describe {query}").fetchall()
    if kind in ("FLOAT", "DOUBLE") or str(kind).startswith("DECIMAL"):
        return "DOUBLE"
    if kind in _WHOLE:
        return "BIGINT"
    raise EtudeError("rollup_sum_not_numeric", 422) from ValueError(
        f"rollup {rollup.name!r}: sum {column!r} is {kind}"
    )


def _referenced(db: duckdb.DuckDBPyConnection, rollup: Rollup, expression: str) -> set[str]:
    """The columns an expression names; raises `rollup_refused` unless it is one expression."""
    found = db.execute("select json_serialize_sql(?)", [f"select ({expression})"]).fetchone()
    tree = json.loads(found[0]) if found else {}
    statements = tree.get("statements", [])
    if tree.get("error") or len(statements) != 1 or len(statements[0]["node"]["select_list"]) != 1:
        raise EtudeError("rollup_refused", 422) from ValueError(
            f"rollup {rollup.name!r}: {expression!r} is not one expression"
        )
    columns: set[str] = set()

    def walk(node: JsonValue) -> None:
        if isinstance(node, dict):
            names = node.get("column_names")
            if node.get("class") == "COLUMN_REF" and isinstance(names, list):
                columns.add(str(names[-1]))
            for each in node.values():
                walk(each)
        elif isinstance(node, list):
            for each in node:
                walk(each)

    walk(statements[0]["node"]["select_list"])
    return columns - set(ADDED)


def _columns(db: duckdb.DuckDBPyConnection, source: str) -> dict[str, str]:
    """Each column of a relation and its type."""
    return {
        name: kind for name, kind, *_ in db.execute(f"describe select * from {source}").fetchall()
    }


def _copy(db: duckdb.DuckDBPyConnection, query: str, target: Path) -> None:
    """Write a query's rows to `target` as Parquet in small row groups; readers never see half a file."""
    partial = target.with_name(target.name + ".partial")
    db.execute(
        f"copy ({query}) to {quote(partial)} (format parquet, row_group_size {ROW_GROUP_ROWS})"
    )
    partial.replace(target)


def _pack(target: Path, blobs: Mapping[Digest, Path]) -> tuple[Content, dict[Digest, Span]]:
    """Store each blob uncompressed in a zip, so each is one exact range of it."""
    partial = target.with_name(target.name + ".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as pack:
        for digest, path in blobs.items():
            pack.write(path, digest)
    spans: dict[Digest, Span] = {}
    with zipfile.ZipFile(partial) as pack, partial.open("rb") as stream:
        for member in pack.infolist():
            stream.seek(member.header_offset + 26)
            lengths = stream.read(4)
            start = (
                member.header_offset
                + _LOCAL_HEADER
                + int.from_bytes(lengths[:2], "little")
                + int.from_bytes(lengths[2:], "little")
            )
            spans[member.filename] = Span(offset=start, size_bytes=member.file_size)
    with partial.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    content = Content(sha256=digest, size_bytes=partial.stat().st_size)
    partial.replace(target)
    return content, spans


def _write(target: Path, body: bytes) -> None:
    """Readers never see half a file."""
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(body)
    partial.replace(target)


def quote(path: Path) -> str:
    """A path as a DuckDB string literal."""
    return "'" + os.fspath(path).replace("'", "''") + "'"
