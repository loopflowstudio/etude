"""Startup policy persists once and does not turn slowness into duplicate work."""

import os
import sys
import time
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Self

import pytest
from pydantic import ValidationError

from etude.launch.database import Database
from etude.launch.environment import install, startup_timeout
from etude.launch.errors import LaunchError
from etude.launch.logging import Console
from etude.launch.providers.package import program_archive
from etude.launch.providers.runpod import machines
from etude.launch.records import Job, Pool
from etude.launch.schemas import Observation, Packet
from etude.launch.ssh import SSH
from tests.launch.fake import bench
from tests.launch.test_runpod_machines import cloud


@pytest.mark.parametrize("seconds", [600, 30, None])
def test_startup_policy_survives_restart_and_ondemand_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds: float | None
) -> None:
    b = bench(tmp_path)
    b.scheduler.set_startup("l4", seconds)
    monkeypatch.setattr(b.service, "started", lambda session: False)
    monkeypatch.setattr(b.service, "observe", lambda session: Observation(alive=True, progress=()))
    run = b.scheduler.launch(
        Job(command="train", max_hours=1, max_usd=Decimal(1), max_attempts=1), rent="l4"
    )
    session = b.store.session(b.scheduler.attempts(run)[0].session)
    expected = None if seconds is None else b.clock() + timedelta(seconds=seconds)
    assert session.startup_deadline == expected
    b.scheduler.set_startup("l4", 1)
    reopened = Database(tmp_path / "launcher.db")
    assert reopened.session(session.id).startup_deadline == expected
    assert reopened.ondemand("l4").startup_seconds == 1
    b.run((seconds or 600) - 1)
    assert b.store.machine(session.machine).deleted_at is None
    b.run(2)
    assert (b.store.machine(session.machine).deleted_at is None) == (seconds is None)
    assert b.service.creates == 1


def test_startup_uses_rental_limit_even_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_STARTUP_DEADLINE", "")
    monkeypatch.delenv("ETUDE_DEADLINE", raising=False)
    assert startup_timeout() is None
    monkeypatch.setenv("ETUDE_DEADLINE", (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(TimeoutError, match="rental deadline"):
        startup_timeout()


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_startup_allowance_is_rejected(seconds: float) -> None:
    with pytest.raises(ValidationError):
        Pool(name="invalid", startup_seconds=seconds)


def test_install_output_arrives_before_exit_with_no_default_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "bootstrap.log"
    gate = tmp_path / "continue"
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\necho preparing\nwhile [ ! -e "{gate}" ]; do sleep 0.01; done\n')
    uv.chmod(0o700)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.setenv("ETUDE_LOG_PATH", str(log))
    monkeypatch.delenv("ETUDE_STARTUP_DEADLINE", raising=False)
    monkeypatch.delenv("ETUDE_DEADLINE", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: str(uv))
    elapsed = 0.0
    collect = Console.write

    def output(self: Console, body: bytes, *, final: bool = False) -> None:
        nonlocal elapsed
        collect(self, body, final=final)
        if b"preparing" in body:
            assert "preparing" in log.read_text()
            elapsed = 200
            gate.touch()

    monkeypatch.setattr("etude.launch.environment.time.monotonic", lambda: elapsed)
    monkeypatch.setattr(Console, "write", output)
    root = install(program_archive())
    assert (root / "ready").exists()
    assert "unlimited" in log.read_text() and "completed" in log.read_text()


def test_detached_ssh_install_is_readable_cancellable_and_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "continue"
    calls = tmp_path / "installs"
    uv = tmp_path / "uv"
    uv.write_text(
        f'#!/bin/sh\necho install >> "{calls}"\necho installing\n'
        f'while [ ! -e "{gate}" ]; do sleep 0.01; done\n'
        f'mkdir -p .venv/bin\nln -s "{sys.executable}" .venv/bin/python\n'
    )
    uv.chmod(0o700)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    ssh_script = tmp_path / "ssh"
    # Avoid a login shell resetting PATH in this executable stand-in.
    ssh_script.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    ssh_script.chmod(0o700)
    monkeypatch.setattr("etude.launch.ssh.login_shell", lambda command: command)
    root = tmp_path / "remote"
    ssh = SSH("fixture", str(root), str(ssh_script), sys.executable)
    packet = Packet(
        session="slow",
        deadline=None,
        work=(),
        startup_deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    try:
        assert (
            ssh.call(
                "start",
                packet.session,
                packet=packet.model_dump_json(),
                startup_deadline=packet.startup_deadline,
            ).text
            == "pending"
        )
        assert (
            ssh.call(
                "start",
                packet.session,
                packet=packet.model_dump_json(),
                startup_deadline=packet.startup_deadline,
            ).text
            == "pending"
        )
        latest = time.monotonic() + 5
        while not calls.exists() and time.monotonic() < latest:
            time.sleep(0.01)
        assert calls.read_text().splitlines() == ["install"]
        assert "starting uv sync" in (ssh.call("logs", packet.session, name="bootstrap").text or "")
        assert not ssh.call("started", packet.session).started
        assert ssh.call("end", packet.session).text is None
    finally:
        gate.touch()
    latest = time.monotonic() + 5
    while not (root / packet.session / "ended").exists() and time.monotonic() < latest:
        time.sleep(0.01)
    assert (root / packet.session / "ended").exists()
    assert not (root / packet.session / "receipt").exists()
    assert calls.read_text().splitlines() == ["install"]


@pytest.mark.parametrize("elapsed_seconds", [400, 700])
def test_delayed_preparation_cannot_create_after_authority_or_startup_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, elapsed_seconds: int
) -> None:
    b = cloud(tmp_path)
    origin = datetime.now(UTC)
    elapsed = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(origin.timestamp() + elapsed, tz=UTC)

    archive = program_archive

    def prepare(*, guardian: bool = False) -> bytes:
        nonlocal elapsed
        code = archive(guardian=guardian)
        elapsed = elapsed_seconds
        return code

    monkeypatch.setattr(machines, "program_archive", prepare)
    monkeypatch.setattr(machines, "datetime", Clock)
    monkeypatch.setattr(b.scheduler, "_clock", Clock.now)
    with pytest.raises(LaunchError):
        b.scheduler.launch(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
    assert b.provider.creates == 0
