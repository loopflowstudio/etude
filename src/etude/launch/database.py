"""The launcher's SQLite file. Records change only through the operations named here.

Every session, attempt and run row carries a version. A write names the version it
read and raises Stale if the row has moved, so two processes working the same
file cannot both take one step. Status and spend are computed, never stored.

inspect reads local schema evidence; upgrade is the only schema writer. Ordinary
opens validate the canonical launcher.sqlite without creating or migrating it.
The migration ledger fences incompatible writers before their next action.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache, partial
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from etude.deadline import limit_time, remaining
from etude.errors import LauncherError
from etude.launch.errors import SchemaAdvanced
from etude.launch.provenance import frontier
from etude.launch.records import (
    RETRYABLE,
    Cause,
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
# A NULL floor identifies a build rather than a released compatibility floor.
_FOLDER = Path(__file__).with_name("migrations")
_LEDGER = """CREATE TABLE migrations(
  name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, applied_at TEXT NOT NULL,
  floor TEXT, program TEXT)"""


# Actual supported pre-ledger histories on this machine, plus the shipped 0.4.0
# schema. Older named recovery snapshots never become operational queues.
_PREDECESSORS: dict[int, tuple[str, ...]] = {
    19: ("baseline", "run_resume_and_writers"),
    17: ("baseline",),
    13: ("baseline",),
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
    expansion: str | None = None
    exports: tuple[str, ...] = ()


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
            expansion=_header(sql, "expands"),
            exports=tuple(
                n.strip() for n in (_header(sql, "export") or "").split(",") if n.strip()
            ),
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


def _code(statement: str) -> str:
    """A statement on one line, without its comments."""
    return " ".join(" ".join(line.partition("--")[0] for line in statement.splitlines()).split())


def _statements(migration: Migration) -> Iterator[str]:
    # executescript would commit the open transaction; run one complete statement at a time.
    pending = ""
    for character in migration.sql:
        pending += character
        if character == ";" and sqlite3.complete_statement(pending):
            if re.match(
                r"(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH|VACUUM)\b",
                _code(pending),
                re.IGNORECASE,
            ):
                raise LauncherError(f"migration {migration.name} must stay inside its transaction")
            yield pending
            pending = ""
    if _code(pending):
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
    if unreleased := [f"{r['name']}@{r['sha256'][:12]}" for r in rows if r["floor"] is None]:
        return f"{file} requires a build containing migrations: {', '.join(unreleased)}"
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


_ADDITIVE = re.compile(r"^(CREATE TABLE|CREATE INDEX|ALTER TABLE \w+ ADD COLUMN)\b", re.IGNORECASE)


def additive(migration: Migration, before: Sequence[Migration]) -> bool:
    """Whether a file holding `before` stays readable by their etude once this is applied.

    Proven, never declared: every statement only creates a table or index or adds a
    column, nothing that existed changes, and every new column can be left out.
    """
    if migration.breaks is not None:
        return False
    for statement in _statements(migration):
        code = _code(statement)
        if not _ADDITIVE.match(code):
            return False
        if code.upper().startswith("ALTER TABLE") and re.search(
            r"\b(CHECK|REFERENCES|UNIQUE|PRIMARY|GENERATED)\b", code, re.IGNORECASE
        ):
            return False
    with closing(sqlite3.connect(":memory:")) as db:
        for predecessor in before:
            for statement in _statements(predecessor):
                db.execute(statement)
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
    """The single operational launcher file shared by compatible clients."""
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
    def __init__(self, path: Path, *, timeout: float = 30) -> None:
        """Validate existing local state; never create or migrate it.

        The finite, overridable lock-contention allowance is in seconds. Schema
        upgrades have their own caller-owned allowance through upgrade().
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("database timeout must be nonnegative and finite")
        _refuse_takeover(path.parent)
        if legacy := _sources(path.parent):
            raise LauncherError(
                f"{path.parent} holds legacy launcher histories: "
                f"{', '.join(map(str, legacy))}; "
                "stop old local scheduling clients, then run etude database upgrade"
            )
        if not path.is_file():
            raise LauncherError(f"{path} is not initialized; run etude database upgrade")
        self._locks: dict[str, tuple[str, Callable[[], datetime]]] = {}
        self._path = path
        self._db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=timeout,
        )
        self._db.row_factory = sqlite3.Row
        try:
            held = self._applied()
            pending = [m.name for m in migrations() if held is None or m.name not in held[0]]
            if pending:
                raise LauncherError(
                    f"{path} needs etude database upgrade (missing: {', '.join(pending)})"
                )
            assert held is not None
            self._generation = held[1]
        except BaseException:
            self._db.close()
            raise

    def _applied(self) -> tuple[tuple[str, ...], int] | None:
        """The ledger's migrations this etude knows and its row count; None without a ledger.

        The rest are a newer etude's additive changes, which this one reads and writes
        past. Raises LauncherError when they raise the floor past this etude, or when
        a known migration's file changed after it was applied.
        """
        if not self._db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone():
            return None
        rows = self._db.execute(
            "SELECT name, sha256, floor FROM migrations ORDER BY rowid"
        ).fetchall()
        known = {m.name: m.sha256 for m in migrations()}
        file = self._path.name
        if reason := _too_new([r for r in rows if r["name"] not in known], file):
            raise LauncherError(reason)
        for row in rows:
            if known.get(row["name"], row["sha256"]) != row["sha256"]:
                raise LauncherError(
                    f"migration {row['name']} changed after it was applied to {self._path}; "
                    f"restore the newest {file}.backup-* or move the file away"
                )
        return tuple(r["name"] for r in rows if r["name"] in known), len(rows)

    def check_schema(self) -> None:
        """Raise SchemaAdvanced if another etude raised the file's floor past this one since it
        opened it. Additive migrations by a newer etude are taken in stride."""
        generation = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if generation == self._generation:
            return
        rows = self._db.execute(
            "SELECT name, sha256, floor, program FROM migrations WHERE rowid > ? ORDER BY rowid",
            (self._generation,),
        ).fetchall()
        known = {m.name: m.sha256 for m in migrations()}
        if any(r["name"] in known and r["sha256"] != known[r["name"]] for r in rows) or _too_new(
            [r for r in rows if r["name"] not in known], self._path.name
        ):
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
    def _together(self) -> Iterator[None]:
        """Several statements that land together or not at all, on the schema this process knows.

        Inside an open transaction it adds nothing: the outer one already checked.
        """
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self.check_schema()
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

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

    def _change(self, table: str, id: str, version: int, **facts: Fact) -> None:
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

    def name_server_pool(self) -> None:
        """The server's built-in machine must not be advertised as client-local.

        Only labels and their references change. Journal IDs and receipts stay put.
        """
        if not any(p.name == "local" for p in self.pools()):
            return
        with self._together():
            if self._db.execute(
                "SELECT 1 FROM pools WHERE name='server' UNION SELECT 1 FROM ondemands"
                " WHERE name='server'"
            ).fetchone():
                raise LauncherError("cannot rename local pool: 'server' is already named")
            self._write("UPDATE pools SET name='server' WHERE name='local'")
            for table in ("machines", "runs"):
                self._write(f"UPDATE {table} SET pool='server' WHERE pool='local'")
            self._write(
                "UPDATE settings SET value='server' WHERE name='default_pool' AND value='local'"
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

    # Jobs and runs

    def launch(
        self,
        runs: Sequence[RunRecord],
        attempts: Sequence[RunAttempt] = (),
        *,
        launch: str | None = None,
    ) -> None:
        with self._together():
            for supply in {self.session(a.session).supply for a in attempts}:
                self.check_lock(supply)
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
                    """INSERT INTO runs(id, job, spec, seed, pool, rent,
                       created_at, autorun, queued, params, source, smoke)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run.id,
                        run.job,
                        run.launch.spec.model_dump_json(),
                        run.seed,
                        run.launch.pool,
                        run.launch.rent,
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

    def runs(
        self, *, experiment: str | None = None, job: str | None = None
    ) -> tuple[RunRecord, ...]:
        clauses: list[str] = []
        values: list[str] = []
        if experiment is not None:
            clauses.append("json_extract(r.spec, '$.experiment') = ?")
            values.append(experiment)
        if job is not None:
            clauses.append("(r.job = ? OR json_extract(r.spec, '$.name') = ?)")
            values.extend((job, job))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._db.execute(f"{_RUNS}{where} ORDER BY r.seq", values)
        return tuple(_run_from_row(row) for row in rows)

    def job_runs(self, job: str) -> tuple[RunRecord, ...]:
        """Every run of one job, oldest first: its seeds side by side."""
        rows = self._db.execute(f"{_RUNS} WHERE r.job = ? ORDER BY r.seq", (job,))
        return tuple(_run_from_row(row) for row in rows)

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


def _read_only(path: Path, timeout: float = 5) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)


def _copy_file(source: Path, target: Path) -> None:
    """Copy closed or signature-checked bytes within the caller's remaining allowance."""
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(1024 * 1024):
            remaining()
            writer.write(chunk)


@contextmanager
def _inspection(path: Path) -> Iterator[sqlite3.Connection]:
    """Read stable evidence without letting SQLite create or update source sidecars."""
    path = path.resolve()
    revision = _signature(path)
    wal = path.with_name(path.name + "-wal")
    # Immutable reads ignore WAL. Copy a WAL-backed database privately instead;
    # SQLite rebuilds its shared-memory index there, including committed WAL rows.
    # Check identities after reading too: copies must not mix checkpoint generations.
    try:
        with ExitStack() as stack:
            if wal.exists() and wal.stat().st_size:
                temporary = stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="etude-inspect-")
                )
                target = Path(temporary) / path.name
                for source in (path, wal):
                    _copy_file(source, target.parent / source.name)
                connection = _read_only(target, timeout=0)
            else:
                connection = sqlite3.connect(
                    path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0
                )
            with closing(connection) as db:
                yield db
    finally:
        if not path.exists() or _signature(path) != revision:
            raise LauncherError(
                "launcher database changed during inspection; rerun database status"
            )


def _snapshot(source: Path, target: Path, timeout: float) -> None:
    """Read a consistent WAL snapshot without modifying the source's records."""
    with closing(_read_only(source, timeout)) as reader:
        target.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(target, timeout=0)) as writer:

            def progress(status: int, left: int, total: int) -> None:
                remaining()

            reader.backup(writer, pages=256, progress=progress, sleep=0.01)
            writer.execute("PRAGMA journal_mode=DELETE")


def _invalid_records(db: sqlite3.Connection) -> list[str]:
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
        invalid = 0
        for row in db.execute(query):
            remaining()
            try:
                read(row)
            except ValueError:
                invalid += 1
        if invalid:
            problems.append(f"  {table}: {invalid} invalid records")
    return problems


class Schema(Record):
    """Local schema evidence; inspection never changes records or selects a queue."""

    path: Path
    sources: tuple[Path, ...]
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    problem: str | None
    takeover: Path | None = None


class Upgrade(Record):
    """The facts published by an explicit upgrade, including recovery locations."""

    path: Path
    sources: tuple[Path, ...]
    backups: tuple[Path, ...]
    applied: tuple[str, ...]
    floor: str | None
    exports: tuple[Path, ...] = ()
    required: tuple[str, ...] = ()


def _merged() -> dict[str, str]:
    raw: object = json.loads(frontier(Path(__file__).resolve().parents[1], timeout=remaining()))
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise LauncherError("invalid packaged migration provenance")
    result = {str(k): str(v) for k, v in raw.items()}
    result.update({r.name: r.sha256 for r in released()})
    return result


def _sources(home: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {
                *home.glob("launcher*.db"),
                *(home / "copies").glob("*/launcher*.db"),
                *(home / "copies").glob("*/launcher.sqlite"),
            }
        )
    )


def inspect(home: Path) -> Schema:
    """Read local catalog membership, without database creation, provider keys or network."""
    home = home.resolve()
    path = launcher_file(home)
    sources = _sources(home)
    if (record := _takeover_path(home)).exists():
        return Schema(
            path=path,
            sources=sources,
            applied=(),
            pending=(),
            takeover=record,
            problem=_takeover_problem(record),
        )
    applied: tuple[str, ...] = ()
    problem: str | None = None
    if path.exists():
        with _inspection(path) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone():
                rows = db.execute(
                    "SELECT name,sha256,floor FROM migrations ORDER BY rowid"
                ).fetchall()
                applied = tuple(r["name"] for r in rows)
                known = {m.name: m for m in migrations()}
                problem = _too_new([r for r in rows if r["name"] not in known], path.name)
                if changed := [
                    r["name"]
                    for r in rows
                    if r["name"] in known and r["sha256"] != known[r["name"]].sha256
                ]:
                    problem = "applied migration content differs: " + ", ".join(changed)
            else:
                problem = "canonical file has no migration ledger"
    pending = tuple(m.name for m in migrations() if m.name not in applied)
    return Schema(path=path, sources=sources, applied=applied, pending=pending, problem=problem)


def has_launcher(home: Path) -> bool:
    """Whether local launcher evidence exists, including a legacy home needing upgrade."""
    return _takeover_path(home).exists() or launcher_file(home).exists() or bool(_sources(home))


def open_home(home: Path, *, timeout: float = 30) -> Database:
    """Open only the canonical file; missing state requires explicit upgrade."""
    return Database(launcher_file(home.resolve()), timeout=timeout)


@contextmanager
def _upgrade_lock(home: Path) -> Iterator[None]:
    with (home / ".launcher-upgrade").open("a") as lock:
        while True:
            remaining()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, remaining(0.05) or 0.05))
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _floor(migration: Migration) -> str | None:
    if migration.floor is not None:
        return migration.floor
    catalog = migrations()
    before = catalog[: catalog.index(migration)]
    if not additive(migration, before):
        return None
    floors = [m.floor for m in before if m.floor is not None]
    return max(floors, key=_version) if floors else None


def _required() -> tuple[str, ...]:
    return tuple(
        f"{m.name}@{m.sha256[:12]}" for m in migrations() if m.floor is None and _floor(m) is None
    )


class Snapshot(Record):
    """Recovery file identity, retained beside the consistent SQLite snapshot."""

    path: Path
    source: Path
    generation: int
    applied: tuple[str, ...]
    ledger_sha256: str | None


def _retain_snapshot(source: Path, target: Path) -> None:
    _snapshot(source, target, remaining() or 0)
    with closing(_read_only(target)) as db:
        generation = int(db.execute("PRAGMA user_version").fetchone()[0])
        rows = (
            db.execute("SELECT * FROM migrations ORDER BY rowid").fetchall()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone()
            else []
        )
        identity = Snapshot(
            path=target,
            source=source,
            generation=generation,
            applied=tuple(r[0] for r in rows),
            ledger_sha256=hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest()
            if rows
            else None,
        )
    manifest = target.with_name(target.name + ".json")
    manifest.write_text(identity.model_dump_json() + "\n")
    _sync(target)
    _sync(manifest)


class ExportRow(Record):
    """A superseded SQLite fact, retained before destructive SQL commits."""

    table: str
    values: dict[str, Fact]


def _sync(path: Path) -> None:
    path.chmod(0o600)
    with path.open("rb") as file:
        os.fsync(file.fileno())
    _sync_directory(path.parent)


def _export(db: sqlite3.Connection, migration: Migration, path: Path) -> Path:
    catalog = migrations()
    before = catalog[: catalog.index(migration)]
    if migration.expansion not in {m.name for m in before} or not migration.exports:
        raise LauncherError(
            f"destructive migration {migration.name} must name its expansion and export tables"
        )
    target = path.with_name(
        f"{path.name}.export-{migration.name}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.jsonl"
    )
    with target.open("xb") as file:
        for table in migration.exports:
            for row in db.execute(f"SELECT * FROM {_quote(table)}"):
                remaining()
                fact = ExportRow(table=table, values=dict(row))
                file.write((fact.model_dump_json() + "\n").encode())
    _sync(target)
    return target


def _contracts(migration: Migration) -> bool:
    if migration.name == "baseline":
        return False  # The shipped baseline seeds only empty settings.
    return any(
        re.match(
            r"(?:DROP\s+(?:TABLE|VIEW)|ALTER\s+TABLE\s+.+\s+(?:DROP\s+COLUMN|RENAME)|UPDATE|DELETE|REPLACE|INSERT)\b",
            _code(statement),
            re.IGNORECASE,
        )
        for statement in _statements(migration)
    )


def _apply(
    db: sqlite3.Connection, applied: tuple[str, ...], path: Path, exports: list[Path]
) -> tuple[str, ...]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone():
        db.execute(_LEDGER)
    existing = {r[0] for r in db.execute("SELECT name FROM migrations")}
    changed: list[str] = []
    for migration in migrations():
        remaining()
        if migration.name in existing:
            continue
        if migration.name not in applied:
            if _contracts(migration):
                exports.append(_export(db, migration, path))
            _check_sqlite(migration)
            try:
                for statement in _statements(migration):
                    remaining()
                    db.execute(statement)
            except sqlite3.Error as error:
                remaining()
                raise LauncherError(
                    f"launcher: migration {migration.name} failed ({type(error).__name__})"
                ) from None
            changed.append(migration.name)
        db.execute(
            "INSERT INTO migrations(name,sha256,applied_at,floor,program) VALUES (?,?,?,?,?)",
            (
                migration.name,
                migration.sha256,
                datetime.now(UTC).isoformat(),
                _floor(migration),
                _program(path),
            ),
        )
    if problems := _invalid_records(db):
        raise LauncherError("launcher rows do not validate:\n" + "\n".join(problems))
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise LauncherError("launcher has broken foreign key references")
    count = db.execute("SELECT count(*) FROM migrations").fetchone()[0]
    db.execute(f"PRAGMA user_version={count}")
    return tuple(changed)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _normalize_history(db: sqlite3.Connection, path: Path) -> tuple[str, ...] | None:
    """Expand this machine's old inline schemas; retain superseded columns/tables."""
    schema = int(db.execute("PRAGMA user_version").fetchone()[0])
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='migrations'").fetchone():
        # Historical automatic copies had a different baseline. Only the actual
        # empty schema-6 copy is supported; it owns no allocations or paid work.
        if schema != 6 or any(
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("runs", "sessions", "rentals", "attempts", "providers", "storages")
        ):
            raise LauncherError(
                f"history {path} has an unsupported migration ledger; retain it for reconciliation"
            )
        return None
    if schema not in _PREDECESSORS:
        raise LauncherError(f"history {path} has unsupported schema {schema}")
    if schema == 13:
        db.execute("ALTER TABLE runs ADD COLUMN smoke TEXT NOT NULL DEFAULT '{}'")
        db.execute("ALTER TABLE sessions ADD COLUMN provider_id TEXT")
        db.execute("UPDATE sessions SET provider_id=receipt")
    return _PREDECESSORS[schema]


def _merge_history(target: sqlite3.Connection, path: Path, converted: Path) -> None:
    """Merge each table in bulk through its identity index, preserving distinct facts."""
    alias = f"history{len(target.execute('PRAGMA database_list').fetchall())}"
    target.execute(f"ATTACH DATABASE ? AS {alias}", (converted.resolve().as_uri() + "?mode=ro",))
    tables = [
        r[0]
        for r in target.execute(
            f"SELECT name FROM {alias}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('migrations','locks')"
        )
    ]
    for table in tables:
        remaining()
        shape = {
            row["name"]: row
            for row in target.execute(f"PRAGMA {alias}.table_info({_quote(table)})")
            if row["name"] != "seq"
        }
        columns = list(shape)
        target_columns = {r[1] for r in target.execute(f"PRAGMA main.table_info({_quote(table)})")}
        if not target_columns:
            sql = target.execute(
                f"SELECT sql FROM {alias}.sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            target.execute(sql)
            target_columns = set(columns)
        for column in set(columns) - target_columns:
            kind = shape[column]["type"]
            if not re.fullmatch(r"[A-Z ]*", kind.upper()):
                raise LauncherError(f"unsupported historical column {table}.{column}")
            target.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(column)} {kind}")
        keys = [name for name, row in shape.items() if row["pk"]] or ["id"]
        match = " AND ".join(f"t.{_quote(k)}=s.{_quote(k)}" for k in keys)
        difference = " OR ".join(f"t.{_quote(c)} IS NOT s.{_quote(c)}" for c in columns)
        table_sql = _quote(table)
        query = (
            f"SELECT t.*,s.body AS source_body FROM main.{table_sql} t JOIN {alias}.{table_sql} s ON {match} WHERE {difference}"
            if table == "ondemands"
            else f"SELECT 1 FROM main.{table_sql} t JOIN {alias}.{table_sql} s ON {match} WHERE {difference} LIMIT 1"
        )
        if table == "ondemands":
            for row in target.execute(query).fetchall():
                before = OnDemand.model_validate_json(row["body"])
                other = OnDemand.model_validate_json(row["source_body"])
                # Two disjoint histories share a named supply whose only change
                # is its concurrency cap. Keep the safer cap; originals remain
                # in their recovery snapshots. Never combine paid allocations.
                if before.model_copy(update={"max_machines": other.max_machines}) != other:
                    raise LauncherError(f"conflicting on-demand identity from {path}")
                retained = before.model_copy(
                    update={"max_machines": min(before.max_machines, other.max_machines)}
                )
                target.execute(
                    "UPDATE ondemands SET body=? WHERE name=?",
                    (retained.model_dump_json(), retained.name),
                )
        elif target.execute(query).fetchone():
            raise LauncherError(
                f"conflicting historical identity in {table} from {path}; retain sources and reconcile before publication"
            )
        selected = ",".join(_quote(c) for c in columns)
        target.execute(
            f"INSERT INTO main.{table_sql}({selected}) SELECT {selected} FROM {alias}.{table_sql} s WHERE NOT EXISTS (SELECT 1 FROM main.{table_sql} t WHERE {match})"
        )


def _require_closed(sources: tuple[Path, ...]) -> None:
    """Reject observed open legacy files; callers must also prevent old clients restarting."""
    if not sources:
        return
    try:
        result = subprocess.run(
            [
                "lsof",
                "-t",
                "--",
                *(
                    str(p)
                    for source in sources
                    for p in (
                        source,
                        source.with_name(source.name + "-wal"),
                        source.with_name(source.name + "-shm"),
                    )
                ),
            ],
            capture_output=True,
            timeout=remaining(),
        )
    except FileNotFoundError:
        raise LauncherError("legacy cutover needs lsof to inspect open local clients") from None
    if result.returncode not in (0, 1):
        raise LauncherError("could not inspect open legacy clients; no cutover performed")
    if result.stdout.strip():
        pids = ",".join(sorted({p for p in result.stdout.decode().split() if p.isdigit()}))
        raise LauncherError(
            f"legacy databases are open by local processes {pids}; stop their scheduling commands before upgrade"
        )


def _signature(source: Path) -> str:
    """Detect intervening filesystem writes; this is evidence, not client exclusion."""
    parts: list[str] = []
    for path in (source, source.with_name(source.name + "-wal")):
        try:
            state = path.stat()
        except FileNotFoundError:
            if path == source:
                raise
            continue
        if path == source or state.st_size:
            parts.append(f"{path.name}:{state.st_ino}:{state.st_size}:{state.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class Evidence(Record):
    """Immutable bytes required to finish a local publication."""

    path: Path
    sha256: str


class Retirement(Record):
    """One original file and its sole recovery destination, including SQLite sidecars."""

    source: Path
    retired: Path
    sha256: str


class Takeover(Record):
    """Durable publication intent; named file identities determine progress on retry."""

    result: Upgrade
    target: Evidence
    evidence: tuple[Evidence, ...]
    files: tuple[Retirement, ...]


def _takeover_path(home: Path) -> Path:
    return home / ".launcher-takeover.json"


def _takeover_problem(record: Path) -> str:
    return (
        f"unfinished launcher takeover; recovery record: {record}; "
        "keep scheduling stopped and rerun etude database upgrade"
    )


def _refuse_takeover(home: Path) -> None:
    if (record := _takeover_path(home)).exists():
        raise LauncherError(_takeover_problem(record))


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            remaining()
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_takeover(record: Path, takeover: Takeover) -> None:
    temporary = record.with_suffix(".pending")
    temporary.write_text(takeover.model_dump_json() + "\n")
    _sync(temporary)
    temporary.replace(record)
    _sync_directory(record.parent)


def _finish_takeover(home: Path) -> Upgrade:
    record = _takeover_path(home)
    takeover = Takeover.model_validate_json(record.read_text())

    def refuse(reason: str) -> None:
        raise LauncherError(
            f"{reason}; preserve all files and reconcile against recovery record: {record}"
        )

    for evidence in (takeover.target, *takeover.evidence):
        if not evidence.path.is_file() or _digest(evidence.path) != evidence.sha256:
            refuse(f"takeover evidence differs or is missing: {evidence.path}")
    # Validate the retained target against this program, including ledger checksums.
    with closing(Database(takeover.target.path)):
        pass
    sources = tuple(item.source for item in takeover.files)
    if unexpected := set(_sources(home)) - set(sources):
        refuse("unrecorded launcher histories: " + ", ".join(map(str, sorted(unexpected))))
    sidecars = {
        path
        for source in (*takeover.result.sources, takeover.result.path)
        for path in (source.with_name(source.name + "-wal"), source.with_name(source.name + "-shm"))
        if path.exists()
    }
    if unexpected_sidecars := sidecars - set(sources):
        refuse("unrecorded SQLite sidecars: " + ", ".join(map(str, sorted(unexpected_sidecars))))
    _require_closed(
        tuple(p for p in (*takeover.result.sources, takeover.result.path) if p.exists())
    )
    canonical = takeover.result.path
    # A prior attempt may have published before archiving the intent.
    published = (
        all(item.retired.exists() for item in takeover.files)
        and canonical.exists()
        and _digest(canonical) == takeover.target.sha256
    )
    for item in takeover.files:
        original = item.source.exists() and not (published and item.source == canonical)
        if original == item.retired.exists():
            refuse(f"expected exactly one original or retired file for {item.source}")
        actual = item.source if original else item.retired
        if _digest(actual) != item.sha256:
            refuse(f"takeover file identity differs: {actual}")
    if canonical.exists() and not published and canonical not in sources:
        refuse(f"unexpected canonical database: {canonical}")
    for item in takeover.files:
        remaining()
        if not item.retired.exists():
            item.source.replace(item.retired)
        # Sync both directories even when a prior run performed the rename.
        _sync_directory(item.retired.parent)
        _sync_directory(item.source.parent)
    if not published:
        publication = takeover.target.path.with_name("publication.sqlite")
        _copy_file(takeover.target.path, publication)
        remaining()
        _sync(publication)
        publication.replace(canonical)
    _sync(canonical)
    _sync_directory(takeover.target.path.parent)
    # The record stays immutable: file identities recover even interrupted renames.
    # Archiving it marks completion and enables ordinary opens.
    record.replace(takeover.target.path.parent / "takeover.json")
    _sync_directory(takeover.target.path.parent)
    _sync_directory(home)
    return takeover.result


def _takeover(home: Path, state: Schema) -> Upgrade:
    """Validate consistent snapshots together; leave originals intact until cutover."""
    # A legacy binary cannot honor a new marker. Source closure and retirement
    # require host-level coordination; this stage must never publish a second queue.
    sources = (*state.sources, *((state.path,) if state.path.exists() else ()))
    _require_closed(sources)
    revisions = {source: _signature(source) for source in sources}
    recovery = home / "recovery" / f"consolidation-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    recovery.mkdir(parents=True, mode=0o700)
    snapshots: list[Path] = []
    exports: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=".launcher-", dir=home) as temporary:
        stage = Path(temporary) / "launcher.sqlite"
        if state.path.exists():
            snapshot = recovery / "canonical-before.sqlite"
            _retain_snapshot(state.path, snapshot)
            snapshots.append(snapshot)
            _snapshot(snapshot, stage, remaining() or 0)
        db = sqlite3.connect(stage, isolation_level=None, uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            _apply(db, state.applied, state.path, exports)
            for index, source in enumerate(state.sources):
                snapshot = recovery / f"{index}-{source.name}"
                _retain_snapshot(source, snapshot)
                snapshots.append(snapshot)
                converted = Path(temporary) / f"{index}.sqlite"
                _snapshot(snapshot, converted, remaining() or 0)
                with closing(sqlite3.connect(converted, isolation_level=None)) as old:
                    old.row_factory = sqlite3.Row
                    old.execute("BEGIN IMMEDIATE")
                    applied = _normalize_history(old, source)
                    if applied is None:
                        old.execute("ROLLBACK")
                        continue
                    _apply(old, applied, state.path, exports)
                    old.execute("COMMIT")
                _merge_history(db, source, converted)
            if problems := _invalid_records(db):
                raise LauncherError("consolidated rows do not validate:\n" + "\n".join(problems))
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise LauncherError("consolidation has broken foreign key references")
            db.execute("COMMIT")
            db.execute("PRAGMA main.journal_mode=WAL")
        except LauncherError as error:
            raise LauncherError(f"{error}; recovery snapshots: {recovery}") from None
        finally:
            db.close()
        # Validation bytes and their directory entries outlive temporary cleanup.
        validated = recovery / "validated.sqlite"
        _snapshot(stage, validated, remaining() or 0)
        _sync(validated)
        _sync_directory(recovery.parent)
        _sync_directory(home)
        _require_closed(sources)
        if any(_signature(source) != revisions[source] for source in sources):
            raise LauncherError(
                f"legacy files changed during validation; no cutover performed; recovery snapshots: {recovery}"
            )
        floors = [m.floor for m in migrations() if m.floor is not None]
        required = _required()
        result = Upgrade(
            path=state.path,
            sources=sources,
            backups=tuple(snapshots),
            exports=tuple(exports),
            applied=tuple(m.name for m in migrations()),
            floor=max(floors, key=_version) if floors and not required else None,
            required=required,
        )
        files = tuple(
            Retirement(
                source=path, retired=recovery / f"{index}-retired-{path.name}", sha256=_digest(path)
            )
            for index, source in enumerate(sources)
            for path in (
                source,
                source.with_name(source.name + "-wal"),
                source.with_name(source.name + "-shm"),
            )
            if path.exists()
        )
        if any(_signature(source) != revisions[source] for source in sources):
            raise LauncherError(
                f"legacy files changed while recording takeover; recovery snapshots: {recovery}"
            )
        evidence_paths = (*snapshots, *(p.with_name(p.name + ".json") for p in snapshots), *exports)
        _write_takeover(
            _takeover_path(home),
            Takeover(
                result=result,
                target=Evidence(path=validated, sha256=_digest(validated)),
                evidence=tuple(Evidence(path=p, sha256=_digest(p)) for p in evidence_paths),
                files=files,
            ),
        )
    return _finish_takeover(home)


def _authorize_upgrade(state: Schema, development: bool, catalog: dict[str, str]) -> None:
    if state.problem and state.takeover is None:
        raise LauncherError(state.problem)
    if development:
        if state.path.exists() or state.sources or state.takeover:
            raise LauncherError("development upgrades require an empty fixture home")
        return
    if unproven := [
        m.name
        for m in migrations()
        if (m.name in state.pending or state.sources or state.takeover)
        and catalog.get(m.name) != m.sha256
    ]:
        raise LauncherError(
            "unmerged or unproven migrations: "
            + ", ".join(unproven)
            + "; use an empty development fixture or a merged build"
        )


def _remove_abandoned_staging(home: Path) -> None:
    """Delete staging left by a killed upgrade; the held upgrade lock proves none is live."""
    for staging in home.glob(".launcher-*"):
        if staging.is_dir():
            shutil.rmtree(staging)


def _mark_abandoned_recovery(home: Path) -> None:
    """Under the upgrade lock, retain and label snapshots with no publication owner."""
    record = _takeover_path(home)
    active = (
        Takeover.model_validate_json(record.read_text()).target.path.parent
        if record.exists()
        else None
    )
    for recovery in (home / "recovery").glob("consolidation-*"):
        if recovery != active and not (recovery / "takeover.json").exists():
            marker = recovery / "abandoned"
            if not marker.exists():
                marker.touch(mode=0o600)
                _sync(marker)


def upgrade(home: Path, *, timeout: float | None = None, development: bool = False) -> Upgrade:
    """Explicitly initialize or advance local state; never schedule or contact a worker.

    Development permits unmerged SQL only in an empty fixture home. Existing
    operational state requires packaged merged-frontier evidence.
    """
    with limit_time(timeout):
        home = home.resolve()
        state = inspect(home)
        if state.problem and state.takeover is None:
            raise LauncherError(state.problem)
        if not state.pending and not state.sources and state.takeover is None:
            return Upgrade(path=state.path, sources=(), backups=(), applied=(), floor=None)
        from_catalog = {} if development else _merged()
        _authorize_upgrade(state, development, from_catalog)
        home.mkdir(parents=True, exist_ok=True)
        with _upgrade_lock(home):
            _remove_abandoned_staging(home)
            _mark_abandoned_recovery(home)
            state = inspect(home)
            _authorize_upgrade(state, development, from_catalog)
            if state.takeover is not None:
                return _finish_takeover(home)
            if not state.pending and not state.sources:
                return Upgrade(path=state.path, sources=(), backups=(), applied=(), floor=None)
            if state.sources:
                try:
                    return _takeover(home, state)
                finally:
                    _mark_abandoned_recovery(home)
            backups: tuple[Path, ...] = ()
            exports: list[Path] = []
            path = state.path
            with tempfile.TemporaryDirectory(prefix=".launcher-", dir=home) as temporary:
                # Existing state migrates in place to preserve open compatible clients.
                target = path if path.exists() else Path(temporary) / path.name
                db = sqlite3.connect(target, isolation_level=None, timeout=0)
                db.row_factory = sqlite3.Row
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    while True:
                        remaining()
                        try:
                            db.execute("BEGIN IMMEDIATE")
                            break
                        except sqlite3.OperationalError as error:
                            if "locked" not in str(error):
                                raise
                            time.sleep(min(0.05, remaining(0.05) or 0.05))
                    if target == path:
                        backup = path.with_name(
                            f"{path.name}.backup-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
                        )
                        _retain_snapshot(path, backup)
                        backups = (backup,)
                    applied = _apply(db, state.applied, path, exports)
                    if not state.applied:
                        db.execute("INSERT INTO pools(name) VALUES ('local')")
                        db.execute(
                            "INSERT INTO machines(name,kind,pool) VALUES ('local','local','local')"
                        )
                    remaining()
                    db.execute("COMMIT")
                except (LauncherError, TimeoutError) as error:
                    raise type(error)(
                        f"{error}; recovery snapshots: {', '.join(map(str, backups)) or 'none (new home)'}"
                    ) from None
                finally:
                    db.close()
                if target != path:
                    target.chmod(0o600)
                    target.replace(path)
                    _sync(path)
            floors = [m.floor for m in migrations() if m.floor is not None]
            required = _required()
            return Upgrade(
                path=path,
                sources=(),
                backups=backups,
                applied=applied,
                exports=tuple(exports),
                floor=max(floors, key=_version) if floors and not required else None,
                required=required,
            )
