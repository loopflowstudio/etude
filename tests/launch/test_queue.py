"""Queueing grows pools; launch acquires capacity before admitting executable work."""

from __future__ import annotations

from decimal import Decimal
from importlib import import_module
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch.client import queue, queue_cohort, wait
from etude.launch.database import Database
from etude.launch.errors import LaunchError
from etude.launch.executor import Refused
from etude.launch.records import Job, LauncherError, Offer, OnDemand, Pool, Stale
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import Packet, Work
from etude.train.records import ParentCheckpoint, Step
from etude.train.run import track
from tests.launch.fake import L4, bench


def job() -> Job:
    return Job(command="train", max_hours=1, max_usd=Decimal(1))


def test_queue_grows_after_restart(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=0)
    run = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(run).phase == "queued"
    # Previously queued rows keep their metadata but gain the corrected scheduling behavior.
    b.store._db.execute("UPDATE runs SET queued=1 WHERE id=?", (run,))
    b.scheduler.set_ondemand_max("l4", 1)
    reopened = Scheduler(
        Database(tmp_path / "launcher.db"), {"runpod": lambda held: b.service}, b.clock
    )
    reopened.tick()
    assert reopened.read(run).phase == "setting_up"
    assert b.service.creates == 1


def test_queue_needs_no_capacity(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=0)
    b.scheduler.add_pool(Pool(name="empty"))
    one = b.scheduler.queue(job(), "empty")
    b.scheduler.tick()
    ids = b.scheduler.queue_cohort("pair", job().runs(1, 2), rent="l4", parallel=2)
    b.scheduler.tick()
    assert all(b.scheduler.read(id).phase == "queued" for id in (one, *ids))
    assert b.service.creates == 0


def test_rented_machine_takes_queued_cohort_and_never_replaces_it(tmp_path: Path) -> None:
    b = bench(tmp_path)
    machine = b.scheduler.rent("l4", hours=3)
    ids = b.scheduler.queue_cohort("pair", job().runs(1, 2), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(ids[0]).machine == machine
    assert b.scheduler.read(ids[1]).phase == "queued"
    b.run(601)
    b.run(601)
    assert all(b.scheduler.read(id).outcome == "succeeded" for id in ids)
    assert {a.machine for id in ids for a in b.scheduler.attempts(id)} == {machine}
    assert b.service.creates == 1
    assert b.store.machine(machine).deleted_at is None
    b.scheduler.terminate(machine)


def test_queue_retry_replaces_lost_capacity(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.rent("l4", hours=2)
    run = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.service.kill(b.scheduler.attempts(run)[0].session)  # before code starts
    b.run(1)
    b.run(1)
    assert b.scheduler.read(run).phase == "running"
    assert b.scheduler.attempts(run)[0].cause == "machine_lost_before_start"
    assert b.service.creates == 2


def test_queue_uses_an_added_machine(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("owned", "host")
    run = b.scheduler.queue(job(), "owned", rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(run).machine == "owned"
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    assert b.service.creates == 0


@pytest.mark.parametrize("cohort", [False, True])
def test_cli_queue_records_the_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cohort: bool,
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    path = tmp_path / "job.py"
    path.write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="train", max_hours=1, max_usd=1)'
        + (".runs(1, 2)" if cohort else "")
    )
    assert (
        main(["queue", str(path), "--rent", "l4", *(["--cohort", "pair"] if cohort else [])]) == 0
    )
    ids = [line.split()[0] for line in capsys.readouterr().out.splitlines()]
    assert len(ids) == (2 if cohort else 1)
    assert all(b.scheduler.record(id).autorun for id in ids)
    assert b.service.creates == 0
    b.scheduler.tick()
    assert b.service.creates == 1


def test_public_queue_functions_run_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    one = queue(Job(command="echo queued", max_hours=0.01, max_usd=Decimal(1)))
    assert wait(one, every=0.01, timeout=10)[0].outcome == "succeeded"
    ids = queue_cohort("pair", Job(command="true", max_hours=0.01, max_usd=Decimal(1)).runs(1, 2))
    assert all(s.outcome == "succeeded" for s in wait(*ids, every=0.01, timeout=10))


def test_tick_moves_an_older_queue_retry(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    earlier = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(earlier).phase == "queued"
    queued = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.service.creates == 3
    assert b.scheduler.read(queued).phase == "setting_up"


@pytest.mark.parametrize("reason", ["limit", "owned", "price", "job", "cohort", "provider"])
def test_launch_refuses_without_recording(tmp_path: Path, reason: str) -> None:
    b = bench(tmp_path, max_machines=0 if reason == "limit" else 1)
    pool: str | None = None
    rent: str | None = "l4"
    selected = job()
    cap = None
    expected = reason
    if reason == "owned":
        b.scheduler.add_pool(Pool(name="empty"))
        pool, rent = "empty", None
        expected = "no free machine"
    elif reason == "price":
        b.service.usd_per_hour = Decimal(999)
        # The fake on-demand has no ceiling; use a deliberately low one.
        b.scheduler.add_ondemand(
            OnDemand(name="cheap", template=L4, max_machines=1, max_usd_per_hour=Decimal(".1"))
        )
        rent = "cheap"
    elif reason == "job":
        selected = Job(command="train", max_hours=1, max_usd=Decimal(".01"))
    elif reason == "cohort":
        cap = Decimal(".01")
    elif reason == "provider":

        def unavailable() -> Offer:
            raise RuntimeError("provider has no capacity")

        b.service.offer = unavailable  # type: ignore[method-assign]
    with pytest.raises(LauncherError, match=expected + ".*etude queue"):
        b.scheduler.launch_cohort("test", selected.runs(1, 2), pool, rent=rent, max_usd=cap)
    assert b.store.runs() == ()
    assert b.store.sessions() == ()
    assert b.service.creates == 0


def test_launch_checks_only_first_parallel_runs_and_queue_drains(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    ids = b.scheduler.launch_cohort("six", job().runs(*range(6)), rent="l4", parallel=1)
    assert all(s.outcome == "succeeded" for s in b.scheduler.wait(*ids, every=601))
    assert b.service.creates == 1


def test_launch_cannot_count_one_machine_twice(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    with pytest.raises(LauncherError, match="machine limit"):
        b.scheduler.launch_cohort("pair", job().runs(1, 2), rent="l4", parallel=2)
    assert not b.store.runs()


def test_cli_continue_waits_without_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    parent = b.scheduler.launch(
        Job(command="train", max_hours=1, max_usd=Decimal(1), until="steps=3"), rent="l4"
    )
    b.run(601)
    # What the parent's job recorded, as tracking keeps it in its runs directory.
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"weights")
    tracked = track(
        experiment="capacity", clocks=[Step("steps", "steps")], run=parent, directory=tmp_path
    )
    tracked.checkpoint(weights, steps=3)
    tracked.finish()
    b.scheduler.set_ondemand_max("l4", 0)
    argv = ["continue", "--run", parent, "--until", "steps=7", "--source", str(tmp_path)]
    assert main(argv) == 0
    child = capsys.readouterr().out.split()[0]
    record = b.scheduler.record(child)
    (given,) = record.spec.environment
    handed = ParentCheckpoint.model_validate_json(given.value)
    assert (handed.parent.source_id, handed.checkpoint.at) == (parent, {"steps": 3})
    assert record.spec.until == "steps=7" and record.autorun
    assert b.scheduler.read(child).phase == "queued"


def test_launch_refusal_records_no_run(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    with pytest.raises(LauncherError, match="provider refused"):
        b.scheduler.launch(job(), rent="l4")
    assert not b.store.runs()
    assert b.store.sessions()[0].refused_at is not None
    b.scheduler.tick()
    assert b.service.creates == 1


def test_launch_checks_total_cost_of_parallel_runs(tmp_path: Path) -> None:
    b = bench(tmp_path)
    with pytest.raises(LauncherError, match="cohort dollars"):
        b.scheduler.launch_cohort(
            "pair", job().runs(1, 2), rent="l4", parallel=2, max_usd=Decimal(".9")
        )
    assert not b.store.runs()
    assert not b.store.sessions()


def test_queue_never_calls_provider(tmp_path: Path) -> None:
    b = bench(tmp_path)

    def unavailable() -> Offer:
        pytest.fail("queue contacted the provider")

    b.service.offer = unavailable  # type: ignore[method-assign]
    run = b.scheduler.queue(job(), rent="l4")
    assert b.scheduler.read(run).phase == "queued"
    assert b.service.creates == 0


@pytest.mark.parametrize("lost_reply", [False, True])
def test_partial_launch_cleans_up_without_executable_work(
    tmp_path: Path,
    lost_reply: bool,
) -> None:
    b = bench(tmp_path)
    create = b.service.create
    calls = 0

    def second_fails(packet: Packet) -> str:
        nonlocal calls
        calls += 1
        assert packet.work == () and packet.kept
        assert not b.store.runs()
        if calls == 2:
            if lost_reply:
                b.service.lose_reply = 1
            else:
                raise Refused("full")
        return create(packet)

    b.service.create = second_fails  # type: ignore[method-assign]
    with pytest.raises(LauncherError, match="unknown" if lost_reply else "refused"):
        b.scheduler.launch_cohort("pair", job().runs(1, 2), rent="l4", parallel=2)
    assert not b.store.runs() and not b.store.cohort_exists("pair")
    assert b.store.sessions()[0].ended_at is not None
    b.scheduler.tick()  # lookup the unknown identity
    b.scheduler.tick()  # delete it without ever handing over work
    assert calls == 2
    assert all(not r.work() and r.killed_at is not None for r in b.service.sessions.values())
    assert all(w.ended_at or w.refused_at for w in b.store.sessions())


def test_launch_lock_fences_rivals_and_keeps_other_pools_working(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    b.scheduler.add_machine("shed", "owned")
    other = Scheduler(Database(tmp_path / "launcher.db"), b.scheduler._services, b.clock)
    create = b.service.create
    queued: list[str] = []

    def competing(packet: Packet) -> str:
        with pytest.raises(LauncherError, match="it is busy"):
            other.launch(job(), rent="l4")
        with pytest.raises(LauncherError, match="busy"):
            other.rent("l4", 1)
        queued.append(other.queue(job(), rent="l4"))
        local = other.launch(job(), "shed")
        other.tick()
        assert other.read(local).machine is not None
        assert other.read(queued[0]).machine is None
        assert packet.work == ()
        return create(packet)

    b.service.create = competing  # type: ignore[method-assign]
    run = b.scheduler.launch(job(), rent="l4")
    assert b.scheduler.read(run).machine is not None
    assert b.scheduler.read(queued[0]).machine is None
    assert b.service.creates == 1


def test_expired_launch_cannot_commit_and_orphan_is_cleaned(tmp_path: Path) -> None:
    b = bench(tmp_path)
    other = Scheduler(Database(tmp_path / "launcher.db"), b.scheduler._services, b.clock)
    create = b.service.create

    def delayed(packet: Packet) -> str:
        receipt = create(packet)
        b.clock.advance(301)
        other.tick()  # expired owner cannot block recovery
        return receipt

    b.service.create = delayed  # type: ignore[method-assign]
    with pytest.raises(LaunchError, match="expired") as caught:
        b.scheduler.launch(job(), rent="l4")
    assert isinstance(caught.value.__cause__, Stale)
    assert not b.store.runs()
    evidence = b.scheduler.logs(run=caught.value.runs[0])
    assert evidence.launches[0].id == caught.value.launch
    assert evidence.launches[0].state == "failed"
    assert any(
        "placement lock expired" in diagnostic.message for diagnostic in evidence.diagnostics
    )
    other.tick()
    assert b.store.sessions()[0].ended_at is not None
    assert not next(iter(b.service.sessions.values())).work()


def test_launch_uses_claimed_added_and_rented_machines(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    reserved = b.scheduler.rent("l4", 2)
    b.scheduler.add_machine("owned", "host")
    ids = b.scheduler.launch_cohort("pair", job().runs(1, 2), "owned", rent="l4", parallel=2)
    assert b.scheduler.read(ids[0]).machine == reserved
    assert b.scheduler.read(ids[1]).machine == "owned"
    assert b.service.creates == 1
    assert all(s.outcome == "succeeded" for s in b.scheduler.wait(*ids, every=601))


def test_launch_confirms_first_wave_before_delivering_any_work(tmp_path: Path) -> None:
    b = bench(tmp_path)
    add = b.service.add

    def delivered(session_id: str, work: Work) -> None:
        assert len(b.store.runs()) == 3
        assert b.service.creates == 2
        assert all(session.receipt is not None for session in b.store.sessions())
        assert all(not held.packet.work for held in b.service.sessions.values())
        add(session_id, work)

    b.service.add = delivered  # type: ignore[method-assign]
    first, second, later = b.scheduler.launch_cohort(
        "three",
        job().runs(1, 2, 3),
        rent="l4",
        parallel=2,
    )
    assert b.scheduler.read(first).machine != b.scheduler.read(second).machine
    assert b.scheduler.read(later).machine is None
    assert len(b.service.sessions) == 2


def test_machines_lists_rented_windows_and_terminate_takes_the_machine_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    assert main(["rent", "--ondemand", "l4", "--hours", "1"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    name, ondemand, state = line.split("  ", 2)
    assert ondemand == "l4" and name.startswith("l4-") and state.startswith("rented until ")
    assert main(["ondemands"]) == 0
    assert f"rented: {name}" in capsys.readouterr().out
    assert main(["terminate", "--machine", name]) == 0
    assert capsys.readouterr().out == ""  # nothing is rented now
    assert main(["machines", "--all"]) == 0
    assert f"{name}  l4  rented, ended " in capsys.readouterr().out
