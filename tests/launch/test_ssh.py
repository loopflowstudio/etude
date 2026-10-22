"""An SSH executable stand-in runs the installed program in a separate process."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from etude.launch.database import Database
from etude.launch.executor import Executor, local
from etude.launch.machines import OwnedMachine
from etude.launch.records import Job, Machine, Pool
from etude.launch.scheduler import Scheduler
from etude.launch.ssh import SSH, SSHJournal, SSHStart
from tests.launch.counting import job_text


def test_remote_host_runs_kept_work_logs_outputs_and_stops(tmp_path: Path) -> None:
    executable = tmp_path / "ssh"
    executable.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    executable.chmod(0o755)
    ssh = SSH("bench", str(tmp_path / "remote"), str(executable), sys.executable)
    journal = SSHJournal(ssh)
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), journal)
    scheduler = Scheduler(Database(tmp_path / "state.db"), {"ssh": lambda held: service})
    scheduler.add_machine("bench", "bench")
    run = scheduler.launch(
        Job(
            command='echo weights > "$ETUDE_OUTPUT_DIR/weights"; echo hello',
            max_hours=0.01,
            max_usd=Decimal(1),
        ),
        "bench",
    )
    (status,) = scheduler.wait(run, every=0.1, timeout=20)
    assert status.outcome == "succeeded"
    assert status.rental_ended is None and status.machine == "bench"
    assert job_text(scheduler.logs(run=run)) == "hello\n"
    assert scheduler.fetch(run, "weights", tmp_path / "out").read_text() == "weights\n"
    running = scheduler.launch(Job(command="sleep 30", max_hours=0.01, max_usd=Decimal(1)), "bench")
    scheduler.cancel(running)
    (cancelled,) = scheduler.wait(running, every=0.1, timeout=20)
    assert cancelled.outcome == "cancelled"
    # An unreachable host provides no evidence of death for a live session.
    pending = scheduler.queue(Job(command="sleep 30", max_hours=0.01, max_usd=Decimal(1)), "bench")
    scheduler.tick()
    held = scheduler.attempts(pending)[0].session
    executable.write_text("#!/bin/sh\nexit 255\n")
    scheduler.tick()
    assert scheduler._store.session(held).ended_at is None
    executable.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    scheduler.cancel(pending)
    assert scheduler.wait(pending, every=0.1, timeout=20)[0].outcome == "cancelled"


def test_continuation_moves_between_local_and_remote_disk_including_kept_work(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "ssh"
    executable.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    executable.chmod(0o755)
    ssh = SSH("bench", str(tmp_path / "remote"), str(executable), sys.executable)
    remote = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    here = local(tmp_path / "local")
    store = Database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: remote, "local": lambda held: here})
    scheduler.add_pool(Pool(name="local"))
    store.add_machine(Machine(name="local", pool="local", kind="local"))
    scheduler.add_machine("bench", "bench")
    command = '''cat "${ETUDE_CHECKPOINT:-/dev/null}" > "$ETUDE_OUTPUT_DIR/checkpoint"
echo "$ETUDE_UNTIL_VALUE" >> "$ETUDE_OUTPUT_DIR/checkpoint"
printf '{"clocks":{"steps":%s},"checkpoint":"checkpoint"}' "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"'''
    first = scheduler.launch(
        Job(command=command, max_hours=0.005, max_usd=Decimal(1), until="steps=3")
    )
    assert scheduler.wait(first, every=0.05, timeout=10)[0].outcome == "succeeded"
    second = scheduler.launch(scheduler.continue_from(first, "steps=7"), "bench")
    assert scheduler.wait(second, every=0.05, timeout=10)[0].outcome == "succeeded"
    third = scheduler.launch(scheduler.continue_from(second, "steps=9"))
    assert scheduler.wait(third, every=0.05, timeout=10)[0].outcome == "succeeded"
    assert scheduler.fetch(third, "checkpoint", tmp_path / "out").read_text() == "3\n7\n9\n"
