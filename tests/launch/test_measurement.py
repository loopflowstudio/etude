"""Operation baselines, not speed targets. Never assert wall-clock durations.

Known debt belongs to task_b8992fd2036f4cbd89ffdfc195361e91: reads refreshing
machines, repeated SQL scans and per-run git preparation remain unchanged here.
"""

from __future__ import annotations

import json
import os
import pstats
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from etude.cli import main
from etude.launch import cli
from etude.launch.commands import Kind
from etude.launch.measurement import span
from etude.launch.providers.runpod.http import PodHTTP
from etude.launch.records import Job
from etude.launch.schemas import Packet
from etude.launch.ssh import SSH
from etude.launch.storage.s3 import Bucket, Keys
from tests.launch.counting import Counting, Counts
from tests.launch.fake import Bench, bench
from tests.launch.test_source import repository  # noqa: F401


def baseline(count: int) -> dict[str, Counts]:
    """Observed 2026-10-13 at 1, 10, 100 and 1000 active sessions and runs.

    Status refreshes sessions; runs is already a database-only read. These fixed
    coefficients pin known debt, not a target or a model of physical HTTP calls.
    """
    return {
        "status": Counts(provider=2 * count, bucket=count, database=64 * count + 13),
        "runs": Counts(database=13 * count + 1),
        "tick": Counts(provider=2 * count, bucket=count, database=26 * count + 12),
    }


def sessions(tmp_path: Path, count: int) -> tuple[Bench, tuple[str, ...], Counting]:
    b = bench(tmp_path, max_on_demand=count)
    ids = b.scheduler.queue_cohort(
        "baseline",
        Job(command="true", max_hours=1, max_usd=Decimal(1)).runs(*range(count)),
        "l4",
        parallel=count,
    )
    b.scheduler.tick()
    b.clock.advance(1)
    b.scheduler.tick()
    return b, ids, Counting(b.store, b.service)


def costs(tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch) -> dict[str, Counts]:
    b, ids, counting = sessions(tmp_path, count)
    monkeypatch.setattr(cli, "default", lambda: b.scheduler)
    results: dict[str, Counts] = {}
    with counting.record():
        cli.status(list(ids))
        results["status"] = counting.snapshot()
    with counting.record():
        cli.history()
        results["runs"] = counting.snapshot()
    with counting.record():
        b.scheduler.tick()
        results["tick"] = counting.snapshot()
    return results


@pytest.mark.parametrize("count", [1, 10])
def test_live_session_costs(
    tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = costs(tmp_path, count, monkeypatch)
    capsys.readouterr()
    assert result == baseline(count)


def test_command_kinds() -> None:
    for app in (
        cli.app,
        cli.pool_app,
        cli.machine_app,
        cli.provider_app,
        cli.storage_app,
        cli.controller_app,
    ):
        for command in app.registered_commands:
            assert command.callback is not None
            assert isinstance(getattr(command.callback, "command_kind", None), Kind)
    assert getattr(cli.controller_show, "command_kind") == Kind.read


def test_ask_wait_counts(tmp_path: Path) -> None:
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)
    with counting.record():
        id = b.scheduler.queue(Job(command="true", max_hours=1, max_usd=Decimal(1)), "l4")
        ask = counting.snapshot()
    with counting.record():
        with pytest.raises(TimeoutError):
            b.scheduler.wait(id, timeout=0)
        wait = counting.snapshot()
    assert ask == Counts(database=22)  # target: ask records intent without remote effects
    # Packet construction reads its paid window once for deadline, cap and reuse.
    assert wait == Counts(provider=4, bucket=1, database=70)  # known debt: wait advances work


def test_cohort_git_and_database_counts(
    tmp_path: Path,
    repository: tuple[Path, Path],  # noqa: F811
) -> None:
    repo, _ = repository
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)
    with counting.record():
        b.scheduler.queue_cohort(
            "cohort",
            Job(command="true", repo=str(repo), max_hours=1, max_usd=Decimal(1)).runs(1, 2, 3),
            "l4",
            parallel=3,
        )
        result = counting.snapshot()
    assert result == Counts(git=57, database=35)  # known debt: 19 git calls per run


@pytest.mark.parametrize(
    ("command", "count", "forbidden"),
    [
        (["--help"], 9, ("numpy", "pydantic", "etude.launch.scheduler", "etude.launch.worker")),
        (
            ["worker", "--help"],
            9,
            ("numpy", "pydantic", "etude.launch.worker", "etude.launch.scheduler"),
        ),
        (["pools"], 36, ("numpy", "boto3", "etude.launch.worker")),
        (
            ["worker", "{directory}"],
            23,
            (
                "numpy",
                "boto3",
                "etude.launch.scheduler",
                "etude.launch.client",
                "etude.launch.controller",
            ),
        ),
    ],
)
def test_command_import_counts(
    tmp_path: Path, command: list[str], count: int, forbidden: tuple[str, ...]
) -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    directory = tmp_path / "files"
    directory.mkdir()
    if command == ["worker", "{directory}"]:
        packet = Packet(
            session="empty",
            deadline=datetime.now(UTC) + timedelta(seconds=10),
            work=(),
            kept=False,
            autorun=False,
            cap_usd=Decimal(0),
        )
        (directory / "packet.json").write_text(packet.model_dump_json())
    args = [value.format(directory=directory) for value in command]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json; from etude.cli import main; "
            f"result=main({args!r}); print(json.dumps(sorted(sys.modules))); "
            "sys.exit(result)",
        ],
        env={**os.environ, "PYTHONPATH": str(source), "ETUDE_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 0, done.stderr
    modules = json.loads(done.stdout.splitlines()[-1])
    loaded = [name for name in modules if name.startswith("etude")]
    # Count actual command execution as well as help: dependencies belong to the
    # selected command, and the courier must never pull NumPy into the worker.
    assert len(loaded) == count, loaded
    assert not [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    # One `etude` command: tracking contributes only its command table.
    assert {name for name in loaded if name.startswith("etude.track")} <= {
        "etude.track",
        "etude.track.cli",
        "etude.track.errors",
    }
    if command[0] == "worker":
        assert not (tmp_path / "home" / "launcher.db").exists()


def test_phases_reads_only_retained_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b, ids, counting = sessions(tmp_path, 1)
    monkeypatch.setattr(cli, "default", lambda: b.scheduler)
    with counting.record():
        cli.status(list(ids), phases=True)
        result = counting.snapshot()
    output = capsys.readouterr().out
    assert "queued" in output and "machine asked for" in output and "code started" in output
    assert "finished not recorded" in output
    assert "machine confirmed not recorded (receipt retained)" in output
    assert result == Counts(database=8)  # one evidence selection; no external calls


def test_profile_and_opt_in_spans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "help.pstats"
    assert main(["--profile", str(profile), "--help"]) == 0
    assert "track what they record" in capsys.readouterr().out
    assert "_main" in pstats.Stats(str(profile)).get_stats_profile().func_profiles
    destination = tmp_path / "spans.log"
    monkeypatch.setenv("ETUDE_SPANS", str(destination))
    values = iter([10.0, 10.25])
    monkeypatch.setattr("etude.launch.measurement.time.perf_counter", lambda: next(values))
    with pytest.raises(ValueError), span("ssh.call"):
        raise ValueError("secret payload")
    assert destination.read_text() == "ssh.call seconds=0.250000000\n"
    monkeypatch.delenv("ETUDE_SPANS")
    with span("bucket.read"):
        pass
    assert destination.read_text() == "ssh.call seconds=0.250000000\n"


@pytest.mark.scale
@pytest.mark.parametrize("count", [10, 100, 1000])
def test_scale_counts(
    tmp_path: Path, count: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = costs(tmp_path, count, monkeypatch)
    capsys.readouterr()
    print("workers operation provider bucket ssh git database")
    for operation, value in result.items():
        print(count, operation, value.provider, value.bucket, value.ssh, value.git, value.database)
    assert result == baseline(count)


def test_counting_covers_physical_transports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)

    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        return 200, b""

    def process(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(subprocess, "run", process)
    bucket = Bucket(
        "fixture",
        "us-west-2",
        Keys(access_key="fixture", secret_key="fixture"),
        counting.transport("bucket", send),
    )
    provider = PodHTTP("fixture", counting.transport("provider", send))
    with counting.record():
        provider.delete_self("fixture")
        bucket.put("key", b"bytes")
        assert bucket.get("key") == b""
        assert SSH("fixture").call("observe", "worker").observation is None
        assert counting.snapshot() == Counts(provider=1, bucket=2, ssh=1)
