"""Explicit schema changes preserve facts; ordinary reads cannot advance state."""

from __future__ import annotations

import hashlib
import io
import json
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
from etude.launch.providers.package import program_archive
from etude.launch.records import Pool
from etude.launch.scheduler import Scheduler
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


def _ready(home: Path) -> None:
    upgrade(home, development=True)


def test_inspect_and_open_missing_leave_home_absent(tmp_path: Path) -> None:
    home = tmp_path / "absent"
    state = inspect(home)
    assert state.applied == () and state.pending
    with pytest.raises(LauncherError, match="database upgrade"):
        open_home(home)
    assert not home.exists()


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
    _ready(tmp_path)
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
    _ready(tmp_path)
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
    _ready(tmp_path)
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
    _ready(tmp_path)
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
    _ready(tmp_path)
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
    _ready(tmp_path)
    known = migrations()
    change = _migration(sql).model_copy(update={"expansion": "run_names", "exports": ("runs",)})
    monkeypatch.setattr(database, "migrations", lambda: (*known, change))
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in (*known, change)})
    with pytest.raises(LauncherError, match="migration extra failed") as error:
        upgrade(tmp_path)
    assert "invalid-private-content" not in str(error.value)
    assert inspect(tmp_path).applied == tuple(m.name for m in known)
    assert len(list(tmp_path.glob("*.backup-*Z"))) == 1


def test_legacy_evidence_never_becomes_empty_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    legacy = tmp_path / "launcher.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE evidence(value TEXT)")
        db.execute("PRAGMA user_version=13")
    db.close()
    before = legacy.read_bytes()
    assert inspect(tmp_path).sources == (legacy,)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        upgrade(tmp_path)
    assert legacy.read_bytes() == before
    assert not launcher_file(tmp_path).exists()


def test_development_refuses_copy_history(tmp_path: Path) -> None:
    copy = tmp_path / "copies" / "previous" / "launcher.db"
    copy.parent.mkdir(parents=True)
    with sqlite3.connect(copy) as db:
        db.execute("CREATE TABLE evidence(value TEXT)")
    with pytest.raises(LauncherError, match="empty fixture"):
        upgrade(tmp_path, development=True)
    assert inspect(tmp_path).sources == (copy,)


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


@pytest.mark.parametrize("version", ["0.3.2", "0.4.0"])
def test_explicit_takeover_retains_cohort_and_worker_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    source = tmp_path / "launcher.db"
    with sqlite3.connect(source) as old:
        old.executescript((FIXTURES / f"launcher-{version}.sql").read_text())
    old.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    result = upgrade(tmp_path)
    assert result.sources == (source,) and not source.exists()
    store = open_home(tmp_path)
    assert len(store.runs()) == 2 and len(store.sessions()) == 2
    assert len(store.rentals()) == 1
    assert store._db.execute("SELECT count(*) FROM cohorts").fetchone()[0] == 1
    assert (
        store._db.execute("SELECT cohort FROM runs WHERE cohort IS NOT NULL").fetchone()[0]
        == "sweep"
    )
    store.close()
    with sqlite3.connect(result.backups[0]) as original:
        assert original.execute("SELECT count(*) FROM runs").fetchone()[0] == 2


def test_shared_inherited_facts_are_imported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("launcher-17.db", "launcher.db"):
        with sqlite3.connect(tmp_path / name) as old:
            old.executescript((FIXTURES / "launcher-0.3.2.sql").read_text())
        old.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    result = upgrade(tmp_path)
    store = open_home(tmp_path)
    assert len(store.runs()) == 2 and len(store.rentals()) == 1
    assert len(result.sources) == 2 and len(result.backups) == 2
    store.close()


def test_old_receipts_and_retired_job_identity_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "launcher.db"
    with sqlite3.connect(source) as old:
        old.executescript((FIXTURES / "launcher-0.3.2.sql").read_text())
        old.execute("ALTER TABLE runs DROP COLUMN smoke")
        old.execute("ALTER TABLE sessions RENAME COLUMN provider_id TO receipt")
        old.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,identity TEXT NOT NULL)")
        old.execute("INSERT INTO jobs(id,identity) VALUES ('retired','{}')")
        old.execute("PRAGMA user_version=13")
    old.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    upgrade(tmp_path)
    store = open_home(tmp_path)
    assert store._db.execute("SELECT identity FROM jobs WHERE id='retired'").fetchone()[0] == "{}"
    assert all(a == b for a, b in store._db.execute("SELECT receipt,provider_id FROM sessions"))
    assert all(r.submission is not None and r.submission.smoke is not None for r in store.runs())
    store.close()


def test_upgrade_allowance_covers_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path)
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
    _ready(tmp_path)
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
    _ready(tmp_path)
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


def test_canonical_and_legacy_histories_join_one_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(tmp_path)
    current = open_home(tmp_path)
    current.add_pool(Pool(name="current"))
    current.close()
    legacy = tmp_path / "launcher-17.db"
    with sqlite3.connect(legacy) as old:
        old.executescript((FIXTURES / "launcher-0.3.2.sql").read_text())
    old.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    result = upgrade(tmp_path)
    assert len(result.sources) == 2 and len(result.backups) == 2
    assert not legacy.exists()
    combined = open_home(tmp_path)
    assert combined.pool("current").name == "current"
    assert len(combined.runs()) == 2 and len(combined.rentals()) == 1
    combined.close()


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("interruption", ["first", "second", "last", "publication"])
def test_interrupted_takeover_resumes_all_histories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    canonical: bool,
    interruption: str,
) -> None:
    if canonical:
        _ready(tmp_path)
        with closing(open_home(tmp_path)) as current:
            current.add_pool(Pool(name="current"))
    for name in ("launcher-17.db", "launcher.db"):
        with closing(sqlite3.connect(tmp_path / name)) as old:
            old.executescript((FIXTURES / "launcher-0.3.2.sql").read_text())
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    replace = Path.replace
    retired = 0

    def interrupted(path: Path, target: Path) -> Path:
        nonlocal retired
        result = replace(path, target)
        if "-retired-" in target.name:
            retired += 1
            if (
                (interruption == "first" and retired == 1)
                or (interruption == "second" and retired == 2)
                or (interruption == "last" and retired == (3 if canonical else 2))
            ):
                raise OSError("publication interrupted")
        if interruption == "publication" and target == launcher_file(tmp_path):
            raise OSError("publication interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupted)
        with pytest.raises(OSError, match="publication interrupted"):
            upgrade(tmp_path)
    state = inspect(tmp_path)
    assert state.takeover and state.takeover.is_file()
    assert state.problem and "unfinished launcher takeover" in state.problem
    assert database.has_launcher(tmp_path)
    with pytest.raises(LauncherError, match="unfinished launcher takeover"):
        open_home(tmp_path)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    assert main(["database", "status"]) == 0
    assert "keep scheduling stopped and rerun etude database upgrade" in capsys.readouterr().out
    with pytest.raises(LauncherError, match="empty fixture"):
        upgrade(tmp_path, development=True)
    with monkeypatch.context() as patch:
        patch.setattr(database, "_merged", lambda: {})
        with pytest.raises(LauncherError, match="unmerged or unproven"):
            upgrade(tmp_path)
    result = upgrade(tmp_path)
    assert len(result.sources) == (3 if canonical else 2)
    assert all(p.exists() for p in result.backups)
    assert not inspect(tmp_path).takeover
    assert not inspect(tmp_path).sources
    with closing(open_home(tmp_path)) as combined:
        assert len(combined.runs()) == 2
        assert len(combined.rentals()) == 1
        if canonical:
            assert combined.pool("current").name == "current"
    assert upgrade(tmp_path).applied == ()
    records = list((tmp_path / "recovery").glob("*/takeover.json"))
    assert len(records) == 1
    assert database.Takeover.model_validate_json(records[0].read_text()).published


@pytest.mark.parametrize(
    "changed",
    [
        "source",
        "target",
        "snapshot",
        "new_history",
        "canonical",
        "missing_snapshot",
        "missing_source",
        "sidecar",
    ],
)
def test_interrupted_takeover_preserves_disagreeing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    source = tmp_path / "launcher.db"
    with closing(sqlite3.connect(source)) as old:
        old.executescript((FIXTURES / "launcher-0.3.2.sql").read_text())
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in migrations()})
    replace = Path.replace

    def interrupted(path: Path, target: Path) -> Path:
        if "-retired-" in target.name:
            raise OSError("publication interrupted")
        return replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", interrupted)
        with pytest.raises(OSError):
            upgrade(tmp_path)
    state = inspect(tmp_path)
    assert state.takeover
    intent = database.Takeover.model_validate_json(state.takeover.read_text())
    victim = {
        "source": source,
        "target": intent.target.path,
        "snapshot": intent.evidence[0].path,
        "new_history": tmp_path / "launcher-99.db",
        "canonical": launcher_file(tmp_path),
        "missing_snapshot": intent.evidence[0].path,
        "missing_source": source,
        "sidecar": source.with_name(source.name + "-wal"),
    }[changed]
    if changed.startswith("missing_"):
        victim.unlink()
    else:
        victim.write_bytes(b"changed evidence")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(LauncherError, match="reconcile against recovery record"):
        upgrade(tmp_path)
    assert all(p.read_bytes() == body for p, body in before.items())
    assert inspect(tmp_path).takeover == state.takeover
