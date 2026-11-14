"""A live evaluation run: match validation, staging, rollups and outcome publication.

EvalRun owns match execution, worker processes and its local DuckDB connection
until finish. Result is the playing code's returned data; EvalResult counts
what play completed. Tournament scheduling lives in eval.tournament.
"""

from __future__ import annotations

import inspect
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
import time
import typing
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Literal, NoReturn, cast

from pydantic import BaseModel, JsonValue

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
    Environment,
    Match,
    MatchRecord,
    Rollup,
    as_config,
    outcomes,
)
from etude.eval.schedule import ordered
from etude.records import Record, RunCounters, RunWriter, Tally
from etude.run import Run, StorageDestination, declare, destinations, report_progress
from etude.stopping import Stopped
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
        environment: Environment,
        matches: Sequence[Match],
        workers: int,
    ) -> None:
        # Imported here: DuckDB is the optional `eval` extra.
        import duckdb  # noqa: PLC0415

        super().__init__(experiment=experiment, name=name, root=root, targets=targets)
        self.environment = environment
        self.matches = tuple(matches)
        self.workers = workers
        self._stop_requested = False
        self._asked: float | None = None
        self._slowest = 0.0
        self.rollups = rollups
        self.id = secrets.token_hex(6)
        self.writer = RunWriter(
            writer=self.id,
            run=self.address,
            started_ms=_now_ms(),
            host=socket.gethostname(),
            attempt=os.environ.get("ETUDE_ATTEMPT"),
            commit=os.environ.get("ETUDE_COMMIT"),
        )
        self.failed = 0  # `fail` is also how `store` refuses a match
        self.db: duckdb.DuckDBPyConnection = duckdb.connect()
        try:
            seal_abandoned(root, self.db)
            stored = self._stored()
        except BaseException:
            self.db.close()
            raise
        self.staging = Staging(root, self.id, rollups)
        self.done = {key for key, record in stored.items() if record.state == "done"}
        self.columns: dict[str, dict[str, ColumnType]] = {}
        for record in stored.values():
            for table, columns in record.columns.items():
                self.columns.setdefault(table, {}).update(columns)

        self._report = _Report(
            total=len(self.matches),
            done=sum(match.key in self.done for match in self.matches),
            writer=self.writer,
        )
        self._report.write()

    @property
    def should_stop(self) -> bool:
        """A requested stop is observed between matches, after retaining completed tables."""
        now = time.monotonic()
        if self._asked is not None:
            self._slowest = max(self._slowest, now - self._asked)
        self._asked = now
        stop = os.environ.get("ETUDE_STOP_FILE")
        deadline = os.environ.get("ETUDE_DEADLINE")
        self._stop_requested |= bool(stop and Path(stop).exists()) or bool(
            deadline and datetime.fromisoformat(deadline).timestamp() - time.time() <= self._slowest
        )
        return self._stop_requested

    def play[C: BaseModel | Mapping[str, JsonValue]](
        self, play: Callable[[Match[C]], Tables | Result]
    ) -> EvalResult:
        """Play the selected matches once, skipping this run's durable results on resume."""
        pending = [match for match in self.matches if match.key not in self.done]
        skipped = len(self.matches) - len(pending)
        played = 0
        report = self._report

        def pending_matches() -> Iterator[Match]:
            for match in pending:
                if self.should_stop:
                    raise Stopped()
                yield match

        started = time.monotonic()
        try:
            with _terminable():
                for scheduled, outcome in _played(pending_matches(), play, self.workers):
                    if isinstance(outcome, BaseException):
                        self.fail(scheduled, outcome)
                    self.store(scheduled, outcome)
                    self.done.add(scheduled.key)
                    played += 1
                    report.played()
                    print(
                        f"etude: {self.name}: {played}/{len(pending)} matches played, "
                        f"{time.monotonic() - started:.0f}s elapsed",
                        file=sys.stderr,
                        flush=True,
                    )
        finally:
            report.write(failed=self.failed)
        return EvalResult(
            experiment=self.experiment,
            name=self.name,
            environment=self.environment,
            played=played,
            skipped=skipped,
        )

    def _stored(self) -> dict[str, MatchRecord]:
        """Each match's standing record (`outcomes`) of the blocks here and at every destination.

        One listing per place and one read of each block not already read here.
        """
        places: list[tuple[Storage, str]] = [(DirectoryStorage(self.files.root), "")]
        places += [(target.storage, run_prefix(self.address)) for target in self._destinations]
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

    def finish(self, state: Literal["finished", "failed", "stopped"]) -> None:
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


# A running eval run says how far it has got at most this often: each report
# rewrites the allocation's progress file.
_REPORT_SECONDS = 15.0
# Matches a recent pace is taken over.
_PACE_MATCHES = 50


class _Report:
    """Under `etude launch`, how far the eval run has got, for the launcher's status.

    `etude-result.json` in `ETUDE_OUTPUT_DIR` holds a tally, the shape of
    `etude.records.Tally`; evaluation does not import the launcher.
    It is progress only: results are the run's blocks.
    """

    def __init__(self, *, total: int, done: int, writer: RunWriter) -> None:
        self.writer = writer
        self.total, self.done = total, done
        self.outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        self.recent: deque[float] = deque(maxlen=_PACE_MATCHES)
        self.written = time.monotonic()

    def played(self) -> None:
        """Count one stored match, and report when the last report is old enough."""
        self.done += 1
        now = time.monotonic()
        self.recent.append(now)
        if now - self.written >= _REPORT_SECONDS:
            self.write()

    def write(self, *, failed: int = 0) -> None:
        if self.outputs is None:
            return
        elapsed = self.recent[-1] - self.recent[0] if self.recent else 0.0
        tally = Tally(
            unit="matches",
            done=self.done,
            total=self.total,
            failed=failed,
            per_hour=(len(self.recent) - 1) / elapsed * 3600 if elapsed > 0 else None,
        )
        report_progress(writer=self.writer, clocks={}, tally=tally)
        self.written = time.monotonic()


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


def _played[C: BaseModel | Mapping[str, JsonValue]](
    pending: Iterator[Match], play: Callable[[Match[C]], Tables | Result], workers: int
) -> Iterator[tuple[Match, Tables | Result | BaseException]]:
    """Each match with its tables or what it raised, in the order they finish.

    Matches start in `etude.eval.schedule` order, at most `workers` at once, so
    the order they start in is the order an author's runtime loads checkpoints in.
    """
    if workers == 1:
        for scheduled in pending:
            try:
                yield scheduled, _play(play, scheduled)
            except Exception as error:  # noqa: BLE001 - recorded with the match, then raised
                yield scheduled, error
        return
    # Fresh processes everywhere, so an eval run behaves alike on Linux and macOS.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, mp_context=context) as pool:
        running: dict[Future[Tables | Result], Match] = {}

        def start() -> None:
            scheduled = next(pending, None)
            if scheduled is not None:
                running[pool.submit(_play, play, scheduled)] = scheduled

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


class EvalResult(Record):
    """What one `EvalRun.play` did."""

    experiment: str
    name: str
    environment: Environment
    played: int
    # Matches already stored by an earlier attempt.
    skipped: int


@contextmanager
def _evaluating_matches(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    workers: int = 1,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
    resume: bool = False,
) -> Iterator[EvalRun]:
    if workers < 1:
        raise EtudeError("workers_must_be_positive", 422)
    if len({scheduled.key for scheduled in matches}) != len(matches):
        raise EtudeError("duplicate_match", 422)
    names = [rollup.name for rollup in rollups]
    if len(set(names)) != len(names) or set(names) & set(ETUDE_TABLES):
        raise EtudeError("rollup_name_refused", 422) from ValueError(names)
    declared = declare(
        experiment=experiment,
        clocks=(),
        run=run,
        kind="evaluation",
        directory=directory,
        resume=resume,
    )
    if declared.record.kind != "evaluation":
        raise EtudeError("run_is_not_an_eval_run", 409)
    path = declared.root / "environment.json"
    if not path.is_file():
        DirectoryStorage(declared.root).put(
            "environment.json", environment.model_dump_json().encode()
        )
    elif Environment.model_validate_json(path.read_bytes()) != environment:
        raise EtudeError("eval_run_environment_conflict", 409)
    writer = EvalRun(
        declared.root,
        declared.name,
        destinations(declared.base, storages),
        tuple(rollups),
        experiment=experiment,
        environment=environment,
        matches=list(ordered(matches)),
        workers=workers,
    )
    try:
        yield writer
    except Stopped:
        writer.finish("stopped")
        raise
    except BaseException:
        writer.finish("failed")
        raise
    else:
        writer.finish("finished")


@lru_cache(maxsize=128)
def _match_type(play: Callable[..., object]) -> type[Match]:
    parameters = list(inspect.signature(play).parameters.values())
    if len(parameters) != 1:
        raise TypeError("play must take exactly one Match parameter")
    kind = typing.get_type_hints(play).get(parameters[0].name, Match)
    if not isinstance(kind, type) or not issubclass(kind, Match):
        raise TypeError("play's parameter must be annotated with Match[YourConfig]")
    return kind


def _play[C: BaseModel | Mapping[str, JsonValue]](
    play: Callable[[Match[C]], Tables | Result], match: Match
) -> Tables | Result:
    kind = _match_type(play)
    # The author's generic annotation is the validation boundary, just as the
    # config parameter is for etude.call. Stored matches retain portable JSON.
    checked = kind.model_validate_json(match.model_dump_json()) if kind is not Match else match
    return play(cast(Match[C], checked))
