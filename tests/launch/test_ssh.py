"""An SSH executable stand-in runs the installed program in a separate process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Thread

import pytest

from etude.cli import main
from etude.jobs import continue_from
from etude.launch.aws import Keys
from etude.launch.executor import Executor, local
from etude.launch.provisioner.machines import OwnedMachine
from etude.launch.records import Machine, Pool
from etude.launch.records import Storage as Place
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import AttemptRecord, Finished
from etude.launch.ssh import SSH, RemoteReply, RemoteRequest, SSHJournal, SSHStart
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket
from etude.records import Clock
from etude.spec import Job, RunRequest, Until
from tests.launch.counting import job_text
from tests.launch.fake import open_database
from tests.launch.test_runpod_machines import Storage
from tests.train.tracking import open_run

# The `ssh` stand-in runs the remote command here; `UNREACHABLE` replaces it.
RELAY = '#!/bin/sh\nshift 2\nshift\nexec sh -c "$1"\n'
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


@pytest.fixture
def courier_pid() -> Iterator[int]:
    with subprocess.Popen(["sleep", "60"]) as courier:
        try:
            yield courier.pid
        finally:
            courier.kill()


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

    # Its courier fetches inputs after the boot process exits, then fails.
    with subprocess.Popen(["sleep", "60"]) as courier:
        try:
            (home / "courier-pid").write_text(str(courier.pid))
            assert service.observe("s1").alive
            assert service.end("s1") is None  # the courier is told to stop
            assert courier.wait(timeout=10) == -signal.SIGTERM
            assert service.end("s1") is not None
            assert not service.observe("s1").alive
        finally:
            courier.kill()


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
    with subprocess.Popen(["sleep", "60"]) as worker:
        # Reap the fixture child when SSH signals it, as init would on the host.
        reap = Thread(target=worker.wait)
        reap.start()
        try:
            (home / "pid").write_text(str(worker.pid))
            assert not service.observe("s1").alive
            assert service.end("s1") is not None
            assert worker.wait(timeout=5) == -signal.SIGTERM
        finally:
            worker.kill()
            reap.join()
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
