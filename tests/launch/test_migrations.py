"""Explicit schema changes preserve facts; ordinary reads cannot advance state."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch import database
from etude.launch.database import (
    Migration,
    additive,
    inspect,
    launcher_file,
    migrations,
    open_home,
    upgrade,
)
from etude.launch.errors import SchemaAdvanced
from etude.launch.provenance import frontier
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import Pool
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Finished
from etude.launch.server import _hand_over
from tests.launch.fake import bench

FIXTURES = Path(__file__).with_name("fixtures")


def _migration(sql: str, *, name: str = "extra", floor: str | None = "0.4.0") -> Migration:
    return Migration(
        name=name,
        after=("run_names",),
        breaks=None,
        sql=sql,
        sha256=hashlib.sha256(sql.encode()).hexdigest(),
        floor=floor,
    )


def test_inspect_and_open_missing_leave_home_absent(tmp_path: Path) -> None:
    home = tmp_path / "absent"
    state = inspect(home)
    assert state.applied == () and state.pending
    with pytest.raises(LauncherError, match="database upgrade"):
        open_home(home)
    assert not home.exists()


@pytest.mark.parametrize("journal", ["checkpointed", "live", "orphan"])
def test_inspection_preserves_records_and_reads_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    journal: str,
) -> None:
    home = tmp_path / "home"
    upgrade(home, development=True)
    path = launcher_file(home)
    with closing(sqlite3.connect(path)) as writer:
        if journal != "checkpointed":
            writer.execute("UPDATE migrations SET sha256='changed' WHERE name='run_names'")
            writer.commit()
        if journal == "orphan":
            home = tmp_path / "orphan"
            home.mkdir()
            for source in (path, path.with_name(path.name + "-wal")):
                shutil.copyfile(source, home / source.name)
        inspected = launcher_file(home)
        before = inspected.read_bytes()
        state = inspect(home)
        assert state.applied == tuple(m.name for m in migrations())
        assert state.problem == (
            None
            if journal == "checkpointed"
            else "applied migration content differs: run_names; "
            "restore the newest launcher.sqlite.backup-* or move the file away"
        )
        monkeypatch.setenv("ETUDE_HOME", str(home))
        assert main(["database", "status"]) == 0
        assert (state.problem or "compatible") in capsys.readouterr().out
        assert inspected.read_bytes() == before
        if state.problem:
            with pytest.raises(LauncherError, match=re.escape(state.problem)):
                open_home(home)
        else:
            open_home(home).close()


def test_inspection_uses_one_snapshot_while_compatible_writer_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    statements: list[str] = []
    read_only = database._read_only
    with closing(sqlite3.connect(launcher_file(tmp_path))) as writer:

        def read(path: Path, timeout: float = 5) -> sqlite3.Connection:
            connection = read_only(path, timeout)

            def statement(sql: str) -> None:
                statements.append(sql)
                if sql.startswith("SELECT name,sha256,floor"):
                    writer.execute("UPDATE migrations SET sha256='changed' WHERE name='run_names'")
                    writer.commit()

            connection.set_trace_callback(statement)
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(database, "_read_only", read)
            state = inspect(tmp_path)
        assert state.problem is None
        assert state.applied == tuple(m.name for m in migrations())
        assert len(statements) == 3
        assert statements[0] == "BEGIN"
        assert all(sql.startswith("SELECT") for sql in statements[1:])
        assert (inspect(tmp_path).problem or "").startswith(
            "applied migration content differs: run_names;"
        )


def test_explicit_initialization_and_idempotent_upgrade(tmp_path: Path) -> None:
    result = upgrade(tmp_path, development=True)
    assert result.applied == tuple(m.name for m in migrations())
    store = open_home(tmp_path)
    assert store.pool("local").name == "local"
    assert store.machine("local").kind == "local"
    store.close()
    assert not inspect(tmp_path).pending
    assert upgrade(tmp_path).applied == ()
    assert not (tmp_path / "copies").exists()


def test_cli_uses_python_outcome_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "cli"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    monkeypatch.setenv("ETUDE_SERVER", "unreachable-host")
    assert main(["database", "status"]) == 0
    assert "run etude database upgrade" in capsys.readouterr().out
    assert not home.exists()
    assert main(["database", "upgrade", "--development"]) == 0
    output = capsys.readouterr().out
    assert str(inspect(home).path) in output
    assert ", ".join(inspect(home).applied) in output


def test_open_pending_migration_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    path = launcher_file(tmp_path)
    before = path.read_bytes()
    known = migrations()
    monkeypatch.setattr(
        database, "migrations", lambda: (*known, _migration("CREATE TABLE extra(value TEXT);"))
    )
    with pytest.raises(LauncherError, match="database upgrade"):
        open_home(tmp_path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.backup-*"))


def test_unmerged_upgrade_refuses_operational_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    known = migrations()
    monkeypatch.setattr(
        database,
        "migrations",
        lambda: (*known, _migration("CREATE TABLE extra(value TEXT);", floor=None)),
    )
    before = launcher_file(tmp_path).read_bytes()
    with pytest.raises(LauncherError, match="unmerged or unproven"):
        upgrade(tmp_path)
    with pytest.raises(LauncherError, match="empty fixture"):
        upgrade(tmp_path, development=True)
    assert launcher_file(tmp_path).read_bytes() == before
    assert not (tmp_path / "copies").exists()


def test_compatible_branch_uses_same_canonical_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    known = migrations()
    monkeypatch.setattr(database, "migrations", lambda: known[:-1])
    with_open = open_home(tmp_path)
    with_open.add_pool(Pool(name="shared"))
    with_open.close()
    monkeypatch.setattr(database, "migrations", lambda: known)
    store = open_home(tmp_path)
    assert store.pool("shared").name == "shared"
    assert store._path == launcher_file(tmp_path)
    store.close()


def test_additive_upgrade_preserves_open_writer_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    older = open_home(tmp_path)
    known = migrations()
    change = _migration("ALTER TABLE pools ADD COLUMN note TEXT;")
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    result = upgrade(tmp_path)
    assert result.applied == ("extra",) and len(result.backups) == 1
    monkeypatch.setattr(database, "migrations", lambda: known)
    older.add_pool(Pool(name="after"))
    assert older.pool("after").name == "after"
    older.close()
    with sqlite3.connect(result.backups[0]) as backup:
        assert backup.execute("SELECT count(*) FROM migrations").fetchone()[0] == len(known)


def test_breaking_upgrade_fences_stale_writer_and_refuses_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    older = open_home(tmp_path)
    known = migrations()
    change = _migration("CREATE TABLE future(value TEXT);", floor="0.5.0")
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    monkeypatch.setattr(database, "version", lambda package: "0.5.0")
    upgrade(tmp_path)
    monkeypatch.setattr(database, "migrations", lambda: known)
    monkeypatch.setattr(database, "version", lambda package: "0.4.0")
    with pytest.raises(LauncherError, match="needs etude >= 0.5.0"):
        open_home(tmp_path)
    with pytest.raises(SchemaAdvanced):
        older.add_pool(Pool(name="forbidden"))
    assert "forbidden" not in {p.name for p in older.pools()}
    older.close()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE extra(value TEXT); SELECT missing FROM nowhere;",
        "UPDATE runs SET spec='invalid-private-content'; SELECT missing FROM nowhere;",
    ],
)
def test_failed_upgrade_preserves_schema_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    upgrade(tmp_path, development=True)
    known = migrations()
    change = _migration(sql).model_copy(update={"expansion": "run_names", "exports": ("runs",)})
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    with pytest.raises(LauncherError, match="migration extra failed") as error:
        upgrade(tmp_path)
    assert "invalid-private-content" not in str(error.value)
    assert inspect(tmp_path).applied == tuple(m.name for m in known)
    assert len(list(tmp_path.glob("*.backup-*Z"))) == 1


def test_program_archive_contains_sql_and_frontier() -> None:
    with zipfile.ZipFile(io.BytesIO(program_archive())) as archive:
        for migration in migrations():
            assert (
                archive.read(f"etude/launch/migrations/{migration.name}.sql").decode()
                == migration.sql
            )
        assert isinstance(json.loads(archive.read("etude/launch/migrations/merged.json")), dict)


def test_previous_release_worker_journal_decodes() -> None:
    finished = Finished.model_validate_json((FIXTURES / "finished-0.4.0.json").read_text())
    progress = finished.progress("attempt", datetime(2026, 11, 14, tzinfo=UTC))
    assert progress.outcome == "succeeded" and progress.clocks == {"steps": 1400.0}
    assert progress.writers == ()


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("ALTER TABLE runs ADD COLUMN note TEXT;", True),
        ("ALTER TABLE runs ADD COLUMN note TEXT NOT NULL DEFAULT '';", True),
        ("ALTER TABLE runs ADD COLUMN note TEXT NOT NULL;", False),
        ("ALTER TABLE runs DROP COLUMN resume_after;", False),
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


@pytest.mark.parametrize(
    "sql,reason,floor",
    [
        ("ALTER TABLE pools ADD COLUMN note TEXT;", None, "0.4.0"),
        ("ALTER TABLE runs DROP COLUMN resume_after;", None, "0.5.0"),
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
    assert recorded[-1].name == "extra"


def test_upgrade_allowance_covers_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    known = migrations()
    change = _migration("CREATE TABLE extra(value TEXT);")
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    writer = sqlite3.connect(launcher_file(tmp_path), isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(TimeoutError):
            upgrade(tmp_path, timeout=0.05)
        assert inspect(tmp_path).applied == tuple(m.name for m in known)
    finally:
        writer.close()


def test_contraction_exports_original_facts_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    store = open_home(tmp_path)
    store._db.execute(
        "INSERT INTO cohorts(name,parallel,created_at) VALUES ('retired',3,'2026-11-14T00:00:00Z')"
    )
    known = migrations()
    change = _migration(
        "ALTER TABLE runs DROP COLUMN cohort; DROP TABLE cohorts;", floor=None
    ).model_copy(
        update={
            "expansion": "run_names",
            "exports": ("cohorts", "runs"),
            "breaks": "cohort writers retired",
        }
    )
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    result = upgrade(tmp_path)
    assert len(result.exports) == 1
    facts = [json.loads(line) for line in result.exports[0].read_text().splitlines()]
    assert facts[0]["values"]["name"] == "retired" and facts[0]["values"]["parallel"] == 3
    snapshot = database.Snapshot.model_validate_json(
        result.backups[0].with_name(result.backups[0].name + ".json").read_text()
    )
    assert snapshot.applied == tuple(m.name for m in known) and snapshot.ledger_sha256
    monkeypatch.setattr(database, "migrations", lambda: known)
    with pytest.raises(SchemaAdvanced):
        store.add_pool(Pool(name="stale"))
    store.close()
    with pytest.raises(LauncherError, match="extra@"):
        open_home(tmp_path)


def test_scheduler_fence_stops_before_provider_actions(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.store._db.execute(
        "INSERT INTO migrations(name,sha256,applied_at,floor,program) VALUES ('future','digest','now','999.0.0',NULL)"
    )
    b.store._db.execute(f"PRAGMA user_version={len(migrations()) + 1}")
    with pytest.raises(SchemaAdvanced):
        Scheduler(b.store, {"runpod": lambda held: b.service}, b.clock).tick()
    assert sum(b.service.calls.values()) == 0


def test_read_open_uses_two_queries_without_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upgrade(tmp_path, development=True)
    connect = sqlite3.connect
    statements: list[str] = []

    def traced(
        path: str,
        *,
        uri: bool = False,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None,
        timeout: float = 5,
    ) -> sqlite3.Connection:
        connection = connect(path, uri=uri, isolation_level=isolation_level, timeout=timeout)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    open_home(tmp_path).close()
    assert len(statements) == 2
    assert all(sql.startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("tracking", [True, False], ids=["clone", "uv-main-pin"])
def test_source_checkout_frontier_reads_merged_sql_from_main(
    tmp_path: Path, tracking: bool
) -> None:
    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    upstream = tmp_path / "upstream"
    folder = upstream / "src" / "etude" / "launch" / "migrations"
    folder.mkdir(parents=True)
    (folder / "merged.sql").write_text("CREATE TABLE merged(value TEXT);\n")
    git("init", "-q", "-b", "main", cwd=upstream)
    git("add", ".", cwd=upstream)
    git("commit", "-qm", "merged", cwd=upstream)
    checkout = tmp_path / "checkout"
    git("clone", "-q", str(upstream), str(checkout), cwd=tmp_path)
    if not tracking:  # uv checks a pin out with refs/heads/main and no origin/* refs.
        git("update-ref", "-d", "refs/remotes/origin/HEAD", cwd=checkout)
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=checkout)
    branch = checkout / "src" / "etude" / "launch" / "migrations"
    (branch / "unmerged.sql").write_text("CREATE TABLE unmerged(value TEXT);\n")

    found = json.loads(frontier(checkout / "src" / "etude"))
    merged = (branch / "merged.sql").read_bytes()
    assert found == {"merged": hashlib.sha256(merged).hexdigest()}


def test_embedded_main_frontier_upgrades_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = migrations()
    change = _migration("CREATE TABLE merged_main(value TEXT);", floor=None)
    packaged = tmp_path / "package" / "etude"
    folder = packaged / "launch" / "migrations"
    folder.mkdir(parents=True)
    (folder / "merged.json").write_text(json.dumps({m.name: m.sha256 for m in (*known, change)}))
    monkeypatch.setattr(database, "__file__", str(packaged / "launch" / "database.py"))
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))

    def no_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("Git unavailable in installed client")

    monkeypatch.setattr(subprocess, "run", no_git)
    result = upgrade(tmp_path / "operational")
    assert "extra" in result.applied
    assert not inspect(tmp_path / "operational").pending
    assert not (tmp_path / "operational" / "copies").exists()


@pytest.mark.parametrize("program", [None, "/missing/etude-program"])
def test_handover_without_usable_program_exits_with_restart_instruction(
    program: str | None,
) -> None:
    with pytest.raises(LauncherError, match="restart etude server run"):
        _hand_over(program)


def test_refusal_lookup_upgrade_from_the_released_schema_preserves_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = migrations()
    released = tuple(m for m in known if m.floor is not None)
    with monkeypatch.context() as old:
        old.setattr(database, "migrations", lambda: released)
        upgrade(tmp_path)
        store = open_home(tmp_path)
        store.add_pool(Pool(name="preserved"))
        store._db.execute(
            "INSERT INTO sessions(id, machine, planned_at, refused_at) VALUES (?, ?, ?, ?)",
            ("old-refusal", "local", datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        store.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in known})
    result = upgrade(tmp_path)
    assert "provider_refusal_lookup" in result.applied
    store = open_home(tmp_path)
    assert store.pool("preserved").name == "preserved"
    assert store.finishing_sessions() == ()
    assert store.session("old-refusal").refusal is None
    assert store.session("old-refusal").refused_at is not None
    assert store.recent_refusal("missing", datetime.now(UTC)) is None
    store.close()
