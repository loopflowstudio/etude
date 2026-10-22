"""An SSH executable stand-in runs the installed program in a separate process."""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.jobs import continue_from
from etude.launch.database import Database
from etude.launch.executor import Executor, local
from etude.launch.machines import OwnedMachine
from etude.launch.records import BootstrapLinks, Job, Machine, Pool, Run
from etude.launch.scheduler import Scheduler
from etude.launch.ssh import SSH, RemoteReply, SSHJournal, SSHStart
from etude.train.records import Step
from etude.train.run import track
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


def test_runs_move_between_local_and_remote_disk_including_kept_work(tmp_path: Path) -> None:
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
    command = (
        'echo "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/weights"\n'
        'printf \'{"clocks":{"steps":%s}}\' "$ETUDE_UNTIL_VALUE"'
        ' > "$ETUDE_OUTPUT_DIR/etude-result.json"'
    )
    ids = []
    for target, pool in (("3", None), ("7", "bench"), ("9", None)):
        job = Job(command=command, max_hours=0.005, max_usd=Decimal(1), until=f"steps={target}")
        ids.append(scheduler.launch(job, pool))
        (status,) = scheduler.wait(ids[-1], every=0.05, timeout=10)
        assert (status.outcome, status.clocks) == ("succeeded", {"steps": float(target)})
    for run, target in zip(ids, ("3\n", "7\n", "9\n"), strict=True):
        assert scheduler.fetch(run, "weights", tmp_path / run).read_text() == target


class CountedSSH(SSH):
    """SSH that counts the remote actions asked of the host."""

    asks: Counter[str]

    def call(
        self,
        action: str,
        session_id: str,
        *,
        packet: str = "",
        work: str = "",
        attempt: str = "",
        name: str = "",
        key: str = "",
        files: dict[str, str] | None = None,
        folder: dict[str, str] | None = None,
        bootstrap: BootstrapLinks | None = None,
        cleanup: tuple[str, ...] = (),
        startup_deadline: datetime | None = None,
    ) -> RemoteReply:
        self.asks[action] += 1
        return super().call(
            action,
            session_id,
            packet=packet,
            work=work,
            attempt=attempt,
            name=name,
            key=key,
            files=files,
            folder=folder,
            bootstrap=bootstrap,
            cleanup=cleanup,
            startup_deadline=startup_deadline,
        )


# Records a checkpoint midway and at the target; each holds the lineage of the
# checkpoint it continued from.
TRAIN = """
import os
from pathlib import Path
from urllib.parse import urlparse
from etude.train.records import Step
from etude.train.run import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
latest = run.latest_checkpoint()
start = 0 if latest is None else int(latest.at["steps"])
kept = "" if latest is None else Path(urlparse(latest.artifact.uri).path).read_text()
target = int(os.environ["ETUDE_UNTIL_VALUE"])
weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.pt"
for step in range(start + 1, target + 1):
    run.log({"loss": 1.0 / step}, steps=step)
    if step in ((start + target) // 2, target):
        weights.write_text(kept + f"trained to {step}\\n")
        run.checkpoint(weights, steps=step)
run.finish()
"""


def test_continuation_moves_between_local_and_remote_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    executable = tmp_path / "ssh"
    executable.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    executable.chmod(0o755)
    ssh = CountedSSH("bench", str(tmp_path / "bench/sessions"), str(executable), sys.executable)
    ssh.asks = Counter()
    remote = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    here = local(tmp_path / "here/sessions")
    store = Database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: remote, "local": lambda held: here})
    scheduler.add_pool(Pool(name="local"))
    store.add_machine(Machine(name="local", pool="local", kind="local"))
    scheduler.add_machine("bench", "bench")
    (tmp_path / "train.py").write_text(TRAIN)
    job = Job(
        command=f"{sys.executable} {tmp_path / 'train.py'}",
        until="steps=3",
        max_hours=0.1,
        max_usd=Decimal(1),
    )
    first = scheduler.launch(Run(job=job, seed=1))
    assert scheduler.wait(first, every=0.05, timeout=30)[0].outcome == "succeeded"

    # Local to bench: the parent's file is relayed to the bench, and only that file.
    ssh.asks.clear()
    continued = continue_from(scheduler.record(first), "steps=7", source=tmp_path / "here/track")
    second = scheduler.launch(continued, "bench")
    assert scheduler.wait(second, every=0.05, timeout=30)[0].outcome == "succeeded"
    assert scheduler.fetch(second, "weights.pt", tmp_path / "2").read_text() == (
        "trained to 3\ntrained to 7\n"
    )
    relayed = tmp_path / "bench/track" / first / "checkpoints"
    assert sorted(p.name for p in relayed.iterdir()) == ["steps-3.json", "steps-3.pt"]
    assert (ssh.asks["held"], ssh.asks["hold"]) == (0, 1)

    # `etude continue` here reads the parent's records from the bench; the
    # child runs in the parent's pool, beside its file.
    ssh.asks.clear()
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ETUDE_TRACK_DIR", str(tmp_path / "here/track"))
    monkeypatch.setattr("etude.launch.cli.default", lambda: scheduler)
    assert main(["continue", "--run", second, "--until", "steps=9"]) == 0
    third = capsys.readouterr().out.split()[0]
    assert scheduler.wait(third, every=0.05, timeout=30)[0].outcome == "succeeded"
    assert scheduler.fetch(third, "weights.pt", tmp_path / "3").read_text() == (
        "trained to 3\ntrained to 7\ntrained to 9\n"
    )
    assert scheduler.status(third).machine == "bench"
    assert (ssh.asks["held"], ssh.asks["hold"]) == (1, 0)
    assert not (tmp_path / "here/track" / second).exists()

    # Bench to local, from the parent's first checkpoint: this host gets only that file.
    ssh.asks.clear()
    earlier = continue_from(
        scheduler.record(second), "steps=9", source=tmp_path / "bench/track", ordinal=1
    )
    fourth = scheduler.launch(earlier)
    assert scheduler.wait(fourth, every=0.05, timeout=30)[0].outcome == "succeeded"
    assert scheduler.fetch(fourth, "weights.pt", tmp_path / "4").read_text() == (
        "trained to 3\ntrained to 5\ntrained to 9\n"
    )
    relayed = tmp_path / "here/track" / second / "checkpoints"
    assert sorted(p.name for p in relayed.iterdir()) == ["steps-5.json", "steps-5.pt"]
    assert (ssh.asks["held"], ssh.asks["hold"]) == (1, 0)


def test_a_runs_folder_relays_between_hosts_over_ssh(tmp_path: Path) -> None:
    executable = tmp_path / "ssh"
    executable.write_text('#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n')
    executable.chmod(0o755)
    run = track(
        experiment="lab",
        clocks=[Step("steps", "training steps")],
        run="r",
        directory=tmp_path / "a/track",
    )
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"weights at 1")
    run.checkpoint(weights, steps=1)
    run.finish()
    hosts = [
        SSHJournal(SSH(name, str(tmp_path / name / "sessions"), str(executable), sys.executable))
        for name in ("a", "b")
    ]
    hosts[1].hold("r", hosts[0].held("r"))
    there = tmp_path / "b/track/r"
    assert (there / "checkpoints/steps-1.pt").read_bytes() == b"weights at 1"
    assert sorted(p.name for p in (there / "checkpoints").iterdir()) == [
        ".stored",
        "steps-1.json",
        "steps-1.pt",
    ]
    assert (there / "run.json").read_bytes() == (tmp_path / "a/track/r/run.json").read_bytes()
