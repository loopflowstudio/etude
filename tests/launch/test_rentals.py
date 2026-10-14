"""Paid windows, acquisition history and allocations stay distinct to callers."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch import cli, client
from etude.launch.controller import PythonClient, Request
from etude.launch.database import Database
from etude.launch.errors import LauncherError
from etude.launch.records import Job, Machine
from tests.launch.counting import Counting, Counts
from tests.launch.fake import bench


def test_rent_refuses_an_added_pool_without_external_calls(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("bench", "fixture")
    counting = Counting(b.store, b.service)
    with counting.record(), pytest.raises(LauncherError, match="only machines you added"):
        b.scheduler.rent("bench", 1)
    assert counting.snapshot() == Counts(database=5)
    assert b.store.rentals() == ()
    assert b.store.sessions() == ()


def test_commands_rent_and_terminate_by_machine_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(cli, "default", lambda: b.scheduler)
    assert main(["rent", "--pool", "l4", "--hours", "1"]) == 0
    assert "rented until" in capsys.readouterr().out
    machine = b.scheduler.machines("l4", rented=True)[0]
    assert machine.kind == "rented" and machine.provider == "fake"
    assert machine.requested_at == b.clock.now
    assert machine.provider_id is not None and machine.address is None
    assert main(["pools"]) == 0
    assert "rented: " + machine.name in capsys.readouterr().out
    run = b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(1)), "l4")
    b.scheduler.tick()
    assert b.store.machine_busy(machine.name)
    assert main(["terminate", "--machine", machine.name]) == 1
    assert "still has a run on it" in capsys.readouterr().err
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    assert main(["terminate", "--machine", machine.name]) == 0
    capsys.readouterr()
    assert b.store.machine(machine.name).deleted_at is not None
    assert b.store.rentals()[0].deadline > b.clock.now
    assert not b.store.machine_busy(machine.name)


def test_python_rental_calls_use_selected_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[Request] = []
    machine = Machine(name="remote-pod", pool="gpu", kind="rented", provider="account")

    def request(self: PythonClient, value: Request) -> bytes:
        requests.append(value)
        return b'"remote-pod"' if value.operation == "rent" else machine.model_dump_json().encode()

    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setattr(client, "selected", lambda: "fixture-controller")
    monkeypatch.setattr(PythonClient, "_request", request)
    assert client.rent("gpu", 1, Decimal(2)) == "remote-pod"
    assert client.terminate("remote-pod") == machine
    assert [r.operation for r in requests] == ["rent", "terminate"]
    assert requests[0].pool == "gpu" and requests[0].max_usd == Decimal(2)
    assert requests[1].machine == "remote-pod"
    assert not (tmp_path / "launcher.db").exists()


def test_machine_allocation_outlives_its_session(tmp_path: Path) -> None:
    b = bench(tmp_path, max_on_demand=1)
    machine = b.scheduler.rent("l4", 1)
    run = b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(1)), "l4")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(run)[0]
    # Retained history can hold an unfinished allocation from an older session.
    # The current session's empty attempt list must not make that machine free.
    b.store._db.execute(
        """INSERT INTO sessions(id,pool,machine,planned_at,ended_at)
        SELECT 'earlier',pool,machine,planned_at,? FROM sessions WHERE id=?""",
        (b.clock.now.isoformat(), attempt.session),
    )
    b.store._db.execute("UPDATE attempts SET session='earlier' WHERE id=?", (attempt.id,))
    counting = Counting(b.store, b.service)
    with counting.record():
        assert b.scheduler._idle_sessions("l4", b.clock.now) == []
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
    with b.store.pool_lock("bench", b.clock) as acquired:
        assert acquired
        b.scheduler._plan(b.store.pool("bench"), b.clock.now)
    placed = b.scheduler.attempts(second)[0]
    assert placed.machine == "bench" and placed.session == attempt.session
    assert b.store.rental_for(b.store.session(placed.session)) is None
    assert b.service.creates == 0


def test_a_database_from_another_version_is_refused_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "launcher.db"
    Database(path).close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 0")
        before = tuple(db.iterdump())
    with pytest.raises(LauncherError, match="another version of etude"):
        Database(path)
    with sqlite3.connect(path) as db:
        assert tuple(db.iterdump()) == before
