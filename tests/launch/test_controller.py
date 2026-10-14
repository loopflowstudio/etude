"""A stand-in SSH runs the installed CLI against a second launcher home."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch.client import (
    _open,
    cancel,
    continue_from,
    fetch,
    launch,
    launch_cohort,
    logs,
    queue,
    queue_cohort,
    status,
    wait,
)
from etude.launch.controller import FetchRequest, forward, warning
from etude.launch.records import Job, LauncherError, Pool
from tests.launch.test_source import git
from tests.launch.test_source import repository as repository


def command(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "etude", *args], capture_output=True, text=True, env=env, timeout=15
    )


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    client, remote = tmp_path / "client", tmp_path / "remote"
    remote.mkdir()
    monkeypatch.setenv("ETUDE_HOME", str(client))
    monkeypatch.delenv("ETUDE_CONTROLLER_LOCAL", raising=False)
    scripts = tmp_path / "bin"
    scripts.mkdir()
    ssh = scripts / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "assert sys.argv[1:3] == ['--', 'controller-alias']\n"
        f"os.environ['ETUDE_HOME'] = {str(remote)!r}\n"
        f"os.chdir({str(remote)!r})\n"
        "raise SystemExit(subprocess.call(sys.argv[3], shell=True))\n"
    )
    ssh.chmod(0o700)
    # Nothing is installed on the stand-in controller: the client sends its own code.
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", sys.executable)
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ["PATH"])
    assert command("controller", "set", "controller-alias").returncode == 0
    assert not (client / "launcher.db").exists()
    return client, remote


def test_remote_launch_freezes_sources_config_and_returns_outputs(
    controller: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = controller
    repo = tmp_path / "source with spaces"
    repo.mkdir()
    (repo / "train.sh").write_text(
        'cat "$ETUDE_CONFIG"\n'
        'echo "$ETUDE_SEED"\n'
        'echo saved > "$ETUDE_OUTPUT_DIR/checkpoint"\n'
        'printf \'{"clocks":{"steps":%s},"checkpoint":"checkpoint"}\' '
        '"$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"\n'
    )
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
        'from etude.launch.records import Job, Run\nfrom pathlib import Path\nhere = Path(__file__).parent\ndef runs(): return Job(command="sh train.sh", max_hours=0.01, max_usd=1, repo=str(here / "source with spaces"), config=(here / "config.txt").read_text(), until="steps=10").runs(1, 2)\n'
    )
    launched = command("launch", str(job), "--cohort", "pair")
    assert launched.returncode == 0, launched.stderr
    ids = [line.split()[0] for line in launched.stdout.splitlines() if not line.startswith("  ")]
    assert len(ids) == 2
    repo.rename(tmp_path / "unavailable-source")
    (tmp_path / "config.txt").unlink()
    assert command("wait", "pair").returncode == 0
    logs = command("logs", "pair")
    assert logs.stdout == "frozen config\n1\nfrozen config\n2\n", logs.stderr
    assert not (client / "launcher.db").exists()
    assert len(list((remote / "sources").glob("*.tar"))) == 1
    assert [pool.name for pool in _open(remote).pools()] == ["controller"]
    assert _open(remote).record(ids[0]).pool == "controller"
    frozen = _open(remote).record(ids[0]).spec
    assert frozen.repo == str(repo) and frozen.revision != "HEAD"
    # Continuation works without the client's repository: it uses the retained archive.
    continued = command("continue", ids[0], "steps=20")
    assert continued.returncode == 0, continued.stderr
    next_id = continued.stdout.split()[0]
    assert command("wait", next_id).returncode == 0
    destination = tmp_path / "fetched files"
    fetched = command("fetch", next_id, "checkpoint", str(destination))
    assert fetched.returncode == 0, fetched.stderr
    assert (destination / "checkpoint").read_text() == "saved\n"
    assert command("controller").stdout.startswith("controller: controller-alias\nlast loop: ")
    assert command("controller", "unset").returncode == 0
    assert command("pools").stdout == "local  this computer (default)\n"
    assert (client / "launcher.db").exists()


def test_loop_survives_an_empty_queue_and_places_later_work(
    controller: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    _, remote = controller
    env = {**os.environ, "ETUDE_HOME": str(remote), "ETUDE_CONTROLLER_LOCAL": "1"}
    with subprocess.Popen(
        [sys.executable, "-m", "etude", "controller", "run"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            deadline = time.monotonic() + 10
            while not (remote / "controller-heartbeat").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert process.poll() is None
            assert (remote / "controller-heartbeat").exists()
            job = tmp_path / "job.py"
            job.write_text(
                'from etude.launch.records import Job, Run\ndef runs(): return Job(command="sleep 0.2", max_hours=0.01, max_usd=1).runs(1, 2)\n'
            )
            assert command("launch", str(job), "--cohort", "later").returncode == 0
            scheduler = _open(remote)
            while scheduler.runs() and time.monotonic() < deadline:
                time.sleep(0.1)  # read only: the standing loop must finish the second run
            assert not scheduler.runs()
            assert len(scheduler.runs(done=True)) == 2
            assert all(
                scheduler.read(run.id).outcome == "succeeded" for run in scheduler.runs(done=True)
            )
        finally:
            process.terminate()
            process.wait(timeout=5)


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
    assert main(["controller"]) == 0
    assert "no controller loop" in capsys.readouterr().err
    (tmp_path / "controller-heartbeat").write_text("2000-01-01T00:00:00+00:00")
    assert main(["status"]) == 0
    assert "no controller loop" in capsys.readouterr().err


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
    controller: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = controller
    assert command("pool", "add", "waiting").returncode == 0
    path = tmp_path / "queued.py"
    path.write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="true", max_hours=1, max_usd=1).runs(1, 2)\n'
    )
    result = command("queue", str(path), "--pool", "waiting", "--cohort", "queued")
    assert result.returncode == 0, result.stderr
    ids = [line.split()[0] for line in result.stdout.splitlines()]
    assert len(ids) == 2
    scheduler = _open(remote)
    assert all(scheduler.record(id).autorun for id in ids)
    assert all(scheduler.read(id).phase == "queued" for id in ids)
    assert not (client / "launcher.db").exists()


def test_python_api_uses_controller_and_preserves_checkpoint_ancestry(
    controller: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, remote = controller
    job = Job(
        command='echo "$ETUDE_FROM_VALUE to $ETUDE_UNTIL_VALUE"; '
        'echo weights > "$ETUDE_OUTPUT_DIR/checkpoint"; '
        'printf \'{"clocks":{"steps":%s},"checkpoint":"checkpoint"}\' '
        '"$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"',
        max_hours=0.01,
        max_usd=Decimal(1),
        until="steps=10",
    )
    first = launch(job)
    assert wait(first, every=0.02, timeout=10)[0].outcome == "succeeded"
    assert status(first).clocks == {"steps": 10}
    continuation = continue_from(first, "steps=20")
    assert continuation.checkpoint is not None and continuation.checkpoint.run == first
    second = queue(continuation)
    assert wait(second, every=0.02, timeout=10)[0].outcome == "succeeded"
    assert logs(second) == "10 to 20\n"
    assert fetch(second, "checkpoint", tmp_path / "out").read_text() == "weights\n"
    assert _open(remote).record(second).checkpoint == continuation.checkpoint
    pair = launch_cohort("python-pair", job.runs(1, 2))
    assert all(s.outcome == "succeeded" for s in wait(*pair, every=0.02, timeout=10))
    assert command("pool", "add", "waiting").returncode == 0
    queued = queue_cohort("python-waiting", job.runs(3, 4), pool="waiting")
    assert all(cancel(run).outcome == "cancelled" for run in queued)
    assert not (client / "launcher.db").exists()


def test_python_continuation_returns_frozen_source_from_controller(
    controller: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    client, _ = controller
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "train.sh").write_text(
        'echo "$ETUDE_FROM_VALUE"; echo saved > "$ETUDE_OUTPUT_DIR/checkpoint"; '
        'printf \'{"clocks":{"steps":%s},"checkpoint":"checkpoint"}\' '
        '"$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"'
    )
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
    run = launch(
        Job(
            command="sh train.sh",
            max_hours=0.01,
            max_usd=Decimal(1),
            repo=str(repo),
            until="steps=10",
        )
    )
    wait(run, every=0.02, timeout=10)
    refs = git(repo, "for-each-ref", "refs/etude/runs")
    continuation = continue_from(run, "steps=20")
    assert continuation.id is None
    assert git(repo, "for-each-ref", "refs/etude/runs") == refs
    repo.rename(tmp_path / "unavailable")
    second = launch(continuation)
    assert wait(second, every=0.02, timeout=10)[0].outcome == "succeeded"
    assert logs(second) == "10\n"
    assert not (client / "launcher.db").exists()


def test_the_controller_is_sent_the_program_and_gets_it_again_if_it_goes_missing(
    controller: tuple[Path, Path],
) -> None:
    client, remote = controller
    (installed,) = (remote / "programs").iterdir()  # sent by `controller set`
    assert (installed / "etude" / "launch" / "cli.py").exists()
    assert command("pools").stdout == "controller  the controller (default)\n"
    shutil.rmtree(installed)
    again = command("pools")  # the command did not start, so it is sent and run again
    assert again.returncode == 0 and again.stdout == "controller  the controller (default)\n"
    assert (installed / "etude" / "launch" / "cli.py").exists()
    assert not (client / "launcher.db").exists()


def test_a_machine_that_cannot_be_a_controller_is_refused_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    monkeypatch.delenv("ETUDE_CONTROLLER_LOCAL", raising=False)
    scripts = tmp_path / "bin"
    scripts.mkdir()
    ssh = scripts / "ssh"
    ssh.write_text("#!/bin/sh\necho 'python3.12: command not found' >&2\nexit 127\n")
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ["PATH"])
    refused = command("controller", "set", "no-python")
    assert refused.returncode == 1
    assert "could not take the program: python3.12: command not found" in refused.stderr
    assert command("controller").stdout.startswith("controller: this machine")


def test_controller_keeps_source_history_and_checks_out_on_client(
    controller: tuple[Path, Path],
    repository: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, remote = controller
    repo, origin = repository
    monkeypatch.setenv("ETUDE_HOME", str(client))
    (repo / "train").write_text("client snapshot\n")
    job = repo.parent / "snapshot.py"
    job.write_text(
        f'from etude.launch.records import Job, Run\ndef runs(): return Job(command="true", max_hours=1, repo={str(repo)!r})\n'
    )
    assert command("pool", "add", "waiting").returncode == 0
    sent = command("queue", str(job), "--pool", "waiting")
    assert sent.returncode == 0, sent.stderr
    id = sent.stdout.split()[0]
    record = _open(remote).record(id)
    assert record.source is not None and record.source.snapshot
    assert record.source.origin == str(origin)
    assert git(origin, "rev-parse", f"refs/etude/runs/{id}") == record.spec.revision
    assert "uncommitted changes" in sent.stdout
    repo.rename(repo.with_name("unavailable"))
    listed = command("runs", "--contains", "v1", "--pool", "waiting")
    assert listed.returncode == 0 and id in listed.stdout, listed.stderr
    destination = client / "recovered"
    result = command("source", id, str(destination))
    assert result.returncode == 0, result.stderr
    assert (destination / "train").read_text() == "client snapshot\n"
    assert not (client / "launcher.db").exists()
    assert _open(remote).read(id).phase == "queued"
