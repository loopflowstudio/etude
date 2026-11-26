"""Operation baselines, not speed targets. Never assert wall-clock durations.

Known debt belongs to task_b8992fd2036f4cbd89ffdfc195361e91: waits and asks
refreshing machines, repeated SQL scans and per-run git preparation remain unchanged here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch import cli, client, server
from etude.launch.aws import Keys
from etude.launch.commands import Kind
from etude.launch.database import launcher_file, upgrade
from etude.launch.executor import Execution
from etude.launch.provisioner.providers.runpod.http import PodHTTP
from etude.launch.records import Machine, OnDemand, Pool
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Packet
from etude.launch.ssh import SSH, SessionRequest
from etude.launch.storage.s3 import Bucket
from etude.launch.storage.transport import HTTPReply
from etude.spec import Job
from tests.launch.counting import Counting, Counts, query_plan
from tests.launch.fake import Bench, bench
from tests.launch.test_source import repository  # noqa: F401


def baseline(count: int) -> dict[str, Counts]:
    """Observed 2026-10-13 (status 2026-10-27) at 1, 10, 100 and 1000 active sessions and runs.

    Status and runs are database-only reads; tick refreshes sessions. These fixed
    coefficients pin known debt, not a target or a model of physical HTTP calls.
    Schema fencing adds a generation read per write transaction and serializes
    standalone writes (2026-11-14). Each read of a run also reads its machine's attempts once, for how many
    shared it (2026-11-06). Storage-backed active attempts read their storage
    selection and one indexed delivery timestamp (2026-11-11).
    """
    # One indexed finishing-session read per supply settles already requested ends.
    return {
        "status": Counts(database=49 * count + 1),
        "runs": Counts(database=17 * count + 1),
        "tick": Counts(provider=2 * count, bucket=count, database=30 * count + 22),
    }


def sessions(tmp_path: Path, count: int) -> tuple[Bench, tuple[str, ...], Counting]:
    b = bench(tmp_path, max_machines=count)
    ids = b.scheduler.queue(
        Job(command="true", max_hours=1, max_usd=Decimal(1)).runs(*range(count)),
        rent="l4",
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
        cli.ondemand_app,
        cli.machine_app,
        cli.provider_app,
        cli.storage_app,
        cli.server_app,
    ):
        for command in app.registered_commands:
            assert command.callback is not None
            assert isinstance(getattr(command.callback, "command_kind", None), Kind)
    assert getattr(cli.server_show, "command_kind") == Kind.read


def test_ask_wait_counts(tmp_path: Path) -> None:
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)
    with counting.record():
        (id,) = b.scheduler.queue(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
        ask = counting.snapshot()
    with counting.record():
        with pytest.raises(TimeoutError):
            b.scheduler.wait(id, timeout=0)
        wait = counting.snapshot()
    assert ask == Counts(database=37)  # target: ask records intent without remote effects
    # Creation also reads provider safeguards for affordable time and the reserved budget,
    # and the status it returns reads the machine's attempts for how many shared it.
    # Refusal pause and shutdown settlement add three indexed local reads.
    assert wait == Counts(provider=4, bucket=1, database=105)  # known debt: wait advances work


@pytest.mark.parametrize("history", [1, 4096])
def test_refusal_status_and_pause_query_counts(tmp_path: Path, history: int) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    (run,) = b.scheduler.queue(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
    b.scheduler.tick()
    b.store.add_pool(Pool(name="history"))
    b.store.add_machine(Machine(name="history", kind="ssh", pool="history", address="fixture"))
    old = (b.clock() - timedelta(days=1)).isoformat()
    b.store._db.executemany(
        "INSERT INTO sessions(id, machine, planned_at, refused_at, refusal) VALUES (?, ?, ?, ?, 'no capacity')",
        [(f"history-{index}", "history", old, old) for index in range(history - 1)],
    )
    b.store._db.execute("UPDATE attempts SET number = ? WHERE run = ?", (history, run))
    b.store._db.executemany(
        """INSERT INTO attempts(id, run, session, machine, number, position,
               limit_seconds, limit_kind, outcome, cause)
           VALUES (?, ?, ?, 'history', ?, 0, 3600, 'out_of_time', 'lost', 'capacity_refused')""",
        [(f"old-{index}", run, f"history-{index}", index + 1) for index in range(history - 1)],
    )
    restarted = Scheduler(b.store, {}, clock=b.clock)
    counting = Counting(b.store, b.service)
    with counting.record():
        assert str(restarted.read(run)) == "queued (provider refused: no capacity), $0.00"
        status = counting.snapshot()
    with counting.record():
        (refused,) = b.store.recent_refusals("l4", b.clock() - timedelta(seconds=60), limit=1)
        pause = counting.snapshot()
    with counting.record():
        assert [record.id for record in b.store.waiting("l4")] == [run]
        waiting = counting.snapshot()
    assert refused.refusal == "no capacity"
    assert status == Counts(database=6)
    assert pause == Counts(database=1)
    assert waiting == Counts(database=1)
    with counting.record():
        b.scheduler.tick()
        planning = counting.snapshot()
    assert planning == Counts(database=48)
    assert b.service.creates == 1
    b.clock.advance(60)
    with counting.record():
        b.scheduler.tick()
        admission = counting.snapshot()
    assert admission == Counts(provider=4, bucket=1, database=111), admission.model_dump()
    assert b.scheduler.attempts(run)[-1].number == history + 1
    assert b.scheduler.read(run).attempts == 1
    since = b.clock() - timedelta(seconds=60)
    for supply, limit in (("l4", 1), ("history", None)):
        plan = query_plan(b.store, lambda s=supply, n=limit: b.store.recent_refusals(s, since, limit=n))
        assert any("sessions_refused" in step for step in plan), plan
        assert not any("SCAN" in step for step in plan), plan
    plan = query_plan(b.store, lambda: b.store.waiting("l4"))
    assert sum("SEARCH a USING INDEX attempts_run" in step for step in plan) == 2, plan
    assert not any("SCAN a" in step or "SCAN r" in step for step in plan), plan


def test_several_run_git_and_database_counts(
    tmp_path: Path,
    repository: tuple[Path, Path],  # noqa: F811
) -> None:
    repo, _ = repository
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)
    with counting.record():
        b.scheduler.queue(
            Job(command="true", repo=str(repo), max_hours=1, max_usd=Decimal(1)).runs(1, 2, 3),
            rent="l4",
        )
        result = counting.snapshot()
    assert result == Counts(git=57, database=49)  # known debt: 19 git calls per run


@pytest.mark.parametrize(
    ("command", "count", "forbidden"),
    [
        (
            ["--help"],
            8,
            ("numpy", "pydantic", "etude.launch.scheduler", "etude.launch.worker"),
        ),
        (
            ["worker", "--help"],
            8,
            ("numpy", "pydantic", "etude.launch.worker", "etude.launch.scheduler"),
        ),
        # Client composition includes four modules each for Vast, Lium and Colab,
        # three each for EC2 and Lambda, the VM host script they share, the AWS
        # signer, the request every provider shares, the storage socket policy
        # it forwards to workers, and shared run-identity validation.
        # The layout adds provisioner/scheduler packages, two scheduler leaves, and shared openssh argv.
        (["pools"], 74, ("numpy", "boto3", "etude.launch.worker")),
        (
            ["worker", "{directory}"],
            36,  # includes the shared Etude home path and S3's AWS signer
            (
                "numpy",
                "boto3",
                "etude.launch.scheduler",
                "etude.launch.client",
                "etude.launch.server",
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
    if command == ["pools"]:
        upgrade(tmp_path / "home", development=True)
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
    assert {name for name in loaded if name.startswith("etude.train")} <= {
        "etude.train",
        "etude.train.cli",
    }
    if command[0] == "worker":
        assert not launcher_file(tmp_path / "home").exists()


def test_reads_need_no_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b, ids, counting = sessions(tmp_path, 1)
    b.scheduler.logs(run=ids[0], refresh=True)  # evidence a read can show
    reached: list[str] = []

    def no_key(held: Machine | OnDemand) -> Execution:
        reached.append(held.name)
        raise LauncherError("RUNPOD_API_KEY is not set")

    keyless = Scheduler(b.store, {"runpod": no_key, "ssh": no_key}, clock=b.clock)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(cli, "default", lambda: keyless)
    monkeypatch.setattr(client, "default", lambda: keyless)
    machine = b.store.sessions()[0].machine
    reads = {
        "status": ["status", ids[0]],
        "history": ["runs"],
        "pools": ["pools"],
        "ondemands": ["ondemands"],
        "machines": ["machines", "--all"],
        "providers": ["providers"],
        "storages": ["storages"],
        "server_show": ["server"],
        "usage": ["usage", "--machine", machine],
        "logs": ["logs", "--run", ids[0]],
    }
    declared = {
        command.callback.__name__
        for app in (cli.app, cli.pool_app, cli.ondemand_app, cli.machine_app, cli.storage_app)
        for command in app.registered_commands
        if command.callback is not None
        and getattr(command.callback, "command_kind", None) == Kind.read
    } | {"server_show"}
    assert set(reads) == declared
    with counting.record():
        for argv in reads.values():
            assert main(argv) == 0, argv
        result = counting.snapshot()
    output = capsys.readouterr().out
    assert reached == []
    assert (result.provider, result.bucket, result.ssh, result.git) == (0, 0, 0, 0)
    assert "last loop: never" in output and "[job machine=" in output
    assert "--follow collects new evidence from machines" in output


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
    # Installed archives carry provenance without consulting the checkout's Git history.
    monkeypatch.setattr("etude.launch.provisioner.providers.package.frontier", lambda root: b"{}")
    b = bench(tmp_path)
    counting = Counting(b.store, b.service)

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> HTTPReply:
        return HTTPReply(200, b"")

    def process(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(subprocess, "run", process)
    bucket = Bucket(
        "fixture",
        "us-west-2",
        Keys(access_key="fixture", secret_key="fixture"),
        counting.transport("bucket", lambda m, u, b, h: HTTPReply(status=200, body=b"")),
    )
    provider = PodHTTP("fixture", counting.transport("provider", send))
    with counting.record():
        provider.delete_self("fixture")
        bucket.put("key", b"bytes")
        assert bucket.get("key") == b""
        assert (
            SSH("fixture").call(SessionRequest(action="observe", session="worker")).observation
            is None
        )
        assert counting.snapshot() == Counts(provider=1, bucket=2, ssh=1)


@pytest.mark.parametrize("history", [1, 500])
def test_idle_pace_query_count_ignores_finished_run_history(
    tmp_path: Path, history: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    b = bench(tmp_path)
    b.store.add_pool(Pool(name="history"))
    b.store.add_machine(Machine(name="history", kind="ssh", pool="history", address="fixture"))
    old = (b.clock() - timedelta(days=1)).isoformat()
    spec = Job(command="true", max_hours=1, max_usd=Decimal(1)).model_dump_json()
    b.store._db.executemany(
        """INSERT INTO runs(id, job, spec, pool, created_at)
           VALUES (?, 'job', ?, 'history', ?)""",
        [(f"done-{index}", spec, old) for index in range(history)],
    )
    b.store._db.executemany(
        "INSERT INTO sessions(id, machine, planned_at, ended_at) VALUES (?, 'history', ?, ?)",
        [(f"sess-{index}", old, old) for index in range(history)],
    )
    b.store._db.executemany(
        """INSERT INTO attempts(id, run, session, machine, number, position,
               limit_seconds, limit_kind, started_at, finished_at, outcome)
           VALUES (?, ?, ?, 'history', 1, 0, 3600, 'out_of_time', ?, ?, 'succeeded')""",
        [(f"att-{index}", f"done-{index}", f"sess-{index}", old, old) for index in range(history)],
    )
    counting = Counting(b.store, b.service)
    with counting.record():
        assert b.store.runs(unfinished=True) == ()
        unfinished_query = counting.snapshot()
    assert unfinished_query == Counts(database=1)

    class _Stop(BaseException):
        pass

    def stop_on_sleep(seconds: float) -> None:
        raise _Stop

    monkeypatch.setattr("etude.launch.server.time.sleep", stop_on_sleep)
    with counting.record():
        with pytest.raises(_Stop):
            server.loop(b.scheduler)
        idle_tick = counting.snapshot()
    assert idle_tick == Counts(database=42), idle_tick.model_dump()
    plan = query_plan(b.store, lambda: b.store.runs(unfinished=True))
    assert not any("SCAN a" in step for step in plan), plan
