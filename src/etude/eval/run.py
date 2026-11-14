"""A live evaluation run: match validation, staging, rollups and outcome publication.

EvalRun owns its local DuckDB connection until finish; match.evaluate owns
scheduling and worker processes. Result is the playing code's returned data.
"""

from __future__ import annotations

import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from etude.errors import EtudeError
from etude.eval.block import (
    Block,
    Staging,
    partial_totals,
    quote,
    run_blocks,
    seal_abandoned,
    stage,
)
from etude.eval.records import (
    ADDED,
    COLUMN_TYPES,
    IDENTIFIER,
    NO_ROWS,
    ColumnType,
    Config,
    Match,
    MatchRecord,
    Rollup,
    as_config,
    outcomes,
)
from etude.identity import run_address
from etude.records import RunCounters
from etude.run import Run, StorageDestination
from etude.storage import DirectoryStorage, Storage, run_prefix

if TYPE_CHECKING:
    import duckdb

# Etude's own tables, which `etude.eval.query.statsdb` keeps beside the author's.
ETUDE_TABLES = (
    "runs",
    "players",
    "matches",
    "seats",
    "groups",
    "tournaments",
    "schedule",
    "training_checkpoints",
    "blobs",
)
# The seat's name before etude-sdk 0.2.7. Refused in rows, so a stored table with
# a `player` column is an older one (`etude.eval.query` reads it as such).
OLD_SEAT = "player"

type Value = bool | int | float | str | None
type Row = Mapping[str, Value]
type Tables = Mapping[str, Sequence[Row]]


@dataclass(frozen=True)
class Result:
    """What `play` returns with tables, optional blobs and execution provenance.

    Rows name a blob by its SHA256 (`hashlib.sha256(blob).hexdigest()`);
    `etude.eval.query.read_blobs` gives its bytes back.
    `execution` records the actual device and runtime settings. It changes
    neither the requested match nor whether its games have already been played.
    """

    tables: Tables = field(default_factory=dict)
    blobs: Sequence[bytes] = ()
    execution: Config = field(default_factory=dict)


class EvalRun(Run):
    """One process's matches of an eval run: staged, sealed into blocks and delivered."""

    def __init__(
        self,
        root: Path,
        name: str,
        targets: Sequence[StorageDestination],
        rollups: tuple[Rollup, ...],
        *,
        experiment: str,
    ) -> None:
        # Imported here: DuckDB is the optional `eval` extra.
        import duckdb  # noqa: PLC0415

        super().__init__(experiment=experiment, name=name, root=root, targets=targets)
        self.rollups = rollups
        self.id = secrets.token_hex(6)
        self.failed = 0  # `fail` is also how `store` refuses a match
        self.db: duckdb.DuckDBPyConnection = duckdb.connect()
        seal_abandoned(root, self.db)
        self.staging = Staging(root, self.id, rollups)
        stored = self._stored()
        self.done = {key for key, record in stored.items() if record.state == "done"}
        self.columns: dict[str, dict[str, ColumnType]] = {}
        for record in stored.values():
            for table, columns in record.columns.items():
                self.columns.setdefault(table, {}).update(columns)

    def _stored(self) -> dict[str, MatchRecord]:
        """Each match's standing record (`outcomes`) of the blocks here and at every destination.

        One listing per place and one read of each block not already read here.
        """
        places: list[tuple[Storage, str]] = [(DirectoryStorage(self.files.root), "")]
        places += [
            (target.storage, run_prefix(run_address(self.name, self.experiment)))
            for target in self._destinations
        ]
        found: list[MatchRecord] = []
        known: dict[str, Block] = {}
        for storage, prefix in places:
            names = (key[len(prefix) :] for key in storage.keys(prefix))
            for block in run_blocks(storage, prefix, self.name, names, known):
                if block.tables and block.rollups != self.rollups:
                    raise EtudeError("eval_run_rollups_conflict", 409) from ValueError(
                        f"block {block.name} keeps rollups"
                        f" {[rollup.name for rollup in block.rollups]}"
                    )
                found += block.matches
        return outcomes(found)

    def fail(self, scheduled: Match, error: BaseException) -> NoReturn:
        detail = f"{type(error).__name__}: {error}"[:2000]
        self.failed += 1
        self.staging.stage(
            MatchRecord(match=scheduled, state="failed", error=detail, wall_ms=_now_ms())
        )
        self._seal()
        raise EtudeError("match_failed", 500) from error

    def store(self, scheduled: Match, outcome: Tables | Result) -> None:
        """Check every table, then stage them, the blobs and the match's record; nothing on refusal."""
        result = outcome if isinstance(outcome, Result) else Result(tables=outcome)
        try:
            execution = as_config(result.execution)
            checked = {
                table: _checked(scheduled, table, rows, self.rollups)
                for table, rows in result.tables.items()
            }
            types = {
                table: _types(table, rows, self.columns.get(table, {}))
                for table, rows in checked.items()
                if rows
            }
            if not types:
                raise EtudeError(NO_ROWS, 422)
            if not all(isinstance(blob, bytes) for blob in result.blobs):
                raise EtudeError("blob_must_be_bytes", 422)
        except EtudeError as refused:
            self.fail(scheduled, refused)
        staged = {table: [self.staging.table(table, scheduled.key)] for table in types}
        for table, columns in types.items():
            self._write(staged[table][0], columns, checked[table], scheduled.key)
        # A match's partial totals are taken now, so a rollup that cannot sum it refuses it.
        try:
            for rollup in self.rollups:
                target = self.staging.partial(rollup.name, scheduled.key)
                partial_totals(self.db, rollup, staged, [scheduled], target)
        except EtudeError as refused:
            self.fail(scheduled, refused)
        for table, columns in types.items():
            self.columns.setdefault(table, {}).update(columns)
        digests = tuple(dict.fromkeys(sha256(blob).hexdigest() for blob in result.blobs))
        record = MatchRecord(
            match=scheduled,
            state="done",
            columns=types,
            wall_ms=_now_ms(),
            blobs=digests,
            execution=execution,
        )
        self.staging.stage(record, result.blobs)
        if self.staging.due():
            self._seal()

    def _write(
        self, target: Path, columns: dict[str, ColumnType], rows: Sequence[Row], match: str
    ) -> None:
        """Write one match's rows of a table to `target` as Parquet; readers never see half a file."""
        stage(
            self.db,
            "_match_rows",
            {**columns, "run": "VARCHAR", "match": "VARCHAR"},
            [[row.get(name) for name in columns] + [self.name, match] for row in rows],
        )
        partial = target.with_name(target.name + ".partial")
        self.db.execute(f"copy _match_rows to {quote(partial)} (format parquet)")
        self.db.execute("drop table _match_rows")
        partial.replace(target)

    def _seal(self) -> None:
        """Seal what is staged into a block and offer it to every destination."""
        with self.files.lock:
            self.staging.seal(self.db)
        self.ship()

    def finish(self, state: Literal["finished", "failed"]) -> None:
        with self.files.lock:
            self.staging.seal(self.db)
        self.staging.close()
        self._write_outcome(state, counters=RunCounters(), writer=self.id)
        self.db.close()
        if not self.ship():
            print(
                f"etude: not delivered: {self.problems}. Files are in {self.files.root}; "
                "deliver them with `etude sync`.",
                file=sys.stderr,
            )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _checked(
    scheduled: Match, table: str, rows: Sequence[Row], rollups: Sequence[Rollup]
) -> list[Row]:
    """The rows, refused unless each names exactly one seat or group of this match."""
    if (
        not IDENTIFIER.match(table)
        or table in ETUDE_TABLES
        or table in (rollup.name for rollup in rollups)
    ):
        raise EtudeError("table_name_refused", 422) from ValueError(table)
    for row in rows:
        seat, group = row.get("seat"), row.get("group")
        where = ValueError(f"table {table!r}: {dict(row)!r}")
        if (seat is None) == (group is None):
            raise EtudeError("row_needs_seat_or_group", 422) from where
        if seat is not None and (type(seat) is not int or not 0 <= seat < len(scheduled.players)):
            raise EtudeError("row_seat_not_seated", 422) from where
        if group is not None and group not in scheduled.groups:
            raise EtudeError("row_group_not_in_match", 422) from where
        for column in row:
            if column in ADDED or column == OLD_SEAT or not IDENTIFIER.match(column):
                raise EtudeError("column_name_refused", 422) from where
    return list(rows)


def _types(
    table: str, rows: Sequence[Row], known: Mapping[str, ColumnType]
) -> dict[str, ColumnType]:
    """Each column's type in this write. Whole numbers may join a column of fractions."""
    found: dict[str, ColumnType] = {"seat": "BIGINT", "group": "VARCHAR"}
    for row in rows:
        for column, value in row.items():
            if value is None:
                continue
            kind = COLUMN_TYPES.get(type(value))
            if kind is None:
                raise EtudeError("column_value_refused", 422) from TypeError(
                    f"table {table!r}, column {column!r}: {type(value).__name__}"
                )
            found[column] = _widened(found.get(column, kind), kind, table, column)
    for column, kind in list(found.items()):
        if column in known:
            found[column] = _widened(known[column], kind, table, column, onto=True)
    # A column given only as nulls here keeps the type an earlier write gave it.
    nulls = {column for row in rows for column in row} - found.keys()
    found.update({column: known[column] for column in nulls if column in known})
    return found


def _widened(
    first: ColumnType, then: ColumnType, table: str, column: str, *, onto: bool = False
) -> ColumnType:
    if first == then:
        return first
    if {first, then} == {"BIGINT", "DOUBLE"} and not (onto and first == "BIGINT"):
        return "DOUBLE"
    raise EtudeError("column_type_conflict", 422) from TypeError(
        f"table {table!r}, column {column!r}: {first} then {then}"
    )
