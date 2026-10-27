"""The eval run: `evaluate` plays matches in worker processes and stores their tables.

Players (`etude.eval.records.Model` and `Code`), `environment` and `Match`
describe what `evaluate` plays; it knows nothing of tournaments
(`etude.eval.tournament` arranges eval runs). The author's `play(match)`
returns tables of rows, or a `Result` that also keeps blobs: bytes such as a
game's replay evidence, which rows name by SHA256. A `Rollup` declared by the
run is summed once per match as it is stored. Etude checks that every row
names a `seat` or a `group` of its match and that a column keeps one type
through the run, adds `run` and `match`, and stages the match
(`etude.eval.block.Staging`). A refused or raising match is recorded as
failed, nothing else of it is stored, and the run stops.

Staged matches are sealed into a block every few minutes, on a failure and on
finish, and each block is offered to every destination once sealed: a run
delivers a few objects per block, never one per match or blob. SIGTERM, as
the launcher's termination sends, seals what is staged before the run ends.

A resume reads the blocks already delivered to any destination, so a
machine with an empty disk skips what was delivered. Workers are fresh
processes (`spawn`), so `play` must be importable by name.
"""

from __future__ import annotations

import multiprocessing
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel

from etude.eval.block import (
    Block,
    Staging,
    partial_totals,
    quote,
    run_blocks,
    seal_abandoned,
    unnest,
)
from etude.eval.records import (
    ADDED,
    COLUMN_TYPES,
    IDENTIFIER,
    ColumnType,
    Environment,
    Match,
    MatchRecord,
    Rollup,
    as_config,
)
from etude.eval.schedule import ordered
from etude.eval.source import digest_files
from etude.run.declaration import declare
from etude.run.delivery import RunFiles, StorageDestination, destinations, ship
from etude.run.errors import EtudeError
from etude.run.records import Record, RunCounters, RunState
from etude.run.storage import DirectoryStorage, Storage, run_prefix

if TYPE_CHECKING:
    import duckdb

KIND = "evaluation"
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
    """What `play` returns when a match keeps blobs beside its tables.

    Rows name a blob by its SHA256 (`hashlib.sha256(blob).hexdigest()`);
    `etude.eval.query.read_blobs` gives its bytes back.
    """

    tables: Tables = field(default_factory=dict)
    blobs: Sequence[bytes] = ()


class EvalRun(Record):
    """What one call to `evaluate` did."""

    experiment: str
    name: str
    environment: Environment
    played: int
    # Matches already stored by an earlier attempt.
    skipped: int


def environment(
    name: str,
    *,
    files: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Environment:
    """What matches are played in: its defining files, hashed like code, and its config."""
    return Environment(name=name, files=digest_files(files), config=as_config(config))


def evaluate(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    play: Callable[[Match], Tables | Result],
    workers: int = 1,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
) -> EvalRun:
    """Play every match not already stored in this eval run, `workers` at a time.

    Matches start in the order `etude.eval.schedule.ordered` gives, so
    consecutive matches reuse the checkpoints the author's runtime keeps loaded.

    The eval run is named `run` (see `etude.run.declaration.declare` for its
    defaults); resuming it requires the same environment and `rollups`
    (`EtudeError("eval_run_rollups_conflict")`): a sealed block's partial
    totals are never taken again. Prints one line to
    stderr as each match is stored: how many of the pending are, and since
    when. Raises
    `EtudeError("match_failed")` after recording the first match that raised
    or returned rows Etude refuses. Delivery failures do not raise: the files
    stay in the run's directory for `etude sync`, and stderr says so.
    """
    if workers < 1:
        raise EtudeError("workers_must_be_positive", 422)
    if len({scheduled.key for scheduled in matches}) != len(matches):
        raise EtudeError("duplicate_match", 422)
    names = [rollup.name for rollup in rollups]
    if len(set(names)) != len(names) or set(names) & set(ETUDE_TABLES):
        raise EtudeError("rollup_name_refused", 422) from ValueError(names)
    declared = declare(experiment=experiment, clocks=(), run=run, kind=KIND, directory=directory)
    if declared.declaration.kind != KIND:
        raise EtudeError("run_is_not_an_eval_run", 409)
    path = declared.root / "environment.json"
    if not path.is_file():
        DirectoryStorage(declared.root).put(
            "environment.json", environment.model_dump_json().encode()
        )
    elif Environment.model_validate_json(path.read_bytes()) != environment:
        raise EtudeError("eval_run_environment_conflict", 409)
    writer = _Writer(
        declared.root, declared.name, destinations(declared.base, storages), tuple(rollups)
    )
    pending = [scheduled for scheduled in matches if scheduled.key not in writer.done]
    started = time.monotonic()
    try:
        with _terminable():
            for count, (scheduled, outcome) in enumerate(_played(pending, play, workers), 1):
                if isinstance(outcome, BaseException):
                    writer.fail(scheduled, outcome)
                writer.store(scheduled, outcome)
                print(
                    f"etude: {declared.name}: {count}/{len(pending)} matches played,"
                    f" {time.monotonic() - started:.0f}s elapsed",
                    file=sys.stderr,
                    flush=True,
                )
    except BaseException:
        writer.finish("failed")
        raise
    writer.finish("finished")
    return EvalRun(
        experiment=experiment,
        name=declared.name,
        environment=environment,
        played=len(pending),
        skipped=len(matches) - len(pending),
    )


class Terminated(Exception):
    """SIGTERM reached an eval run: what is staged is sealed before it ends."""


@contextmanager
def _terminable() -> Iterator[None]:
    """Turn SIGTERM into `Terminated` while matches are played, on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminated(signum: int, frame: FrameType | None) -> NoReturn:
        raise Terminated()

    before = signal.signal(signal.SIGTERM, terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, before)


def _played(
    pending: Sequence[Match], play: Callable[[Match], Tables | Result], workers: int
) -> Iterator[tuple[Match, Tables | Result | BaseException]]:
    """Each match with its tables or what it raised, in the order they finish.

    Matches start in `etude.eval.schedule` order, at most `workers` at once, so
    the order they start in is the order an author's runtime loads checkpoints in.
    """
    order = ordered(pending)
    if workers == 1:
        for scheduled in order:
            try:
                yield scheduled, play(scheduled)
            except Exception as error:  # noqa: BLE001 - recorded with the match, then raised
                yield scheduled, error
        return
    # Fresh processes everywhere, so an eval run behaves alike on Linux and macOS.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(min(workers, len(pending) or 1), mp_context=context) as pool:
        running: dict[Future[Tables | Result], Match] = {}

        def start() -> None:
            scheduled = next(order, None)
            if scheduled is not None:
                running[pool.submit(play, scheduled)] = scheduled

        for _ in range(workers):
            start()
        try:
            while running:
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    raised = future.exception()
                    yield running.pop(future), raised if raised is not None else future.result()
                    # Started only once the last is stored: a failure stops the run.
                    start()
        finally:
            pool.shutdown(cancel_futures=True)


class _Writer:
    """One process's matches of an eval run: staged, sealed into blocks and delivered."""

    def __init__(
        self,
        root: Path,
        name: str,
        targets: Sequence[StorageDestination],
        rollups: tuple[Rollup, ...],
    ) -> None:
        # Imported here: DuckDB is the optional `eval` extra.
        import duckdb  # noqa: PLC0415

        self.files = RunFiles(root)
        self.name = name
        self.targets = list(targets)
        self.rollups = rollups
        self.id = secrets.token_hex(6)
        self.problems: dict[str, str] = {}
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
        """Match records of the blocks here and at every destination; a done one wins.

        One listing per place and one read of each block not already read here.
        """
        places: list[tuple[Storage, str]] = [(DirectoryStorage(self.files.root), "")]
        places += [(target.storage, run_prefix(self.name)) for target in self.targets]
        found: dict[str, MatchRecord] = {}
        known: dict[str, Block] = {}
        for storage, prefix in places:
            names = (key[len(prefix) :] for key in storage.keys(prefix))
            for block in run_blocks(storage, prefix, self.name, names, known):
                if block.tables and block.rollups != self.rollups:
                    raise EtudeError("eval_run_rollups_conflict", 409) from ValueError(
                        f"block {block.name} keeps rollups"
                        f" {[rollup.name for rollup in block.rollups]}"
                    )
                for record in block.matches:
                    if found.get(record.match.key) is None or record.state == "done":
                        found[record.match.key] = record
        return found

    def fail(self, scheduled: Match, error: BaseException) -> NoReturn:
        detail = f"{type(error).__name__}: {error}"[:2000]
        self.staging.stage(
            MatchRecord(match=scheduled, state="failed", error=detail, wall_ms=_now_ms())
        )
        self._seal()
        raise EtudeError("match_failed", 500) from error

    def store(self, scheduled: Match, outcome: Tables | Result) -> None:
        """Check every table, then stage them, the blobs and the match's record; nothing on refusal."""
        result = outcome if isinstance(outcome, Result) else Result(tables=outcome)
        try:
            checked = {
                table: _checked(scheduled, table, rows, self.rollups)
                for table, rows in result.tables.items()
            }
            types = {
                table: _types(table, rows, self.columns.get(table, {}))
                for table, rows in checked.items()
                if rows
            }
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
            match=scheduled, state="done", columns=types, wall_ms=_now_ms(), blobs=digests
        )
        self.staging.stage(record, result.blobs)
        if self.staging.due():
            self._seal()

    def _write(
        self, target: Path, columns: dict[str, ColumnType], rows: Sequence[Row], match: str
    ) -> None:
        """Write one match's rows of a table to `target` as Parquet; readers never see half a file."""
        select, values = unnest(
            {**columns, "run": "VARCHAR", "match": "VARCHAR"},
            [[row.get(name) for name in columns] + [self.name, match] for row in rows],
        )
        partial = target.with_name(target.name + ".partial")
        self.db.execute(f"copy ({select}) to {quote(partial)} (format parquet)", values)
        partial.replace(target)

    def _seal(self) -> None:
        """Seal what is staged into a block and offer it to every destination."""
        with self.files.lock:
            self.staging.seal(self.db)
        self._ship()

    def _ship(self) -> bool:
        for target in self.targets:
            reason = ship(self.files, self.name, target)
            if reason is None:
                self.problems.pop(target.name, None)
            else:
                self.problems[target.name] = reason
        return not self.problems

    def finish(self, state: Literal["finished", "failed"]) -> None:
        with self.files.lock:
            self.staging.seal(self.db)
        self.staging.close()
        DirectoryStorage(self.files.root).put(
            "state.json",
            RunState(state=state, counters=RunCounters(), writer=self.id)
            .model_dump_json()
            .encode(),
        )
        self.db.close()
        if not self._ship():
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
