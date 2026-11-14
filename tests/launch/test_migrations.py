"""Released launcher facts survive upgrades; incompatible writers stop before effects."""

from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from etude.errors import LauncherError
from etude.launch import database
from etude.launch.database import (
    Migration,
    additive,
    copies,
    launcher_file,
    migrations,
    open_home,
)
from etude.launch.errors import SchemaAdvanced
from etude.launch.providers.package import program_archive
from etude.launch.records import Pool
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import Finished

FIXTURES = Path(__file__).with_name("fixtures")


def _released(home: Path, version: str, name: str | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / (name or f"launcher-{'17' if version == '0.3.2' else '19'}.db")
    with sqlite3.connect(path) as db:
        db.executescript((FIXTURES / f"launcher-{version}.sql").read_text())
    os.utime(path, (0, 0))  # Released fixture has been quiet long enough for takeover.
    return path


def _migration(sql: str, *, name: str = "extra", floor: str | None = "0.4.0") -> Migration:
    return Migration(
        name=name,
        after=("run_resume_and_writers",),
        breaks=None,
        sql=sql,
        sha256=hashlib.sha256(sql.encode()).hexdigest(),
        floor=floor,
    )


def _dump(path: Path) -> tuple[str, ...]:
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize(
    "version,name", [("0.3.2", None), ("0.4.0", None), ("0.3.2", "launcher.db")]
)
def test_takes_over_released_facts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str, name: str | None
) -> None:
    source = _released(tmp_path, version, name)
    before = source.read_bytes()
    store = open_home(tmp_path)
    assert len(store.runs()) == 2
    assert len(store.sessions()) == 2
    assert len(store.rentals()) == 1
    assert store.cohort("sweep").parallel == 2
    scheduler = Scheduler(store, {})
    phases = {scheduler.read(run.id).phase for run in store.runs()}
    assert phases == {"running", "queued"}
    assert all(
        run.submission is not None and run.submission.resume_after == 0 for run in store.runs()
    )
    assert all(
        attempt.writers == () for run in store.runs() for attempt in store.attempts(run=run.id)
    )
    # Compare every original column, including JSON and operational evidence.
    with sqlite3.connect(source) as original:
        tables = [
            r[0] for r in original.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            columns = ",".join(
                '"' + r[1] + '"' for r in original.execute(f'PRAGMA table_info("{table}")')
            )
            query = f'SELECT {columns} FROM "{table}" ORDER BY rowid'
            assert [tuple(row) for row in store._db.execute(query)] == original.execute(
                query
            ).fetchall()
    store.close()
    assert source.read_bytes() == before
    assert "took over " + source.name in capsys.readouterr().err
    assert not list(tmp_path.glob("*.backup-*"))
    open_home(tmp_path).close()
    assert capsys.readouterr().err == ""


def test_fresh_schema_equals_upgrade(tmp_path: Path) -> None:
    _released(tmp_path / "old", "0.3.2")
    old = open_home(tmp_path / "old")
    fresh = open_home(tmp_path / "fresh")
    assert database._schema(old._db) == database._schema(fresh._db)
    assert old.fresh is False
    assert fresh.fresh is True
    old.close()
    fresh.close()


def test_takes_over_newest_and_names_others_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _released(tmp_path, "0.3.2")
    _released(tmp_path, "0.4.0")
    with sqlite3.connect(tmp_path / "launcher-19.db") as db:
        db.execute("INSERT INTO settings(name,value) VALUES ('marker','newest')")
    _released(tmp_path, "0.3.2", "launcher.db")
    os.utime(tmp_path / "launcher-19.db", (0, 0))
    store = open_home(tmp_path)
    assert (
        store._db.execute("SELECT value FROM settings WHERE name='marker'").fetchone()[0]
        == "newest"
    )
    store.close()
    said = capsys.readouterr().err
    assert "took over launcher-19.db" in said
    assert "launcher-17.db (schema 17) is not read, use etude 0.3.2 for it" in said
    assert "launcher.db (schema 17) is not read, use etude 0.3.1 for it" in said
    open_home(tmp_path).close()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("fresh", ["source", "wal"])
@pytest.mark.parametrize("heartbeat", [None, "stale", "fresh", "malformed"])
@pytest.mark.parametrize(
    "version,name", [("0.3.2", None), ("0.4.0", None), ("0.3.2", "launcher.db")]
)
def test_takeover_waits_for_source_and_wal_to_be_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: str,
    heartbeat: str | None,
    version: str,
    name: str | None,
) -> None:
    source = _released(tmp_path, version, name)
    now = datetime(2026, 11, 14, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    if heartbeat is not None:
        value = {
            "stale": datetime.fromtimestamp(now - 60, UTC).isoformat(),
            "fresh": datetime.fromtimestamp(now, UTC).isoformat(),
            "malformed": "unreadable heartbeat",
        }[heartbeat]
        (tmp_path / "controller-heartbeat").write_text(value)
    # Keep a real WAL open, as an older wait command does between scheduling passes.
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO settings(name,value) VALUES ('marker','older wait')")
        writer.commit()
        wal = source.with_name(source.name + "-wal")
        before = source.read_bytes(), wal.read_bytes()
        for path in (source, wal):
            os.utime(path, (now - 60, now - 60))
        changed = source if fresh == "source" else wal
        os.utime(changed, (now - 2, now - 2))
        with pytest.raises(
            LauncherError,
            match=rf"an older etude wrote {source.name} 2s ago; stop its commands "
            r"\(etude wait, etude controller run\), then run this again",
        ):
            open_home(tmp_path)
        assert not launcher_file(tmp_path).exists()
        assert (source.read_bytes(), wal.read_bytes()) == before
        for path in (source, wal):
            os.utime(path, (now - 30, now - 30))
        store = open_home(tmp_path)
        assert (
            store._db.execute("SELECT value FROM settings WHERE name='marker'").fetchone()[0]
            == "older wait"
        )
        store.close()
        assert (source.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_unsupported_legacy_stays_with_its_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "launcher.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE evidence(value TEXT)")
        db.execute("PRAGMA user_version=13")
    before = path.read_bytes()
    store = open_home(tmp_path)
    assert store.runs() == ()
    store.close()
    assert path.read_bytes() == before
    assert "use etude 0.2.15 for it" in capsys.readouterr().err


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE extra(value TEXT);\nSELECT missing FROM nowhere;",
        "UPDATE runs SET spec='invalid-private-content';",
    ],
)
def test_failing_migration_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    _released(tmp_path, "0.3.2")
    store = open_home(tmp_path)
    store.close()
    path = launcher_file(tmp_path)
    before = _dump(path)
    known = migrations()
    monkeypatch.setattr(database, "migrations", lambda: (*known, _migration(sql)))
    with pytest.raises((LauncherError, sqlite3.OperationalError)) as error:
        open_home(tmp_path)
    assert "invalid-private-content" not in str(error.value)
    assert _dump(path) == before
    assert len(list(tmp_path.glob("*.backup-*"))) == 1


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("ALTER TABLE runs ADD COLUMN note TEXT;", True),
        ("ALTER TABLE runs ADD COLUMN note TEXT NOT NULL DEFAULT '';", True),
        ("ALTER TABLE runs ADD COLUMN note TEXT NOT NULL;", False),
        ("ALTER TABLE runs DROP COLUMN cohort;", False),
        ("CREATE INDEX extra ON runs(id);", True),
        ("CREATE UNIQUE INDEX extra ON runs(job);", False),
        ("UPDATE runs SET queued=0;", False),
    ],
)
def test_reader_floor_classification(sql: str, expected: bool) -> None:
    assert additive(_migration(sql), migrations()) == expected


def test_declared_json_break_raises_floor() -> None:
    change = _migration("CREATE TABLE extra(value TEXT);").model_copy(
        update={"breaks": "new JSON shape"}
    )
    assert not additive(change, migrations())


def test_older_reader_and_writer_survive_additive_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = open_home(tmp_path)
    known = migrations()
    with monkeypatch.context() as patch:
        patch.setattr(
            database,
            "migrations",
            lambda: (*known, _migration("ALTER TABLE pools ADD COLUMN note TEXT;")),
        )
        open_home(tmp_path).close()
    older.add_pool(Pool(name="after"))
    assert older.pool("after").name == "after"
    older.close()
    reader = open_home(tmp_path)
    assert reader.pool("after").name == "after"
    reader.close()


def test_breaking_upgrade_refuses_reader_and_fences_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = open_home(tmp_path)
    known = migrations()
    with monkeypatch.context() as patch:
        patch.setattr(
            database,
            "migrations",
            lambda: (*known, _migration("CREATE TABLE future(value TEXT);", floor="0.5.0")),
        )
        patch.setattr(database, "version", lambda package: "0.5.0")
        open_home(tmp_path).close()
    with pytest.raises(LauncherError, match=r"needs etude >= 0.5.0 \(raised by: extra\)"):
        open_home(tmp_path)
    with pytest.raises(SchemaAdvanced, match="etude was upgraded while this ran"):
        older.add_pool(Pool(name="forbidden"))
    assert older.pools() == ()
    older.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_unreleased_build_uses_unchanging_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    source = _released(tmp_path, "0.3.2") if legacy else launcher_file(tmp_path)
    if not legacy:
        open_home(tmp_path).close()
    before = source.read_bytes()
    known = migrations()
    change = _migration("CREATE TABLE extra(value TEXT);", floor=None)
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    store = open_home(tmp_path)
    assert store._path.parent.parent == tmp_path / "copies"
    store.add_pool(Pool(name="branch"))
    path = store._path
    store.close()
    open_home(tmp_path).close()
    assert source.read_bytes() == before
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT name FROM pools WHERE name='branch'").fetchone() == (
            "branch",
        )
    assert launcher_file(tmp_path).exists() is not legacy


def test_loop_hands_over_before_tick(tmp_path: Path) -> None:
    successor = tmp_path / "programs" / "successor"
    executable = successor / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nprintf 'successor active\\n'\n")
    executable.chmod(0o700)
    script = """
import sys
from pathlib import Path
from typing import Literal
from etude.launch.database import open_home
from etude.launch.controller import loop
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import Finished
store = open_home(Path(sys.argv[1]))
store._db.execute("INSERT INTO migrations(name,sha256,applied_at,floor,program) VALUES ('future','digest','now','999.0.0',?)", (sys.argv[2],))
store._db.execute("PRAGMA user_version=3")
loop(Scheduler(store, {}))
"""
    result = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c", script, str(tmp_path), str(successor)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "successor active" in result.stdout
    assert "the loop continues in" in result.stderr


def test_program_archive_ships_migrations() -> None:
    with zipfile.ZipFile(io.BytesIO(program_archive())) as archive:
        for migration in migrations():
            assert (
                archive.read(f"etude/launch/migrations/{migration.name}.sql").decode()
                == migration.sql
            )
        assert "etude/launch/migrations/released.json" in archive.namelist()


def test_copy_paid_windows_remain_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _released(tmp_path, "0.4.0")
    open_home(tmp_path).close()
    known = migrations()
    with monkeypatch.context() as patch:
        patch.setattr(
            database,
            "migrations",
            lambda: (*known, _migration("CREATE TABLE extra(value TEXT);", floor=None)),
        )
        branch = open_home(tmp_path)
        path = branch._path
        branch.close()
        assert copies(tmp_path, datetime(2026, 11, 14, tzinfo=UTC)) == ()
    found = copies(tmp_path, datetime(2026, 11, 14, tzinfo=UTC))
    assert len(found) == 1
    assert found[0].path == str(path.parent)
    assert found[0].rentals == 1 and str(found[0].usd_per_hour) == "0.59"
    assert found[0].unreleased == ("extra",)
    assert copies(tmp_path, datetime(2026, 11, 15, tzinfo=UTC)) == ()


def test_up_to_date_open_takes_no_write_lock_or_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = open_home(tmp_path)
    real_connect = sqlite3.connect
    statements: list[str] = []

    def connect(
        path: Path,
        *,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None,
        timeout: float = 5,
    ) -> sqlite3.Connection:
        connection = real_connect(path, isolation_level=isolation_level, timeout=timeout)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    # An existing writer does not block the read-only opening path.
    store._db.execute("BEGIN IMMEDIATE")
    other = open_home(tmp_path, timeout=0)
    assert len(statements) == 3
    assert not any(sql.startswith(("BEGIN", "INSERT", "UPDATE")) for sql in statements)
    assert not list(tmp_path.glob("*.backup-*"))
    other.close()
    store._db.execute("ROLLBACK")
    store.close()


def test_sqlite_requirement_names_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_home(tmp_path).close()
    before = _dump(launcher_file(tmp_path))
    known = migrations()
    monkeypatch.setattr(
        database, "migrations", lambda: (*known, _migration("ALTER TABLE runs DROP COLUMN cohort;"))
    )
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 31, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.31.1")
    with pytest.raises(
        LauncherError, match="migration extra needs SQLite >= 3.35; this Python has 3.31.1"
    ):
        open_home(tmp_path)
    assert _dump(launcher_file(tmp_path)) == before


def test_backups_keep_two_and_restore_previous_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_home(tmp_path).close()
    applied = list(migrations())
    for index in range(3):
        applied.append(_migration(f"CREATE TABLE extra{index}(value TEXT);", name=f"extra{index}"))
        monkeypatch.setattr(database, "migrations", lambda: tuple(applied))
        open_home(tmp_path).close()
    backups = sorted(tmp_path.glob("launcher.sqlite.backup-*"))
    assert len(backups) == 2
    with sqlite3.connect(backups[-1]) as previous:
        assert previous.execute("SELECT count(*) FROM migrations").fetchone() == (4,)
        assert (
            previous.execute("SELECT name FROM sqlite_master WHERE name='extra2'").fetchone()
            is None
        )


def test_released_migration_cannot_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = database._FOLDER
    for file in original.iterdir():
        (tmp_path / file.name).write_bytes(file.read_bytes())
    with (tmp_path / "baseline.sql").open("a") as stream:
        stream.write("-- edited\n")
    monkeypatch.setattr(database, "_FOLDER", tmp_path)
    database.migrations.cache_clear()
    try:
        with pytest.raises(LauncherError, match="released migration baseline was edited"):
            database.migrations()
    finally:
        database.migrations.cache_clear()


@pytest.mark.parametrize(
    "sql,reason,floor",
    [
        ("ALTER TABLE pools ADD COLUMN note TEXT;", None, "0.4.0"),
        ("ALTER TABLE runs DROP COLUMN cohort;", None, "0.5.0"),
        ("CREATE TABLE extra(value TEXT);", "new JSON shape", "0.5.0"),
        ("ALTER TABLE pools ADD COLUMN note TEXT NOT NULL DEFAULT NULL;", None, "0.5.0"),
    ],
)
def test_release_records_floor(
    sql: str, reason: str | None, floor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = migrations()
    change = _migration(sql, floor=None).model_copy(update={"breaks": reason})
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    recorded = database.release("0.5.0")
    assert recorded[-1].floor == floor
    assert recorded[-1].sha256 == change.sha256
    assert recorded[:-1] == database.released()


def test_failing_takeover_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _released(tmp_path, "0.3.2")
    before = source.read_bytes()
    known = migrations()
    monkeypatch.setattr(
        database, "migrations", lambda: (*known, _migration("SELECT missing FROM nowhere;"))
    )
    with pytest.raises(LauncherError, match="migration extra failed"):
        open_home(tmp_path)
    assert not launcher_file(tmp_path).exists()
    assert source.read_bytes() == before
    assert not [path for path in tmp_path.glob(".launcher-*") if path.is_dir()]


def test_previous_release_worker_journal_decodes() -> None:
    finished = Finished.model_validate_json((FIXTURES / "finished-0.4.0.json").read_text())
    progress = finished.progress("attempt", datetime(2026, 11, 14, tzinfo=UTC))
    assert progress.outcome == "succeeded"
    assert progress.clocks == {"steps": 1400.0}
    assert progress.writers == ()


def test_editing_unreleased_migration_starts_new_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = open_home(tmp_path)
    known = migrations()
    first = _migration("CREATE TABLE extra(value TEXT);", floor=None)
    monkeypatch.setattr(database, "migrations", lambda: (*known, first))
    branch = open_home(tmp_path)
    branch.add_pool(Pool(name="branch"))
    original_path = branch._path
    branch.close()
    real.add_pool(Pool(name="later"))
    original = open_home(tmp_path)
    assert {pool.name for pool in original.pools()} == {"branch"}
    original.close()
    changed = _migration("CREATE TABLE extra(value TEXT, detail TEXT);", floor=None)
    monkeypatch.setattr(database, "migrations", lambda: (*known, changed))
    next_copy = open_home(tmp_path)
    assert next_copy._path != original_path
    assert {pool.name for pool in next_copy.pools()} == {"later"}
    next_copy.close()
    real.close()
