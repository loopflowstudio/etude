"""The launcher's SQLite file. Records change only through the operations named here.

Every session, attempt and run row carries a version. A write names the version it
read and raises Stale if the row has moved, so two processes working the same
file cannot both take one step. Status and spend are computed, never stored.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from etude.launch.records import (
    RETRYABLE,
    Cause,
    Cohort,
    Diagnostic,
    Hardware,
    Image,
    Job,
    Launch,
    LauncherError,
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
    RunRecord,
    Session,
    SourcePosition,
    Stale,
    Storage,
    Supply,
    Target,
    job_identity,
)
from etude.launch.usage import Transfer

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS launches(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS launch_runs(launch TEXT NOT NULL, run TEXT NOT NULL,
  PRIMARY KEY(launch, run));
CREATE INDEX IF NOT EXISTS launch_runs_run ON launch_runs(run);
CREATE TABLE IF NOT EXISTS launch_sessions(launch TEXT NOT NULL, session TEXT NOT NULL, acquired INTEGER NOT NULL,
  PRIMARY KEY(launch, session));
CREATE TABLE IF NOT EXISTS diagnostics(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log_streams(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks(
  supply TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('default_pool', 'local');
INSERT OR IGNORE INTO settings VALUES ('default_rent', '');
CREATE TABLE IF NOT EXISTS cohorts(
  name TEXT PRIMARY KEY, parallel INTEGER NOT NULL, max_usd TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS providers(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, registry TEXT NOT NULL, storage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS storages(
  name TEXT PRIMARY KEY, bucket TEXT NOT NULL, prefix TEXT NOT NULL, provider TEXT NOT NULL,
  region TEXT);
CREATE TABLE IF NOT EXISTS pools(
  name TEXT PRIMARY KEY, off INTEGER NOT NULL DEFAULT 0,
  startup_seconds REAL, delivery_seconds REAL, log_seconds REAL);
CREATE TABLE IF NOT EXISTS ondemands(name TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machines(
  name TEXT PRIMARY KEY, kind TEXT NOT NULL,
  pool TEXT REFERENCES pools(name), ondemand TEXT REFERENCES ondemands(name),
  cost_usd TEXT NOT NULL DEFAULT '0', expires_at TEXT,
  provider TEXT, address TEXT, storage TEXT, deleted_at TEXT, image TEXT,
  CHECK ((pool IS NULL) != (ondemand IS NULL)));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  job TEXT NOT NULL REFERENCES jobs(id), spec TEXT NOT NULL, seed INTEGER,
  pool TEXT REFERENCES pools(name), rent TEXT REFERENCES ondemands(name),
  cohort TEXT REFERENCES cohorts(name),
  created_at TEXT NOT NULL,
  cancel_requested_at TEXT, version INTEGER NOT NULL DEFAULT 1,
  autorun INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
  params TEXT NOT NULL DEFAULT '{}', source TEXT,
  CHECK (pool IS NOT NULL OR rent IS NOT NULL));
CREATE TABLE IF NOT EXISTS sessions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  machine TEXT NOT NULL REFERENCES machines(name), planned_at TEXT NOT NULL,
  claimed_at TEXT, startup_deadline TEXT, receipt TEXT, started_at TEXT, refused_at TEXT, ended_at TEXT,
  version INTEGER NOT NULL DEFAULT 1, autorun INTEGER NOT NULL DEFAULT 0, hardware TEXT);
CREATE TABLE IF NOT EXISTS rentals(
  machine TEXT PRIMARY KEY REFERENCES machines(name), usd_per_hour TEXT NOT NULL,
  cap_usd TEXT NOT NULL, deadline TEXT NOT NULL, kept INTEGER NOT NULL DEFAULT 0,
  billed_usd TEXT NOT NULL DEFAULT '0', billed_through TEXT);
CREATE TABLE IF NOT EXISTS attempts(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  run TEXT NOT NULL REFERENCES runs(id), session TEXT NOT NULL REFERENCES sessions(id), machine TEXT NOT NULL REFERENCES machines(name),
  number INTEGER NOT NULL, position INTEGER NOT NULL, limit_seconds INTEGER NOT NULL,
  limit_kind TEXT NOT NULL, started_at TEXT, finished_at TEXT, outcome TEXT, cause TEXT,
  clocks TEXT NOT NULL DEFAULT '{}', outputs TEXT NOT NULL DEFAULT '[]', exit_code INTEGER,
  version INTEGER NOT NULL DEFAULT 1, planned_at TEXT, added INTEGER NOT NULL DEFAULT 0,
  prepared TEXT);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job);
CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run);
CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session);
"""

Fact = str | int | None
# Changes with the schema. There are no migrations: a file from another version is refused.
_VERSION = 11


def _text(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


# SQLite scalars already match the model fields. Only JSON columns need
# adapting; records own scalar validation.
_RUNS = "SELECT r.* FROM runs r"


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord.model_validate(
        {
            **dict(row),
            "spec": Job.model_validate_json(row["spec"]),
            "params": json.loads(row["params"]),
            "source": SourcePosition.model_validate_json(row["source"]) if row["source"] else None,
        }
    )


# A rented machine has one working session for its whole life. What the provider
# said about making and deleting the machine is recorded on that session.
_MACHINES = """SELECT m.name, m.pool, m.ondemand, m.kind, m.cost_usd, m.expires_at,
    m.provider, m.address, m.storage, m.image,
    w.claimed_at AS requested_at, w.receipt AS provider_id, w.hardware, w.refused_at,
    COALESCE(w.ended_at, m.deleted_at) AS deleted_at
    FROM machines m LEFT JOIN sessions w ON w.machine=m.name AND m.kind='rented'"""
# A session's supply is its machine's: one owner for the fact.
_SESSIONS = """SELECT w.*, COALESCE(m.pool, m.ondemand) AS supply
    FROM sessions w JOIN machines m ON m.name = w.machine"""
_RENTALS = "SELECT r.* FROM rentals r"


def _machine(row: sqlite3.Row) -> Machine:
    return Machine.model_validate(
        {
            **dict(row),
            "image": Image.model_validate_json(row["image"]) if row["image"] else None,
            "hardware": _hardware(row),
        }
    )


def _session(row: sqlite3.Row) -> Session:
    return Session.model_validate({**dict(row), "hardware": _hardware(row)})


def _hardware(row: sqlite3.Row) -> object:  # validated by the record holding it
    return json.loads(row["hardware"]) if row["hardware"] else None


def _attempt(row: sqlite3.Row) -> RunAttempt:
    return RunAttempt.model_validate(
        {
            **dict(row),
            "clocks": json.loads(row["clocks"]),
            "outputs": json.loads(row["outputs"]),
            "prepared": None if row["prepared"] is None else json.loads(row["prepared"]),
        }
    )


class Database:
    def __init__(self, path: Path, *, timeout: float = 30) -> None:
        """Open local state; timeout bounds SQLite lock contention, in seconds."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("database timeout must be nonnegative and finite")
        self._locks: dict[str, tuple[str, Callable[[], datetime]]] = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, timeout=timeout)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        with self._together():
            made = self._db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if made and version != _VERSION:
                raise LauncherError(
                    f"{path} was written by another version of etude; move or delete it to start again"
                )
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    self._db.execute(statement)
            self._db.execute(f"PRAGMA user_version = {_VERSION}")

    def retain_transfer_json(self, raw: str) -> None:
        self.retain_transfer(Transfer.model_validate_json(raw))

    def retain_transfer(self, event: Transfer) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO transfers VALUES (?, ?)", (event.id, event.model_dump_json())
        )

    def transfers(self) -> tuple[Transfer, ...]:
        return tuple(
            Transfer.model_validate_json(row[0])
            for row in self._db.execute("SELECT body FROM transfers ORDER BY rowid")
        )

    def begin_launch(self, launch: Launch) -> None:
        with self._together():
            for request in launch.requests:
                if self._db.execute("SELECT 1 FROM runs WHERE id=?", (request.run,)).fetchone():
                    raise LauncherError(f"run {request.run!r} already admitted")
                for previous in self.launches(request.run):
                    if previous.state in {"preparing", "admitting"} or any(
                        session.refused_at is None and session.ended_at is None
                        for session in self.launch_sessions(previous.id, acquired_only=True)
                    ):
                        raise LauncherError(
                            f"run {request.run!r}: reconciling existing request {previous.id}"
                        )
            self.save_launch(launch)
            self._db.executemany(
                "INSERT INTO launch_runs VALUES (?, ?)",
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
        self._db.execute(
            "INSERT OR REPLACE INTO launches VALUES (?, ?)", (launch.id, launch.model_dump_json())
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
        self._db.execute(
            "INSERT OR IGNORE INTO launch_sessions VALUES (?, ?, ?)",
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
        self._db.execute(
            "INSERT OR IGNORE INTO diagnostics VALUES (?, ?)",
            (diagnostic.id, diagnostic.model_dump_json()),
        )

    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(
            Diagnostic.model_validate_json(row[0])
            for row in self._db.execute("SELECT body FROM diagnostics ORDER BY rowid")
        )

    def retain_log(self, stream: LogStream) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO log_streams VALUES (?, ?)",
            (stream.id, stream.model_dump_json()),
        )

    def log(self, identity: str) -> LogStream | None:
        row = self._db.execute("SELECT body FROM log_streams WHERE id=?", (identity,)).fetchone()
        return None if row is None else LogStream.model_validate_json(row[0])

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _together(self) -> Iterator[None]:
        """Several statements that land together or not at all."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

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
                self._db.execute(
                    """INSERT INTO locks VALUES (?,?,?) ON CONFLICT(supply) DO UPDATE
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
            self._db.execute("DELETE FROM locks WHERE supply=? AND owner=?", (supply, owner))

    def check_lock(self, supply: str) -> None:
        """Fence stale processes before effects and inside placement transactions."""
        held = self._locks.get(supply)
        if held is None:
            raise Stale(f"{supply!r} has no placement lock")
        owner, clock = held
        now = clock()
        changed = self._db.execute(
            "UPDATE locks SET expires_at=? WHERE supply=? AND owner=? AND expires_at>?",
            ((now + timedelta(minutes=5)).isoformat(), supply, owner, now.isoformat()),
        )
        if changed.rowcount != 1:
            raise Stale(f"{supply!r} placement lock expired")

    def _change(self, table: str, id: str | str | str, version: int, **facts: Fact) -> None:
        sets = "".join(f"{name} = ?, " for name in facts)
        done = self._db.execute(
            f"UPDATE {table} SET {sets}version = version + 1 WHERE id = ? AND version = ?",
            (*facts.values(), id, version),
        )
        if done.rowcount != 1:
            raise Stale(f"{table} {id} is no longer at version {version}")

    # Providers, storage, pools and machines

    def set_provider(self, provider: Provider) -> None:
        """Record a provider's settings, replacing any it had. Pools already added keep their storage."""
        self.storage(provider.storage)  # raises if no such storage
        self._db.execute(
            "INSERT OR REPLACE INTO providers VALUES (?,?,?,?)",
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
        self._db.execute(
            "INSERT OR IGNORE INTO storages VALUES (?,?,?,?,?)",
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
                self._db.execute(
                    "INSERT INTO pools VALUES (?,?,?,?,?)",
                    (
                        pool.name,
                        int(pool.off),
                        pool.startup_seconds,
                        pool.delivery_seconds,
                        pool.log_seconds,
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
                self._db.execute(
                    "INSERT INTO ondemands VALUES (?,?)",
                    (ondemand.name, ondemand.model_dump_json()),
                )
        if self.ondemand(ondemand.name) != ondemand:
            raise LauncherError(f"on-demand {ondemand.name!r} already exists with other settings")

    def change_ondemand(self, ondemand: OnDemand) -> None:
        """Replace an on-demand's settings; machines it already rented keep their windows."""
        self.ondemand(ondemand.name)  # raises if no such on-demand
        self._db.execute(
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
            self._db.execute("UPDATE pools SET name='controller' WHERE name='local'")
            for table in ("machines", "runs"):
                self._db.execute(f"UPDATE {table} SET pool='controller' WHERE pool='local'")
            self._db.execute(
                "UPDATE settings SET value='controller' WHERE name='default_pool' AND value='local'"
            )

    def set_pool_startup(self, name: str, seconds: float | None) -> None:
        pool = self.pool(name)
        checked = Pool.model_validate({**pool.model_dump(), "startup_seconds": seconds})
        self._db.execute(
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
                self._db.execute("UPDATE settings SET value=? WHERE name=?", (value or "", name))

    def turn_pool(self, name: str, *, off: bool) -> None:
        self.pool(name)  # raises if no such pool
        self._db.execute("UPDATE pools SET off = ? WHERE name = ?", (int(off), name))

    def add_machine(self, machine: Machine) -> None:
        """Record a machine in its pool or on-demand. Adding the same one again changes nothing."""
        if machine.pool is not None:
            self.pool(machine.pool)  # raises if no such pool
        if machine.ondemand is not None:
            self.ondemand(machine.ondemand)
        self._db.execute(
            "INSERT OR IGNORE INTO machines(name, kind, pool, ondemand, cost_usd, expires_at,"
            " provider, address, storage, image) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                machine.name,
                machine.kind,
                machine.pool,
                machine.ondemand,
                str(machine.cost_usd),
                _text(machine.expires_at),
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
        return _machine(row)

    def machines(self, supply: str) -> tuple[Machine, ...]:
        """A pool's machines, or every machine ever rented from an on-demand."""
        rows = self._db.execute(
            f"{_MACHINES} WHERE m.pool = ? OR m.ondemand = ? ORDER BY m.rowid", (supply, supply)
        )
        return tuple(_machine(row) for row in rows)

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
                    self._db.execute(
                        "INSERT INTO cohorts VALUES (?, ?, ?, ?)",
                        (
                            cohort.name,
                            cohort.parallel,
                            None if cohort.max_usd is None else str(cohort.max_usd),
                            cohort.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise LauncherError(f"cohort {cohort.name!r} already exists") from error
            for run in runs:
                self._db.execute(
                    "INSERT OR IGNORE INTO jobs VALUES (?, ?)",
                    (run.job, json.dumps(job_identity(run.spec), sort_keys=True)),
                )
                self._db.execute(
                    """INSERT INTO runs(id, job, spec, seed, pool, rent, cohort,
                       created_at, autorun, queued, params, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run.id,
                        run.job,
                        run.spec.model_dump_json(),
                        run.seed,
                        run.pool,
                        run.rent,
                        run.cohort,
                        run.created_at.isoformat(),
                        int(run.autorun),
                        int(run.queued),
                        json.dumps(run.params),
                        run.source.model_dump_json() if run.source else None,
                    ),
                )

            self._place(attempts)
            if launch is not None:
                evidence = self.launch_evidence(launch)
                self.save_launch(evidence.model_copy(update={"state": "admitted"}))

    def cancel(self, run: RunRecord, at: datetime) -> None:
        self._change("runs", run.id, run.version, cancel_requested_at=at.isoformat())

    def run(self, id: str) -> RunRecord:
        row = self._db.execute(f"{_RUNS} WHERE r.id = ?", (id,)).fetchone()
        if row is None:
            raise LauncherError(f"no run {id!r}")
        return _run(row)

    def runs(self) -> tuple[RunRecord, ...]:
        return tuple(_run(row) for row in self._db.execute(f"{_RUNS} ORDER BY r.seq"))

    def job_runs(self, job: str) -> tuple[RunRecord, ...]:
        """Every run of one job, oldest first: its seeds side by side."""
        rows = self._db.execute(f"{_RUNS} WHERE r.job = ? ORDER BY r.seq", (job,))
        return tuple(_run(row) for row in rows)

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
                WHERE (r.pool = ? OR r.rent = ?) AND r.cancel_requested_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.run = r.id AND (
                  a.outcome IS NULL OR a.outcome != 'lost' OR a.cause NOT IN ({marks})))
                ORDER BY r.seq""",
            (supply, supply, *sorted(RETRYABLE)),
        ).fetchall()
        return tuple(run for row in rows if (run := _run(row)).spec.max_attempts > row["tries"])

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
            self._db.execute(
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
                self._db.execute(
                    "INSERT INTO rentals VALUES (?,?,?,?,?,?,?)",
                    (
                        r.machine,
                        str(r.usd_per_hour),
                        str(r.cap_usd),
                        r.deadline.isoformat(),
                        int(r.kept),
                        str(r.billed_usd),
                        _text(r.billed_through),
                    ),
                )
            if launch is not None:
                self.link_launch(launch, session.id, acquired=True)
            self._place(attempts)

    def place(self, session: Session, attempts: Sequence[RunAttempt]) -> None:
        """Put more attempts on a session that already exists."""
        with self._together():
            self.check_lock(session.supply)
            self._change("sessions", session.id, session.version)
            if self.machine_busy(session.machine):
                raise Stale(f"session {session.id!r} already has work")
            self._place(attempts)

    def _place(self, attempts: Sequence[RunAttempt]) -> None:
        for a in attempts:
            busy = self._db.execute(
                "SELECT 1 FROM attempts WHERE (run = ? OR machine = ?) AND outcome IS NULL",
                (a.run, a.machine),
            ).fetchone()
            if busy is not None:
                raise Stale(f"run {a.run} already has an attempt in hand")
            self._db.execute(
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
                    _text(a.planned_at),
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
                startup_deadline=_text(deadline),
            )

    def record_receipt(self, session: Session, receipt: str) -> None:
        self._change(
            "sessions",
            session.id,
            session.version,
            receipt=receipt,
        )

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
            self._db.execute(
                "UPDATE rentals SET usd_per_hour=? WHERE machine=?", (str(price), session.machine)
            )

    def record_bill(self, session: Session, usd: Decimal, through: datetime) -> None:
        with self._together():
            self._change("sessions", session.id, session.version)
            self._db.execute(
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
        return _session(row)

    def rentals(self) -> tuple[Rental, ...]:
        return tuple(Rental.model_validate(dict(row)) for row in self._db.execute(_RENTALS))

    def machine_busy(self, machine: str) -> bool:
        """Allocation authority: any unfinished attempt holds its machine."""
        return (
            self._db.execute(
                "SELECT 1 FROM attempts WHERE machine=? AND outcome IS NULL", (machine,)
            ).fetchone()
            is not None
        )

    def sessions(self) -> tuple[Session, ...]:
        return tuple(_session(row) for row in self._db.execute(f"{_SESSIONS} ORDER BY w.seq"))

    def record_refusal(self, session: Session, at: datetime) -> None:
        self._change("sessions", session.id, session.version, refused_at=at.isoformat())

    def confirm_ended(self, session: Session, at: datetime) -> None:
        self._change("sessions", session.id, session.version, ended_at=at.isoformat())
        self._db.execute(  # a rented machine is gone with it; one you added remains
            "UPDATE machines SET deleted_at = ? WHERE name = ? AND kind = 'rented'",
            (at.isoformat(), session.machine),
        )

    def session(self, id: str) -> Session:
        row = self._db.execute(f"{_SESSIONS} WHERE w.id = ?", (id,)).fetchone()
        if row is None:
            raise LauncherError(f"no session {id!r}")
        return _session(row)

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
        return tuple(_session(row) for row in rows)

    def cohort_sessions(self, name: str) -> tuple[Session, ...]:
        rows = self._db.execute(
            f"""{_SESSIONS} WHERE w.id IN (SELECT a.session FROM attempts a
               JOIN runs r ON r.id = a.run WHERE r.cohort = ?) ORDER BY w.seq""",
            (name,),
        )
        return tuple(_session(row) for row in rows)

    def cohort_runs(self, name: str) -> tuple[RunRecord, ...]:
        self.cohort(name)  # raises if no such cohort
        rows = self._db.execute(f"{_RUNS} WHERE r.cohort = ? ORDER BY r.seq", (name,))
        return tuple(_run(row) for row in rows)

    def cohort_attempts(self, name: str) -> tuple[RunAttempt, ...]:
        rows = self._db.execute(
            """SELECT a.* FROM attempts a JOIN runs r ON r.id = a.run
               WHERE r.cohort = ? ORDER BY a.seq""",
            (name,),
        )
        return tuple(_attempt(row) for row in rows)

    # Attempts

    def start(self, attempt: RunAttempt, at: datetime) -> None:
        self._change("attempts", attempt.id, attempt.version, started_at=at.isoformat())

    def record_progress(self, attempt: RunAttempt, clocks: dict[str, float]) -> None:
        """Retain where the job last said its clocks stood."""
        self._change("attempts", attempt.id, attempt.version, clocks=json.dumps(clocks))

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
        return tuple(_attempt(row) for row in rows)
