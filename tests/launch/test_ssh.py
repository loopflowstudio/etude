"""An SSH executable stand-in runs the installed program in a separate process."""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from etude.cli import main
from etude.jobs import continue_from
from etude.launch import ssh_boot
from etude.launch.aws import Keys
from etude.launch.errors import Refused
from etude.launch.executor import Executor, local
from etude.launch.openssh import ssh_command
from etude.launch.provisioner.machines import Allocation, OwnedMachine
from etude.launch.provisioner.providers.package import program_archive
from etude.launch.records import BootstrapLinks, Machine, Pool, RunAttempt, Session
from etude.launch.records import Storage as Place
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import AttemptRecord, Finished, Inbox, Packet, Work
from etude.launch.ssh import (
    SSH,
    HeldRequest,
    RemoteCall,
    RemoteReply,
    RemoteRequest,
    SessionRequest,
    SSHJournal,
    SSHStart,
    StartRequest,
    Unreachable,
)
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.disk import DirectoryJournal
from etude.launch.storage.files import write_bytes
from etude.launch.storage.s3 import Bucket
from etude.launch.worker import remote as remote_worker
from etude.records import Clock
from etude.spec import Job, RunRequest, Until
from tests.launch.counting import job_text
from tests.launch.fake import Clock as FakeClock
from tests.launch.fake import open_database
from tests.launch.test_runpod_machines import Storage
from tests.train.tracking import open_run

# The `ssh` stand-in runs the remote command here; `UNREACHABLE` replaces it.
RELAY = (
    "#!/bin/sh\n"
    'while [ "$#" -gt 0 ]; do\n'
    '    case "$1" in\n'
    "        -G) printf 'connecttimeout none\\nserveraliveinterval 0\\nserveralivecountmax 3\\n'; exit 0 ;;\n"
    "        -o) shift 2 ;;\n"
    "        --) shift; break ;;\n"
    "        -*) shift ;;\n"
    "        *) break ;;\n"
    "    esac\n"
    "done\n"
    "shift\n"
    'exec sh -c "$1"\n'
)
UNREACHABLE = "#!/bin/sh\nexit 255\n"


@pytest.fixture(autouse=True)
def ssh_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "ssh"
    executable.write_text(RELAY)
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", sys.executable)


@pytest.fixture
def exited_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


@contextlib.contextmanager
def _reaped_sleep() -> Iterator[tuple[int, Callable[[float], int]]]:
    """A live `sleep 60` reaped as soon as it exits, as init would on a remote host."""
    with subprocess.Popen(["sleep", "60"]) as proc:
        done = threading.Event()

        def _reap() -> None:
            proc.wait()
            done.set()

        reaper = threading.Thread(target=_reap, daemon=True)
        reaper.start()

        def wait_exit(timeout: float = 5.0) -> int:
            if not done.wait(timeout) or proc.returncode is None:
                raise TimeoutError(f"process {proc.pid} did not exit within {timeout} s")
            return proc.returncode

        try:
            yield proc.pid, wait_exit
        finally:
            if proc.poll() is None:
                proc.kill()
            reaper.join(timeout=5)


@pytest.fixture
def courier_pid() -> Iterator[int]:
    with _reaped_sleep() as (pid, _):
        yield pid


def _owned_host(
    tmp_path: Path, service: Executor | None = None, *, slots: int = 1
) -> tuple[Scheduler, Path]:
    """A scheduler with one owned SSH host, and the SSH stand-in that reaches it."""
    if service is None:
        ssh = SSH("bench", str(tmp_path / "remote"))
        service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    scheduler = Scheduler(open_database(tmp_path / "state.db"), {"ssh": lambda held: service})
    scheduler.add_pool(Pool(name="bench", slots=slots))
    scheduler.add_machine("bench", "bench", pool="bench")
    return scheduler, tmp_path / "ssh"


def test_remote_host_runs_kept_work_logs_outputs_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_STORAGE_TIMEOUT_SECONDS", "120")
    scheduler, executable = _owned_host(tmp_path)
    (run,) = scheduler.launch(
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
    assert job_text(scheduler.logs(refresh=True, run=run)) == "hello\n"
    assert scheduler.fetch(run, "weights", tmp_path / "out").read_text() == "weights\n"
    (running,) = scheduler.launch(
        Job(command="sleep 30", max_hours=0.01, max_usd=Decimal(1)), "bench"
    )
    scheduler.cancel(running)
    (cancelled,) = scheduler.wait(running, every=0.1, timeout=20)
    assert cancelled.outcome == "cancelled"
    # An unreachable host provides no evidence of death for a live session.
    (pending,) = scheduler.queue(
        Job(command="sleep 30", max_hours=0.01, max_usd=Decimal(1)), "bench"
    )
    scheduler.tick()
    held = scheduler.attempts(pending)[0].session
    executable.write_text(UNREACHABLE)
    scheduler.tick()
    assert scheduler._store.session(held).ended_at is None
    executable.write_text(RELAY)
    scheduler.cancel(pending)
    assert scheduler.wait(pending, every=0.1, timeout=20)[0].outcome == "cancelled"


def _echo(text: str) -> Job:
    return Job(command=f"echo {text}", max_hours=0.01, max_usd=Decimal(1))


def test_a_start_that_never_reached_an_owned_host_is_fenced_and_frees_it(tmp_path: Path) -> None:
    scheduler, executable = _owned_host(tmp_path)
    executable.write_text(UNREACHABLE)
    (lost,) = scheduler.queue(_echo("lost"), "bench")
    scheduler.tick()
    assert scheduler.read(lost).phase == "renting"
    session = scheduler.attempts(lost)[0].session
    executable.write_text(RELAY)
    scheduler.cancel(lost)
    (queued,) = scheduler.queue(_echo("queued"), "bench")
    (status,) = scheduler.wait(queued, every=0.1, timeout=20)
    assert status.outcome == "succeeded"
    assert scheduler.read(lost).outcome == "cancelled"
    # The fence stops a start still in flight from claiming the session later.
    home = tmp_path / "remote" / session
    assert (home / "cancel-start").exists() and (home / "ended").exists()
    assert not (home / "boot-claim").exists()


def test_a_lost_start_reply_during_install_is_recovered_and_cancel_aborts_it(
    tmp_path: Path,
) -> None:
    scheduler, executable = _owned_host(tmp_path, slots=2)
    executable.write_text(UNREACHABLE)
    (installing,) = scheduler.queue(_echo("installing"), "bench")
    scheduler.tick()
    home = tmp_path / "remote" / scheduler.attempts(installing)[0].session
    # The start reached the host before its reply was lost; its install holds the claim.
    home.mkdir(parents=True)
    (home / "boot-claim").touch()
    executable.write_text(RELAY)
    scheduler.tick()
    assert scheduler.read(installing).phase == "setting_up"
    scheduler.cancel(installing)
    (waiting,) = scheduler.queue(_echo("waiting"), "bench")
    scheduler.tick()
    assert (home / "cancel-start").exists()
    assert scheduler.read(waiting).phase == "queued"
    assert not scheduler.attempts(waiting)  # even the empty slot is closed until setup stops
    (home / "ended").write_text(datetime.now(UTC).isoformat())  # the aborted install's note
    (status,) = scheduler.wait(waiting, every=0.1, timeout=20)
    assert status.outcome == "succeeded"
    assert scheduler.attempts(waiting)[0].session != home.name
    assert scheduler.read(installing).outcome == "cancelled"


def test_a_setup_process_that_died_frees_its_owned_host(tmp_path: Path, exited_pid: int) -> None:
    scheduler, executable = _owned_host(tmp_path)
    executable.write_text(UNREACHABLE)
    (installing,) = scheduler.queue(_echo("installing"), "bench")
    scheduler.tick()
    home = tmp_path / "remote" / scheduler.attempts(installing)[0].session
    # The start reached the host, then its boot process was killed (OOM, reboot).
    home.mkdir(parents=True)
    (home / "boot-claim").write_text(str(exited_pid))
    executable.write_text(RELAY)
    (queued,) = scheduler.queue(_echo("queued"), "bench")
    (status,) = scheduler.wait(queued, every=0.1, timeout=20)
    assert status.outcome == "succeeded"
    first = scheduler.attempts(installing)[0]
    assert first.outcome == "lost" and first.cause == "machine_lost_before_start"
    assert (home / "ended").exists()
    assert scheduler.read(installing).outcome == "succeeded"  # retried once the host was free


class CountedSSH(SSH):
    """SSH that counts the remote actions asked of the host."""

    def __init__(self, address: str, root: str) -> None:
        super().__init__(address, root)
        self.asks: Counter[str] = Counter()

    def call(self, request: RemoteRequest) -> RemoteReply:
        self.asks[request.action] += 1
        return super().call(request)


def _bucket_host(ssh: SSH, storage: Storage) -> Executor:
    place = Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1")
    bucket = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage)
    return Executor(
        OwnedMachine("bench"), SSHStart(ssh), BucketJournal(place, bucket), control=SSHJournal(ssh)
    )


def test_a_bucket_backed_host_is_free_once_setup_before_any_receipt_fails_or_is_stopped(
    tmp_path: Path,
    exited_pid: int,
) -> None:
    service = _bucket_host(SSH("bench", str(tmp_path / "remote")), Storage())
    home = tmp_path / "remote" / "s1"
    home.mkdir(parents=True)
    (home / "boot-claim").write_text(str(os.getpid()))
    assert service.observe("s1").alive  # installing; storage has heard nothing yet
    (home / "boot-claim").write_text(str(exited_pid))
    assert not service.observe("s1").alive  # the install died without a note

    # Its courier fetches inputs after the boot process exits, then is stopped.
    with _reaped_sleep() as (pid, wait_exit):
        (home / "courier-pid").write_text(str(pid))
        assert service.observe("s1").alive
        assert service.end("s1") is not None
        assert wait_exit(10.0) == -signal.SIGTERM
        assert not service.observe("s1").alive


@pytest.mark.parametrize("legacy", [False, True], ids=["courier-pid", "v0.4.6"])
def test_bucket_host_stays_alive_through_final_delivery(
    tmp_path: Path, legacy: bool, exited_pid: int, courier_pid: int
) -> None:
    ssh = CountedSSH("bench", str(tmp_path))
    storage = Storage()
    service = _bucket_host(ssh, storage)
    home = tmp_path / "s1"
    home.mkdir()
    (home / "courier-claim").touch()
    if not legacy:
        (home / "courier-pid").write_text(str(courier_pid))
    (home / "pid").write_text(str(exited_pid))
    ended = datetime.now(UTC).isoformat()
    (home / "ended").write_text(ended)
    storage.objects["etude/sessions/s1/out/receipt"] = str(courier_pid).encode()

    assert service.observe("s1").alive
    assert ssh.asks == {"observe": 1}
    assert not (Path(os.environ["ETUDE_HOME"]) / "programs").exists()
    assert service.end("s1") is None  # all attempts delivered; the session log is still uploading
    assert ssh.asks == {"observe": 2, "end": 1}
    storage.objects["etude/sessions/s1/out/ended"] = ended.encode()
    assert not service.observe("s1").alive
    assert service.end("s1") == datetime.fromisoformat(ended)
    assert ssh.asks["observe"] == 2


def test_courier_lost_at_receipt_releases_the_owned_host(tmp_path: Path, exited_pid: int) -> None:
    storage = Storage()
    service = _bucket_host(SSH("bench", str(tmp_path / "remote")), storage)
    scheduler, executable = _owned_host(tmp_path, service)
    executable.write_text(UNREACHABLE)
    (run,) = scheduler.queue(_echo("lost"), "bench")
    scheduler.tick()
    session = scheduler.attempts(run)[0].session
    home = tmp_path / "remote" / session
    home.mkdir(parents=True)
    (home / "boot-claim").write_text(str(exited_pid))
    (home / "courier-pid").write_text(str(exited_pid))
    storage.objects[f"etude/sessions/{session}/out/receipt"] = str(exited_pid).encode()
    executable.write_text(RELAY)

    scheduler.tick()  # recover the start whose SSH reply was lost
    scheduler.tick()  # reconcile the bucket receipt with the dead courier
    (attempt,) = scheduler.attempts(run)
    assert attempt.cause == "create_unconfirmed"
    assert scheduler.session(session).ended_at is not None
    assert (home / "cancel-start").exists() and (home / "ended").exists()


def test_queued_work_waits_for_the_bucket_hosts_last_upload(
    tmp_path: Path, exited_pid: int, courier_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = CountedSSH("bench", str(tmp_path / "remote"))
    storage = Storage()
    scheduler, _ = _owned_host(tmp_path, _bucket_host(ssh, storage))
    # The controlled host accepts work; advance its last upload independently of the scheduler.
    monkeypatch.setattr(SSHStart, "start", lambda *args: None)
    (run,) = scheduler.queue(_echo("first"), "bench")
    scheduler.tick()
    (attempt,) = scheduler.attempts(run)
    home = tmp_path / "remote" / attempt.session
    home.mkdir(parents=True)
    now = datetime.now(UTC)
    (home / "courier-pid").write_text(str(courier_pid))
    (home / "pid").write_text(str(exited_pid))
    (home / "ended").write_text(now.isoformat())
    out = f"etude/sessions/{attempt.session}/out"
    storage.objects[f"{out}/receipt"] = str(courier_pid).encode()
    record = AttemptRecord(
        started_at=now,
        exited=Finished(
            finished_at=now, outcome="succeeded", cause=None, clocks={}, outputs=(), exit_code=0
        ),
        delivered=True,
    )
    storage.objects[f"etude/{run}/attempts/{attempt.id}.json"] = record.model_dump_json().encode()
    (queued,) = scheduler.queue(_echo("next"), "bench")
    ssh.asks.clear()
    scheduler.tick()
    assert scheduler.read(run).outcome == "succeeded"
    assert scheduler.read(queued).phase == "queued"
    assert scheduler.session(attempt.session).ended_at is None
    assert ssh.asks == {"observe": 2, "end": 1}

    ssh.asks.clear()
    storage.objects[f"{out}/ended"] = now.isoformat().encode()
    scheduler.tick()
    (next_attempt,) = scheduler.attempts(queued)
    assert next_attempt.machine == "bench" and next_attempt.session != attempt.session
    assert scheduler.session(attempt.session).ended_at is not None
    assert ssh.asks == {"end": 1}


def test_dead_courier_leaves_its_worker_to_host_cleanup(tmp_path: Path, exited_pid: int) -> None:
    storage = Storage()
    service = _bucket_host(SSH("bench", str(tmp_path)), storage)
    home = tmp_path / "s1"
    home.mkdir()
    (home / "courier-pid").write_text(str(exited_pid))
    storage.objects["etude/sessions/s1/out/receipt"] = str(exited_pid).encode()
    with _reaped_sleep() as (pid, wait_exit):
        (home / "pid").write_text(str(pid))
        assert not service.observe("s1").alive
        assert service.end("s1") is not None
        assert wait_exit(5.0) == -signal.SIGTERM
    assert (home / "ended").exists()


def test_runs_move_between_local_and_remote_disk_including_kept_work(tmp_path: Path) -> None:
    ssh = SSH("bench", str(tmp_path / "remote"))
    remote = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    here = local(tmp_path / "local")
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: remote, "local": lambda held: here})
    scheduler.add_pool(Pool(name="local"))
    store.add_machine(Machine(name="local", pool="local", kind="local"))
    scheduler.add_machine("bench", "bench")
    command = (
        'echo "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/weights"\n'
        'printf \'{"clocks":{"steps":%s}}\' "$ETUDE_UNTIL_VALUE"'
        ' > "$ETUDE_OUTPUT_DIR/etude-result.json"'
    )
    ids: list[str] = []
    for target, pool in (("3", None), ("7", "bench"), ("9", None)):
        job = Job(
            command=command,
            max_hours=0.005,
            max_usd=Decimal(1),
            until=Until.parse(f"steps={target}"),
        )
        ids.extend(scheduler.launch(job, pool))
        (status,) = scheduler.wait(ids[-1], every=0.05, timeout=10)
        assert (status.outcome, status.clocks) == ("succeeded", {"steps": float(target)})
    for run, target in zip(ids, ("3\n", "7\n", "9\n"), strict=True):
        assert scheduler.fetch(run, "weights", tmp_path / run).read_text() == target


# Records a checkpoint midway and at the target; each holds the lineage of the
# checkpoint it continued from.
TRAIN = """
import os
from pathlib import Path
from etude.records import Clock
from etude.train.tracking import track
run = track(clocks=[Clock(name='steps', kind='step', meaning='training steps')])
latest = (run._checkpoint_here(_saved_checkpoint) if (_saved_checkpoint := run._latest_record()) is not None else None)
start = 0 if latest is None else int(latest.at["steps"])
kept = "" if latest is None else latest.path.read_text()
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
    ssh = CountedSSH("bench", str(tmp_path / "bench/sessions"))
    remote = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    here = local(tmp_path / "here/sessions")
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: remote, "local": lambda held: here})
    scheduler.add_pool(Pool(name="local"))
    store.add_machine(Machine(name="local", pool="local", kind="local"))
    scheduler.add_machine("bench", "bench")
    (tmp_path / "train.py").write_text(TRAIN)
    job = Job(
        experiment="lab",
        command=f"{sys.executable} {tmp_path / 'train.py'}",
        until=Until.parse("steps=3"),
        max_hours=0.1,
        max_usd=Decimal(1),
    )
    (first,) = scheduler.launch(RunRequest(job=job, seed=1))
    assert scheduler.wait(first, every=0.05, timeout=30)[0].outcome == "succeeded"

    # Local to bench: the parent's file is relayed to the bench, and only that file.
    ssh.asks.clear()
    continued = continue_from(scheduler.record(first), "steps=7", source=tmp_path / "here/track")
    (second,) = scheduler.launch(continued, "bench")
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
    monkeypatch.setattr("etude.launch.client.default", lambda: scheduler)
    monkeypatch.setattr(
        "etude.controller.advance.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: scheduler.standing(experiment)
    )
    monkeypatch.setattr("etude.launch.client._client", lambda: scheduler)
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
    (fourth,) = scheduler.launch(earlier)
    assert scheduler.wait(fourth, every=0.05, timeout=30)[0].outcome == "succeeded"
    assert scheduler.fetch(fourth, "weights.pt", tmp_path / "4").read_text() == (
        "trained to 3\ntrained to 5\ntrained to 9\n"
    )
    relayed = tmp_path / "here/track" / second / "checkpoints"
    assert sorted(p.name for p in relayed.iterdir()) == ["steps-5.json", "steps-5.pt"]
    assert (ssh.asks["held"], ssh.asks["hold"]) == (1, 0)


def test_a_runs_folder_relays_between_hosts_over_ssh(tmp_path: Path) -> None:
    run = open_run(
        experiment="lab",
        clocks=[Clock(name="steps", kind="step", meaning="training steps")],
        run="r",
        directory=tmp_path / "a/track",
    )
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"weights at 1")
    run.checkpoint(weights, steps=1)
    run.finish()
    hosts = [SSHJournal(SSH(name, str(tmp_path / name / "sessions"))) for name in ("a", "b")]
    hosts[1].hold("lab/r", hosts[0].held("lab/r"))
    there = tmp_path / "b/track/lab/r"
    assert (there / "checkpoints/steps-1.pt").read_bytes() == b"weights at 1"
    assert sorted(p.name for p in (there / "checkpoints").iterdir()) == [
        ".stored",
        "steps-1.json",
        "steps-1.pt",
    ]
    assert (there / "run.json").read_bytes() == (tmp_path / "a/track/lab/r/run.json").read_bytes()
    # A file the host does not hold is absent, not unreachable.
    with pytest.raises(FileNotFoundError, match="steps-9.pt"):
        hosts[0].held("r", "checkpoints/steps-9.pt")


def test_ssh_command_adds_liveness_defaults_only_where_unset_and_probes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = tmp_path / "probes"
    script = tmp_path / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if '-G' in sys.argv:\n"
        f"    with open({str(probes)!r}, 'a') as f: f.write(sys.argv[-1] + '\\n')\n"
        "    host = sys.argv[-1]\n"
        "    if host == 'unset-host':\n"
        "        print('connecttimeout none')\n"
        "        print('serveraliveinterval 0')\n"
        "        print('serveralivecountmax 3')\n"
        "    elif host == 'custom-host':\n"
        "        print('connecttimeout 15')\n"
        "        print('serveraliveinterval 45')\n"
        "        print('serveralivecountmax 3')\n"
        "    elif host == 'partial-host':\n"
        "        print('connecttimeout 12')\n"
        "        print('serveraliveinterval 0')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    assert ssh_command("unset-host", "true", batch=True) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=30",
        "-o",
        "ServerAliveInterval=20",
        "--",
        "unset-host",
        "true",
    ]
    assert ssh_command("custom-host", "true", batch=False) == [
        "ssh",
        "--",
        "custom-host",
        "true",
    ]
    assert ssh_command("partial-host", "true", batch=True) == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=20",
        "--",
        "partial-host",
        "true",
    ]
    # Repeated calls reuse the per-address probe without invoking `ssh -G` again.
    ssh_command("unset-host", "true")
    ssh_command("custom-host", "true", batch=True)
    ssh_command("partial-host", "true")
    assert probes.read_text().splitlines() == ["unset-host", "custom-host", "partial-host"]


def test_ssh_observe_and_steady_calls_omit_program_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "payloads.jsonl"
    script = tmp_path / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == '-G':\n"
        "    print('connecttimeout none\\nserveraliveinterval 0')\n"
        "    raise SystemExit(0)\n"
        "while len(args) >= 2 and args[0] == '-o':\n"
        "    args = args[2:]\n"
        "if args and args[0] == '--':\n"
        "    args = args[1:]\n"
        "body = sys.stdin.buffer.read()\n"
        "parsed = json.loads(body)\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'action': parsed['request']['action'], 'has_program': 'program' in parsed}) + '\\n')\n"
        "raise SystemExit(subprocess.run(args[1], shell=True, input=body).returncode)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))

    ssh = SSH("bench", str(tmp_path / "remote"))
    home = tmp_path / "remote" / "s1"
    home.mkdir(parents=True)
    (home / "boot-claim").write_text(str(os.getpid()))
    # Fast-path observe needs neither program nor installation.
    assert ssh.call(SessionRequest(action="observe", session="s1")).observation is not None
    # First non-fast-path call gets exit 97 without program, then retries with program.
    assert ssh.call(HeldRequest(run="missing", key="checkpoints/missing.pt")).missing is not None
    # Once installed, subsequent non-fast-path calls omit the program archive.
    assert ssh.call(HeldRequest(run="missing", key="checkpoints/missing.pt")).missing is not None

    recorded = [json.loads(line) for line in log.read_text().splitlines()]
    assert recorded == [
        {"action": "observe", "has_program": False},
        {"action": "held", "has_program": False},
        {"action": "held", "has_program": True},
        {"action": "held", "has_program": False},
    ]


def test_wait_backs_off_while_steady_and_resets_on_state_change(
    tmp_path: Path, courier_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh = CountedSSH("bench", str(tmp_path / "remote"))
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    scheduler = Scheduler(
        open_database(tmp_path / "state.db"),
        {"ssh": lambda held: service},
        clock=clock,
        sleep=sleep,
    )
    scheduler.add_pool(Pool(name="bench"))
    scheduler.add_machine("bench", "bench", pool="bench")
    monkeypatch.setattr(SSHStart, "start", lambda *args: None)
    (run,) = scheduler.queue(_echo("steady"), "bench")
    scheduler.tick()
    attempt = scheduler.attempts(run)[0]
    session = attempt.session
    home = tmp_path / "remote" / session
    attempt_dir = home / "attempts" / attempt.id
    attempt_dir.mkdir(parents=True)
    (home / "pid").write_text(str(courier_pid))
    (attempt_dir / "claim").write_text(clock().isoformat())
    scheduler.tick()  # enter steady running state before measuring backoff

    ssh.asks.clear()
    with pytest.raises(TimeoutError):
        scheduler.wait(run, timeout=120)
    # 120 s steady wait: <= 16 passes and <= 16 SSH observe calls, backing off to 10 s.
    assert len(sleeps) <= 15
    assert ssh.asks["observe"] <= 16
    assert sleeps[:5] == [1.0, 2.0, 4.0, 8.0, 10.0]

    # A state change (running -> stopping via cancel) returns backoff to 1 s.
    sleeps.clear()
    scheduler.cancel(run)
    # After the second sleep, finish the session so wait returns cleanly.
    real_sleep = sleep

    def sleep_then_end(seconds: float) -> None:
        real_sleep(seconds)
        if len(sleeps) == 2:
            (home / "ended").write_text(clock().isoformat())

    scheduler._sleep = sleep_then_end
    (done,) = scheduler.wait(run, timeout=60)
    assert done.outcome == "cancelled"
    assert sleeps[0] == 1.0


def test_start_claim_write_failure_leaves_no_claim_and_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "enospc-python"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import errno, os, sys\n"
        "real_open = os.open\n"
        "real_write = os.write\n"
        "claim_fds = set()\n"
        "def failing_open(path, flags, *args, **kwargs):\n"
        "    fd = real_open(path, flags, *args, **kwargs)\n"
        "    if str(path).endswith('boot-claim'):\n"
        "        claim_fds.add(fd)\n"
        "    return fd\n"
        "def failing_write(fd, data):\n"
        "    if fd in claim_fds:\n"
        "        raise OSError(errno.ENOSPC, 'No space left on device')\n"
        "    return real_write(fd, data)\n"
        "os.open = failing_open\n"
        "os.write = failing_write\n"
        "assert sys.argv[1] == '-c'\n"
        "exec(compile(sys.argv[2], '<boot>', 'exec'), {'__name__': '__main__'})\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", str(wrapper))
    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    packet = Packet(session="full", deadline=None, work=())
    reply = ssh.call(StartRequest(session="full", packet=packet))
    assert reply.refused is not None and "No space left on device" in reply.refused
    with pytest.raises(Refused, match="No space left on device"):
        SSHStart(ssh).start(
            Allocation(id="full", address="bench", created_at=datetime.now(UTC)), packet, None
        )
    assert not (root / "full" / "boot-claim").exists()
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", sys.executable)
    assert not ssh.call(SessionRequest(action="claimed", session="full")).claimed


@pytest.mark.parametrize("fail_at", ["mkdir", "claim_write"])
def test_full_disk_start_settles_as_refused_in_one_ssh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    root = tmp_path / "remote"
    if fail_at == "mkdir":
        # A read-only parent makes `home.mkdir` fail with PermissionError/OSError before claim.
        root.mkdir()
        root.chmod(0o500)
    else:
        wrapper = tmp_path / "enospc-python"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import errno, os, sys\n"
            "real_open = os.open\n"
            "real_write = os.write\n"
            "claim_fds = set()\n"
            "def failing_open(path, flags, *args, **kwargs):\n"
            "    fd = real_open(path, flags, *args, **kwargs)\n"
            "    if str(path).endswith('boot-claim'):\n"
            "        claim_fds.add(fd)\n"
            "    return fd\n"
            "def failing_write(fd, data):\n"
            "    if fd in claim_fds:\n"
            "        raise OSError(errno.ENOSPC, 'No space left on device')\n"
            "    return real_write(fd, data)\n"
            "os.open = failing_open\n"
            "os.write = failing_write\n"
            "assert sys.argv[1] == '-c'\n"
            "exec(compile(sys.argv[2], '<boot>', 'exec'), {'__name__': '__main__'})\n"
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("ETUDE_REMOTE_PYTHON", str(wrapper))

    actions: list[str] = []
    real_call = SSH.call

    def tracked_call(self: SSH, request: RemoteRequest) -> RemoteReply:
        actions.append(request.action)
        return real_call(self, request)

    monkeypatch.setattr(SSH, "call", tracked_call)
    try:
        scheduler, _ = _owned_host(tmp_path)
        (run,) = scheduler.queue(_echo("full-disk"), "bench")
        scheduler.tick()
        assert actions == ["start"]
        (attempt,) = scheduler.attempts(run)
        assert attempt.outcome == "lost" and attempt.cause == "capacity_refused"
        session = scheduler.session(attempt.session)
        assert session.refused_at is not None
        assert session.refusal is not None
        assert session.finish_requested_at is None
        assert session.ended_at is None
    finally:
        if root.exists():
            root.chmod(0o700)


def test_failed_ssh_keeps_sanitized_last_stderr_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_API_KEY", "supersecret999")
    script = tmp_path / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-G" ]; then printf "connecttimeout none\\nserveraliveinterval 0\\n"; exit 0; fi\n'
        "echo 'first warning line' >&2\n"
        "echo 'fatal: https://bucket.invalid/obj?sig=secret key=supersecret999' >&2\n"
        "exit 255\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    ssh = SSH("bench", str(tmp_path / "remote"))
    with pytest.raises(Unreachable) as raised:
        ssh.call(SessionRequest(action="claimed", session="s1"))
    message = str(raised.value)
    assert "fatal:" in message
    assert "[redacted URL]" in message
    assert "supersecret999" not in message
    assert "bucket.invalid" not in message


def test_old_empty_boot_claim_observes_not_alive_and_ends_lost(tmp_path: Path) -> None:
    scheduler, executable = _owned_host(tmp_path)
    executable.write_text(UNREACHABLE)
    (stuck,) = scheduler.queue(_echo("stuck"), "bench")
    scheduler.tick()
    session = scheduler.attempts(stuck)[0].session
    home = tmp_path / "remote" / session
    home.mkdir(parents=True)
    claim = home / "boot-claim"
    claim.touch()
    executable.write_text(RELAY)
    ssh = SSH("bench", str(tmp_path / "remote"))
    # Fresh empty claim (as from an older worker mid-claim) still counts as booting.
    assert ssh.call(SessionRequest(action="observe", session=session)).observation is not None
    assert ssh.call(SessionRequest(action="observe", session=session)).observation.alive  # type: ignore[union-attr]
    # Once older than 60 s, an empty claim is dead and the session settles as lost.
    old = time.time() - 120
    os.utime(claim, (old, old))
    obs = ssh.call(SessionRequest(action="observe", session=session)).observation
    assert obs is not None and not obs.alive
    scheduler.tick()
    scheduler.tick()
    first = scheduler.attempts(stuck)[0]
    assert first.outcome == "lost" and first.cause == "machine_lost_before_start"
    assert scheduler.session(session).ended_at is not None


def test_end_signals_boot_claim_pid_and_returns_ended_at_when_ended_unwritable(
    tmp_path: Path, exited_pid: int
) -> None:
    root = tmp_path / "remote"
    journal = SSHJournal(SSH("bench", str(root)))
    # Live boot-claim PID is signalled with SIGTERM and confirmed ended in one RPC.
    booting = root / "s_boot"
    booting.mkdir(parents=True)
    with _reaped_sleep() as (pid, wait_exit):
        (booting / "boot-claim").write_text(str(pid))
        assert journal.end("s_boot") is not None
        assert wait_exit(5.0) == -signal.SIGTERM
        assert (booting / "ended").exists()
    # When boot-claim exists (fencing duplicate starts) and the disk is unwritable,
    # end tolerates unwritable cancel-start and ended files and still answers ended_at.
    full = root / "s_full"
    full.mkdir(parents=True)
    (full / "boot-claim").write_text(str(exited_pid))
    full.chmod(0o500)
    try:
        ended_at = journal.end("s_full")
        assert ended_at is not None
        assert not (full / "cancel-start").exists()
        assert not (full / "ended").exists()
    finally:
        full.chmod(0o700)
    # Without boot-claim or pid, cancel-start remains strict so an unconfirmed start stays fenced.
    unclaimed = root / "s_unclaimed"
    unclaimed.mkdir(parents=True)
    unclaimed.chmod(0o500)
    try:
        with pytest.raises(Unreachable):
            journal.end("s_unclaimed")
    finally:
        unclaimed.chmod(0o700)


def test_ssh_command_reprobes_when_home_or_ssh_config_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = tmp_path / "probes"
    script = tmp_path / "bin" / "ssh"
    script.parent.mkdir()
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$HOME|$3" >> {probes}\n'
        'if [ -f "$HOME/.ssh/config" ] && grep -q custom "$HOME/.ssh/config"; then\n'
        '    printf "connecttimeout 15\\nserveraliveinterval 45\\n"\n'
        "else\n"
        '    printf "connecttimeout none\\nserveraliveinterval 0\\n"\n'
        "fi\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(script.parent) + os.pathsep + os.environ["PATH"])

    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    (home_a / ".ssh").mkdir(parents=True)
    (home_b / ".ssh").mkdir(parents=True)
    config_a = home_a / ".ssh" / "config"
    config_a.write_text("# empty\n")

    monkeypatch.setenv("HOME", str(home_a))
    first = ssh_command("lab-host", "true")
    second = ssh_command("lab-host", "true")
    assert (
        first
        == second
        == [
            "ssh",
            "-o",
            "ConnectTimeout=30",
            "-o",
            "ServerAliveInterval=20",
            "--",
            "lab-host",
            "true",
        ]
    )
    assert len(probes.read_text().splitlines()) == 1

    # Editing ~/.ssh/config bumps its mtime and invalidates the cache entry.
    config_a.write_text("# custom\n")
    bumped = config_a.stat().st_mtime_ns + 1_000_000_000
    os.utime(config_a, ns=(bumped, bumped))
    edited = ssh_command("lab-host", "true")
    assert edited == ["ssh", "--", "lab-host", "true"]
    assert len(probes.read_text().splitlines()) == 2

    # Switching HOME probes for the new home directory.
    monkeypatch.setenv("HOME", str(home_b))
    other = ssh_command("lab-host", "true")
    assert other == [
        "ssh",
        "-o",
        "ConnectTimeout=30",
        "-o",
        "ServerAliveInterval=20",
        "--",
        "lab-host",
        "true",
    ]
    assert len(probes.read_text().splitlines()) == 3


def test_refusing_owned_host_cools_down_and_yields_to_healthy_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_root = tmp_path / "full"
    full_root.mkdir()
    full_root.chmod(0o500)
    healthy_root = tmp_path / "healthy"
    full_ssh = CountedSSH("full-host", str(full_root))
    healthy_ssh = CountedSSH("healthy-host", str(healthy_root))
    services = {
        "full-host": Executor(OwnedMachine("full-host"), SSHStart(full_ssh), SSHJournal(full_ssh)),
        "healthy-host": Executor(
            OwnedMachine("healthy-host"), SSHStart(healthy_ssh), SSHJournal(healthy_ssh)
        ),
    }
    clock = FakeClock()
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(
        store,
        {"ssh": lambda held: services[held.name]},
        clock=clock,
        sleep=sleep,
    )
    scheduler.add_pool(Pool(name="bench"))
    scheduler.add_machine("full-host", "full-host", pool="bench")
    try:
        # Part 1: lone refusing owned host retries only after 60 s and lets wait back off.
        (run,) = scheduler.queue(_echo("retry"), "bench")
        (second,) = scheduler.queue(_echo("waiting"), "bench")
        scheduler.tick()
        assert full_ssh.asks == {"start": 1}
        status = scheduler.read(run)
        assert status.phase == "queued"
        assert status.reason is not None and status.reason.startswith(
            "machine refused (full-host):"
        )
        assert any(
            d.operation == "machine create" and "machine refused" in d.message
            for d in scheduler.diagnostics(run=run)
        )
        # During the 60 s cooldown, ticks do not start again and _trouble names the refused host.
        clock.advance(59)
        scheduler.tick()
        assert full_ssh.asks == {"start": 1}
        waiting_status = scheduler.read(second)
        assert (
            waiting_status.reason is not None
            and "machine refused (full-host):" in waiting_status.reason
        )

        # A steady wait over the remaining cooldown backs off instead of spinning at 1 s.
        clock.advance(1)  # 60 s elapsed: next tick retries once, then enters a fresh 60 s cooldown
        with pytest.raises(TimeoutError):
            scheduler.wait(run, timeout=30)
        assert full_ssh.asks == {"start": 2}
        assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0]

        # Part 2: adding a healthy sibling lets the next tick place on it immediately.
        scheduler.add_machine("healthy-host", "healthy-host", pool="bench")
        # Stub healthy start so the test counts starts cleanly without running a full uv install.
        started: list[str] = []

        def fake_start(
            self: SSHStart,
            allocation: Allocation,
            packet: Packet,
            bootstrap: BootstrapLinks | None,
        ) -> None:
            if self.ssh is healthy_ssh:
                started.append(allocation.id)
                home = healthy_root / allocation.id
                home.mkdir(parents=True, exist_ok=True)
                (home / "boot-claim").write_text(str(os.getpid()))
                return
            real_start(self, allocation, packet, bootstrap)

        real_start = SSHStart.start
        monkeypatch.setattr(SSHStart, "start", fake_start)

        scheduler.tick()
        assert full_ssh.asks == {"start": 2}
        assert len(started) == 1
        assert scheduler.read(run).machine == "healthy-host"
    finally:
        full_root.chmod(0o700)


def test_post_pid_full_disk_observe_and_end_settle_without_tmp_writes(
    tmp_path: Path, exited_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    etude_home = tmp_path / "etude-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    ro_tmp = tmp_path / "ro-tmp"
    ro_tmp.mkdir()
    ro_tmp.chmod(0o500)
    (tmp_path / "ssh").write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '    case "$1" in\n'
        "        -G) printf 'connecttimeout none\\nserveraliveinterval 0\\nserveralivecountmax 3\\n'; exit 0 ;;\n"
        "        -o) shift 2 ;;\n"
        "        --) shift; break ;;\n"
        "        -*) shift ;;\n"
        "        *) break ;;\n"
        "    esac\n"
        "done\n"
        "shift\n"
        f'exec env TMPDIR={str(ro_tmp)!r} sh -c "$1"\n'
    )
    (tmp_path / "ssh").chmod(0o755)

    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    digest, _ = ssh._program()
    program_dir = etude_home / "programs" / digest
    program_dir.mkdir(parents=True)
    (program_dir / ".venv").symlink_to(Path(sys.prefix))
    (program_dir / "ready").write_text("ok\n")

    journal = SSHJournal(ssh)
    home = root / "s_post_pid"
    home.mkdir(parents=True)
    (home / "pid").write_text(str(exited_pid))
    home.chmod(0o500)
    try:
        observation = journal.observe("s_post_pid")
        assert observation.alive is False
        ended_at = journal.end("s_post_pid")
        assert ended_at is not None
        assert not (home / "ended").exists()
        assert not (home / "ended.partial").exists()
    finally:
        home.chmod(0o700)
        ro_tmp.chmod(0o700)


def test_directory_journal_end_full_disk_and_courier_handoff(
    tmp_path: Path, exited_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    journal = DirectoryJournal(root)

    # Injected ENOSPC on `ended` write still returns `now` and leaves no partial.
    dead = root / "s_enospc"
    dead.mkdir(parents=True)
    (dead / "pid").write_text(str(exited_pid))

    def fail_enospc(path: Path, text: str) -> None:
        path.with_name(path.name + ".partial").write_text("")
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr("etude.launch.storage.disk.write", fail_enospc)
        ended_at = journal.end("s_enospc")
    assert ended_at is not None
    assert not (dead / "ended").exists()
    assert not (dead / "ended.partial").exists()

    # Live worker signalled on an unwritable dir settles once it exits.
    live = root / "s_live"
    live.mkdir(parents=True)
    with subprocess.Popen(["sleep", "60"]) as worker:
        try:
            (live / "pid").write_text(str(worker.pid))
            live.chmod(0o500)
            ended_at = journal.end("s_live")
            assert ended_at is not None
            with pytest.raises(ProcessLookupError):
                os.kill(worker.pid, 0)
        finally:
            live.chmod(0o700)
            worker.kill()

    # A worker that stays alive still returns None instead of faking settlement.
    stubborn = root / "s_stubborn"
    stubborn.mkdir(parents=True)
    (stubborn / "pid").write_text(str(os.getpid()))
    with monkeypatch.context() as patch:
        patch.setattr("etude.launch.storage.disk.os.kill", lambda pid, sig: None)
        patch.setattr("etude.launch.storage.disk.time.sleep", lambda _: None)
        assert journal.end("s_stubborn") is None

    # Bucket-backed owned host: Executor.end still waits for a live courier on full disk.
    etude_home = tmp_path / "etude-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    remote_root = tmp_path / "remote"
    ssh = SSH("bench", str(remote_root))
    digest, _ = ssh._program()
    program_dir = etude_home / "programs" / digest
    program_dir.mkdir(parents=True)
    (program_dir / ".venv").symlink_to(Path(sys.prefix))
    (program_dir / "ready").write_text("ok\n")

    executor = _bucket_host(ssh, Storage())
    courier_home = remote_root / "s_courier"
    courier_home.mkdir(parents=True)
    (courier_home / "pid").write_text(str(exited_pid))
    with subprocess.Popen(["sleep", "60"]) as courier:
        try:
            (courier_home / "courier-pid").write_text(str(courier.pid))
            courier_home.chmod(0o500)
            assert executor.end("s_courier") is None
            courier.terminate()
            courier.wait(timeout=5)
            assert executor.end("s_courier") is not None
        finally:
            courier_home.chmod(0o700)
            courier.kill()


def test_install_enospc_leaves_no_ended_and_empty_ended_settles(
    tmp_path: Path, exited_pid: int
) -> None:
    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    journal = SSHJournal(ssh)

    # Part 1: install ENOSPC that truncates/creates a 0-byte target leaves neither `ended` nor `ended.partial`.
    buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(program_archive())) as src,
        zipfile.ZipFile(buf, "w") as dst,
    ):
        for info in src.infolist():
            if info.filename != "etude/launch/environment.py":
                dst.writestr(info, src.read(info.filename))
        dst.writestr(
            "etude/launch/environment.py",
            "import errno, os\n"
            "_orig_open = os.open\n"
            "_orig_write = os.write\n"
            "_ended_fds = set()\n"
            "def _fail_open(path, flags, *a, **kw):\n"
            "    fd = _orig_open(path, flags, *a, **kw)\n"
            "    if os.path.basename(str(path)).startswith('.ended.'):\n"
            "        _ended_fds.add(fd)\n"
            "    return fd\n"
            "def _fail_write(fd, data):\n"
            "    if fd in _ended_fds:\n"
            "        raise OSError(errno.ENOSPC, 'No space left on device')\n"
            "    return _orig_write(fd, data)\n"
            "def install(code):\n"
            "    os.open = _fail_open\n"
            "    os.write = _fail_write\n"
            "    raise OSError(errno.ENOSPC, 'No space left on device')\n",
        )
    archive = buf.getvalue()
    ssh._archive = (hashlib.sha256(archive).hexdigest(), base64.b64encode(archive).decode())

    packet = Packet(session="s_install_enospc", work=(), deadline=None)
    SSHStart(ssh).start(
        Allocation(id=packet.session, address="bench", created_at=datetime.now(UTC)),
        packet,
        None,
    )
    install_home = root / packet.session
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        obs = journal.observe(packet.session)
        if not obs.alive:
            break
        time.sleep(0.05)
    assert not journal.observe(packet.session).alive
    assert "No space left on device" in (install_home / "bootstrap.log").read_text()
    assert not (install_home / "ended").exists()
    assert not list(install_home.glob("*.partial"))
    assert journal.end(packet.session) is not None

    # Part 2: pre-existing 0-byte `home/ended` is treated as absent across `_BOOT` and settles.
    stale_boot = root / "s_empty_boot"
    stale_boot.mkdir(parents=True)
    (stale_boot / "boot-claim").write_text(str(exited_pid))
    (stale_boot / "ended").write_bytes(b"")
    assert journal.ended("s_empty_boot") is None
    assert not journal.observe("s_empty_boot").alive
    ended_at = journal.end("s_empty_boot")
    assert ended_at is not None
    assert datetime.fromisoformat((stale_boot / "ended").read_text().strip()) == ended_at

    # Even when the session directory is still unwritable, a pre-existing 0-byte `ended` settles.
    full_boot = root / "s_empty_full"
    full_boot.mkdir(parents=True)
    (full_boot / "boot-claim").write_text(str(exited_pid))
    (full_boot / "ended").write_bytes(b"")
    full_boot.chmod(0o500)
    try:
        assert journal.ended("s_empty_full") is None
        assert journal.end("s_empty_full") is not None
    finally:
        full_boot.chmod(0o700)

    # A live boot with a 0-byte `ended` still observes alive instead of falsely dead.
    live_boot = root / "s_empty_live_boot"
    live_boot.mkdir(parents=True)
    (live_boot / "ended").write_bytes(b"")
    with subprocess.Popen(["sleep", "60"]) as proc:
        try:
            (live_boot / "boot-claim").write_text(str(proc.pid))
            assert journal.observe("s_empty_live_boot").alive is True
        finally:
            proc.kill()

    # Part 3: DirectoryJournal treats a 0-byte `ended` as absent in ended/end/logs/_alive.
    disk = DirectoryJournal(tmp_path / "disk")
    dead_disk = tmp_path / "disk" / "s_dead"
    dead_disk.mkdir(parents=True)
    (dead_disk / "pid").write_text(str(exited_pid))
    (dead_disk / "ended").write_bytes(b"")
    assert disk.ended("s_dead") is None
    assert disk.logs("s_dead", producer="worker").complete is False
    settled = disk.end("s_dead")
    assert settled is not None
    assert datetime.fromisoformat((dead_disk / "ended").read_text().strip()) == settled

    live_disk = tmp_path / "disk" / "s_live"
    live_disk.mkdir(parents=True)
    (live_disk / "ended").write_bytes(b"")
    with subprocess.Popen(["sleep", "60"]) as worker:
        try:
            (live_disk / "pid").write_text(str(worker.pid))
            assert disk.observe("s_live").alive is True
            killed_at = disk.end("s_live")
            assert killed_at is not None
            assert datetime.fromisoformat((live_disk / "ended").read_text().strip()) == killed_at
        finally:
            worker.kill()


def test_ssh_launch_full_disk_failover_and_solo_refusal_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    full_root = tmp_path / "mini-1"
    full_root.mkdir()
    full_root.chmod(0o500)
    healthy_root = tmp_path / "mini-2"
    mini1_ssh = CountedSSH("mini-1", str(full_root))
    mini2_ssh = CountedSSH("mini-2", str(healthy_root))
    services = {
        "mini-1": Executor(OwnedMachine("mini-1"), SSHStart(mini1_ssh), SSHJournal(mini1_ssh)),
        "mini-2": Executor(OwnedMachine("mini-2"), SSHStart(mini2_ssh), SSHJournal(mini2_ssh)),
    }
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: services[held.name]})
    scheduler.add_pool(Pool(name="lab"))
    scheduler.add_machine("mini-1", "mini-1", pool="lab")

    job_file = tmp_path / "pass9_ssh_refusal_cli.py"
    job_file.write_text(
        "from etude.jobs import TrainingJob\n"
        "from tests.launch.tasks import RUNTIME, Command, train_command\n"
        "def training_jobs():\n"
        "    return TrainingJob(\n"
        "        name='base', train=train_command, runtime=RUNTIME,\n"
        "        config=Command(command='echo ok'), repo=None, max_hours=0.01,\n"
        "    )\n"
    )
    monkeypatch.setattr("etude.launch.cli.default", lambda: scheduler)
    monkeypatch.setattr("etude.launch.client.default", lambda: scheduler)
    monkeypatch.setattr("etude.launch.client._client", lambda: scheduler)
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: scheduler.standing(experiment)
    )
    monkeypatch.setattr(
        "etude.controller.advance.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    try:
        # Solo full-disk pool exits non-zero with `machine refused` and records zero runs.
        assert main(["launch", str(job_file), "--pool", "lab"]) != 0
        captured = capsys.readouterr()
        assert "machine refused (mini-1)" in captured.err
        assert store.runs() == ()
        assert mini1_ssh.asks == {"start": 1}

        # With healthy mini-2 alongside full-disk mini-1, launch fails over and runs on mini-2.
        scheduler.add_machine("mini-2", "mini-2", pool="lab")
        (run,) = scheduler.launch(_echo("healthy"), pool="lab")
        (status,) = scheduler.wait(run, every=0.1, timeout=20)
        assert status.outcome == "succeeded"
        assert status.machine == "mini-2"
        assert job_text(scheduler.logs(refresh=True, run=run)) == "healthy\n"
    finally:
        full_root.chmod(0o700)


def _spawn_detached_sleep() -> int:
    """Spawn a sleep process reparented to PID 1 so SIGTERM reaps it like a detached worker."""
    out = subprocess.check_output(
        ["sh", "-c", "sleep 60 </dev/null >/dev/null 2>&1 & echo $!"],
        text=True,
    )
    return int(out.strip())


def test_ssh_end_live_pid_with_new_program_digest_settles_without_exit_97(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    etude_home = tmp_path / "fresh-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    # Point at an uninstalled digest; any exit 97 / uv sync would try to populate programs/.
    ssh._archive = ("new-digest-not-installed", base64.b64encode(b"not-a-zip").decode())
    journal = SSHJournal(ssh)

    real_run = subprocess.run
    ssh_codes: list[int] = []

    def counted_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        result: subprocess.CompletedProcess[Any] = real_run(*args, **kwargs)
        cmd = args[0] if args else kwargs.get("args", ())
        if isinstance(cmd, list) and cmd and cmd[0] == "ssh" and "-G" not in cmd:
            ssh_codes.append(result.returncode)
        return result

    monkeypatch.setattr(subprocess, "run", counted_run)

    # Part 1: live pid on writable dir terminates and writes `ended` in 1 SSH call (no exit 97).
    writable = root / "s_writable"
    writable.mkdir(parents=True)
    worker_pid = _spawn_detached_sleep()
    try:
        (writable / "pid").write_text(str(worker_pid))
        ended_at = journal.end("s_writable")
        assert ended_at is not None
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        assert datetime.fromisoformat((writable / "ended").read_text().strip()) == ended_at
    finally:
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert ssh_codes == [0]
    assert not (etude_home / "programs").exists()

    # Part 2: live pid on full-disk (unwritable) dir still settles in 1 SSH call without exit 97.
    ssh_codes.clear()
    full_dir = root / "s_full_disk"
    full_dir.mkdir(parents=True)
    worker_pid = _spawn_detached_sleep()
    try:
        (full_dir / "pid").write_text(str(worker_pid))
        full_dir.chmod(0o500)
        ended_at = journal.end("s_full_disk")
        assert ended_at is not None
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        assert not (full_dir / "ended").exists()
        assert not (full_dir / "ended.partial").exists()
    finally:
        full_dir.chmod(0o700)
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert ssh_codes == [0]
    assert not (etude_home / "programs").exists()

    # Part 3: bucket-backed host leaves live courier-pid to Executor.end's observe wait (no exit 97).
    ssh_codes.clear()
    executor = _bucket_host(ssh, Storage())
    bucket_dir = root / "s_bucket"
    bucket_dir.mkdir(parents=True)
    worker_pid = _spawn_detached_sleep()
    courier_pid = _spawn_detached_sleep()
    try:
        (bucket_dir / "pid").write_text(str(worker_pid))
        (bucket_dir / "courier-pid").write_text(str(courier_pid))
        bucket_dir.chmod(0o500)
        assert executor.end("s_bucket") is None
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
        os.kill(courier_pid, 0)  # courier stayed alive for final delivery
        assert ssh_codes == [0, 0]  # 1 end + 1 observe, no exit 97
        os.kill(courier_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(courier_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                break
        assert executor.end("s_bucket") is not None
        assert ssh_codes == [0, 0, 0, 0]
    finally:
        bucket_dir.chmod(0o700)
        for pid in (worker_pid, courier_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert not (etude_home / "programs").exists()


def test_ssh_1slot_launch_exits_and_cleans_up_without_tick_and_fresh_add_skips_exit_97(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    etude_home = tmp_path / "fresh-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    root = tmp_path / "remote"
    ssh = CountedSSH("bench", str(root))
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: service})
    scheduler.add_pool(Pool(name="bench"))
    scheduler.add_machine("bench", "bench", pool="bench")

    real_run = subprocess.run
    ssh_events: list[tuple[str, int]] = []

    def tracked_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        result: subprocess.CompletedProcess[Any] = real_run(*args, **kwargs)
        cmd = args[0] if args else kwargs.get("args", ())
        if isinstance(cmd, list) and cmd and cmd[0] == "ssh" and "-G" not in cmd:
            action = json.loads(kwargs["input"])["request"]["action"]
            ssh_events.append((action, result.returncode))
        return result

    monkeypatch.setattr(subprocess, "run", tracked_run)

    # Fresh host (no `programs/` installed yet): launch runs `start` + stdlib `add` (no exit 97).
    (run,) = scheduler.launch(
        Job(
            command='echo done > "$ETUDE_OUTPUT_DIR/out.txt"',
            max_hours=0.01,
            max_usd=Decimal(1),
        ),
        "bench",
    )
    assert ssh_events == [("start", 0), ("add", 0)]
    attempt = scheduler.attempts(run)[0]
    home = root / attempt.session
    assert (home / "finish").exists()
    assert (home / "work" / f"{attempt.id}.json").exists()

    # Without any further scheduler call (`tick`/`wait`), the worker runs, cleans up, writes `ended`, and exits.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if (home / "ended").exists() and (home / "ended").read_text().strip():
            break
        time.sleep(0.05)
    assert (home / "ended").exists() and (home / "ended").read_text().strip()
    attempt_dir = home / "attempts" / attempt.id
    assert (attempt_dir / "finished.json").exists()
    assert not (attempt_dir / "src").exists()
    assert not (home / "sources").exists()
    assert (attempt_dir / "outputs" / "out.txt").read_text() == "done\n"
    worker_pid = int((home / "pid").read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(worker_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)

    # Bucket-backed owned host packs work + `finish=True` into one `inbox.json` PUT.
    memory = Storage()
    puts = 0

    def counted_send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Any:
        nonlocal puts
        if method == "PUT":
            puts += 1
        return memory(method, url, body, headers)

    place = Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1")
    bucket = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), counted_send)
    bucket_journal = BucketJournal(place, bucket)
    bucket_journal.prepare(Packet(session="s_bucket_add", work=(), deadline=None, kept=True))
    puts_before = puts
    work = Work(
        attempt="a1",
        run="r1",
        command="echo ok",
        limit_seconds=60,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )
    bucket_journal.add("s_bucket_add", work, finish=True)
    assert puts == puts_before + 1
    inbox = Inbox.model_validate_json(memory.objects["etude/sessions/s_bucket_add/inbox.json"])
    assert len(inbox.work) == 1 and inbox.work[0].work.attempt == "a1"
    assert inbox.finish is True


def test_ssh_boot_module_handles_corrupt_job_files_and_refused_stop(
    tmp_path: Path, exited_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    etude_home = tmp_path / "upgraded-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    root = tmp_path / "remote"
    home = root / "s_corrupt"
    attempt_dir = home / "attempts" / "a1"
    outputs = attempt_dir / "outputs"
    outputs.mkdir(parents=True)
    started = datetime.now(UTC)
    (home / "pid").write_text(str(os.getpid()))
    (attempt_dir / "claim").write_text(started.isoformat())
    (attempt_dir / "log").write_text("line one\n")
    # Invalid UTF-8 bytes in etude-result.json and a partial line in usage.jsonl.
    (outputs / "etude-result.json").write_bytes(b"\xff\xfe{not-utf8")
    (home / "usage.jsonl").write_text('{"incomplete": true\n')

    # Direct unit test of ssh_boot.handle_observe + SSHJournal client validation.
    raw = ssh_boot.handle_observe(home)
    assert raw["observation"]["alive"] is True
    assert raw["observation"]["attempts"][0]["result"] is None

    ssh = SSH("bench", str(root))
    ssh._archive = ("missing-digest", base64.b64encode(b"not-a-zip").decode())
    journal = SSHJournal(ssh)
    obs = journal.observe("s_corrupt")
    assert obs.alive is True
    assert len(obs.progress) == 1
    assert obs.progress[0].clocks == {}
    assert obs.progress[0].log_bytes == len("line one\n")
    assert obs.transfers == ()

    # Valid UTF-8 but invalid schema in etude-result.json still validates cleanly without Unreachable.
    (outputs / "etude-result.json").write_text('{"clocks":{"steps":true}}')
    obs = journal.observe("s_corrupt")
    assert obs.alive is True and obs.progress[0].clocks == {}

    # finish and stop run in stdlib without exit 97; unwritable stop returns structured Refused.
    assert journal.finish("s_corrupt") is None
    assert (home / "finish").exists()
    journal.stop("s_corrupt", "a1")
    assert (home / "stop" / "a1").exists()

    ro_home = root / "s_ro_stop"
    ro_home.mkdir(parents=True)
    (ro_home / "pid").write_text(str(exited_pid))
    ro_home.chmod(0o500)
    try:
        refused = ssh_boot.handle_stop(ro_home, "a1")
        assert "refused" in refused
        with pytest.raises(Refused):
            journal.stop("s_ro_stop", "a1")
    finally:
        ro_home.chmod(0o700)
    assert not (etude_home / "programs").exists()


def test_stdlib_ssh_observe_stop_and_cancel_on_full_disk_upgraded_host(
    tmp_path: Path, exited_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Part 1: DirectoryJournal._alive/end guards invalid/non-positive pids and Linux zombie state.
    disk = DirectoryJournal(tmp_path / "disk")
    for name, content in (("s_empty_pid", ""), ("s_zero_pid", "0"), ("s_bad_pid", "abc")):
        folder = tmp_path / "disk" / name
        folder.mkdir(parents=True)
        (folder / "pid").write_text(content)
        assert disk._alive(name) is False
        assert disk.end(name) is not None
        assert (folder / "ended").exists()

    zombie = tmp_path / "disk" / "s_zombie"
    zombie.mkdir(parents=True)
    (zombie / "pid").write_text(str(os.getpid()))
    real_read_text = Path.read_text

    def fake_zombie_stat(self: Path, *args: Any, **kwargs: Any) -> str:
        if str(self) == f"/proc/{os.getpid()}/stat":
            return f"{os.getpid()} (python3.12) Z 1 1 1 0 -1 0\n"
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fake_zombie_stat)
        assert disk._alive("s_zombie") is False

    # Track every SSH exit code to assert 0 exit-97 retries on missing program digest.
    etude_home = tmp_path / "upgraded-home"
    monkeypatch.setenv("ETUDE_HOME", str(etude_home))
    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    ssh._archive = ("upgraded-digest-uninstalled", base64.b64encode(b"not-a-zip").decode())
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: service})
    scheduler.add_pool(Pool(name="bench"))
    scheduler.add_machine("bench", "bench", pool="bench")

    real_run = subprocess.run
    ssh_codes: list[tuple[str, int]] = []

    def tracked_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        result: subprocess.CompletedProcess[Any] = real_run(*args, **kwargs)
        cmd = args[0] if args else kwargs.get("args", ())
        if isinstance(cmd, list) and cmd and cmd[0] == "ssh" and "-G" not in cmd:
            action = json.loads(kwargs["input"])["request"]["action"]
            ssh_codes.append((action, result.returncode))
        return result

    monkeypatch.setattr(subprocess, "run", tracked_run)

    def seed_session(run_id: str, session_id: str, attempt_id: str) -> datetime:
        now = datetime.now(UTC)
        session = Session(
            id=session_id,
            supply="bench",
            machine="bench",
            planned_at=now,
            claimed_at=now,
            provider_id=session_id,
            started_at=now,
            refused_at=None,
            ended_at=None,
            version=1,
        )
        attempt = RunAttempt(
            id=attempt_id,
            run=run_id,
            session=session.id,
            machine="bench",
            number=1,
            position=0,
            limit_seconds=3600,
            limit_kind="out_of_time",
            started_at=now,
            finished_at=None,
            outcome=None,
            cause=None,
            clocks={},
            outputs=(),
            exit_code=None,
            version=1,
            planned_at=now,
        )
        with store.lock("bench", lambda: now):
            store.plan(session, (attempt,))
            store.claim(session, now)
            session = store.session(session.id)
            store.record_provider_id(session, session.id)
            session = store.session(session.id)
            store.record_started(session, now)
        return now

    # Part 2: Non-cancelled finished job on a full-disk, upgraded pure-SSH host settles
    # `succeeded` (keeping finished.json and logs) with 0 exit-97 retries.
    (done_run,) = scheduler.queue(Job(command="echo done", max_hours=1, max_attempts=1), "bench")
    now = seed_session(done_run, "s_done_full", "a_done_full")
    done_home = root / "s_done_full"
    done_attempt = done_home / "attempts" / "a_done_full"
    done_attempt.mkdir(parents=True)
    (done_home / "pid").write_text(str(exited_pid))
    (done_attempt / "claim").write_text(now.isoformat())
    (done_attempt / "log").write_text("finished output\n")
    (done_attempt / "finished.json").write_text(
        Finished(
            finished_at=now,
            outcome="succeeded",
            cause=None,
            clocks={"steps": 100.0},
            outputs=(),
            exit_code=0,
        ).model_dump_json()
    )
    done_home.chmod(0o500)
    try:
        scheduler.tick()
        status = scheduler.read(done_run)
        assert status.outcome == "succeeded"
        assert status.clocks == {"steps": 100.0}
        assert store.session("s_done_full").ended_at is not None
        assert job_text(scheduler.logs(run=done_run)) == "finished output\n"
    finally:
        done_home.chmod(0o700)
    assert ssh_codes and all(code == 0 for _, code in ssh_codes), ssh_codes
    assert not (etude_home / "programs").exists()

    # Part 3: Cancelled live job (`max_attempts=1`) on a full-disk, upgraded pure-SSH host:
    # observe succeeds (0 exit-97), stop returns Refused -> escalates to end -> settles `cancelled`.
    ssh_codes.clear()
    (cancel_run,) = scheduler.queue(Job(command="sleep 60", max_hours=1, max_attempts=1), "bench")
    seed_session(cancel_run, "s_cancel_full", "a_cancel_full")
    home = root / "s_cancel_full"
    home.mkdir(parents=True)
    worker_pid = _spawn_detached_sleep()
    try:
        (home / "pid").write_text(str(worker_pid))
        home.chmod(0o500)
        # Before cancel, read-only observe succeeds with 0 exit-97 retries and keeps session open.
        scheduler.tick()
        assert store.session("s_cancel_full").ended_at is None
        os.kill(worker_pid, 0)
        assert ssh_codes == [("observe", 0)]

        # After cancel, stop returns structured Refused -> escalates to end -> settles `cancelled`.
        status = scheduler.cancel(cancel_run)
        assert status.outcome == "cancelled"
        assert store.session("s_cancel_full").ended_at is not None
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        home.chmod(0o700)
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert all(code == 0 for _, code in ssh_codes), ssh_codes
    assert not (etude_home / "programs").exists()

    # Part 4: Cancelled full-disk job that already wrote finished.json keeps finished.json facts.
    ssh_codes.clear()
    (kept_run,) = scheduler.queue(Job(command="sleep 60", max_hours=1, max_attempts=1), "bench")
    now = seed_session(kept_run, "s_cancel_kept", "a_cancel_kept")
    kept_home = root / "s_cancel_kept"
    kept_attempt = kept_home / "attempts" / "a_cancel_kept"
    kept_attempt.mkdir(parents=True)
    (kept_home / "pid").write_text(str(exited_pid))
    (kept_attempt / "claim").write_text(now.isoformat())
    (kept_attempt / "finished.json").write_text(
        Finished(
            finished_at=now,
            outcome="cancelled",
            cause=None,
            clocks={"steps": 42.0},
            outputs=(),
            exit_code=0,
        ).model_dump_json()
    )
    kept_home.chmod(0o500)
    try:
        status = scheduler.cancel(kept_run)
        assert status.outcome == "cancelled"
        assert status.clocks == {"steps": 42.0}
        assert scheduler.attempts(kept_run)[0].exit_code == 0
        assert store.session("s_cancel_kept").ended_at is not None
    finally:
        kept_home.chmod(0o700)
    assert all(code == 0 for _, code in ssh_codes), ssh_codes
    assert not (etude_home / "programs").exists()


def test_stdlib_ssh_calls_do_not_build_program_archive(
    tmp_path: Path, exited_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds = 0
    real_archive = program_archive

    def counted_archive() -> bytes:
        nonlocal builds
        builds += 1
        return real_archive()

    monkeypatch.setattr("etude.launch.ssh.program_archive", counted_archive)
    root = tmp_path / "remote"
    home = root / "s1"
    home.mkdir(parents=True)
    (home / "pid").write_text(str(exited_pid))
    ssh = SSH("bench", str(root))
    journal = SSHJournal(ssh)
    work = Work(
        attempt="a1",
        run="r1",
        command="true",
        limit_seconds=60,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )

    assert journal.started("s1") is True
    assert journal.claimed("s1") is True
    assert journal.observe("s1").alive is False
    journal.add("s1", work, finish=False)
    journal.stop("s1", "a1")
    assert journal.finish("s1") is None
    assert journal.ended("s1") is None
    assert journal.logs("s1", producer="worker").received is False
    assert journal.end("s1") is not None
    assert builds == 0
    assert ssh._archive is None

    # A `.venv` action builds the program archive lazily and caches it.
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "etude-home"))
    assert ssh.call(HeldRequest(run="missing", key="checkpoints/missing.pt")).missing is not None
    assert builds == 1


def test_almost_full_disk_1slot_ssh_launch_settles_and_frees_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Part 1: ssh_boot.handle_add turns ENOSPC during `sources/` write into structured refusal
    # and leaves no `.partial` file behind.
    unit_home = tmp_path / "unit" / "s_enospc"
    unit_home.mkdir(parents=True)
    real_write_bytes = ssh_boot._write_bytes

    def fail_sources_write(path: Path, data: bytes) -> None:
        if "sources" in path.parts:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write_bytes(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(ssh_boot, "_write_bytes", fail_sources_write)
        reply = ssh_boot.handle_add(
            unit_home,
            {
                "work": {"attempt": "a1", "run": "r1"},
                "files": {"sources/abc.tar": base64.b64encode(b"tarbytes").decode()},
                "finish": True,
            },
        )
    assert "refused" in reply and "No space left on device" in reply["refused"]
    assert not (unit_home / "work" / "a1.json").exists()
    assert not list(unit_home.rglob("*.partial"))

    # Part 2: End-to-end 1-slot SSH launch where `start` succeeds (`boot-claim` written)
    # and `add` hits ENOSPC on `sources/` write -> `_observe` ends the unstarted session,
    # settles the attempt, and frees the machine instead of retrying `_hand` forever.
    root = tmp_path / "remote"
    ssh = SSH("bench", str(root))
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    healthy_root = tmp_path / "remote_healthy"
    healthy_ssh = SSH("healthy", str(healthy_root))
    healthy_service = Executor(
        OwnedMachine("healthy"), SSHStart(healthy_ssh), SSHJournal(healthy_ssh)
    )
    services = {"bench": service, "healthy": healthy_service}
    store = open_database(tmp_path / "state.db")
    scheduler = Scheduler(store, {"ssh": lambda held: services[held.name]})
    scheduler.add_pool(Pool(name="bench"))
    scheduler.add_machine("bench", "bench", pool="bench")

    boot_pid = _spawn_detached_sleep()
    actions: list[str] = []
    real_run = subprocess.run

    def fake_enospc_ssh(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        cmd = args[0] if args else kwargs.get("args", ())
        if isinstance(cmd, list) and cmd and cmd[0] == "ssh" and "-G" not in cmd:
            payload = json.loads(kwargs["input"])
            request = payload["request"]
            action = request["action"]
            actions.append(action)
            home = root / request.get("session", "")
            if action == "start":
                # Simulate almost-full disk: small 5-byte `boot-claim` succeeds, boot stays alive.
                home.mkdir(parents=True, exist_ok=True)
                (home / "boot-claim").write_text(str(boot_pid))
                return subprocess.CompletedProcess(cmd, 0, b"{}", b"")
            if action == "add":
                # Simulate ENOSPC when writing `sources/` during `handle_add`.
                with monkeypatch.context() as patch:
                    patch.setattr(ssh_boot, "_write_bytes", fail_sources_write)
                    res = ssh_boot.handle_add(
                        home,
                        {
                            **request,
                            "files": {"sources/commit.tar": base64.b64encode(b"payload").decode()},
                        },
                    )
                return subprocess.CompletedProcess(cmd, 0, json.dumps(res).encode(), b"")
        return real_run(*args, **kwargs)

    try:
        monkeypatch.setattr(subprocess, "run", fake_enospc_ssh)
        (run,) = scheduler.launch(Job(command="true", max_hours=1, max_attempts=1), pool="bench")
        session_id = scheduler.attempts(run)[0].session
        assert actions == ["start", "add"]
        assert store.session(session_id).ended_at is None

        # Next tick observes the unstarted session, retries `add`, gets `Refused`, ends the
        # session, records a 60 s refusal cooldown, and settles as `capacity_refused` (0 tries).
        scheduler.tick()
        status = scheduler.read(run)
        assert not status.done
        assert status.phase == "queued"
        assert status.attempts == 0
        assert status.reason is not None and "machine refused (bench)" in status.reason
        attempt = scheduler.attempts(run)[0]
        assert attempt.outcome == "lost"
        assert attempt.cause == "capacity_refused"
        ended_session = store.session(session_id)
        assert ended_session.ended_at is not None
        assert ended_session.refused_at is not None
        assert store.finishing_sessions() == ()
        with pytest.raises(ProcessLookupError):
            os.kill(boot_pid, 0)

        # Adding a healthy sibling lets the next tick fail over immediately while `bench` cools down.
        scheduler.add_machine("healthy", "healthy", pool="bench")
        monkeypatch.setattr(subprocess, "run", real_run)
        (finished,) = scheduler.wait(run, every=0.1, timeout=20)
        assert finished.outcome == "succeeded"
        assert finished.machine == "healthy"
        assert finished.attempts == 1
    finally:
        try:
            os.kill(boot_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    # Part 3: On a 2-slot owned session where attempt 1 is already started, `Refused` on attempt 2's
    # delivery leaves attempt 1 running; once attempt 1 finishes, the next tick ends the session.
    store2 = open_database(tmp_path / "state2.db")
    clock = FakeClock()
    root2 = tmp_path / "remote2"
    ssh2 = SSH("multi", str(root2))
    worker_pid = _spawn_detached_sleep()
    multi_home = root2 / "s_multi"
    attempt1_dir = multi_home / "attempts" / "a1"
    attempt1_dir.mkdir(parents=True)
    (multi_home / "pid").write_text(str(worker_pid))
    (attempt1_dir / "claim").write_text(clock().isoformat())

    service2 = Executor(OwnedMachine("multi"), SSHStart(ssh2), SSHJournal(ssh2))
    scheduler2 = Scheduler(store2, {"ssh": lambda held: service2}, clock=clock)
    scheduler2.add_pool(Pool(name="lab", slots=2))
    scheduler2.add_machine("multi", "multi", pool="lab")
    r1, r2 = scheduler2.queue(Job(command="true", max_hours=1, max_attempts=1).runs(1, 2), "lab")
    now = clock()
    s_multi = Session(
        id="s_multi",
        supply="lab",
        machine="multi",
        planned_at=now,
        claimed_at=now,
        provider_id="s_multi",
        started_at=now,
        refused_at=None,
        ended_at=None,
        version=1,
    )
    a1 = RunAttempt(
        id="a1",
        run=r1,
        session="s_multi",
        machine="multi",
        number=1,
        position=0,
        limit_seconds=3600,
        limit_kind="out_of_time",
        started_at=now,
        finished_at=None,
        outcome=None,
        cause=None,
        clocks={},
        outputs=(),
        exit_code=None,
        version=1,
        planned_at=now,
    )
    a2 = RunAttempt(
        id="a2",
        run=r2,
        session="s_multi",
        machine="multi",
        number=1,
        position=1,
        limit_seconds=3600,
        limit_kind="out_of_time",
        started_at=None,
        finished_at=None,
        outcome=None,
        cause=None,
        clocks={},
        outputs=(),
        exit_code=None,
        version=1,
        planned_at=now,
        added=True,
    )
    with store2.lock("lab", clock):
        store2.plan(s_multi, (a1, a2))
        store2.claim(s_multi, now)
        s_multi = store2.session("s_multi")
        store2.record_provider_id(s_multi, "s_multi")
        s_multi = store2.session("s_multi")
        store2.record_started(s_multi, now)

    try:
        monkeypatch.setattr(subprocess, "run", real_run)
        multi_home.chmod(0o500)
        scheduler2.tick()
        # Attempt 1 stays running; session is NOT killed while attempt 1 is active.
        assert store2.session("s_multi").ended_at is None
        os.kill(worker_pid, 0)
        assert scheduler2.read(r1).phase == "running"

        # Once attempt 1 finishes, attempt 2's delivery refusal ends the session cleanly.
        multi_home.chmod(0o700)
        (attempt1_dir / "finished.json").write_text(
            Finished(
                finished_at=now,
                outcome="succeeded",
                cause=None,
                clocks={},
                outputs=(),
                exit_code=0,
            ).model_dump_json()
        )
        multi_home.chmod(0o500)
        scheduler2.tick()  # records r1 succeeded
        assert scheduler2.read(r1).outcome == "succeeded"
        scheduler2.tick()  # only unstarted a2 remains open -> refusal ends session with cooldown
        ended_multi = store2.session("s_multi")
        assert ended_multi.ended_at is not None
        assert ended_multi.refused_at is not None
        assert scheduler2.read(r2).phase == "queued"
        assert scheduler2.read(r2).attempts == 0
        a2_done = scheduler2.attempts(r2)[0]
        assert a2_done.outcome == "lost"
        assert a2_done.cause == "capacity_refused"
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        multi_home.chmod(0o700)
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_local_full_disk_cancel_settles_via_directory_journal_refused(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    journal = DirectoryJournal(root)
    ro_home = root / "s_ro"
    ro_home.mkdir(parents=True)
    ro_home.chmod(0o500)
    work = Work(
        attempt="a1",
        run="r1",
        command="sleep 60",
        limit_seconds=60,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
    )
    try:
        with pytest.raises(Refused):
            journal.stop("s_ro", "a1")
        with pytest.raises(Refused):
            journal.add("s_ro", work)
    finally:
        ro_home.chmod(0o700)

    # End-to-end local pool cancel on a read-only (full-disk) session settles `cancelled`
    # instead of wedging in `stopping`.
    store = open_database(tmp_path / "state.db")
    clock = FakeClock()
    scheduler = Scheduler(store, {"local": lambda held: local(root)}, clock=clock)
    scheduler.add_pool(Pool(name="local"))
    store.add_machine(Machine(name="local", pool="local", kind="local"))
    (run,) = scheduler.queue(Job(command="sleep 60", max_hours=1, max_attempts=1), "local")
    now = clock()
    worker_pid = _spawn_detached_sleep()
    home = root / "s_local_full"
    attempt_dir = home / "attempts" / "a_local_full"
    attempt_dir.mkdir(parents=True)
    (home / "pid").write_text(str(worker_pid))
    (attempt_dir / "claim").write_text(now.isoformat())

    session = Session(
        id="s_local_full",
        supply="local",
        machine="local",
        planned_at=now,
        claimed_at=now,
        provider_id="s_local_full",
        started_at=now,
        refused_at=None,
        ended_at=None,
        version=1,
    )
    attempt = RunAttempt(
        id="a_local_full",
        run=run,
        session=session.id,
        machine="local",
        number=1,
        position=0,
        limit_seconds=3600,
        limit_kind="out_of_time",
        started_at=now,
        finished_at=None,
        outcome=None,
        cause=None,
        clocks={},
        outputs=(),
        exit_code=None,
        version=1,
        planned_at=now,
    )
    with store.lock("local", clock):
        store.plan(session, (attempt,))
        store.claim(session, now)
        session = store.session(session.id)
        store.record_provider_id(session, session.id)
        session = store.session(session.id)
        store.record_started(session, now)

    try:
        home.chmod(0o500)
        status = scheduler.cancel(run)
        assert status.outcome == "cancelled"
        assert store.session("s_local_full").ended_at is not None
        assert store.finishing_sessions() == ()
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        home.chmod(0o700)
        try:
            os.kill(worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_ssh_boot_and_remote_reject_path_traversal_and_bad_digest(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    escape = tmp_path / "escaped"
    body = base64.b64encode(b"evil").decode()

    for bad_session in ("../escaped", "/tmp/escaped", "", "."):
        with pytest.raises(ValueError, match="invalid session"):
            ssh_boot.dispatch(
                {"root": str(root), "request": {"action": "observe", "session": bad_session}}
            )

    for action, extra in (
        ("stop", {"attempt": "../escaped"}),
        ("logs", {"producer": "job", "attempt": "../escaped"}),
        ("add", {"work": {"attempt": "../escaped", "run": "r"}, "files": {}}),
    ):
        with pytest.raises(ValueError, match="invalid attempt"):
            ssh_boot.dispatch(
                {"root": str(root), "request": {"action": action, "session": "s1", **extra}}
            )

    for bad_file in ("../escaped", "sources/../../escaped", "/tmp/escaped"):
        with pytest.raises(ValueError, match="invalid relative path|escapes"):
            ssh_boot.dispatch(
                {
                    "root": str(root),
                    "request": {
                        "action": "add",
                        "session": "s1",
                        "work": {"attempt": "a1", "run": "r"},
                        "files": {bad_file: body},
                    },
                }
            )

    with pytest.raises(ValueError, match="invalid program digest"):
        ssh_boot.dispatch(
            {
                "root": str(root),
                "digest": "../escaped",
                "request": {"action": "held", "run": "r"},
            }
        )

    packet = Packet(session="s1", deadline=None, work=())
    with pytest.raises(ValueError, match="invalid relative path|escapes"):
        remote_worker.invoke(
            RemoteCall(
                root=str(root),
                request=StartRequest(
                    session="s1",
                    packet=packet,
                    files={"../escaped": body},
                ),
            )
        )

    with pytest.raises(ValidationError):
        Work(
            attempt="a1",
            command="true",
            limit_seconds=60,
            limit_kind="out_of_time",
            seed=None,
            until=None,
            config=None,
            repo="r",
            commit="../../escape",
            run="exp/job@0",
        )

    assert not escape.exists()


def test_ssh_bounded_log_truncates_on_line_boundaries_before_sanitization(tmp_path: Path) -> None:
    root = tmp_path / "remote"
    log = root / "s1" / "attempts" / "a1" / "log"
    log.parent.mkdir(parents=True)

    # Place secrets across both the 4096-byte head cut and the tail cut.
    opening = b"start line\n" + b"a" * (4096 - len(b"start line\n") - 20)
    head_secret = b"https://user:secret@host/path?X-Amz-Security-Token=SECRET123\n"
    middle = b"m" * (200 * 1024) + b"\n"
    tail_secret = b"Authorization: Bearer SECRET456\n"
    tail_keep = b"x" * (1024 * 1024 - 4096 - 100 - len(tail_secret) + 12) + b"\nfinal line\n"
    log.write_bytes(opening + head_secret + middle + tail_secret + tail_keep)

    journal = SSHJournal(SSH("bench", str(root)))
    stream = journal.logs("s1", "a1", producer="job")
    text = "".join(chunk.text for chunk in stream.chunks)
    assert "start line" in text
    assert "final line" in text
    assert stream.truncated is True
    assert "SECRET123" not in text
    assert "SECRET456" not in text
    assert "user:secret" not in text


def test_atomic_writes_concurrent_and_handle_end_directory_mode(tmp_path: Path) -> None:
    fresh = tmp_path / "s_fresh"
    reply = ssh_boot.handle_end(fresh)
    assert reply["ended_at"] is not None
    assert fresh.stat().st_mode & 0o777 == 0o700
    assert (fresh / "ended").stat().st_mode & 0o777 == 0o600

    target = tmp_path / "concurrent.txt"
    errors: list[BaseException] = []

    def writer(tag: str) -> None:
        payload = (tag * 1024).encode()
        try:
            for _ in range(40):
                write_bytes(target, payload)
                ssh_boot._write_ended(tmp_path, tag * 10)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(ch,)) for ch in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert target.read_bytes() in (b"A" * 1024, b"B" * 1024)
    assert (tmp_path / "ended").read_text() in ("A" * 10, "B" * 10)
    assert not list(tmp_path.glob("*.partial"))
