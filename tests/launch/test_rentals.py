"""Paid windows, acquisition history and allocations stay distinct to callers."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch import cli, client
from etude.launch.controller import PythonClient, RentRequest, Request, TerminateRequest
from etude.launch.database import Database, launcher_file
from etude.launch.errors import LauncherError
from etude.launch.records import Job, Machine
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench


def test_rent_refuses_a_pool_without_external_calls(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("bench", "fixture")
    counting = Counting(b.store, b.service)
    with counting.record(), pytest.raises(LauncherError, match="no on-demand named 'bench'"):
        b.scheduler.rent("bench", 1)
    assert counting.snapshot() == Counts(database=5)
    assert b.store.rentals() == ()
    assert b.store.sessions() == ()


def test_commands_rent_and_terminate_by_machine_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(cli, "default", lambda: b.scheduler)
    assert main(["rent", "l4", "--hours", "1"]) == 0
    assert "rented until" in capsys.readouterr().out
    machine = b.scheduler.machines("l4")[0]
    assert machine.kind == "rented" and machine.provider == "fake"
    assert machine.claimed_at == b.clock.now
    assert machine.provider_id is not None and machine.address is None
    assert main(["ondemands"]) == 0
    assert "rented: " + machine.name in capsys.readouterr().out
    run = b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(1)), rent="l4")
    b.scheduler.tick()
    assert b.store.machine_busy(machine.name)
    assert main(["terminate", machine.name]) == 1
    assert "still has a run on it" in capsys.readouterr().err
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    assert main(["terminate", machine.name]) == 0
    capsys.readouterr()
    assert b.store.machine(machine.name).deleted_at is not None
    assert b.store.rentals()[0].deadline > b.clock.now
    assert not b.store.machine_busy(machine.name)


def test_python_rental_calls_use_selected_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[Request] = []
    machine = Machine(name="remote-pod", ondemand="gpu", kind="rented", provider="account")

    def request(self: PythonClient, value: Request) -> bytes:
        requests.append(value)
        return b'"remote-pod"' if value.operation == "rent" else machine.model_dump_json().encode()

    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setattr(client, "selected", lambda: "fixture-controller")
    monkeypatch.setattr(PythonClient, "_request", request)
    assert client.rent("gpu", 1, Decimal(2)) == "remote-pod"
    assert client.terminate("remote-pod") == machine
    assert requests == [
        RentRequest(ondemand="gpu", hours=1, max_usd=Decimal(2)),
        TerminateRequest(machine="remote-pod"),
    ]
    assert not (launcher_file(tmp_path)).exists()


def test_machine_allocation_outlives_its_session(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", 1)
    run = b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(1)), rent="l4")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(run)[0]
    # Retained history can hold an unfinished allocation from an older session.
    # The current session's empty attempt list must not make that machine free.
    b.store._db.execute(
        """INSERT INTO sessions(id,machine,planned_at,ended_at)
        SELECT 'earlier',machine,planned_at,? FROM sessions WHERE id=?""",
        (b.clock.now.isoformat(), attempt.session),
    )
    b.store._db.execute("UPDATE attempts SET session='earlier' WHERE id=?", (attempt.id,))
    ondemand = b.store.ondemand("l4")
    counting = Counting(b.store, b.service)
    with counting.record():
        assert b.scheduler._idle_sessions(ondemand, b.clock.now) == []
    assert counting.snapshot() == Counts(database=3)
    assert b.store.machine_busy(machine)


def test_added_session_reuses_capacity_without_a_paid_window(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("bench", "fixture")
    first = b.scheduler.queue(Job(command="train", max_hours=1), "bench")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(first)[0]
    # Recover after completion was recorded but before session cleanup happened.
    b.store.finish(attempt, b.clock.now, "succeeded")
    second = b.scheduler.queue(Job(command="train", max_hours=1), "bench")
    with b.store.lock("bench", b.clock) as acquired:
        assert acquired
        b.scheduler._plan(b.store.pool("bench"), b.clock.now)
    placed = b.scheduler.attempts(second)[0]
    assert placed.machine == "bench" and placed.session == attempt.session
    assert b.store.rental_for(b.store.session(placed.session)) is None
    assert b.service.creates == 0


@pytest.mark.parametrize("version", [10, 11, 12, 13, 14])
def test_old_database_evidence_is_refused_without_migration(tmp_path: Path, version: int) -> None:
    path = launcher_file(tmp_path)
    Database(path).close()
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX transfers_delivery")
        db.execute("INSERT INTO settings VALUES ('kept', 'historical evidence')")
        db.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(LauncherError, match="another version"):
        Database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (version,)
        assert db.execute("SELECT value FROM settings WHERE name = 'kept'").fetchone() == (
            "historical evidence",
        )
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='transfers_delivery'").fetchone()
            is None
        )


def test_a_database_from_another_version_is_refused_and_left_alone(tmp_path: Path) -> None:
    path = launcher_file(tmp_path)
    Database(path).close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 0")
        before = tuple(db.iterdump())
    with pytest.raises(LauncherError, match="another version of etude"):
        Database(path)
    with sqlite3.connect(path) as db:
        assert tuple(db.iterdump()) == before
