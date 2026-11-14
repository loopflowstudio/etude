"""A stand-in SSH runs the installed CLI against a second launcher home."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import (
    _open,
    cancel,
    fetch,
    launch,
    logs,
    queue,
    status,
    wait,
)
from etude.launch.database import launcher_file
from etude.launch.records import Pool
from etude.launch.server import FetchRequest, PythonClient, forward, warning
from etude.spec import Job, RunRequest, Runtime, Until
from tests.launch.counting import job_text
from tests.launch.test_source import git
from tests.launch.test_source import repository as repository


@pytest.fixture(autouse=True)
def _command_environment(command_python: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_TEST_PYTHON", command_python)
    monkeypatch.setattr("tests.launch.tasks.RUNTIME", Runtime(python=command_python))


# A tracked job: it records a checkpoint, and a continuation starts from its parent's.
TRACKED = """
import os
from pathlib import Path
from etude.train.records import Step
from etude.train.tracking import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
latest = run.latest_checkpoint()
start = 0 if latest is None else int(latest.at["steps"])
target = int(os.environ["ETUDE_UNTIL_VALUE"])
weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.pt"
weights.write_text(f"from {start} to {target}\\n")
run.log({"loss": 0.5}, steps=target)
run.checkpoint(weights, steps=target)
run.finish()
"""


def command(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if args and args[0] in {"launch", "queue"}:
        args = (*args, "--source", os.environ["ETUDE_TEST_SOURCE"])
    result = subprocess.run(
        [sys.executable, "-m", "etude", *args], capture_output=True, text=True, env=env, timeout=15
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        "\n".join(line for line in result.stdout.splitlines() if ": state from " not in line)
        + ("\n" if result.stdout.endswith("\n") else ""),
        result.stderr,
    )


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    client, remote = tmp_path / "client", tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("ETUDE_TEST_SOURCE", str(remote / "track"))
    monkeypatch.setenv("ETUDE_HOME", str(client))
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
    scripts = tmp_path / "bin"
    scripts.mkdir()
    ssh = scripts / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "assert sys.argv[1:3] == ['--', 'server-alias']\n"
        f"os.environ['ETUDE_HOME'] = {str(remote)!r}\n"
        f"os.chdir({str(remote)!r})\n"
        "raise SystemExit(subprocess.call(sys.argv[3], shell=True))\n"
    )
    ssh.chmod(0o700)
    # Nothing is installed on the stand-in server: the client sends its own code.
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", sys.executable)
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ["PATH"])
    # The installer owns its deadline and child cleanup; an outer CLI timeout
    # can expire during cold imports and leave the remote installation running.
    assert main(["server", "set", "server-alias", "--startup-seconds", "15"]) == 0
    assert not launcher_file(client).exists()
    return client, remote


def test_remote_launch_freezes_sources_config_and_returns_outputs(
    server: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = server
    repo = tmp_path / "source with spaces"
    repo.mkdir()
    (repo / "train.sh").write_text(
        f'cat "$ETUDE_CONFIG"\necho "$ETUDE_SEED"\n{sys.executable} train.py\n'
    )
    (repo / "train.py").write_text(TRACKED)
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "source",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (tmp_path / "config.txt").write_text("frozen config\n")
    job = tmp_path / "job.py"
    job.write_text(
        'from etude.jobs import TrainingJob\nfrom tests.launch.tasks import Command, RUNTIME, train_command\nfrom etude.launch.records import Job, RunRequest\nfrom pathlib import Path\nhere = Path(__file__).parent\ndef training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="sh train.sh", text=(here / "config.txt").read_text()), experiment="capacity", max_hours=0.01, max_usd=1, repo=str(here / "source with spaces"), until="steps=10", name="base").runs(1, 2)\n'
    )
    launched = command("launch", str(job), "--experiment", "capacity")
    assert launched.returncode == 0, launched.stderr
    ids = [line.split()[0] for line in launched.stdout.splitlines() if not line.startswith("  ")]
    assert len(ids) == 2
    repo.rename(tmp_path / "unavailable-source")
    (tmp_path / "config.txt").unlink()
    assert command("wait", "--experiment", "capacity").returncode == 0
    logs = command("logs", "--experiment", "capacity", "--job-only")
    assert "frozen config\n1\n" in logs.stdout and "frozen config\n2\n" in logs.stdout, logs.stderr
    assert not launcher_file(client).exists()
    assert len(list((remote / "sources").glob("*.tar"))) == 1
    assert [pool.name for pool in _open(remote).pools()] == ["server"]
    assert _open(remote).record(ids[0]).launch.pool == "server"
    frozen = _open(remote).record(ids[0]).launch.spec
    assert frozen.repo == str(repo) and frozen.revision != "HEAD"
    # Continuation works without the client's repository: it uses the retained archive.
    continued = command("continue", "--run", ids[0], "--until", "steps=20")
    assert continued.returncode == 0, continued.stderr
    next_id = continued.stdout.split()[0]
    assert command("wait", next_id).returncode == 0
    destination = tmp_path / "fetched files"
    fetched = command("fetch", next_id, "weights.pt", str(destination))
    assert fetched.returncode == 0, fetched.stderr
    assert (destination / "weights.pt").read_text() == "from 10 to 20\n"
    assert command("server").stdout.startswith("server: server-alias\nlast loop: ")
    assert command("server", "unset").returncode == 0
    assert command("pools").stdout == "local  this computer (default)\n"
    assert launcher_file(client).exists()


def test_loop_survives_an_empty_queue_and_places_later_work(
    server: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    _, remote = server
    env = {**os.environ, "ETUDE_HOME": str(remote), "ETUDE_SERVER_LOCAL": "1"}
    with subprocess.Popen(
        [sys.executable, "-m", "etude", "server", "run"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            heartbeat = remote / "server-heartbeat"
            _until_while_alive(process, heartbeat.exists)
            job = tmp_path / "job.py"
            job.write_text(
                'from etude.jobs import TrainingJob\nfrom tests.launch.tasks import Command, RUNTIME, train_command\nfrom etude.launch.records import Job, RunRequest\ndef training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="sleep 0.2"), max_hours=0.01, max_usd=1, name="base", repo=None).runs(1, 2)\n'
            )
            assert command("launch", str(job)).returncode == 0
            scheduler = _open(remote)
            # Read only: the standing loop must finish both runs.
            _until_while_alive(process, lambda: not scheduler.runs())
            assert len(scheduler.runs(done=True)) == 2
            assert all(
                scheduler.read(run.id).outcome == "succeeded" for run in scheduler.runs(done=True)
            )
        finally:
            process.terminate()
            process.wait(timeout=5)


def _until_while_alive(process: subprocess.Popen[bytes], done: Callable[[], bool]) -> None:
    """Wait for the standing loop to make `done` true; fail as soon as it exits.

    The only time limit catches a hung loop. Progress is judged by the condition,
    not the clock, because cold CLI starts on a loaded machine take seconds.
    """
    give_up = time.monotonic() + 300
    while not done():
        assert process.poll() is None, process.stderr.read() if process.stderr else ""
        assert time.monotonic() < give_up, "the server loop made no progress"
        time.sleep(0.05)


def test_waiting_work_warns_when_loop_is_absent_or_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    scheduler = _open(tmp_path)
    scheduler.turn_pool("local", off=True)
    scheduler.add_pool(Pool(name="waiting"))
    scheduler.queue(Job(command="true", max_hours=1, max_usd=Decimal(1)), pool="waiting")
    scheduler.tick()
    assert warning(scheduler) is not None
    assert main(["server"]) == 0
    assert "no server loop" in capsys.readouterr().err
    (tmp_path / "server-heartbeat").write_text("2000-01-01T00:00:00+00:00")
    assert main(["status"]) == 0
    assert "no server loop" in capsys.readouterr().err


def test_fetch_checks_digest_before_replacing_client_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bad_reply(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"body": "bmV3", "sha256": "bad"}).encode()
        )

    target = tmp_path / "result"
    target.write_text("old")
    monkeypatch.setattr(subprocess, "run", bad_reply)
    with pytest.raises(LauncherError, match="digest mismatch"):
        forward("alias", [], fetch=FetchRequest(run="run", name="result", destination=tmp_path))
    assert target.read_text() == "old"


def test_remote_queue_preserves_choice_without_local_database(
    server: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = server
    assert command("pool", "add", "waiting").returncode == 0
    path = tmp_path / "queued.py"
    path.write_text(
        'from etude.jobs import TrainingJob\nfrom tests.launch.tasks import Command, RUNTIME, train_command\nfrom etude.launch.records import Job, RunRequest\ndef training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="true"), max_hours=1, max_usd=1, name="base", repo=None).runs(1, 2)\n'
    )
    result = command("queue", str(path), "--pool", "waiting")
    assert result.returncode == 0, result.stderr
    ids = [line.split()[0] for line in result.stdout.splitlines()]
    assert len(ids) == 2
    scheduler = _open(remote)
    assert all(scheduler.record(id).launch.autorun for id in ids)
    assert all(scheduler.read(id).phase == "queued" for id in ids)
    assert not launcher_file(client).exists()


def test_python_api_uses_server_and_preserves_checkpoint_ancestry(
    server: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = server
    job = Job(
        command='echo "to $ETUDE_UNTIL_VALUE"; '
        'echo weights > "$ETUDE_OUTPUT_DIR/weights"; '
        'printf \'{"clocks":{"steps":%s}}\' '
        '"$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"',
        max_hours=0.01,
        max_usd=Decimal(1),
        until=Until.parse("steps=10"),
    )
    (first,) = launch(job)
    assert wait(first, every=0.02, timeout=10)[0].outcome == "succeeded"
    assert status(first).clocks == {"steps": 10}
    assert job_text(logs(run=first, refresh=True)) == "to 10\n"
    assert fetch(first, "weights", tmp_path / "out").read_text() == "weights\n"
    pair = launch(job.runs(1, 2))
    assert all(s.outcome == "succeeded" for s in wait(*pair, every=0.02, timeout=10))
    assert command("pool", "add", "waiting").returncode == 0
    queued = queue(job.runs(3, 4), pool="waiting")
    assert all(cancel(run).outcome == "cancelled" for run in queued)
    assert not launcher_file(client).exists()


def test_the_server_is_sent_the_program_and_gets_it_again_if_it_goes_missing(
    server: tuple[Path, Path],
) -> None:
    client, remote = server
    (installed,) = (remote / "programs").iterdir()  # sent by `server set`
    assert (installed / "etude" / "launch" / "cli.py").exists()
    assert command("pools").stdout == "server  the server (default)\n"
    shutil.rmtree(installed)
    again = command("pools")  # the command did not start, so it is sent and run again
    assert again.returncode == 0 and again.stdout == "server  the server (default)\n"
    assert (installed / "etude" / "launch" / "cli.py").exists()
    assert not launcher_file(client).exists()


@pytest.mark.parametrize("install_seconds, expected_calls", [(20.0, 2), (3.0, 3)])
def test_log_deadline_includes_missing_server_installation(
    monkeypatch: pytest.MonkeyPatch, install_seconds: float, expected_calls: int
) -> None:
    elapsed = 0.0
    calls = 0

    def transport(
        command: list[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        nonlocal elapsed, calls
        calls += 1
        duration = (1.0, install_seconds, 3.0)[calls - 1]
        elapsed += min(duration, timeout)
        if duration > timeout:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 97 if calls == 1 else 0, b"{}", b"")

    monkeypatch.setattr(time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(subprocess, "run", transport)
    with pytest.raises(LauncherError, match="did not answer|time limit reached"):
        PythonClient("fixture-server").logs(run="chosen", refresh=True, timeout=5)
    assert elapsed == 5
    assert calls == expected_calls


def test_a_machine_that_cannot_be_a_server_is_refused_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
    scripts = tmp_path / "bin"
    scripts.mkdir()
    ssh = scripts / "ssh"
    ssh.write_text("#!/bin/sh\necho 'python3.12: command not found' >&2\nexit 127\n")
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ["PATH"])
    refused = command("server", "set", "no-python")
    assert refused.returncode == 1
    assert "could not take the program: python3.12: command not found" in refused.stderr
    assert command("server").stdout.startswith("server: this machine")


def test_server_keeps_source_history_and_checks_out_on_client(
    server: tuple[Path, Path],
    repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, remote = server
    repo, origin = repository
    monkeypatch.setenv("ETUDE_HOME", str(client))
    (repo / "train").write_text("client snapshot\n")
    job = repo.parent / "snapshot.py"
    job.write_text(
        f'from etude.jobs import TrainingJob\nfrom tests.launch.tasks import Command, RUNTIME, train_command\ndef training_jobs(): return TrainingJob(name="base", train=train_command, config=Command(command="true"), runtime=RUNTIME, max_hours=1, repo={str(repo)!r})\n'
    )
    assert command("pool", "add", "waiting").returncode == 0
    sent = command("queue", str(job), "--pool", "waiting")
    assert sent.returncode == 0, sent.stderr
    id = sent.stdout.split()[0]
    record = _open(remote).record(id)
    assert record.launch.source is not None and record.launch.source.snapshot
    assert record.launch.source.origin == str(origin)
    assert git(origin, "rev-parse", f"refs/etude/runs/{id}") == record.launch.spec.revision
    repo.rename(repo.with_name("unavailable"))
    listed = command("runs", "--contains", "v1", "--pool", "waiting")
    assert listed.returncode == 0 and id in listed.stdout, listed.stderr
    destination = client / "recovered"
    result = command("source", id, str(destination))
    assert result.returncode == 0, result.stderr
    assert (destination / "train").read_text() == "client snapshot\n"
    assert not launcher_file(client).exists()
    assert _open(remote).read(id).phase == "queued"


def test_remote_resume_keeps_one_run_and_its_previous_attempt(
    server: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = server
    spec = Job(
        command='echo "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/result"; printf \'{"clocks":{"steps":%s}}\' "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"',
        max_hours=0.01,
        until=Until(clock="steps", value=10),
    )
    (identity,) = launch(RunRequest(job=spec, seed=7))
    assert wait(identity, every=0.02, timeout=10)[0].outcome == "succeeded"
    resumed = RunRequest(
        job=spec.model_copy(update={"until": Until(clock="steps", value=20)}),
        seed=7,
        id=identity,
        resume=True,
    )
    assert launch(resumed)[0] == identity
    seen = wait(identity, every=0.02, timeout=10)[0]
    assert (seen.outcome, seen.attempts, seen.clocks) == ("succeeded", 2, {"steps": 20})
    assert fetch(identity, "result", tmp_path / "resumed").read_text() == "20\n"
    assert not launcher_file(client).exists()
    assert len(_open(remote).runs(done=True)) == 1
