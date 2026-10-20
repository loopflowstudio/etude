"""The eval run: `evaluate` plays matches in worker processes and stores their tables.

`agent`, `environment` and `Match` describe what `evaluate` plays; it knows
nothing of tournaments (`etude.eval.tournament` arranges eval runs). The
author's `play(match)` returns tables of rows. Etude checks that every row
names a `player` (a seat) or a `group` of its match and that a column keeps
one type through the run, adds `run` and `match`, and writes each table as one
Parquet file per match, then the match's record; both are offered to every
destination after each match. A refused or raising match is recorded as
failed, nothing else of it is stored, and the run stops.

On finish, each table's files from this process are merged into one, which
replaces them once delivered. File names carry their write time, and a reader
takes a match's rows from the latest file holding them, so a match is never
counted twice.

A resume reads the match records already delivered to any destination, so a
machine with an empty disk skips what was delivered. Workers are fresh
processes (`spawn`), so `play` must be importable by name.
"""

from __future__ import annotations

import multiprocessing
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from etude.eval.records import (
    COLUMN_TYPES,
    Agent,
    ColumnType,
    Config,
    Environment,
    Match,
    MatchRecord,
)
from etude.run.declaration import declare
from etude.run.delivery import RunFiles, StorageDestination, destinations, ship
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Record, RunCounters, RunState
from etude.run.storage import DirectoryStorage, Storage, run_prefix

if TYPE_CHECKING:
    import duckdb

KIND = "evaluation"
# Author table and column names: plain SQL identifiers. A leading underscore is Etude's.
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Etude's own tables, which `etude.eval.query.connect` builds beside the author's.
ETUDE_TABLES = (
    "runs",
    "agents",
    "matches",
    "seats",
    "groups",
    "tournaments",
    "entrants",
    "training_checkpoints",
)
# Columns Etude adds to every row.
ADDED = ("run", "match")
# Tournaments are kept beside the runs, so no run takes this name.
TOURNAMENTS = "tournaments"

type Value = bool | int | float | str | None
type Row = Mapping[str, Value]
type Tables = Mapping[str, Sequence[Row]]

_CONFIG: TypeAdapter[Config] = TypeAdapter(dict[str, JsonValue])


class EvalRun(Record):
    """What one call to `evaluate` did."""

    experiment: str
    name: str
    environment: Environment
    played: int
    # Matches already stored by an earlier attempt.
    skipped: int


def agent(
    name: str,
    *,
    checkpoint: ArtifactRef | None = None,
    code: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Agent:
    """A saved model, by its artifact's digest, or code, by its source files; and its config.

    A directory stands for every file below it. Paths are hashed relative to
    their repository, so the identity is the same on every checkout.
    """
    if (checkpoint is None) == (len(code) == 0):
        raise EtudeError("agent_needs_checkpoint_or_code", 422)
    if checkpoint is not None:
        if checkpoint.sha256 is None:
            raise EtudeError("checkpoint_agent_requires_digest", 422)
        return Agent(
            name=name,
            kind="checkpoint",
            artifact=checkpoint.sha256,
            uri=checkpoint.uri,
            config=as_config(config),
        )
    if not _files(code):
        raise EtudeError("code_agent_without_files", 422)
    return Agent(name=name, kind="code", artifact=_digest_files(code), config=as_config(config))


def environment(
    name: str,
    *,
    files: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Environment:
    """What matches are played in: its defining files, hashed like code, and its config."""
    return Environment(name=name, files=_digest_files(files), config=as_config(config))


def as_config(config: Mapping[str, object] | BaseModel | None) -> Config:
    if config is None:
        return {}
    data = config.model_dump(mode="json") if isinstance(config, BaseModel) else dict(config)
    try:
        return _CONFIG.validate_python(data)
    except ValidationError as error:
        raise EtudeError("config_is_not_json", 422) from error


def _files(paths: Sequence[Path | str]) -> list[Path]:
    found: list[Path] = []
    for given in map(Path, paths):
        if given.is_dir():
            found += sorted(path for path in given.rglob("*") if path.is_file())
        elif given.is_file():
            found.append(given)
        else:
            raise EtudeError("identity_file_missing", 422) from FileNotFoundError(given)
    return found


def _repository_path(path: Path) -> str:
    """The path below the nearest enclosing repository, or as given outside one."""
    resolved = path.resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return resolved.relative_to(parent).as_posix()
    return path.as_posix()


def _digest_files(paths: Sequence[Path | str]) -> str:
    digest = sha256()
    named = {_repository_path(path): path for path in _files(paths)}
    for name in sorted(named):
        data = named[name].read_bytes()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    return digest.hexdigest()


def evaluate(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    play: Callable[[Match], Tables],
    workers: int = 1,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
) -> EvalRun:
    """Play every match not already stored in this eval run, `workers` at a time.

    The eval run is named `run` (see `etude.run.declaration.declare` for its
    defaults); resuming it requires the same environment. Raises
    `EtudeError("match_failed")` after recording the first match that raised
    or returned rows Etude refuses. Delivery failures do not raise: the files
    stay in the run's directory for `etude sync`, and stderr says so.
    """
    if workers < 1:
        raise EtudeError("workers_must_be_positive", 422)
    if len({scheduled.key for scheduled in matches}) != len(matches):
        raise EtudeError("duplicate_match", 422)
    if run == TOURNAMENTS:
        raise EtudeError("run_name_reserved", 422)
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
    writer = _Writer(declared.root, declared.name, destinations(declared.base, storages))
    pending = [scheduled for scheduled in matches if scheduled.key not in writer.done]
    try:
        for scheduled, outcome in _played(pending, play, workers):
            if isinstance(outcome, BaseException):
                writer.fail(scheduled, outcome)
            writer.store(scheduled, outcome)
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


def _played(
    pending: Sequence[Match], play: Callable[[Match], Tables], workers: int
) -> Iterator[tuple[Match, Tables | BaseException]]:
    """Each match with its tables or what it raised, in the order they finish."""
    if workers == 1:
        for scheduled in pending:
            try:
                yield scheduled, play(scheduled)
            except Exception as error:  # noqa: BLE001 - recorded with the match, then raised
                yield scheduled, error
        return
    # Fresh processes everywhere, so an eval run behaves alike on Linux and macOS.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(min(workers, len(pending) or 1), mp_context=context) as pool:
        futures = {pool.submit(play, scheduled): scheduled for scheduled in pending}
        try:
            for future in as_completed(futures):
                raised = future.exception()
                yield futures[future], raised if raised is not None else future.result()
        finally:
            # A failure stops the run: matches not yet started never start.
            pool.shutdown(cancel_futures=True)


class _Writer:
    """One process's writes to an eval run's folder, and their delivery."""

    def __init__(self, root: Path, name: str, targets: Sequence[StorageDestination]) -> None:
        # Imported here: DuckDB is the optional `eval` extra.
        import duckdb  # noqa: PLC0415

        self.files = RunFiles(root)
        self.name = name
        self.targets = list(targets)
        self.id = secrets.token_hex(6)
        self.problems: dict[str, str] = {}
        self.written: dict[str, list[str]] = {}
        self.db: duckdb.DuckDBPyConnection = duckdb.connect()
        stored = self._stored()
        self.done = {key for key, record in stored.items() if record.state == "done"}
        self.columns: dict[str, dict[str, ColumnType]] = {}
        for record in stored.values():
            for table, columns in record.columns.items():
                self.columns.setdefault(table, {}).update(columns)

    def _stored(self) -> dict[str, MatchRecord]:
        """Match records here and at every destination; a done one wins."""
        places: list[tuple[Storage, str]] = [(DirectoryStorage(self.files.root), "")]
        places += [(target.storage, run_prefix(self.name)) for target in self.targets]
        found: dict[str, MatchRecord] = {}
        for storage, prefix in places:
            for key in storage.keys(prefix + "matches/"):
                if not key.endswith(".json"):
                    continue
                record = MatchRecord.model_validate_json(storage.get(key))
                if found.get(record.match.key) is None or record.state == "done":
                    found[record.match.key] = record
        return found

    def fail(self, scheduled: Match, error: BaseException) -> NoReturn:
        detail = f"{type(error).__name__}: {error}"[:2000]
        self._record(MatchRecord(match=scheduled, state="failed", error=detail, wall_ms=_now_ms()))
        self._ship()
        raise EtudeError("match_failed", 500) from error

    def store(self, scheduled: Match, tables: Tables) -> None:
        """Check every table, then write them and the match's record; nothing on refusal."""
        try:
            checked = {table: _checked(scheduled, table, rows) for table, rows in tables.items()}
            types = {
                table: _types(table, rows, self.columns.get(table, {}))
                for table, rows in checked.items()
                if rows
            }
        except EtudeError as refused:
            self.fail(scheduled, refused)
        written = time.time_ns()
        for table, columns in types.items():
            key = f"tables/{table}.{written:020d}.{scheduled.key}.parquet"
            self._write(key, columns, checked[table], scheduled.key)
            self.written.setdefault(table, []).append(key)
            self.columns.setdefault(table, {}).update(columns)
        self._record(MatchRecord(match=scheduled, state="done", columns=types, wall_ms=_now_ms()))
        self._ship()

    def _write(
        self, key: str, columns: dict[str, ColumnType], rows: Sequence[Row], match: str
    ) -> None:
        names = list(columns)
        self.db.execute(
            "create or replace table _rows ("
            + ", ".join(f'"{name}" {kind}' for name, kind in columns.items())
            + ', "run" VARCHAR, "match" VARCHAR)'
        )
        self.db.executemany(
            f"insert into _rows values ({', '.join('?' for _ in range(len(names) + 2))})",
            [[row.get(name) for name in names] + [self.name, match] for row in rows],
        )
        self._copy("select * from _rows", key)

    def _copy(self, query: str, key: str) -> None:
        """Write a query's result to `key` as Parquet; readers never see half a file."""
        target = self.files.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        self.db.execute(f"copy ({query}) to {quote(partial)} (format parquet)")
        partial.replace(target)

    def _record(self, record: MatchRecord) -> None:
        with self.files.lock:
            DirectoryStorage(self.files.root).put(record.file, record.model_dump_json().encode())

    def _merge(self) -> None:
        """Replace each table's files from this process with one, once that one is delivered."""
        retired: list[str] = []
        for table, keys in self.written.items():
            if len(keys) < 2:
                continue
            files = ", ".join(quote(self.files.root / key) for key in keys)
            self._copy(
                f"select * from read_parquet([{files}], union_by_name = true)",
                f"tables/{table}.{time.time_ns():020d}.{self.id}.parquet",
            )
            retired += keys
        if not retired:
            return
        with self.files.lock:
            with (self.files.root / "tables" / f"{self.id}.removed").open("a") as note:
                note.write("".join(key + "\n" for key in retired))
            for key in retired:
                (self.files.root / key).unlink()
        self.written.clear()

    def _ship(self) -> bool:
        for target in self.targets:
            reason = ship(self.files, self.name, target)
            if reason is None:
                self.problems.pop(target.name, None)
            else:
                self.problems[target.name] = reason
        return not self.problems

    def finish(self, state: Literal["finished", "failed"]) -> None:
        self._merge()
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


def quote(path: Path) -> str:
    """A path as a DuckDB string literal."""
    return "'" + os.fspath(path).replace("'", "''") + "'"


def _checked(scheduled: Match, table: str, rows: Sequence[Row]) -> list[Row]:
    """The rows, refused unless each names exactly one seat or group of this match."""
    if not IDENTIFIER.match(table) or table in ETUDE_TABLES:
        raise EtudeError("table_name_refused", 422) from ValueError(table)
    for row in rows:
        player, group = row.get("player"), row.get("group")
        where = ValueError(f"table {table!r}: {dict(row)!r}")
        if (player is None) == (group is None):
            raise EtudeError("row_needs_player_or_group", 422) from where
        if player is not None and (
            type(player) is not int or not 0 <= player < len(scheduled.agents)
        ):
            raise EtudeError("row_player_not_seated", 422) from where
        if group is not None and group not in scheduled.groups:
            raise EtudeError("row_group_not_in_match", 422) from where
        for column in row:
            if column in ADDED or not IDENTIFIER.match(column):
                raise EtudeError("column_name_refused", 422) from where
    return list(rows)


def _types(
    table: str, rows: Sequence[Row], known: Mapping[str, ColumnType]
) -> dict[str, ColumnType]:
    """Each column's type in this write. Whole numbers may join a column of fractions."""
    found: dict[str, ColumnType] = {"player": "BIGINT", "group": "VARCHAR"}
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
