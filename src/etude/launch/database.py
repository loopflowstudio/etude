"""The launcher's SQLite file. Records change only through the operations named here.

Every session, attempt and run row carries a version. A write names the version it
read and raises Stale if the row has moved, so two processes working the same
file cannot both take one step. Status and spend are computed, never stored.

open_home selects one launcher.sqlite, taking over a released predecessor or
isolating unreleased SQL in a copy. Named migrations are applied transactionally;
their ledger fences writers when a later release raises the reader floor.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache, partial
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from etude.errors import LauncherError
from etude.launch.errors import SchemaAdvanced
from etude.launch.records import (
    RETRYABLE,
    Cause,
    Cohort,
    Diagnostic,
    Hardware,
    Image,
    Launch,
    LogStream,
    Machine,
    OnDemand,
    Outcome,
    Output,
    Pool,
    Prepared,
    Provider,
    Rental,
    RunAttempt,
    Session,
    Stale,
    Storage,
    Supply,
    Target,
)
from etude.launch.usage import Transfer
from etude.records import RunRecord, RunWriter, Tally
from etude.spec import Job, Record, Smoke, SourcePosition

Fact = str | int | float | None

# The schema is the packaged migrations, applied in order and recorded in this ledger.
# PRAGMA user_version counts its rows: one pragma tells a process the file moved on.
# A NULL floor marks an unreleased migration, which only a copy ever holds.
_FOLDER = Path(__file__).with_name("migrations")
_LEDGER = """CREATE TABLE migrations(
  name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL,
  floor TEXT, program TEXT)"""


class Predecessor(Record):
    """A pre-ledger schema, the etude that reads it and the files a take-over may copy.

    `files` maps each released name to its own reader: 0.3.1 wrote schema 17 to
    `launcher.db`, 0.3.2 to `launcher-17.db`.
    """

    files: dict[str, str] = {}
    migrations: tuple[str, ...] = ()
    etude: str


# Newest first: selection, ledger seeding and older-file notices share these facts.
_PREDECESSORS = {
    19: Predecessor(
        files={"launcher-19.db": "0.4.0"},
        migrations=("baseline", "run_resume_and_writers"),
        etude="0.4.0",
    ),
    17: Predecessor(
        files={"launcher-17.db": "0.3.2", "launcher.db": "0.3.1"},
        migrations=("baseline",),
        etude="0.3.2",
    ),
    15: Predecessor(etude="0.3.0"),
    14: Predecessor(etude="0.2.16"),
    13: Predecessor(etude="0.2.15"),
    11: Predecessor(etude="0.2.14"),
    10: Predecessor(etude="0.2.6"),
    8: Predecessor(etude="0.2.0"),
    2: Predecessor(etude="0.1.2"),
    1: Predecessor(etude="0.1.1"),
}


class Released(Record):
    """A migration as its release recorded it, in `migrations/released.json`."""

    name: str
    sha256: str
    floor: str  # the oldest etude that can open a file holding this migration


class Migration(Record):
    """One packaged SQL file. Its stem is its name. Header lines `-- after: a, b` name the
    migrations it must follow, and `-- breaks: reason` makes it raise the reader floor
    even when its schema change looks additive."""

    name: str
    after: tuple[str, ...]
    breaks: str | None
    sql: str
    sha256: str
    floor: str | None  # None until a release records it


@cache
def released() -> tuple[Released, ...]:
    """What every release so far recorded, oldest first."""
    raw = json.loads((_FOLDER / "released.json").read_text())
    return tuple(Released.model_validate(entry) for entry in raw)


def _header(sql: str, key: str) -> str | None:
    for line in sql.splitlines():
        if not line.startswith("--"):
            return None
        if line.startswith(f"-- {key}:"):
            return line.removeprefix(f"-- {key}:").strip()
    return None


@cache
def migrations() -> tuple[Migration, ...]:
    """Every packaged migration, dependencies first and otherwise by name.

    Raises LauncherError when a released migration's file no longer matches its release.
    """
    floors = {r.name: r for r in released()}
    found: dict[str, Migration] = {}
    for entry in _FOLDER.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        body = entry.read_bytes()
        sql = body.decode()
        name = entry.name.removesuffix(".sql")
        digest = hashlib.sha256(body).hexdigest()
        if name in floors and floors[name].sha256 != digest:
            raise LauncherError(f"released migration {name} was edited; add a new one instead")
        found[name] = Migration(
            name=name,
            after=tuple(n.strip() for n in (_header(sql, "after") or "").split(",") if n.strip()),
            breaks=_header(sql, "breaks"),
            sql=sql,
            sha256=digest,
            floor=floors[name].floor if name in floors else None,
        )
    if missing := sorted(floors.keys() - found.keys()):
        raise LauncherError(f"released migrations are missing: {', '.join(missing)}")
    unknown = sorted({a for m in found.values() for a in m.after} - found.keys())
    if unknown:
        raise LauncherError(f"migrations name unknown predecessors: {', '.join(unknown)}")
    ordered: list[Migration] = []
    while len(ordered) < len(found):
        done = {m.name for m in ordered}
        ready = [m for m in found.values() if m.name not in done and set(m.after) <= done]
        if not ready:
            raise LauncherError(
                f"migrations form a cycle: {', '.join(sorted(found.keys() - done))}"
            )
        ordered.append(min(ready, key=lambda m: m.name))
    return tuple(ordered)


def _statements(migration: Migration) -> Iterator[str]:
    # executescript would commit the open transaction; run one complete statement at a time.
    pending = ""
    for character in migration.sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            code = "\n".join(line.partition("--")[0] for line in pending.splitlines()).strip()
            if re.match(
                r"(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH|VACUUM)\b",
                code,
                re.IGNORECASE,
            ):
                raise LauncherError(f"migration {migration.name} must stay inside its transaction")
            yield pending
            pending = ""
    if any(line.strip() and not line.lstrip().startswith("--") for line in pending.splitlines()):
        raise LauncherError(f"migration {migration.name} ends inside a statement")


def _version(text: str) -> tuple[int, ...]:
    """A release version's numbers, ignoring any local or development suffix."""
    found = re.match(r"\d+(?:\.\d+)*", text)
    if found is None:
        raise LauncherError(f"{text!r} is not an etude version")
    return tuple(int(part) for part in found.group().split("."))


def _too_new(rows: Sequence[sqlite3.Row], file: str) -> str | None:
    """Why ledger rows this etude does not know keep it from the file, or None if they are
    all additive changes it can still read and write."""
    if unreleased := [r["name"] for r in rows if r["floor"] is None]:
        return f"{file} was migrated by an unreleased etude ({', '.join(unreleased)})"
    if not rows:
        return None
    floor = max((r["floor"] for r in rows), key=_version)
    if _version(floor) <= _version(version("etude-sdk")):
        return None
    raised = ", ".join(r["name"] for r in rows if _version(r["floor"]) == _version(floor))
    return f"{file} needs etude >= {floor} (raised by: {raised})"


def _schema(db: sqlite3.Connection) -> tuple[dict[str, frozenset[tuple[object, ...]]], set[str]]:
    """Each table's columns, as a set because column order is not schema, and every index."""
    tables = [
        r[0]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("sqlite_")
    ]
    columns = {
        t: frozenset(
            tuple(row)[1:] for row in db.execute(f"SELECT * FROM pragma_table_xinfo('{t}')")
        )
        for t in tables
    }
    indexes = {r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index'") if r[0]}
    return columns, indexes


def _reference(migrations: Sequence[Migration]) -> sqlite3.Connection:
    """An empty database holding exactly these migrations."""
    db = sqlite3.connect(":memory:")
    for migration in migrations:
        for statement in _statements(migration):
            db.execute(statement)
    return db


_ADDITIVE = re.compile(r"^(CREATE TABLE|CREATE INDEX|ALTER TABLE \w+ ADD COLUMN)\b", re.IGNORECASE)


def additive(migration: Migration, before: Sequence[Migration]) -> bool:
    """Whether a file holding `before` stays readable by their etude once this is applied.

    Proven, never declared: every statement only creates a table or index or adds a
    column, nothing that existed changes, and every new column can be left out.
    """
    if migration.breaks is not None:
        return False
    for statement in _statements(migration):
        code = "\n".join(line.partition("--")[0] for line in statement.splitlines()).strip()
        code = " ".join(code.split())
        if not _ADDITIVE.match(code):
            return False
        if code.upper().startswith("ALTER TABLE") and re.search(
            r"\b(CHECK|REFERENCES|UNIQUE|PRIMARY|GENERATED)\b", code, re.IGNORECASE
        ):
            return False
    db = _reference(before)
    try:
        old_columns, old_indexes = _schema(db)
        for statement in _statements(migration):
            db.execute(statement)
        new_columns, new_indexes = _schema(db)
        for table, columns in old_columns.items():
            if not columns <= new_columns.get(table, frozenset()):
                return False
            # xinfo after cid: name, type, notnull, default, pk, hidden.
            if any(
                c[2] and (c[3] is None or str(c[3]).strip("() ").upper() == "NULL")
                for c in new_columns[table] - columns
            ):
                return False
        return old_indexes <= new_indexes
    finally:
        db.close()


def release(at: str) -> tuple[Released, ...]:
    """Record every unreleased migration as released at version `at`, for the release cut.

    Each keeps the previous floor when it is additive and is raised to `at` otherwise.
    """
    recorded = released()
    floor = max((r.floor for r in recorded), key=_version, default=at)
    applied: list[Migration] = []
    added: list[Released] = []
    for migration in migrations():
        if migration.floor is None:
            if not additive(migration, applied):
                floor = at
            added.append(Released(name=migration.name, sha256=migration.sha256, floor=floor))
        applied.append(migration)
    return (*recorded, *added)


def _program(path: Path) -> str | None:
    """The controller program running this process, when it is one kept in the file's home."""
    program = Path(sys.prefix).resolve().parent  # programs/<digest>/.venv
    homes = path.resolve().parents
    return (
        str(program) if program.parent.name == "programs" and program.parents[1] in homes else None
    )


def _body(record: type[Record], row: sqlite3.Row) -> Record:
    return record.model_validate_json(row["body"])


def launcher_file(home: Path) -> Path:
    """The released launcher file; use open_home to select a development copy."""
    return home / "launcher.sqlite"


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


# SQLite scalars already match the model fields. Only JSON columns need
# adapting; records own scalar validation.
_RUNS = "SELECT r.* FROM runs r"


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    spec = Job.model_validate_json(row["spec"])
    return RunRecord.model_validate(
        {
            **{key: row[key] for key in row.keys() if key not in {"seq", "tries"}},
            "spec": spec,
            "experiment": spec.experiment or "",
            "params": json.loads(row["params"]),
            "source": SourcePosition.model_validate_json(row["source"]) if row["source"] else None,
            "smoke": Smoke.model_validate_json(row["smoke"]),
        }
    )


# A rented machine has one working session for its whole life. What the provider
# said about making and deleting the machine is recorded on that session.
_MACHINES = """SELECT m.name, m.pool, m.ondemand, m.kind, m.cost_usd, m.expires_at,
    m.provider, m.address, m.storage, m.image,
    w.claimed_at, w.provider_id, w.hardware, w.refused_at,
    COALESCE(w.ended_at, m.deleted_at) AS deleted_at
    FROM machines m LEFT JOIN sessions w ON w.machine=m.name AND m.kind='rented'"""
# A session's supply is its machine's: one owner for the fact.
_SESSIONS = """SELECT w.*, COALESCE(m.pool, m.ondemand) AS supply
    FROM sessions w JOIN machines m ON m.name = w.machine"""
_RENTALS = "SELECT r.* FROM rentals r"


def _machine_from_row(row: sqlite3.Row) -> Machine:
    return Machine.model_validate(
        {
            **dict(row),
            "image": Image.model_validate_json(row["image"]) if row["image"] else None,
            "hardware": _hardware(row),
        }
    )


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session.model_validate({**dict(row), "hardware": _hardware(row)})


def _hardware(row: sqlite3.Row) -> object:  # validated by the record holding it
    return json.loads(row["hardware"]) if row["hardware"] else None


def _writers_json(attempt: RunAttempt, reported: tuple[RunWriter, ...]) -> str:
    """The allocation's writers with the latest report of each, in first-seen order."""
    merged = {writer.writer: writer for writer in (*attempt.writers, *reported)}
    return json.dumps([writer.model_dump(mode="json") for writer in merged.values()])


def _attempt_from_row(row: sqlite3.Row) -> RunAttempt:
    return RunAttempt.model_validate(
        {
            **dict(row),
            "clocks": json.loads(row["clocks"]),
            "writers": json.loads(row["writers"]),
            "outputs": json.loads(row["outputs"]),
            "prepared": None if row["prepared"] is None else json.loads(row["prepared"]),
            "tally": None if row["tally"] is None else json.loads(row["tally"]),
        }
    )


class Database:
    def __init__(self, path: Path, *, timeout: float = 30, backup: bool = True) -> None:
        """Open local state, migrating it first; timeout bounds SQLite lock contention, in seconds.

        A staging snapshot may omit a backup; its source remains the recovery file.
        Raises LauncherError, changing nothing, for a file a newer etude migrated, a
        migration edited after it was applied, or rows the current records reject.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("database timeout must be nonnegative and finite")
        self._locks: dict[str, tuple[str, Callable[[], datetime]]] = {}
        self._path = path
        self._timeout = timeout
        self._backup = backup
        self._notice: str | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=timeout)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self.fresh = False  # whether opening created the file's schema
        try:
            if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone():
                rows = self._db.execute(
                    "SELECT name, sha256, floor FROM migrations ORDER BY rowid"
                ).fetchall()
                known = {m.name: m for m in migrations()}
                if reason := _too_new([r for r in rows if r["name"] not in known], path.name):
                    raise LauncherError(reason)
                for row in rows:
                    if row["name"] in known and row["sha256"] != known[row["name"]].sha256:
                        raise LauncherError(
                            f"migration {row['name']} changed after it was applied to {path}"
                        )
                if set(known) <= {r["name"] for r in rows}:
                    self._generation = len(rows)
                    return
            with self._transaction():
                self._generation = self._migrate()
            if self._notice is not None:
                print(self._notice, file=sys.stderr)
        except BaseException:
            self._db.close()
            raise

    def _migrate(self) -> int:
        """Apply what the file lacks and return its generation. Runs inside a write transaction,
        so a second etude opening the file waits and then finds nothing left to do."""
        known = {m.name: m for m in migrations()}
        file = self._path.name
        ledger = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migrations'"
        ).fetchone()
        newer: list[sqlite3.Row] = []
        if ledger:
            applied: tuple[str, ...] = ()
            for row in self._db.execute(
                "SELECT name, sha256, floor FROM migrations ORDER BY rowid"
            ):
                if row["name"] not in known:
                    newer.append(row)
                elif row["sha256"] != known[row["name"]].sha256:
                    raise LauncherError(
                        f"migration {row['name']} changed after it was applied to {self._path}; "
                        f"restore the newest {file}.backup-* or move the file away"
                    )
                else:
                    applied += (row["name"],)
            if reason := _too_new(newer, file):
                raise LauncherError(reason)
        else:
            made = self._db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            schema = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if made and (schema not in _PREDECESSORS or not _PREDECESSORS[schema].migrations):
                raise LauncherError(
                    f"{self._path} was written by an unreleased etude (schema {schema}); "
                    "move or delete it to start again"
                )
            applied = _PREDECESSORS[schema].migrations if made else ()
            self.fresh = not made
        pending = [m for name, m in known.items() if name not in applied]
        if newer:
            # A newer etude made only additive changes: this one reads and writes what it knows.
            if pending:
                raise LauncherError(
                    f"{file} has migrations from a newer etude but lacks "
                    f"{', '.join(m.name for m in pending)}; upgrade etude"
                )
            return len(applied) + len(newer)
        if ledger and not pending:
            return len(applied)
        backup = self._back_up() if ledger and pending and self._backup else None
        program = _program(self._path)
        if not ledger:
            self._db.execute(_LEDGER)
            for name in applied:  # taken over from a released file's user_version
                self._ledger(known[name], program)
        for migration in pending:
            _check_sqlite(migration)
            try:
                for statement in _statements(migration):
                    self._db.execute(statement)
            except sqlite3.Error as error:
                raise LauncherError(
                    f"launcher: migration {migration.name} failed: {error}"
                ) from error
            self._ledger(migration, program)
        problems = self._invalid_records()
        if problems:
            raise LauncherError(
                f"{file} cannot be migrated; these rows do not validate:\n" + "\n".join(problems)
            )
        generation = len(applied) + len(pending)
        self._db.execute(f"PRAGMA user_version = {generation}")
        if backup is not None:
            names = ", ".join(m.name for m in pending)
            self._notice = (
                f"launcher: {file}: applied {names} (backup {backup.name}){self._lockout(applied)}"
            )
        return generation

    def _lockout(self, applied: Sequence[str]) -> str:
        """The clause naming who can no longer open the file, when this migration raised its floor."""
        floors = [m.floor for m in migrations() if m.floor is not None]
        before = [m.floor for m in migrations() if m.name in applied and m.floor is not None]
        if not floors:
            return ""
        now = max(floors, key=_version)
        if before and _version(now) <= _version(max(before, key=_version)):
            return ""
        return f"; etude < {now} can no longer open it"

    def _ledger(self, migration: Migration, program: str | None) -> None:
        self._db.execute(
            "INSERT INTO migrations(name, sha256, applied_at, floor, program) VALUES (?,?,?,?,?)",
            (
                migration.name,
                migration.sha256,
                datetime.now(UTC).isoformat(),
                migration.floor,
                program,
            ),
        )

    def _back_up(self) -> Path:
        """Copy the file as it is before migrating, keeping the two newest copies.

        A second connection reads it: this one holds the write lock, and WAL lets
        a reader see the last committed state.
        """
        target = self._path.with_name(
            f"{self._path.name}.backup-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
        )
        source = sqlite3.connect(self._path, timeout=self._timeout)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        target.chmod(0o600)
        backups = sorted(
            (p for p in self._path.parent.glob(f"{self._path.name}.backup-*")),
            key=lambda p: p.name,
        )
        for old in backups[:-2]:
            old.unlink()
        return target

    def _invalid_records(self) -> list[str]:
        """Every row that the current records reject, so a migration can be refused whole."""
        rows: dict[str, tuple[str, Callable[[sqlite3.Row], object]]] = {
            "pools": ("SELECT * FROM pools", lambda row: Pool.model_validate(dict(row))),
            "machines": (_MACHINES, _machine_from_row),
            "runs": (_RUNS, _run_from_row),
            "sessions": (_SESSIONS, _session_from_row),
            "rentals": (_RENTALS, lambda row: Rental.model_validate(dict(row))),
            "attempts": ("SELECT * FROM attempts", _attempt_from_row),
            "providers": (
                "SELECT * FROM providers",
                lambda row: Provider.model_validate(dict(row)),
            ),
            "storages": ("SELECT * FROM storages", lambda row: Storage.model_validate(dict(row))),
            "cohorts": ("SELECT * FROM cohorts", lambda row: Cohort.model_validate(dict(row))),
        }
        bodies: dict[str, type[Record]] = {
            "launches": Launch,
            "diagnostics": Diagnostic,
            "log_streams": LogStream,
            "transfers": Transfer,
            "ondemands": OnDemand,
        }
        for table, record in bodies.items():
            rows[table] = (f"SELECT * FROM {table}", partial(_body, record))
        problems: list[str] = []
        for table, (query, read) in rows.items():
            for row in self._db.execute(query):
                try:
                    read(row)
                except ValueError as error:
                    problems.append(f"  {table}: invalid record ({type(error).__name__})")
        return problems

    def check_schema(self) -> None:
        """Raise SchemaAdvanced if another etude raised the file's floor past this one since it
        opened it. Additive migrations by a newer etude are taken in stride."""
        generation = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if generation == self._generation:
            return
        rows = self._db.execute(
            "SELECT name, floor, program FROM migrations WHERE rowid > ? ORDER BY rowid",
            (self._generation,),
        ).fetchall()
        known = {m.name for m in migrations()}
        if _too_new([r for r in rows if r["name"] not in known], self._path.name):
            raise SchemaAdvanced(rows[-1]["program"] if rows else None)
        self._generation = generation

    def retain_transfer_json(self, raw: str) -> None:
        self.retain_transfer(Transfer.model_validate_json(raw))

    def retain_transfer(self, event: Transfer) -> None:
        self._write(
            "INSERT OR IGNORE INTO transfers(id, body) VALUES (?, ?)",
            (event.id, event.model_dump_json()),
        )

    def transfers(self) -> tuple[Transfer, ...]:
        return tuple(
            Transfer.model_validate_json(row[0])
            for row in self._db.execute("SELECT body FROM transfers ORDER BY rowid")
        )

    def last_delivery(self, attempt: str) -> datetime | None:
        """Most recent successful write retained for one allocation, without network I/O."""
        row = self._db.execute(
            """SELECT json_extract(body, '$.observed_at') FROM transfers
               WHERE json_extract(body, '$.attempt') = ?
                 AND json_extract(body, '$.completed') = 1
                 AND json_extract(body, '$.status') BETWEEN 200 AND 299
                 AND json_extract(body, '$.operation') IN ('PUT', 'POST')
               ORDER BY julianday(json_extract(body, '$.observed_at')) DESC LIMIT 1""",
            (attempt,),
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row[0])

    def begin_launch(self, launch: Launch) -> None:
        with self._together():
            for request in launch.requests:
                if (
                    request.resume_version is None
                    and self._db.execute("SELECT 1 FROM runs WHERE id=?", (request.run,)).fetchone()
                ):
                    raise LauncherError(
                        f"run {request.run!r} already admitted; choose --experiment NAME for a new run; "
                        "use --resume to carry on, or --resume --until CLOCK=TARGET to train further"
                    )
                for previous in self.launches(request.run):
                    if previous.state in {"preparing", "admitting"} or (
                        previous.state != "admitted"
                        and any(
                            session.refused_at is None and session.ended_at is None
                            for session in self.launch_sessions(previous.id, acquired_only=True)
                        )
                    ):
                        raise LauncherError(
                            f"run {request.run!r}: reconciling existing request {previous.id}"
                        )
            self.save_launch(launch)
            self._db.executemany(
                "INSERT INTO launch_runs(launch, run) VALUES (?, ?)",
                ((launch.id, r.run) for r in launch.requests),
            )

    def launch_evidence(self, identity: str) -> Launch:
        row = self._db.execute("SELECT body FROM launches WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise LauncherError(f"unknown launch {identity}")
        return Launch.model_validate_json(row[0])

    def claim_launch(self, identity: str) -> Launch:
        with self._together():
            launch = self.launch_evidence(identity)
            if launch.state != "preparing":
                raise LauncherError(
                    f"launch {identity} already {launch.state}; inspect retained evidence"
                )
            launch = launch.model_copy(update={"state": "admitting"})
            self.save_launch(launch)
            return launch

    def save_launch(self, launch: Launch) -> None:
        self._write(
            "INSERT OR REPLACE INTO launches(id, body) VALUES (?, ?)",
            (launch.id, launch.model_dump_json()),
        )

    def launches(self, run: str) -> tuple[Launch, ...]:
        return tuple(
            Launch.model_validate_json(row[0])
            for row in self._db.execute(
                "SELECT l.body FROM launches l JOIN launch_runs r ON l.id=r.launch "
                "WHERE r.run=? ORDER BY l.rowid",
                (run,),
            )
        )

    def link_launch(self, launch: str, session: str, *, acquired: bool = False) -> None:
        self._write(
            "INSERT OR IGNORE INTO launch_sessions(launch, session, acquired) VALUES (?, ?, ?)",
            (launch, session, int(acquired)),
        )

    def launch_sessions(self, launch: str, *, acquired_only: bool = False) -> tuple[Session, ...]:
        return tuple(
            self.session(row[0])
            for row in self._db.execute(
                "SELECT session FROM launch_sessions WHERE launch=? AND (NOT ? OR acquired)",
                (launch, int(acquired_only)),
            )
        )

    def retain_diagnostic(self, diagnostic: Diagnostic) -> None:
        self._write(
            "INSERT OR IGNORE INTO diagnostics(id, body) VALUES (?, ?)",
            (diagnostic.id, diagnostic.model_dump_json()),
        )

    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(
            Diagnostic.model_validate_json(row[0])
            for row in self._db.execute("SELECT body FROM diagnostics ORDER BY rowid")
        )

    def retain_log(self, stream: LogStream) -> None:
        self._write(
            "INSERT OR REPLACE INTO log_streams(id, body) VALUES (?, ?)",
            (stream.id, stream.model_dump_json()),
        )

    def log(self, identity: str) -> LogStream | None:
        row = self._db.execute("SELECT body FROM log_streams WHERE id=?", (identity,)).fetchone()
        return None if row is None else LogStream.model_validate_json(row[0])

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    @contextmanager
    def _together(self) -> Iterator[None]:
        """Several statements that land together or not at all, on the schema this process knows.

        Inside an open transaction it adds nothing: the outer one already checked.
        """
        if self._db.in_transaction:
            yield
            return
        with self._transaction():
            self.check_schema()
            yield

    def _write(self, sql: str, parameters: Sequence[Fact] = ()) -> sqlite3.Cursor:
        """One write statement, refused once the schema has moved on."""
        with self._together():
            return self._db.execute(sql, parameters)

    @contextmanager
    def lock(self, supply: str, clock: Callable[[], datetime]) -> Iterator[bool]:
        """Lease placement authority over a pool or on-demand for five minutes.

        No transaction is held: other supplies and readers keep working. Every
        placement write renews the lease and fences an expired owner; abandoned
        sessions retain their IDs.
        """
        owner = uuid4().hex
        now = clock()
        with self._together():
            acquired = (
                self._write(
                    """INSERT INTO locks(supply, owner, expires_at) VALUES (?,?,?) ON CONFLICT(supply) DO UPDATE
                SET owner=excluded.owner, expires_at=excluded.expires_at
                WHERE locks.expires_at <= ?""",
                    (supply, owner, (now + timedelta(minutes=5)).isoformat(), now.isoformat()),
                ).rowcount
                == 1
            )
        if not acquired:
            yield False
            return
        self._locks[supply] = owner, clock
        try:
            yield True
        finally:
            self._locks.pop(supply)
            self._write("DELETE FROM locks WHERE supply=? AND owner=?", (supply, owner))

    def check_lock(self, supply: str) -> None:
        """Fence stale processes before effects and inside placement transactions."""
        held = self._locks.get(supply)
        if held is None:
            raise Stale(f"{supply!r} has no placement lock")
        owner, clock = held
        now = clock()
        changed = self._write(
            "UPDATE locks SET expires_at=? WHERE supply=? AND owner=? AND expires_at>?",
            ((now + timedelta(minutes=5)).isoformat(), supply, owner, now.isoformat()),
        )
        if changed.rowcount != 1:
            raise Stale(f"{supply!r} placement lock expired")

    def _change(self, table: str, id: str | str | str, version: int, **facts: Fact) -> None:
        sets = "".join(f"{name} = ?, " for name in facts)
        done = self._write(
            f"UPDATE {table} SET {sets}version = version + 1 WHERE id = ? AND version = ?",
            (*facts.values(), id, version),
        )
        if done.rowcount != 1:
            raise Stale(f"{table} {id} is no longer at version {version}")

    # Providers, storage, pools and machines

    def set_provider(self, provider: Provider) -> None:
        """Record a provider's settings, replacing any it had. Pools already added keep their storage."""
        self.storage(provider.storage)  # raises if no such storage
        self._write(
            "INSERT OR REPLACE INTO providers(name, kind, registry, storage) VALUES (?,?,?,?)",
            tuple(provider.model_dump().values()),
        )

    def provider(self, name: str) -> Provider:
        row = self._db.execute("SELECT * FROM providers WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no provider named {name!r}")
        return Provider.model_validate(dict(row))

    def providers(self) -> tuple[Provider, ...]:
        return tuple(
            Provider.model_validate(dict(row))
            for row in self._db.execute("SELECT * FROM providers")
        )

    def add_storage(self, storage: Storage) -> None:
        """Record a storage. Adding the same one again changes nothing; it never changes."""
        self._write(
            "INSERT OR IGNORE INTO storages(name, bucket, prefix, provider, region) VALUES (?,?,?,?,?)",
            tuple(storage.model_dump().values()),
        )
        if self.storage(storage.name) != storage:
            raise LauncherError(f"storage {storage.name!r} already exists with other settings")

    def storage(self, name: str) -> Storage:
        row = self._db.execute("SELECT * FROM storages WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no storage named {name!r}")
        return Storage.model_validate(dict(row))

    def storages(self) -> tuple[Storage, ...]:
        return tuple(
            Storage.model_validate(dict(row)) for row in self._db.execute("SELECT * FROM storages")
        )

    def _unnamed(self, name: str) -> None:
        """Pools and on-demands share one namespace, so a name refers to one supply."""
        for table in ("pools", "ondemands"):
            if self._db.execute(f"SELECT 1 FROM {table} WHERE name=?", (name,)).fetchone():
                raise LauncherError(f"{name!r} already names a {table[:-1]}")

    def add_pool(self, pool: Pool) -> None:
        """Record a pool. Adding the same one again changes nothing."""
        with self._together():
            if not self._db.execute("SELECT 1 FROM pools WHERE name=?", (pool.name,)).fetchone():
                self._unnamed(pool.name)
                self._write(
                    "INSERT INTO pools(name, off, startup_seconds, delivery_seconds, log_seconds, slots) VALUES (?,?,?,?,?,?)",
                    (
                        pool.name,
                        int(pool.off),
                        pool.startup_seconds,
                        pool.delivery_seconds,
                        pool.log_seconds,
                        pool.slots,
                    ),
                )
        if self.pool(pool.name) != pool:
            raise LauncherError(f"pool {pool.name!r} already exists with other settings")

    def pool(self, name: str) -> Pool:
        row = self._db.execute("SELECT * FROM pools WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no pool named {name!r}")
        return Pool.model_validate(dict(row))

    def pools(self) -> tuple[Pool, ...]:
        return tuple(
            Pool.model_validate(dict(row)) for row in self._db.execute("SELECT * FROM pools")
        )

    def add_ondemand(self, ondemand: OnDemand) -> None:
        """Record an on-demand. Adding the same one again changes nothing."""
        with self._together():
            if not self._db.execute(
                "SELECT 1 FROM ondemands WHERE name=?", (ondemand.name,)
            ).fetchone():
                self._unnamed(ondemand.name)
                self._write(
                    "INSERT INTO ondemands(name, body) VALUES (?,?)",
                    (ondemand.name, ondemand.model_dump_json()),
                )
        if self.ondemand(ondemand.name) != ondemand:
            raise LauncherError(f"on-demand {ondemand.name!r} already exists with other settings")

    def change_ondemand(self, ondemand: OnDemand) -> None:
        """Replace an on-demand's settings; machines it already rented keep their windows."""
        self.ondemand(ondemand.name)  # raises if no such on-demand
        self._write(
            "UPDATE ondemands SET body=? WHERE name=?", (ondemand.model_dump_json(), ondemand.name)
        )

    def ondemand(self, name: str) -> OnDemand:
        row = self._db.execute("SELECT body FROM ondemands WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no on-demand named {name!r}")
        return OnDemand.model_validate_json(row[0])

    def ondemands(self) -> tuple[OnDemand, ...]:
        return tuple(
            OnDemand.model_validate_json(row[0])
            for row in self._db.execute("SELECT body FROM ondemands ORDER BY rowid")
        )

    def supply(self, name: str) -> Supply:
        """The pool or on-demand with this name."""
        row = self._db.execute("SELECT body FROM ondemands WHERE name = ?", (name,)).fetchone()
        return self.pool(name) if row is None else OnDemand.model_validate_json(row[0])

    def name_controller_pool(self) -> None:
        """The controller's built-in machine must not be advertised as client-local.

        Only labels and their references change. Journal IDs and receipts stay put.
        """
        if not any(p.name == "local" for p in self.pools()):
            return
        with self._together():
            if self._db.execute(
                "SELECT 1 FROM pools WHERE name='controller' UNION SELECT 1 FROM ondemands"
                " WHERE name='controller'"
            ).fetchone():
                raise LauncherError("cannot rename local pool: 'controller' is already named")
            self._write("UPDATE pools SET name='controller' WHERE name='local'")
            for table in ("machines", "runs"):
                self._write(f"UPDATE {table} SET pool='controller' WHERE pool='local'")
            self._write(
                "UPDATE settings SET value='controller' WHERE name='default_pool' AND value='local'"
            )

    def set_pool_startup(self, name: str, seconds: float | None) -> None:
        pool = self.pool(name)
        checked = Pool.model_validate({**pool.model_dump(), "startup_seconds": seconds})
        self._write(
            "UPDATE pools SET startup_seconds=? WHERE name=?", (checked.startup_seconds, name)
        )

    def default_target(self) -> Target:
        # An empty value names nothing.
        rows = dict(self._db.execute("SELECT name, value FROM settings").fetchall())
        return Target(pool=rows["default_pool"] or None, rent=rows["default_rent"] or None)

    def set_default_target(self, target: Target) -> None:
        with self._together():
            if target.pool is not None:
                self.pool(target.pool)
            if target.rent is not None:
                self.ondemand(target.rent)
            for name, value in (("default_pool", target.pool), ("default_rent", target.rent)):
                self._write("UPDATE settings SET value=? WHERE name=?", (value or "", name))

    def turn_pool(self, name: str, *, off: bool) -> None:
        self.pool(name)  # raises if no such pool
        self._write("UPDATE pools SET off = ? WHERE name = ?", (int(off), name))

    def add_machine(self, machine: Machine) -> None:
        """Record a machine in its pool or on-demand. Adding the same one again changes nothing."""
        if machine.pool is not None:
            self.pool(machine.pool)  # raises if no such pool
        if machine.ondemand is not None:
            self.ondemand(machine.ondemand)
        self._write(
            "INSERT OR IGNORE INTO machines(name, kind, pool, ondemand, cost_usd, expires_at,"
            " provider, address, storage, image) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                machine.name,
                machine.kind,
                machine.pool,
                machine.ondemand,
                str(machine.cost_usd),
                _iso(machine.expires_at),
                machine.provider,
                machine.address,
                machine.storage,
                machine.image.model_dump_json() if machine.image else None,
            ),
        )
        if self.machine(machine.name) != machine:
            raise LauncherError(f"machine {machine.name!r} already exists with other settings")

    def machine(self, name: str) -> Machine:
        row = self._db.execute(f"{_MACHINES} WHERE m.name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no machine named {name!r}")
        return _machine_from_row(row)

    def machines(self, supply: str) -> tuple[Machine, ...]:
        """A pool's machines, or every machine ever rented from an on-demand."""
        rows = self._db.execute(
            f"{_MACHINES} WHERE m.pool = ? OR m.ondemand = ? ORDER BY m.rowid", (supply, supply)
        )
        return tuple(_machine_from_row(row) for row in rows)

    # Jobs, runs and cohorts

    def launch(
        self,
        runs: Sequence[RunRecord],
        cohort: Cohort | None,
        attempts: Sequence[RunAttempt] = (),
        *,
        launch: str | None = None,
    ) -> None:
        with self._together():
            for supply in {self.session(a.session).supply for a in attempts}:
                self.check_lock(supply)
            if cohort is not None:
                try:
                    self._write(
                        "INSERT INTO cohorts(name, parallel, max_usd, created_at) VALUES (?, ?, ?, ?)",
                        (
                            cohort.name,
                            cohort.parallel,
                            None if cohort.max_usd is None else str(cohort.max_usd),
                            cohort.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise LauncherError(f"cohort {cohort.name!r} already exists") from error
            evidence = self.launch_evidence(launch) if launch is not None else None
            resuming = (
                {r.run for r in evidence.requests if r.resume_version is not None}
                if evidence is not None
                else set()
            )
            for run in runs:
                if run.launch.created_at is None:
                    raise LauncherError(f"run {run.id!r} has no submission timestamp")
                if run.id in resuming:
                    previous = self.attempts(run=run.id)
                    if len(previous) != run.launch.resume_after or any(
                        a.outcome is None for a in previous
                    ):
                        raise Stale(f"run {run.id!r} still has an allocated attempt")
                    self._change(
                        "runs",
                        run.id,
                        run.launch.version,
                        job=run.job,
                        spec=run.launch.spec.model_dump_json(),
                        source=run.launch.source.model_dump_json() if run.launch.source else None,
                        pool=run.launch.pool,
                        rent=run.launch.rent,
                        cancel_requested_at=None,
                        resume_after=run.launch.resume_after,
                        params=json.dumps(run.launch.params),
                        smoke=run.launch.smoke.model_dump_json(),
                    )
                    continue
                self._write(
                    """INSERT INTO runs(id, job, spec, seed, pool, rent, cohort,
                       created_at, autorun, queued, params, source, smoke)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run.id,
                        run.job,
                        run.launch.spec.model_dump_json(),
                        run.seed,
                        run.launch.pool,
                        run.launch.rent,
                        run.cohort,
                        run.launch.created_at.isoformat(),
                        int(run.launch.autorun),
                        int(run.launch.queued),
                        json.dumps(run.launch.params),
                        run.launch.source.model_dump_json() if run.launch.source else None,
                        run.launch.smoke.model_dump_json(),
                    ),
                )

            self._place(attempts)
            if evidence is not None:
                self.save_launch(evidence.model_copy(update={"state": "admitted"}))

    def cancel(self, run: RunRecord, at: datetime) -> None:
        self._change("runs", run.id, run.launch.version, cancel_requested_at=at.isoformat())

    def run(self, id: str) -> RunRecord:
        row = self._db.execute(f"{_RUNS} WHERE r.id = ?", (id,)).fetchone()
        if row is None:
            raise LauncherError(
                f"the launcher has no run {id!r}; a run started with etude train is read with "
                "etude report FILE"
            )
        return _run_from_row(row)

    def runs(self, *, experiment: str | None = None) -> tuple[RunRecord, ...]:
        if experiment is None:
            rows = self._db.execute(f"{_RUNS} ORDER BY r.seq")
        else:
            rows = self._db.execute(
                f"{_RUNS} WHERE json_extract(r.spec, '$.experiment') = ? ORDER BY r.seq",
                (experiment,),
            )
        return tuple(_run_from_row(row) for row in rows)

    def job_runs(self, job: str) -> tuple[RunRecord, ...]:
        """Every run of one job, oldest first: its seeds side by side."""
        rows = self._db.execute(f"{_RUNS} WHERE r.job = ? ORDER BY r.seq", (job,))
        return tuple(_run_from_row(row) for row in rows)

    def cohort_exists(self, name: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM cohorts WHERE name=?", (name,)).fetchone() is not None
        )

    def cohort(self, name: str) -> Cohort:
        row = self._db.execute("SELECT * FROM cohorts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise LauncherError(f"no cohort {name!r}")
        return Cohort(
            name=row["name"],
            parallel=row["parallel"],
            max_usd=None if row["max_usd"] is None else Decimal(row["max_usd"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def waiting(self, supply: str) -> tuple[RunRecord, ...]:
        """Runs that may use this pool or on-demand and want a new attempt, oldest first.

        A run waits when nobody cancelled it and every attempt so far had a
        confirmed infrastructure loss, with tries left. The launcher checks budgets.
        """
        marks = ", ".join("?" for _ in RETRYABLE)
        rows = self._db.execute(
            f"""SELECT r.*, (SELECT count(*) FROM attempts a WHERE a.run = r.id) AS tries
                FROM runs r
                WHERE r.id IN (
                  SELECT id FROM runs WHERE pool = ? UNION SELECT id FROM runs WHERE rent = ?
                ) AND r.cancel_requested_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.run = r.id AND a.number > r.resume_after AND (
                  a.outcome IS NULL OR (a.outcome != 'stopped' AND
                  (a.outcome != 'lost' OR a.cause NOT IN ({marks})))))
                ORDER BY r.seq""",
            (supply, supply, *sorted(RETRYABLE)),
        ).fetchall()
        return tuple(
            run
            for row in rows
            if (run := _run_from_row(row)).launch.spec.max_attempts > row["tries"]
        )

    # Sessions

    def plan(
        self,
        session: Session,
        attempts: Sequence[RunAttempt] = (),
        rented: Machine | None = None,
        rental: Rental | None = None,
        *,
        launch: str | None = None,
    ) -> None:
        """Record a session, its attempts and any machine to rent, before any provider is asked."""
        with self._together():
            self.check_lock(session.supply)
            if self.machine_busy(session.machine):
                raise Stale(f"machine {session.machine!r} still has a run on it")
            live = [w for w in self.unsettled_sessions() if w.supply == session.supply]
            if rented is None:
                if self.pool(session.supply).off:
                    raise Stale(f"pool {session.supply!r} is turned off")
                if any(w.machine == session.machine for w in live):
                    raise Stale(f"machine {session.machine!r} already has a session")
            else:
                limit = self.ondemand(session.supply).max_machines
                manual = rental is not None and rental.kept and limit == 0
                if not manual and len(live) >= limit:
                    raise Stale(f"on-demand {session.supply!r} is at its machine limit")
                self.add_machine(rented)
            self._write(
                """INSERT INTO sessions(id,machine,planned_at,autorun) VALUES (?,?,?,?)""",
                (
                    session.id,
                    session.machine,
                    session.planned_at.isoformat(),
                    int(session.autorun),
                ),
            )
            if rental is not None:
                r = rental
                self._write(
                    "INSERT INTO rentals(machine, usd_per_hour, cap_usd, deadline, kept, billed_usd, billed_through) VALUES (?,?,?,?,?,?,?)",
                    (
                        r.machine,
                        str(r.usd_per_hour),
                        str(r.cap_usd),
                        r.deadline.isoformat(),
                        int(r.kept),
                        str(r.billed_usd),
                        _iso(r.billed_through),
                    ),
                )
            if launch is not None:
                self.link_launch(launch, session.id, acquired=True)
            self._place(attempts)

    def place(self, session: Session, attempts: Sequence[RunAttempt]) -> None:
        """Put more attempts on a session that already exists, within its supply's slots."""
        with self._together():
            self.check_lock(session.supply)
            self._change("sessions", session.id, session.version)
            self._place(attempts)

    def _place(self, attempts: Sequence[RunAttempt]) -> None:
        """Insert attempts; each run has one in hand, and each machine its supply's slots."""
        for a in attempts:
            # One read: the run's open attempts, the machine's, and its supply's slots.
            # An on-demand stored before slots existed takes one run at a time.
            running, held, slots = self._db.execute(
                """SELECT
                     (SELECT count(*) FROM attempts WHERE run = ? AND outcome IS NULL),
                     (SELECT count(*) FROM attempts WHERE machine = ? AND outcome IS NULL),
                     (SELECT coalesce(p.slots, json_extract(o.body, '$.slots'), 1)
                      FROM machines m LEFT JOIN pools p ON p.name = m.pool
                      LEFT JOIN ondemands o ON o.name = m.ondemand WHERE m.name = ?)""",
                (a.run, a.machine, a.machine),
            ).fetchone()
            if running:
                raise Stale(f"run {a.run} already has an attempt in hand")
            if held >= slots:
                raise Stale(f"machine {a.machine!r} has no free slot")
            self._write(
                """INSERT INTO attempts(id, run, session, machine, number, position, limit_seconds,
                   limit_kind, planned_at, added) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    a.id,
                    a.run,
                    a.session,
                    a.machine,
                    a.number,
                    a.position,
                    a.limit_seconds,
                    a.limit_kind,
                    _iso(a.planned_at),
                    int(a.added),
                ),
            )

    def claim(self, session: Session, at: datetime) -> None:
        """Take the one right to create this session. Whoever gets Stale must not create."""
        with self._together():
            self.check_lock(session.supply)
            seconds = self.supply(session.supply).startup_seconds
            deadline = None if seconds is None else at + timedelta(seconds=seconds)
            self._change(
                "sessions",
                session.id,
                session.version,
                claimed_at=at.isoformat(),
                startup_deadline=_iso(deadline),
            )

    def record_provider_id(self, session: Session, provider_id: str) -> None:
        """Confirm the session's machine was created, under the provider's name for it."""
        self._change("sessions", session.id, session.version, provider_id=provider_id)

    def record_hardware(self, session: Session, hardware: Hardware) -> None:
        self._change(
            "sessions",
            session.id,
            session.version,
            hardware=hardware.model_dump_json(),
        )

    def record_started(self, session: Session, at: datetime) -> None:
        self._change("sessions", session.id, session.version, started_at=at.isoformat())

    def record_price(self, session: Session, price: Decimal) -> None:
        with self._together():
            self._change("sessions", session.id, session.version)
            self._write(
                "UPDATE rentals SET usd_per_hour=? WHERE machine=?", (str(price), session.machine)
            )

    def record_bill(self, session: Session, usd: Decimal, through: datetime) -> None:
        with self._together():
            self._change("sessions", session.id, session.version)
            self._write(
                "UPDATE rentals SET billed_usd=?,billed_through=? WHERE machine=?",
                (str(usd), through.isoformat(), session.machine),
            )

    def rental_for(self, session: Session) -> Rental | None:
        """Paid window, if this working session runs on a rented machine."""
        row = self._db.execute(f"{_RENTALS} WHERE r.machine=?", (session.machine,)).fetchone()
        return None if row is None else Rental.model_validate(dict(row))

    def rental(self, machine: str) -> Rental:
        row = self._db.execute(f"{_RENTALS} WHERE r.machine=?", (machine,)).fetchone()
        if row is None:
            raise LauncherError(f"machine {machine!r} has no rental")
        return Rental.model_validate(dict(row))

    def rented_session(self, machine: str) -> Session:
        """The one working session of a rented machine."""
        row = self._db.execute(
            f"{_SESSIONS} JOIN rentals r ON r.machine=w.machine WHERE w.machine=?", (machine,)
        ).fetchone()
        if row is None:
            raise LauncherError(f"machine {machine!r} has no rental")
        return _session_from_row(row)

    def rentals(self) -> tuple[Rental, ...]:
        return tuple(Rental.model_validate(dict(row)) for row in self._db.execute(_RENTALS))

    def machine_busy(self, machine: str) -> bool:
        """Allocation authority: any unfinished attempt holds its machine."""
        return self.open_attempts(machine) > 0

    def open_attempts(self, machine: str) -> int:
        """Unfinished attempts on a machine, whichever session placed them: its taken slots."""
        (count,) = self._db.execute(
            "SELECT count(*) FROM attempts WHERE machine=? AND outcome IS NULL", (machine,)
        ).fetchone()
        return int(count)

    def sessions(self) -> tuple[Session, ...]:
        return tuple(
            _session_from_row(row) for row in self._db.execute(f"{_SESSIONS} ORDER BY w.seq")
        )

    def record_refusal(self, session: Session, at: datetime) -> None:
        self._change("sessions", session.id, session.version, refused_at=at.isoformat())

    def record_finish(self, session: Session, at: datetime) -> None:
        self._change("sessions", session.id, session.version, finish_requested_at=at.isoformat())

    def confirm_ended(self, session: Session, at: datetime) -> None:
        with self._together():
            self._change("sessions", session.id, session.version, ended_at=at.isoformat())
            # A rented machine is gone with its session; one you added remains.
            self._write(
                "UPDATE machines SET deleted_at = ? WHERE name = ? AND kind = 'rented'",
                (at.isoformat(), session.machine),
            )

    def session(self, id: str) -> Session:
        row = self._db.execute(f"{_SESSIONS} WHERE w.id = ?", (id,)).fetchone()
        if row is None:
            raise LauncherError(f"no session {id!r}")
        return _session_from_row(row)

    def unsettled_sessions(self) -> tuple[Session, ...]:
        """Sessions not refused and either still running or with attempts not yet settled.

        A session confirmed ended whose final read failed still has open
        attempts; it stays here until they are settled.
        """
        rows = self._db.execute(
            f"""{_SESSIONS} WHERE w.refused_at IS NULL AND (w.ended_at IS NULL OR EXISTS
               (SELECT 1 FROM attempts a WHERE a.session = w.id AND a.outcome IS NULL))
               ORDER BY w.seq"""
        )
        return tuple(_session_from_row(row) for row in rows)

    def cohort_sessions(self, name: str) -> tuple[Session, ...]:
        rows = self._db.execute(
            f"""{_SESSIONS} WHERE w.id IN (SELECT a.session FROM attempts a
               JOIN runs r ON r.id = a.run WHERE r.cohort = ?) ORDER BY w.seq""",
            (name,),
        )
        return tuple(_session_from_row(row) for row in rows)

    def cohort_runs(self, name: str) -> tuple[RunRecord, ...]:
        self.cohort(name)  # raises if no such cohort
        rows = self._db.execute(f"{_RUNS} WHERE r.cohort = ? ORDER BY r.seq", (name,))
        return tuple(_run_from_row(row) for row in rows)

    def cohort_attempts(self, name: str) -> tuple[RunAttempt, ...]:
        rows = self._db.execute(
            """SELECT a.* FROM attempts a JOIN runs r ON r.id = a.run
               WHERE r.cohort = ? ORDER BY a.seq""",
            (name,),
        )
        return tuple(_attempt_from_row(row) for row in rows)

    # Attempts

    def start(self, attempt: RunAttempt, at: datetime) -> None:
        self._change("attempts", attempt.id, attempt.version, started_at=at.isoformat())

    def record_progress(
        self,
        attempt: RunAttempt,
        clocks: dict[str, float],
        tally: Tally | None,
        writers: tuple[RunWriter, ...] = (),
    ) -> None:
        """Retain where the job last said its clocks and counted work stood."""
        self._change(
            "attempts",
            attempt.id,
            attempt.version,
            clocks=json.dumps(clocks),
            tally=None if tally is None else tally.model_dump_json(),
            writers=_writers_json(attempt, writers),
        )

    def finish(
        self,
        attempt: RunAttempt,
        at: datetime,
        outcome: Outcome,
        *,
        cause: Cause | None = None,
        clocks: dict[str, float] | None = None,
        outputs: Sequence[Output] = (),
        exit_code: int | None = None,
        prepared: Prepared | None = None,
        tally: Tally | None = None,
        writers: tuple[RunWriter, ...] = (),
    ) -> None:
        self._change(
            "attempts",
            attempt.id,
            attempt.version,
            finished_at=at.isoformat(),
            outcome=outcome,
            cause=cause,
            clocks=json.dumps(clocks or {}),
            outputs=json.dumps([o.model_dump(mode="json") for o in outputs]),
            exit_code=exit_code,
            prepared=None if prepared is None else prepared.model_dump_json(),
            tally=None if tally is None else tally.model_dump_json(),
            writers=_writers_json(attempt, writers),
        )

    def fail_before_start(self, attempt: RunAttempt, cause: Cause, at: datetime) -> None:
        if attempt.started_at is not None:
            raise LauncherError(f"attempt {attempt.id} already started")
        self.finish(attempt, at, "lost", cause=cause)

    def attempts(
        self, *, run: str | None = None, session: str | None = None
    ) -> tuple[RunAttempt, ...]:
        """A run's attempts in the order tried, or a session's in the order it runs them."""
        if (run is None) == (session is None):
            raise LauncherError("name a run or a session")
        column, value, order = ("run", run, "number") if run else ("session", session, "position")
        rows = self._db.execute(
            f"SELECT * FROM attempts WHERE {column} = ? ORDER BY {order}", (value,)
        )
        return tuple(_attempt_from_row(row) for row in rows)


def _check_sqlite(migration: Migration) -> None:
    if re.search(
        r"\bDROP\s+COLUMN\b", migration.sql, re.IGNORECASE
    ) and sqlite3.sqlite_version_info < (3, 35):
        raise LauncherError(
            f"launcher: migration {migration.name} needs SQLite >= 3.35; "
            f"this Python has {sqlite3.sqlite_version}"
        )


def _snapshot(source: Path, target: Path, timeout: float) -> None:
    """Read a consistent WAL snapshot without modifying the source's records."""
    reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=timeout)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


def _predecessor(home: Path) -> Path | None:
    for expected, predecessor in _PREDECESSORS.items():
        for name in predecessor.files:
            path = home / name
            if not path.exists():
                continue
            reader = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                schema = reader.execute("PRAGMA user_version").fetchone()[0]
            finally:
                reader.close()
            if schema == expected:
                return path
            if name != "launcher.db":
                raise LauncherError(
                    f"launcher: {name} holds unexpected schema {schema}; open it with its matching etude"
                )
    return None


def _check_source_writes(source: Path) -> None:
    # Older wait commands schedule without a heartbeat, but every pass writes SQLite.
    modified = source.stat().st_mtime
    try:
        modified = max(modified, source.with_name(source.name + "-wal").stat().st_mtime)
    except FileNotFoundError:
        pass
    age = time.time() - modified
    if age < 30:
        raise LauncherError(
            f"launcher: an older etude wrote {source.name} {max(0, int(age))}s ago; "
            "stop its commands (etude wait, etude controller run), then run this again"
        )


def _older_files(home: Path, source: Path | None) -> str:
    messages: list[str] = []
    for path in sorted(home.glob("launcher*.db")):
        if path == source:
            messages.append(f"{path.name} is left as it was and is no longer read")
            continue
        reader = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            schema = reader.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError:
            messages.append(f"{path.name} is not read (cannot inspect its schema)")
            continue
        finally:
            reader.close()
        matching = _PREDECESSORS.get(schema)
        reader_etude = matching.files.get(path.name, matching.etude) if matching else None
        suffix = f", use etude {reader_etude} for it" if reader_etude else ""
        messages.append(f"{path.name} (schema {schema}) is not read{suffix}")
    return "; ".join(messages)


def _working_file(home: Path, unreleased: Sequence[Migration]) -> Path:
    real = launcher_file(home)
    return home / "copies" / _unreleased_digest(unreleased) / real.name if unreleased else real


def has_launcher(home: Path) -> bool:
    """Whether this build has launcher evidence to read, without creating a home."""
    unreleased = [m for m in migrations() if m.floor is None]
    return (
        _working_file(home, unreleased).exists()
        or launcher_file(home).exists()
        or _predecessor(home) is not None
    )


def open_home(home: Path, *, timeout: float = 30) -> Database:
    """Open the home's launcher, taking over a released predecessor or isolating a branch.

    Creation is serialized across processes. A staged snapshot is fully migrated
    before publication, so a failed takeover leaves no half-created launcher.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("database timeout must be nonnegative and finite")
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    real = launcher_file(home)
    unreleased = [m for m in migrations() if m.floor is None]
    target = _working_file(home, unreleased)
    if target.exists():
        return Database(target, timeout=timeout)
    with (home / ".launcher-open").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LauncherError(
                        "launcher: another process is opening this home; run this again"
                    ) from None
                time.sleep(min(0.05, left))
        if target.exists():
            return Database(target, timeout=timeout)
        source = real if real.exists() else _predecessor(home)
        if source is not None and not unreleased:
            _check_source_writes(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".launcher-", dir=target.parent) as temporary:
            staging = Path(temporary) / real.name
            if source is not None:
                _snapshot(source, staging, timeout)
            store = Database(staging, timeout=timeout, backup=False)
            fresh = store.fresh
            runs = store._db.execute("SELECT count(*) FROM runs").fetchone()[0]
            sessions = store._db.execute(
                "SELECT count(*) FROM sessions WHERE ended_at IS NULL"
            ).fetchone()[0]
            applied = tuple(
                r[0] for r in store._db.execute("SELECT name FROM migrations ORDER BY rowid")
            )
            store.close()  # checkpoint WAL before publishing the one file
            staging.chmod(0o600)
            staging.replace(target)
        if unreleased:
            names = ", ".join(m.name for m in unreleased)
            print(
                f"launcher: launcher.sqlite is behind this unreleased etude ({names}); using copy {target.parent}/. Runs and rentals made here are not in launcher.sqlite",
                file=sys.stderr,
            )
        elif source is not None:
            previous = next(p.migrations for p in _PREDECESSORS.values() if source.name in p.files)
            missing = ", ".join(n for n in applied if n not in previous)
            suffix = f"; applied {missing}" if missing else ""
            print(
                f"launcher: took over {source.name} as launcher.sqlite ({runs} runs, {sessions} open sessions){suffix}",
                file=sys.stderr,
            )
        if not unreleased and (other := _older_files(home, source)):
            print("launcher: " + other, file=sys.stderr)
        result = Database(target, timeout=timeout)
        result.fresh = fresh
        return result


class Copy(Record):
    """A copy an unreleased etude works on, as seen from outside it."""

    path: str
    unreleased: tuple[str, ...]  # the migrations that sent its etude to a copy
    rentals: int  # paid windows not yet ended
    usd_per_hour: Decimal
    ends: datetime  # the latest of their deadlines


def _unreleased_digest(unreleased: Sequence[Migration]) -> str:
    joined = "".join(f"{m.name}:{m.sha256}\n" for m in unreleased)
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def copies(home: Path, now: datetime) -> tuple[Copy, ...]:
    """Other copies under `home` holding paid windows that have not ended, read without changing
    them. The copy this build works on is left out: its runs are already in view."""
    unreleased = [m for m in migrations() if m.floor is None]
    own = _unreleased_digest(unreleased) if unreleased else None
    found: list[Copy] = []
    for path in sorted((home / "copies").glob("*/launcher.sqlite")):
        if path.parent.name == own:
            continue
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            names = tuple(
                r[0] for r in db.execute("SELECT name FROM migrations WHERE floor IS NULL")
            )
            live = [
                (Decimal(rate), datetime.fromisoformat(deadline))
                for rate, deadline in db.execute(
                    """SELECT r.usd_per_hour, r.deadline FROM rentals r
                    JOIN machines m ON m.name = r.machine
                    WHERE m.deleted_at IS NULL"""
                )
            ]
        finally:
            db.close()
        live = [(rate, deadline) for rate, deadline in live if deadline > now]
        if live:
            found.append(
                Copy(
                    path=str(path.parent),
                    unreleased=names,
                    rentals=len(live),
                    usd_per_hour=sum((rate for rate, _ in live), Decimal(0)),
                    ends=max(deadline for _, deadline in live),
                )
            )
    return tuple(found)
