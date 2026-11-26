"""The launcher's rules, against a provider that runs on a test clock."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from unittest.mock import patch

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.database import launcher_file
from etude.launch.listing import show_machines, show_ondemands, show_runs
from etude.launch.provisioner.machines import Refused
from etude.launch.records import (
    GPU,
    LogStream,
    Machine,
    MachineTemplate,
    Offer,
    OnDemand,
    Pool,
    Provider,
    Rental,
    Session,
    Stale,
    Storage,
    Tally,
    Target,
)
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Observation
from etude.launch.ssh import Unreachable
from etude.launch.storage.disk import DirectoryJournal
from etude.records import Clock as RunClock
from etude.records import RunRecord
from etude.spec import Job, RunRequest
from etude.storage_timeout import StorageUnavailable
from etude.train.tracking import TrainingRun as TrackedRun
from tests.launch.counting import Counting, job_text
from tests.launch.fake import L4, Bench, Clock, FakeService, bench, open_database
from tests.train.tracking import open_run

USD_PER_RUN = Decimal("0.59") * 600 / 3600  # every fake run takes 600 seconds


def job(**changes: object) -> Job:
    fields: dict[str, object] = {
        "command": "python train.py",
        "max_hours": 1.0,
        "max_usd": Decimal(5),
        **changes,
    }
    return Job(**fields)  # type: ignore[arg-type]


def test_one_run_finishes_and_its_session_is_confirmed_ended(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(until="steps=26000"), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(one).phase == "setting_up"
    b.run(1)
    assert b.scheduler.read(one).phase == "running"
    b.run(600)
    status = b.scheduler.read(one)
    assert (status.phase, status.outcome, status.clocks) == ("done", "succeeded", {"steps": 26_000})
    assert status.rental_ended is True and status.attempts == 1
    assert abs(status.usd - USD_PER_RUN) < Decimal("0.001")
    assert str(status) == "done: succeeded, steps 26000, ~$0.10"
    assert job_text(b.scheduler.logs(refresh=True, run=one)).startswith("log of ")


def test_known_rental_stays_starting_until_the_worker_checks_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    checked_in = [False]
    monkeypatch.setattr(b.service, "started", lambda session_id: checked_in[0])
    (run,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    machine = b.store.machine(b.scheduler.attempts(run)[0].machine)
    assert machine.provider_id is not None
    assert machine.started_at is None and machine.state(b.clock()) == "starting"
    checked_in[0] = True
    b.run(1)
    machine = b.store.machine(machine.name)
    assert machine.started_at is not None and machine.state(b.clock()) == "available"


def test_a_lost_create_reply_is_looked_up_and_never_asked_twice(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.lose_reply = 1
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(one).phase == "renting"
    b.run(1)
    assert b.scheduler.read(one).phase == "setting_up"
    b.run(700)
    assert b.scheduler.read(one).outcome == "succeeded"
    assert b.service.creates == 1


def test_a_create_that_never_appears_is_lost_after_the_rental_deadline_and_frees_the_pool(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, max_machines=1)
    b.service.lose_reply = 1
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    held = b.scheduler.attempts(one)[0].session
    del b.service.sessions[held]  # the provider has no such session
    b.run(3599)
    assert b.scheduler.read(one).phase == "renting"
    (two,) = b.scheduler.queue(job(), rent="l4")
    b.run(1)
    status = b.scheduler.read(one)
    assert (status.outcome, status.cause, status.rental_ended) == (
        "lost",
        "create_unconfirmed",
        False,
    )
    assert status.attempts == 1 and status.usd == 0  # no machine existed, so none was paid for
    assert held not in {s.id for s in b.store.unsettled_sessions()}
    assert b.service.creates == 2
    assert b.scheduler.read(two).phase == "setting_up"
    b.run(1)
    assert b.scheduler.read(two).phase == "running" and b.service.creates == 2


def test_terminate_releases_a_rental_whose_lost_create_left_no_machine(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    b.service.lose_reply = 1
    machine = b.scheduler.rent("l4", hours=1)
    held = b.store.rented_session(machine).id
    del b.service.sessions[held]  # the provider has no such session
    ended = b.scheduler.terminate(machine)
    assert ended.refused_at is not None and ended.deleted_at is None
    assert b.scheduler.rentals()[0].usd == 0
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()  # a released claim does not pause queued work
    assert b.scheduler.read(one).phase == "setting_up"
    assert b.service.creates == 2


def test_crashed_admission_releases_its_claim_without_pausing_queued_work(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = Machine(name="unclaimed", kind="rented", ondemand="l4", provider="fake")
    session = Session(
        id="unclaimed",
        supply="l4",
        machine=machine.name,
        planned_at=b.clock(),
        claimed_at=None,
        provider_id=None,
        started_at=None,
        refused_at=None,
        ended_at=None,
        version=1,
        autorun=True,
    )
    rental = Rental(
        machine=machine.name,
        usd_per_hour=Decimal("0.59"),
        cap_usd=Decimal(1),
        deadline=b.clock() + timedelta(hours=1),
    )
    with b.store.lock("l4", b.clock) as acquired:
        assert acquired
        b.store.plan(session, rented=machine, rental=rental)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    released = b.store.session(session.id)
    assert released.refused_at == b.clock() and released.refusal is None
    assert b.service.creates == 1
    assert b.scheduler.read(one).phase == "setting_up"


def test_failed_admission_cleanup_does_not_pause_queued_work(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    with patch.object(b.scheduler, "_create", side_effect=RuntimeError("before claim")):
        with pytest.raises(LauncherError, match="before claim"):
            b.scheduler.launch(job(), rent="l4")
    (released,) = b.store.sessions()
    assert released.refused_at == b.clock() and released.refusal is None
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.service.creates == 1
    assert b.scheduler.read(one).phase == "setting_up"


@pytest.mark.parametrize("lost", [False, True])
def test_repeated_capacity_refusals_preserve_the_attempt_budget_until_admission(
    tmp_path: Path, lost: bool
) -> None:
    b = bench(tmp_path)
    b.service.refuse = 5
    (one,) = b.scheduler.queue(job(max_attempts=1), rent="l4")
    for creates in range(1, 6):
        b.scheduler.tick()
        # Status is a database-only read in another scheduler, after diagnostics expire.
        b.store._db.execute("DELETE FROM diagnostics")
        status = Scheduler(b.store, {}, clock=b.clock).read(one)
        assert str(status) == "queued (provider refused: no capacity), $0.00"
        assert status.attempts == 0 and status.usd == 0
        assert status.machine is None and status.hardware is None
        assert not status.estimated
        assert b.service.creates == creates
        assert not b.store.unsettled_sessions()
        assert [a.number for a in b.scheduler.attempts(one)] == list(range(1, creates + 1))
        b.run(59)
        assert b.service.creates == creates
        b.clock.advance(1)

    b.scheduler.tick()
    admitted = b.scheduler.attempts(one)[-1]
    assert admitted.number == 6 and admitted.cause is None
    assert b.scheduler.read(one).phase == "setting_up"
    assert b.scheduler.read(one).attempts == 1
    assert b.service.creates == 6
    if lost:
        b.run(1)
        b.service.kill(admitted.session)
        b.run(1)
    else:
        b.run(601)
    status = b.scheduler.read(one)
    assert status.phase == "done"
    assert status.outcome == ("lost" if lost else "succeeded")
    assert status.cause == ("machine_lost" if lost else None)
    assert status.attempts == 1 and b.service.creates == 6
    assert status.usd > 0 and status.estimated
    assert status.machine == admitted.machine


def test_refused_machine_is_only_listed_with_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    (run,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    (attempt,) = b.scheduler.attempts(run)
    b.store.record_hardware(
        b.store.session(attempt.session), GPU(id="NVIDIA L4", min_vcpus=6, min_memory_gb=48)
    )
    assert b.scheduler.read(run).hardware is None
    show_machines(b.scheduler, ended=False)
    assert capsys.readouterr().out == ""
    show_machines(b.scheduler, ended=True)
    assert capsys.readouterr().out == (
        f"{attempt.machine}  l4  unavailable, nothing rented {b.clock():%Y-%m-%d %H:%M} UTC\n"
    )
    show_runs(b.scheduler, [run])
    output = capsys.readouterr().out
    assert "queued (provider refused: no capacity), $0.00" in output
    assert "machine " not in output
    show_ondemands(b.scheduler)
    assert "rented:" not in capsys.readouterr().out
    b.clock.advance(61)
    b.scheduler.tick()
    rented = b.scheduler.attempts(run)[-1].machine
    show_ondemands(b.scheduler)
    assert f"rented: {rented};" in capsys.readouterr().out


@pytest.mark.parametrize("elapsed", [0, 61])
def test_refused_status_keeps_each_runs_own_reason_after_restart(
    tmp_path: Path, elapsed: int
) -> None:
    b = bench(tmp_path)
    (one, two) = b.scheduler.queue(job().runs(1, 2), rent="l4")
    with patch.object(
        b.service,
        "create",
        side_effect=[Refused("first capacity refusal"), Refused("second capacity refusal")],
    ):
        b.scheduler.tick()
    # Diagnostic retention cannot change scheduling or status facts.
    b.store._db.execute("DELETE FROM diagnostics")
    b.clock.advance(elapsed)
    restarted = Scheduler(b.store, {}, clock=b.clock)
    assert str(restarted.read(one)) == "queued (provider refused: first capacity refusal), $0.00"
    assert str(restarted.read(two)) == "queued (provider refused: second capacity refusal), $0.00"


def test_refused_status_without_a_recorded_reason(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    session = b.store.session(b.scheduler.attempts(one)[-1].session)
    b.store.record_refusal(session, b.clock(), None)
    restarted = Scheduler(b.store, {}, clock=b.clock)
    assert str(restarted.read(one)) == "queued (provider refused capacity), $0.00"


def test_empty_refusal_records_a_reason_for_status_and_admission(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    with patch.object(b.service, "create", side_effect=Refused("")):
        b.scheduler.tick()
        with pytest.raises(LauncherError, match="provider refused: no reason given"):
            b.scheduler.launch(job(), rent="l4")
    restarted = Scheduler(b.store, {}, clock=b.clock)
    assert str(restarted.read(one)) == "queued (provider refused: no reason given), $0.00"
    (refused,) = b.store.recent_refusals("l4", b.clock() - timedelta(seconds=60), limit=1)
    assert refused.refusal == "no reason given"


@pytest.mark.parametrize("spot", [True, False])
def test_a_lost_machine_retries_the_same_run_with_remaining_hours(
    tmp_path: Path, spot: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_ondemand(
        OnDemand(
            name="market",
            template=L4.model_copy(update={"spot": spot}),
            max_machines=1,
            setup_seconds=0,
        )
    )
    (one,) = b.scheduler.queue(job(max_attempts=1 if spot else 2), rent="market")
    b.scheduler.tick()
    b.run(100)
    first = b.scheduler.attempts(one)[0]
    before = b.scheduler.read(one)
    b.service.kill(first.session)
    b.run(1)
    attempts = b.scheduler.attempts(one)
    assert len(attempts) == 2
    assert attempts[0].cause == ("spot_machine_lost" if spot else "machine_lost")
    status = b.scheduler.read(one)
    assert status.usd >= before.usd > 0
    assert status.attempts == (1 if spot else 2)
    show_runs(b.scheduler, [one])
    used = f", {status.attempts} of {1 if spot else 2} attempts used"
    assert used in capsys.readouterr().out
    assert attempts[1].limit_seconds == 3500
    assert attempts[1].machine != first.machine
    b.run(601)
    assert b.scheduler.read(one).outcome == "succeeded"


def test_a_lost_machine_is_settled_even_when_its_final_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(100)
    first = b.scheduler.attempts(one)[0]
    b.service.kill(first.session)
    observe = b.service.observe
    reads = 0

    def flaky(session_id: str) -> Observation:
        nonlocal reads
        if session_id == first.session:
            reads += 1
            if reads == 2:  # the read after the machine is confirmed gone
                raise StorageUnavailable("GET: no reply")
        return observe(session_id)

    monkeypatch.setattr(b.service, "observe", flaky)
    b.run(1)
    assert b.scheduler.attempts(one)[0].outcome is None  # ended, not yet settled
    b.run(1)
    attempts = b.scheduler.attempts(one)
    assert attempts[0].cause == "machine_lost" and len(attempts) == 2


def test_cancel_before_and_during_a_run(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    (running,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    (queued,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert str(b.scheduler.read(queued)) == "queued (waiting for l4: at its machine limit), $0.00"
    status = b.scheduler.cancel(queued)
    assert (status.phase, status.outcome, status.attempts) == ("done", "cancelled", 0)
    b.run(100)
    assert str(b.scheduler.cancel(running)).startswith("stopping")
    b.run(1)
    status = b.scheduler.read(running)
    assert (status.outcome, status.rental_ended) == ("cancelled", True)
    assert status.usd < USD_PER_RUN / 2
    assert b.service.creates == 1


def test_hours_and_dollars_each_end_a_run_under_their_own_name(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.seconds = 7200
    (slow,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    (dear,) = b.scheduler.queue(job(max_usd=Decimal("0.10")), rent="l4")
    b.scheduler.tick()
    b.run(3600)
    assert b.scheduler.read(slow).outcome == "out_of_time"
    status = b.scheduler.read(dear)
    assert status.outcome == "over_budget" and status.usd <= Decimal("0.10")


def test_several_runs_share_one_session_and_split_its_cost(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    ids = b.scheduler.queue(job().runs(1, 2, 3), rent="l4")
    b.scheduler.tick()
    statuses = b.scheduler.wait(*ids, every=60)
    assert [s.outcome for s in statuses] == ["succeeded"] * 3
    assert b.service.creates == 1 and len({s.machine for s in statuses}) == 1
    assert all(abs(s.usd - USD_PER_RUN) < Decimal("0.001") for s in statuses)
    assert all(s.rental_ended for s in statuses)


def test_a_machine_told_to_finish_is_given_no_more_work(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    b.service.finish_seconds = 60  # delivering its last files
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(600)
    assert b.scheduler.read(one).outcome == "succeeded"  # and its idle machine was told to finish
    (two,) = b.scheduler.queue(job(max_hours=0.5), rent="l4")  # it fits what is left of that rental
    b.run(1)
    assert b.scheduler.read(two).phase == "queued"  # the one machine allowed is still finishing
    (status,) = b.scheduler.wait(two, every=60)
    assert (status.outcome, status.attempts, b.service.creates) == ("succeeded", 1, 2)


def test_a_write_from_an_old_read_is_refused(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(one)[0]
    session = b.store.session(attempt.session)
    b.run(1)
    with pytest.raises(Stale):
        b.store.start(attempt, b.clock())
    b.run(600)
    with pytest.raises(Stale):
        b.store.confirm_ended(session, b.clock())


def test_supplies_are_stored_and_a_run_names_where_it_may_go(tmp_path: Path) -> None:
    b = bench(tmp_path)
    assert [o.name for o in b.scheduler.ondemands()] == ["l4"]
    b.scheduler.add_ondemand(
        OnDemand(name="l4", template=L4, max_machines=4, setup_seconds=0)
    )  # same: no change
    with pytest.raises(LauncherError, match="other settings"):
        b.scheduler.add_ondemand(OnDemand(name="l4", template=L4, max_machines=9))
    with pytest.raises(LauncherError, match="no provider"):
        b.scheduler.add_ondemand(
            OnDemand(name="other", template=MachineTemplate(provider="lambda-cloud"))
        )
    with pytest.raises(LauncherError, match="already names a ondemand"):
        b.scheduler.add_pool(Pool(name="l4"))  # one name, one supply
    with pytest.raises(LauncherError, match="no pool named"):
        b.scheduler.queue(job(), pool="shed")
    with pytest.raises(LauncherError, match="no on-demand named"):
        b.scheduler.queue(job(), rent="shed")
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert (b.scheduler.record(one).launch.pool, b.scheduler.record(one).launch.rent) == (
        None,
        "l4",
    )
    held = b.store.session(b.scheduler.attempts(one)[0].session)
    rented = b.store.machine(held.machine)
    assert (held.supply, rented.ondemand, rented.pool, rented.kind, rented.provider_id) == (
        "l4",
        "l4",
        None,
        "rented",
        f"provider-{held.id}",
    )
    b.run(601)
    assert b.store.machine(held.machine).deleted_at is not None  # gone with its rental


def _holds(b: Bench, run: str) -> tuple[str | None, Decimal]:
    """The owned machine a run's first attempt is on, or None when rented, and its price."""
    held = b.store.session(b.scheduler.attempts(run)[0].session)
    machine = b.store.machine(held.machine)
    rental = b.store.rental_for(held)
    return (machine.name if machine.kind != "rented" else None), (
        Decimal(0) if rental is None else rental.usd_per_hour
    )


def test_a_launch_with_a_pool_and_rent_uses_free_owned_machines_before_renting(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, max_machines=1)
    b.scheduler.add_pool(Pool(name="lab"))
    b.scheduler.add_machine("bench", "owned", pool="lab")
    b.scheduler.add_machine("shed", "owned")  # no pool named: a pool of one
    assert [p.name for p in b.scheduler.pools()] == ["lab", "shed"]
    first, second, third = (b.scheduler.queue(job(), "lab", rent="l4")[0] for _ in range(3))
    b.scheduler.tick()

    assert not b.store.session(b.scheduler.attempts(first)[0].session).autorun
    assert b.store.session(b.scheduler.attempts(second)[0].session).autorun
    assert _holds(b, first) == ("bench", Decimal(0))  # owned: its cost is already paid
    assert _holds(b, second) == (None, Decimal("0.59"))  # rented
    assert (
        str(b.scheduler.read(third))
        == "queued (waiting for lab: no free machine; l4: at its machine limit), $0.00"
    )

    b.scheduler.turn_pool("lab", off=True)
    with pytest.raises(LauncherError, match="turned off"):
        b.scheduler.queue(job(), "lab", rent="l4")
    b.run(601)
    assert [b.scheduler.read(r).outcome for r in (first, second)] == ["succeeded", "succeeded"]
    b.run(1)  # the finished rental ended; the limit allows another
    assert _holds(b, third) == (None, Decimal("0.59"))  # the off pool gave nothing; it rented
    assert b.store.machine("bench").deleted_at is None  # owned: it remains
    assert len(b.store.machines("l4")) == 1


def test_a_pool_alone_never_rents_and_rent_alone_never_takes_owned_machines(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("shed", "owned")
    owned = [b.scheduler.queue(job(), "shed")[0] for _ in range(2)]
    (rented,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert _holds(b, owned[0]) == ("shed", Decimal(0))
    assert str(b.scheduler.read(owned[1])) == "queued (waiting for shed: no free machine), $0.00"
    assert _holds(b, rented) == (None, Decimal("0.59"))
    assert b.service.creates == 1  # only the run that may rent was rented for
    b.run(601)
    assert _holds(b, owned[1]) == ("shed", Decimal(0))  # it waited for the owned machine
    assert b.service.creates == 1


def test_an_owned_machine_leaves_its_pool_at_expiry_and_its_run_retries_elsewhere(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="lab"))
    expiry = b.clock.now + timedelta(seconds=100)
    b.scheduler.add_machine("window", "owned", pool="lab", cost_usd=Decimal(3), expires_at=expiry)
    (run,) = b.scheduler.queue(job(), "lab", rent="l4")
    b.scheduler.tick()
    assert _holds(b, run) == ("window", Decimal(0))
    b.run(1)
    b.run(1)
    assert b.scheduler.read(run).phase == "running"
    b.run(100)  # past its expiry: its session ends and the attempt is lost with it
    first, second = b.scheduler.attempts(run)
    assert (first.outcome, first.cause, first.machine) == ("lost", "machine_lost", "window")
    assert b.store.machine(second.machine).kind == "rented" and b.service.creates == 1
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    (later,) = b.scheduler.queue(job(), "lab")  # the pool has no machine left to give
    b.run(1)
    assert b.scheduler.read(later).phase == "queued"
    assert b.store.machine("window").deleted_at is None  # owned machines are never deleted


def test_quote_prints_planned_first_wave_costs_and_rents_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path, max_machines=1)
    b.scheduler.add_pool(Pool(name="lab"))
    b.scheduler.add_machine("mini", "owned", pool="lab")
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    monkeypatch.setattr(import_module("etude.launch.client"), "default", lambda: b.scheduler)
    monkeypatch.setattr(
        "etude.controller.advance.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: b.scheduler.standing(experiment)
    )
    monkeypatch.setattr(import_module("etude.launch.client"), "_client", lambda: b.scheduler)
    path = tmp_path / "job.py"
    path.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.spec import Job
def training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command='train'), max_hours=1, max_usd=2, name="base", repo=None).runs(1, 2, 3)
"""
    )
    counting = Counting(b.store, b.service)
    with counting.record():
        code = main(["launch", str(path), "--pool", "lab", "--rent", "l4"] + ["--quote"])
    assert code == 0
    assert counting.snapshot().provider == 1  # one price; nothing created
    output = capsys.readouterr().out
    assert "job/base@1  new, would launch" in output
    assert "job/base@3  new, would queue" in output  # neither machine has a slot left
    assert "machine mini  planned rental cost $0.00" in output
    assert "planned first-wave rental cost: $1.18; nothing rented" in output
    assert "job rental budget: $6.00" in output
    assert b.service.creates == 0 and not b.store.runs() and not b.store.sessions()
    b.scheduler.set_ondemand_max("l4", 0)
    quoted = b.scheduler.quote(job().runs(1, 2), "lab", rent="l4")
    assert len(quoted.machines) == 1 and quoted.machines[0].machine == "mini"


def test_availability_asks_the_provider_once_and_rents_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path, max_machines=2)
    b.scheduler.rent("l4", hours=1)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    monkeypatch.setattr(import_module("etude.launch.client"), "default", lambda: b.scheduler)
    monkeypatch.setattr(
        "etude.controller.advance.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: b.scheduler.standing(experiment)
    )
    monkeypatch.setattr(import_module("etude.launch.client"), "_client", lambda: b.scheduler)
    counting = Counting(b.store, b.service)
    with counting.record():
        assert main(["availability", "l4"]) == 0
    assert counting.snapshot().provider == 1
    assert b.service.creates == 1
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith(
        "l4  NVIDIA L4 with at least 6 vCPUs, 48 GB at $0.59/hour (no ceiling)  available"
        "  1 of 2 in use  rentable now  asked "
    )
    b.scheduler.set_ondemand_max("l4", 1)
    assert not b.scheduler.availability("l4").rentable


def test_machines_shows_an_owned_machines_cost_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(Pool(name="lab"))
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    monkeypatch.setattr(import_module("etude.launch.client"), "default", lambda: b.scheduler)
    monkeypatch.setattr(
        "etude.controller.advance.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: b.scheduler.standing(experiment)
    )
    monkeypatch.setattr(import_module("etude.launch.client"), "_client", lambda: b.scheduler)
    assert main(["machine", "add", "mini", "me@mini.local", "--pool", "lab"]) == 0
    assert (
        main(
            ["machine", "add", "window", "me@window", "--pool", "lab", "--cost-usd", "12.5"]
            + ["--expires-at", "2026-10-12T00:00:00"]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["machines"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "mini  lab  available, owned, $0.00 committed, never expires",
        "window  lab  available, owned, $12.50 committed, expires 2026-10-12 00:00 UTC",
    ]
    b.clock.advance(24 * 3600)
    assert main(["machines"]) == 0
    assert "window  lab  unavailable, owned, $12.50 committed, expired 2026-10-12 00:00 UTC" in (
        capsys.readouterr().out
    )


def test_a_job_is_what_decides_the_result_not_its_name_or_limits(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (first,) = b.scheduler.queue(
        RunRequest(job=job(config="hidden = 256", name="small"), seed=1), rent="l4"
    )
    b.scheduler.tick()
    richer = job(config="hidden = 256", name="small again", max_usd=Decimal(9), max_hours=2.0)
    (second,) = b.scheduler.queue(RunRequest(job=richer, seed=2), rent="l4")
    b.scheduler.tick()
    (other,) = b.scheduler.queue(RunRequest(job=job(config="hidden = 1024"), seed=1), rent="l4")
    b.scheduler.tick()
    same, also, different = (b.scheduler.record(r) for r in (first, second, other))
    assert same.job == also.job != different.job
    assert same.job is not None
    assert [r.seed for r in b.store.runs(job=same.job)] == [1, 2]
    assert (also.launch.spec.max_usd, also.name) == (Decimal(9), also.id)
    assert same.launch.spec.max_usd == Decimal(5)


def test_queue_returns_its_id_even_when_the_provider_cannot_quote_a_price(tmp_path: Path) -> None:
    b = bench(tmp_path)
    quote = b.service.offer

    def no_quote() -> Offer:
        raise ConnectionError("pricing is down")

    b.service.offer = no_quote  # type: ignore[method-assign]
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert str(b.scheduler.read(one)) == "queued (waiting for l4: pricing is down), $0.00"
    b.service.offer = quote  # type: ignore[method-assign]
    b.run(1)
    assert b.scheduler.read(one).phase == "setting_up" and b.service.creates == 1


def test_an_ondemand_rents_nothing_above_its_price_ceiling(tmp_path: Path) -> None:
    b = bench(tmp_path)
    capped = OnDemand(name="capped", template=L4, max_usd_per_hour=Decimal("0.50"), setup_seconds=0)
    b.scheduler.add_ondemand(capped)
    assert b.store.ondemand("capped") == capped.model_copy(update={"storage": "test"})
    (one,) = b.scheduler.queue(job(), rent="capped")  # the fake provider asks $0.59
    b.scheduler.tick()
    assert b.scheduler.read(one).phase == "queued" and b.service.creates == 0
    b.service.usd_per_hour = Decimal("0.49")
    b.run(1)
    held = b.store.session(b.scheduler.attempts(one)[0].session)
    assert next(r for r in b.store.rentals() if r.machine == held.machine).usd_per_hour == Decimal(
        "0.49"
    )


def test_a_rental_made_by_hand_takes_several_launches_and_one_setup(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", hours=2)
    held = b.store.rented_session(machine).id
    assert (
        b.service.creates == 1 and next(r for r in b.store.rentals() if r.machine == machine).kept
    )
    assert b.service.sessions[held].packet.autorun

    (first,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(1)  # the work is handed over
    b.run(1)
    assert b.scheduler.read(first).phase == "running"
    b.run(600)
    assert b.scheduler.read(first).outcome == "succeeded"
    b.run(100)  # idle: the rental stays, and nobody's run pays for it
    (second,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    (too_long,) = b.scheduler.queue(job(max_hours=3.0), rent="l4")  # more time than it has left
    b.scheduler.tick()
    b.run(1)
    b.run(601)
    statuses = [b.scheduler.read(r) for r in (first, second)]
    assert [s.outcome for s in statuses] == ["succeeded", "succeeded"]
    assert {s.machine for s in statuses} == {machine} and b.service.creates == 1
    assert all(abs(s.usd - USD_PER_RUN) < Decimal("0.001") for s in statuses)
    assert str(b.scheduler.read(too_long)) == "queued (waiting for l4: at its machine limit), $0.00"

    (still,) = b.scheduler.rentals()
    assert (
        still.rental.machine == machine and still.usd > USD_PER_RUN * 2
    )  # idle time is the rental's
    ended = b.scheduler.terminate(machine)
    assert ended.deleted_at is not None
    b.run(1)
    assert b.scheduler.read(too_long).phase == "setting_up" and b.service.creates == 2


def test_a_rented_machine_cannot_be_terminated_under_a_run_and_ends_at_its_deadline(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", hours=1, max_usd=Decimal("0.59") / 2)  # dollars buy 30 min
    held = b.store.rented_session(machine).id
    assert next(
        r for r in b.store.rentals() if r.machine == machine
    ).deadline == b.clock.now + timedelta(minutes=30)
    (one,) = b.scheduler.queue(job(max_hours=0.2), rent="l4")
    b.scheduler.tick()
    b.run(1)
    with pytest.raises(LauncherError, match="still has a run"):
        b.scheduler.terminate(machine)
    with pytest.raises(LauncherError, match="no machine to rent"):
        b.scheduler.rent("l4", hours=1)  # the pool's one machine is held
    b.run(1800)
    assert b.scheduler.read(one).outcome == "succeeded"
    assert b.store.session(held).ended_at is not None  # nobody released it; it ended itself


def test_pool_limit_can_stop_and_resume_automatic_growth(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.set_ondemand_max("l4", 0)
    (waiting,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.service.creates == 0 and b.scheduler.read(waiting).phase == "queued"
    b.scheduler.set_ondemand_max("l4", 1)
    b.run(1)
    assert b.service.creates == 1 and b.scheduler.read(waiting).phase == "setting_up"
    b.scheduler.set_ondemand_max("l4", 0)
    b.run(601)
    assert b.scheduler.read(waiting).outcome == "succeeded"
    with pytest.raises(LauncherError, match="zero or greater"):
        b.scheduler.set_ondemand_max("l4", -1)


def test_the_default_target_is_persistent_and_off_default_pool_requires_one(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("shed", "owned")
    b.scheduler.set_default("shed", rent="l4")
    (one,) = b.scheduler.queue(job())
    b.scheduler.tick()
    assert (b.scheduler.record(one).launch.pool, b.scheduler.record(one).launch.rent) == (
        "shed",
        "l4",
    )
    assert open_database(launcher_file(tmp_path)).default_target() == Target(pool="shed", rent="l4")
    b.scheduler.turn_pool("shed", off=True)
    assert b.scheduler.record(b.scheduler.queue(job())[0]).launch.pool is None  # rents only
    b.scheduler.set_default("shed")
    with pytest.raises(LauncherError, match="name a pool"):
        b.scheduler.queue(job())


def test_completion_between_progress_and_machine_reads_uses_final_journal(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (run,) = b.scheduler.queue(job(until="steps=42"), rent="l4")
    b.scheduler.tick()
    b.clock.advance(599)
    observe = b.service.observe
    reads = 0

    def racing(session_id: str) -> Observation:
        nonlocal reads
        reads += 1
        seen = observe(session_id)
        if reads == 1:
            b.clock.advance(2)  # work finishes after progress, before machine state
            return seen.model_copy(update={"alive": False})
        return seen

    b.service.observe = racing  # type: ignore[method-assign]
    b.scheduler.tick()
    status = b.scheduler.read(run)
    assert status.outcome == "succeeded" and status.clocks == {"steps": 42}
    assert status.rental_ended
    assert b.scheduler.attempts(run)[0].outputs


def test_a_retry_resumes_its_run_without_creating_a_parent_run(tmp_path: Path) -> None:
    """The launcher hands a later attempt its run, and says it resumes; tracking does the rest."""
    b = bench(tmp_path)
    (run,) = b.scheduler.queue(job(until="steps=100"), rent="l4")
    b.scheduler.tick()
    first = b.scheduler.attempts(run)[0]
    b.run(100)
    b.service.kill(first.session)
    b.run(1)
    second = b.scheduler.attempts(run)[1]
    works = [b.service.sessions[a.session].packet.work[0] for a in (first, second)]
    assert [(w.run, w.resumes, w.here) for w in works] == [
        (run, False, False),
        (run, True, False),
    ]
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    assert len(b.scheduler.runs(done=True)) == 1


def test_a_rental_on_r2_lasts_as_long_as_its_run_beyond_twelve_hours(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_storage(Storage(name="r2", bucket="bucket", prefix="prefix", provider="r2"))
    b.scheduler.add_ondemand(
        b.store.ondemand("l4").model_copy(update={"name": "long", "storage": "r2"})
    )
    (run,) = b.scheduler.queue(job(max_hours=48, max_usd=Decimal(100)), rent="long")
    b.scheduler.tick()
    held = b.store.session(b.scheduler.attempts(run)[0].session)
    rental = b.store.rental_for(held)
    assert rental is not None
    assert rental.deadline == b.clock.now + timedelta(hours=48)
    assert b.scheduler.attempts(run)[0].limit_seconds == 48 * 3600


def test_a_run_longer_than_its_storage_grants_is_refused_before_renting(tmp_path: Path) -> None:
    b = bench(tmp_path)  # S3 federation keys last 36 hours
    with pytest.raises(LauncherError, match="at most 36 hours"):
        b.scheduler.queue(job(max_hours=37, max_usd=Decimal(100)), rent="l4")
    with pytest.raises(LauncherError, match="grants last at most 36 hours"):
        b.scheduler.rent("l4", hours=37)
    assert b.scheduler.runs() == () and b.service.sessions == {}


def test_retries_share_the_runs_dollar_limit(tmp_path: Path) -> None:
    b = bench(tmp_path, usd_per_hour="1")
    b.service.seconds = 1000
    (run,) = b.scheduler.queue(job(max_usd=Decimal(200) / 3600), rent="l4")
    b.scheduler.tick()
    b.run(100)
    b.service.kill(b.scheduler.attempts(run)[0].session)
    b.run(1)
    attempts = b.scheduler.attempts(run)
    assert len(attempts) == 2
    assert attempts[1].limit_seconds <= 100
    assert attempts[1].limit_kind == "over_budget"
    b.run(101)
    status = b.scheduler.read(run)
    assert status.outcome == "over_budget"
    assert status.usd <= Decimal(200) / 3600
    assert len(b.scheduler.attempts(run)) == 2


def test_a_job_needs_dollars_only_where_money_can_be_spent(tmp_path: Path) -> None:
    b = bench(tmp_path)
    free = Job(command="train", max_hours=1)  # no max_usd
    with pytest.raises(
        LauncherError, match="on-demand 'l4' can spend money, so the job needs max_usd"
    ):
        b.scheduler.queue(free, rent="l4")
    assert b.store.runs() == ()
    b.scheduler.add_pool(Pool(name="mine"))  # no template: nothing here can spend
    (run,) = b.scheduler.queue(free, "mine")
    assert b.scheduler.record(run).launch.spec.max_usd is None
    with pytest.raises(LauncherError, match="more than zero"):
        b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(0)), "mine")


class Host(FakeService):
    """An owned machine with its own disk and no bucket: a run's folder lives only there."""

    def __init__(self, clock: Clock, root: Path) -> None:
        super().__init__(clock, "0")
        self.disk = DirectoryJournal(root / "sessions")
        self.track = root / "track"
        self.reachable = True

    def held(self, run: str, key: str | None = None) -> dict[str, bytes]:
        self._call("ssh")
        if not self.reachable:
            raise Unreachable("SSH transport failed; remote outcome unconfirmed")
        return self.disk.held(run, key)

    def hold(self, run: str, files: dict[str, bytes]) -> None:
        self._call("ssh")
        self.disk.hold(run, files)


def _tracked(directory: Path, run: str, *, resume: bool = False) -> TrackedRun:
    with patch.dict(
        os.environ,
        {
            "ETUDE_RUN": run,
            "ETUDE_ATTEMPT": "fixture",
            "ETUDE_OUT": str(directory / run / "out"),
        },
    ):
        return open_run(
            experiment="lab",
            clocks=[RunClock(name="steps", kind="step", meaning="training steps")],
            run=run,
            directory=directory,
            resume=resume,
        )


def test_a_retry_on_another_owned_host_waits_for_and_continues_the_runs_folder(
    tmp_path: Path,
) -> None:
    clock = Clock()
    hosts = {name: Host(clock, tmp_path / name) for name in ("first", "second")}
    store = open_database(launcher_file(tmp_path))
    scheduler = Scheduler(store, {"ssh": lambda held: hosts[held.name]}, clock=clock)
    scheduler.add_pool(Pool(name="lab"))
    scheduler.add_machine("first", "owned", pool="lab", expires_at=clock() + timedelta(seconds=150))
    scheduler.add_machine("second", "owned", pool="lab")
    (run,) = scheduler.queue(job(until="steps=100", max_usd=None), "lab")
    scheduler.tick()
    first = scheduler.attempts(run)[0]
    assert store.session(first.session).machine == "first"

    tracked = _tracked(hosts["first"].track, run)
    weights = tmp_path / "weights.pt"
    for step in (1, 2):
        weights.write_bytes(f"weights at {step}".encode())
        tracked.checkpoint(weights, steps=step)
    tracked.finish()
    for host, content in (("first", b"latest state"), ("second", b"stale state")):
        out = hosts[host].track / run / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "state.pt").write_bytes(content)

    hosts["first"].reachable = False
    clock.advance(200)  # the first host's window has ended: the retry goes elsewhere
    hosts["first"].kill(first.session)
    scheduler.tick()
    scheduler.tick()
    second = scheduler.attempts(run)[1]
    assert store.session(second.session).machine == "second"
    assert not hosts["second"].sessions  # it waits for the folder, and starts nothing
    assert scheduler.read(run).phase == "renting"

    hosts["first"].reachable = True
    scheduler.tick()
    scheduler.tick()
    assert hosts["second"].sessions
    # Asked on each pass until the holder answered, then never again.
    assert hosts["first"].calls["ssh"] == 3 and hosts["second"].calls["ssh"] == 1
    assert (hosts["second"].track / run / "out/state.pt").read_bytes() == b"latest state"
    relayed = hosts["second"].track / run / "checkpoints"
    assert sorted(p.name for p in relayed.iterdir()) == [
        ".stored",
        "steps-1.json",
        "steps-2.json",
        "steps-2.pt",
    ]

    resumed = _tracked(hosts["second"].track, run, resume=True)
    latest = (
        resumed._checkpoint_here(_saved_checkpoint)
        if (_saved_checkpoint := resumed._latest_record()) is not None
        else None
    )
    assert latest is not None and latest.ordinal == 2
    weights.write_bytes(b"weights at 3")
    assert resumed.checkpoint(weights, steps=3).sha256 is not None
    resumed.finish()
    record = json.loads((relayed / "steps-3.json").read_text())
    assert record["ordinal"] == 3


def _reporting(b: Bench, **changes: object) -> None:
    """Every running attempt's journal also says `changes`."""
    observe = b.service.observe

    def reported(session_id: str) -> Observation:
        seen = observe(session_id)
        return seen.model_copy(
            update={
                "progress": tuple(
                    entry.model_copy(update=changes) if entry.finished_at is None else entry
                    for entry in seen.progress
                )
            }
        )

    b.service.observe = reported  # type: ignore[method-assign]


def test_status_says_how_far_a_running_job_has_got_through_its_work(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    tally = Tally(unit="matches", done=412, total=1800, failed=1, per_hour=940.0)
    _reporting(b, tally=tally)
    b.run(1)
    said = str(b.scheduler.read(one))
    assert said.startswith("running, 412/1800 matches (1 failed), 940/hour, ~1.5 hours left")
    assert b.scheduler.read(one).tally == tally


def test_a_tally_under_an_hour_left_is_in_minutes_and_a_finished_one_has_no_pace() -> None:
    left = Tally(unit="matches", done=1700, total=1800, per_hour=1200.0)
    assert str(left) == "1700/1800 matches, 1200/hour, ~5 minutes left"
    last = left.model_copy(update={"done": 1790})
    assert str(last) == "1790/1800 matches, 1200/hour, ~1 minute left"
    assert str(left.model_copy(update={"done": 1800})) == "1800/1800 matches"


def test_a_job_that_exited_is_not_lost_when_its_files_never_arrive(tmp_path: Path) -> None:
    """The machine outlives the job, its completion record never lands, and it ends at last."""
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(1)
    _reporting(
        b, finished_at=b.clock(), outcome="succeeded", exit_code=0, delivered=False, outputs=()
    )
    b.run(1)
    attempt = b.scheduler.attempts(one)[0]
    assert attempt.outcome is None  # its files may still come while the machine lives
    b.service.kill(attempt.session)
    b.run(1)
    status = b.scheduler.read(one)
    assert (status.phase, status.outcome, status.attempts) == ("done", "succeeded", 1)
    assert any("not confirmed stored" in d.message for d in b.scheduler.diagnostics(run=one))


def test_a_lost_run_with_no_time_to_retry_says_what_ended_it(tmp_path: Path) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(max_hours=0.1), rent="l4")
    b.scheduler.tick()
    b.run(1)
    observe = b.service.observe

    def silent(session_id: str) -> Observation:
        return observe(session_id).model_copy(update={"progress": ()})

    b.service.observe = silent  # type: ignore[method-assign]
    b.clock.advance(400)  # past its hours, with nothing more heard of it
    b.service.kill(b.scheduler.attempts(one)[0].session)
    b.run(1)
    assert str(b.scheduler.read(one)).startswith("done: out_of_time (machine_lost)")


def test_a_read_shows_a_running_jobs_output_and_a_pass_fetches_it_only_as_it_grows(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    (one,) = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(1)
    attempt = b.scheduler.attempts(one)[0]
    assert not any(s.received for s in b.scheduler.logs(run=one).streams if s.producer == "job")

    logs = b.service.logs
    fetched = 0

    def counted(session_id: str, attempt: str | None = None, *, producer: str = "job") -> LogStream:
        nonlocal fetched
        fetched += 1
        return logs(session_id, attempt, producer=producer)

    b.service.logs = counted  # type: ignore[method-assign]
    _reporting(b, log_bytes=100)
    b.run(1)
    assert f"log of {attempt.id}" in job_text(b.scheduler.logs(run=one))
    assert fetched == 1
    b.run(40)  # nothing new stored
    assert fetched == 1
    _reporting(b, log_bytes=200)
    b.run(1)
    assert fetched == 2


def test_assigned_run_name_agrees_with_quote_and_stored_record(tmp_path: Path) -> None:
    b = bench(tmp_path)
    spec = job(name="baseline", experiment="pilot", config='{ "lr": 0.1 }')
    request = RunRequest(job=spec, seed=7, id="baseline-seed-7")
    quote = b.scheduler.quote([request], rent="l4")
    assert [machine.run for machine in quote.machines] == ["baseline-seed-7"]
    (identity,) = b.scheduler.queue(request, rent="l4")
    record = b.scheduler.record(identity)
    assert record.name == "baseline-seed-7"
    assert record.config_json == '{ "lr": 0.1 }'
    assert record.launch.rent == "l4"
    assert RunRecord.model_validate_json(record.model_dump_json()) == record
    # SQLite's flat submission columns also describe historical stored records.
    historical = {**record.model_dump(exclude={"submission"}), **record.launch.model_dump()}
    assert RunRecord.model_validate(historical) == record
    assert b.service.creates == 0


def test_unassigned_hand_built_requests_have_seeded_quote_labels_and_opaque_ids(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    spec = job(name="baseline")
    quote = b.scheduler.quote([RunRequest(job=spec, seed=7)], rent="l4")
    assert [machine.run for machine in quote.machines] == ["baseline@7"]
    (identity,) = b.scheduler.queue(RunRequest(job=spec, seed=7), rent="l4")
    assert b.scheduler.record(identity).name == identity
    (unnamed,) = b.scheduler.queue(Job(command="true", max_hours=1, max_usd=Decimal(1)), rent="l4")
    assert b.scheduler.record(unnamed).name == unnamed
    assert b.service.creates == 0


def test_wait_settles_delayed_shutdown_before_the_next_launch(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    b.service.finish_seconds = 5
    (one,) = b.scheduler.launch(job(), rent="l4")
    (status,) = b.scheduler.wait(one, every=1, timeout=700)
    assert status.outcome == "succeeded" and status.rental_ended
    assert not b.store.finishing_sessions()
    assert b.scheduler.logs(run=one).streams
    (two,) = b.scheduler.launch(job(), rent="l4")
    assert b.scheduler.read(two).phase != "queued"
    b.scheduler.wait(two, every=1, timeout=700)
    assert b.service.creates == 2


def test_launch_names_the_machine_still_finishing_and_quote_settles_its_receipt(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, max_machines=1)
    b.service.finish_seconds = 5
    (one,) = b.scheduler.launch(job(), rent="l4")
    b.run(601)
    machine = b.scheduler.read(one).machine
    with pytest.raises(LauncherError, match=rf"\(finishing: {machine}\)"):
        b.scheduler.launch(job(), rent="l4")
    with pytest.raises(TimeoutError, match=f"machines still finishing: {machine}"):
        b.scheduler.wait(one, timeout=0)
    b.clock.advance(5)
    quote = b.scheduler.quote([RunRequest(job=job())], rent="l4")
    assert quote.machines and b.service.creates == 1
    assert b.scheduler.read(one).rental_ended


def test_pool_refusal_tries_healthy_sibling_before_renting(tmp_path: Path) -> None:
    def setup(
        subdir: str, *, refuse_first: int, refuse_second: int
    ) -> tuple[Scheduler, FakeService, FakeService, FakeService]:
        clock = Clock()
        store = open_database(tmp_path / subdir / "state.db")
        first = FakeService(clock)
        second = FakeService(clock)
        rent = FakeService(clock)
        first.refuse = refuse_first
        second.refuse = refuse_second
        owned = {"m1": first, "m2": second}
        scheduler = Scheduler(
            store,
            {"ssh": lambda held: owned[held.name], "runpod": lambda held: rent},
            clock=clock,
        )
        scheduler.add_storage(
            Storage(name="west", bucket="b", prefix="p", provider="s3", region="us-west-2")
        )
        scheduler.set_provider(
            Provider(name="fake", kind="runpod", registry="ghcr.io/test", storage="west")
        )
        scheduler.add_ondemand(OnDemand(name="l4", template=L4, max_machines=2, setup_seconds=0))
        scheduler.add_pool(Pool(name="lab"))
        scheduler.add_machine("m1", "m1.local", pool="lab")
        scheduler.add_machine("m2", "m2.local", pool="lab")
        return scheduler, first, second, rent

    # Machine 1 refuses -> same tick places on healthy machine 2 with 0 provider rent calls.
    scheduler, m1, m2, rent = setup("one_refuses", refuse_first=1, refuse_second=0)
    (run,) = scheduler.queue(job(), pool="lab", rent="l4")
    scheduler.tick()
    assert m1.creates == 1
    assert m2.creates == 1
    assert rent.creates == 0
    assert rent.offers == 0
    assert scheduler.read(run).machine == "m2"

    # Both pool machines refuse -> same tick spills over to 1 on-demand rental.
    scheduler2, m1_b, m2_b, rent_b = setup("both_refuse", refuse_first=1, refuse_second=1)
    (run2,) = scheduler2.queue(job(), pool="lab", rent="l4")
    scheduler2.tick()
    assert m1_b.creates == 1
    assert m2_b.creates == 1
    assert rent_b.creates == 1
    machine2 = scheduler2.read(run2).machine
    assert machine2 is not None and machine2.startswith("l4-")


def test_unfinished_runs_and_waiting_share_retryability_rules(tmp_path: Path) -> None:
    b = bench(tmp_path)
    now = b.clock().isoformat()
    b.store.add_pool(Pool(name="lab"))
    b.store.add_machine(Machine(name="m1", kind="ssh", pool="lab", address="m1"))

    def add_run(
        name: str,
        *,
        max_attempts: int = 2,
        cancelled: bool = False,
        resume_after: int = 0,
        attempts: tuple[tuple[str | None, str | None], ...] = (),
    ) -> str:
        spec = job(name=name, max_attempts=max_attempts).model_dump_json()
        b.store._db.execute(
            """INSERT INTO runs(id, job, spec, pool, created_at, cancel_requested_at, resume_after)
               VALUES (?, ?, ?, 'lab', ?, ?, ?)""",
            (name, name, spec, now, now if cancelled else None, resume_after),
        )
        for index, (outcome, cause) in enumerate(attempts, start=1):
            session_id = f"sess-{name}-{index}"
            b.store._db.execute(
                "INSERT INTO sessions(id, machine, planned_at, ended_at) VALUES (?, 'm1', ?, ?)",
                (session_id, now, None if outcome is None else now),
            )
            b.store._db.execute(
                """INSERT INTO attempts(id, run, session, machine, number, position,
                       limit_seconds, limit_kind, outcome, cause)
                   VALUES (?, ?, ?, 'm1', ?, 0, 3600, 'out_of_time', ?, ?)""",
                (f"att-{name}-{index}", name, session_id, index, outcome, cause),
            )
        return name

    fresh = add_run("fresh")
    running_last = add_run("running-last", max_attempts=1, attempts=((None, None),))
    add_run("succeeded", attempts=(("succeeded", None),))
    add_run("failed", attempts=(("failed", None),))
    add_run("cancelled-idle", cancelled=True)
    stopping = add_run("cancelled-open", cancelled=True, attempts=((None, None),))
    stopped_retry = add_run("stopped-retry", max_attempts=2, attempts=(("stopped", None),))
    lost_retry = add_run("lost-retry", max_attempts=2, attempts=(("lost", "machine_lost"),))
    add_run(
        "lost-exhausted",
        max_attempts=1,
        attempts=(("lost", "machine_lost"),),
    )
    exempt_retry = add_run(
        "exempt-retry",
        max_attempts=1,
        attempts=(("lost", "capacity_refused"), ("lost", "spot_machine_lost")),
    )
    add_run("non-retryable", max_attempts=3, attempts=(("lost", "input_undelivered"),))
    resumed = add_run(
        "resumed",
        max_attempts=2,
        resume_after=1,
        attempts=(("failed", None),),
    )

    waiting = {r.id for r in b.store.waiting("lab")}
    unfinished = {r.id for r in b.store.runs(unfinished=True)}
    assert waiting == {fresh, stopped_retry, lost_retry, exempt_retry, resumed}
    assert unfinished == waiting | {running_last, stopping}


def test_explicit_launch_and_quote_order_refused_owned_behind_ondemand(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = open_database(tmp_path / "state.db")
    m1 = FakeService(clock, seconds=10)
    m2 = FakeService(clock, seconds=10)
    rent = FakeService(clock, seconds=10)
    owned = {"m1": m1, "m2": m2}
    scheduler = Scheduler(
        store,
        {"ssh": lambda held: owned[held.name], "runpod": lambda held: rent},
        clock=clock,
    )
    scheduler.add_storage(
        Storage(name="west", bucket="b", prefix="p", provider="s3", region="us-west-2")
    )
    scheduler.set_provider(
        Provider(name="fake", kind="runpod", registry="ghcr.io/test", storage="west")
    )
    scheduler.add_ondemand(OnDemand(name="l4", template=L4, max_machines=1, setup_seconds=0))
    scheduler.add_pool(Pool(name="lab"))
    scheduler.add_machine("m1", "m1.local", pool="lab")

    # Solo pool host refuses queued work and enters the 60 s cooldown.
    m1.refuse = 1
    (queued,) = scheduler.queue(job(), pool="lab")
    scheduler.tick()
    assert m1.creates == 1
    assert scheduler.read(queued).phase == "queued"

    # 10 s later: queued retry still waits, while `--pool lab --rent l4` prefers on-demand over refused m1.
    clock.advance(10)
    scheduler.tick()
    assert m1.creates == 1
    assert scheduler.read(queued).phase == "queued"

    quote = scheduler.quote([RunRequest(job=job())], pool="lab", rent="l4")
    assert len(quote.machines) == 1
    assert quote.machines[0].machine is None
    assert quote.machines[0].usd_per_hour == Decimal("0.59")
    assert rent.offers == 1
    assert rent.creates == 0

    (rented,) = scheduler.launch(job(), pool="lab", rent="l4")
    assert m1.creates == 1
    assert rent.creates == 1
    assert (scheduler.read(rented).machine or "").startswith("l4-")

    # Multi-run quote/launch with a refused host and 1-machine on-demand places on l4 and queues
    # overflow instead of promising refused m1 alongside a paid rental.
    clock.advance(10)  # `rented` finishes on l4; m1's refusal is 20 s old (< 60 s)
    scheduler.tick()
    assert scheduler.read(rented).outcome == "succeeded"
    overflow_quote = scheduler.quote(
        [RunRequest(job=job()), RunRequest(job=job())], pool="lab", rent="l4"
    )
    assert len(overflow_quote.machines) == 1
    assert overflow_quote.machines[0].machine is None
    assert overflow_quote.machines[0].usd_per_hour == Decimal("0.59")

    placed_on_l4, overflow_queued = scheduler.launch(job().runs(10, 11), pool="lab", rent="l4")
    assert (scheduler.read(placed_on_l4).machine or "").startswith("l4-")
    assert scheduler.read(overflow_queued).phase == "queued"
    assert m1.creates == 1
    assert rent.creates == 2

    # With l4 at its machine limit, explicit quote and launch fall back to recently refused m1.
    fallback_quote = scheduler.quote([RunRequest(job=job())], pool="lab", rent="l4")
    assert len(fallback_quote.machines) == 1
    assert fallback_quote.machines[0].machine == "m1"
    assert fallback_quote.machines[0].usd_per_hour == Decimal(0)

    (fallback,) = scheduler.launch(job(), pool="lab", rent="l4")
    assert m1.creates == 2
    assert rent.creates == 2
    assert scheduler.read(fallback).machine == "m1"

    # Solo-pool explicit launch still retries a recently refused host immediately.
    clock.advance(10)  # `placed_on_l4`, `overflow_queued`, `fallback` advance; refuse m1 again
    scheduler.tick()
    clock.advance(10)
    scheduler.tick()
    m1.refuse = 1
    with pytest.raises(LauncherError, match=r"machine refused \(m1\)"):
        scheduler.launch(job(), pool="lab")
    clock.advance(5)  # still within 60 s cooldown
    (solo_retry,) = scheduler.launch(job(), pool="lab")
    assert scheduler.read(solo_retry).machine == "m1"

    # When an unrefused sibling exists, explicit quote and launch prefer it over both l4 and refused m1.
    scheduler.add_machine("m2", "m2.local", pool="lab")
    clock.advance(10)  # `solo_retry` finishes; m1's refusal is 15 s old (< 60 s)
    scheduler.tick()  # `queued` runs on healthy m2
    assert scheduler.read(queued).machine == "m2"
    clock.advance(10)  # `queued` finishes on m2; m1's refusal is 25 s old (< 60 s)
    scheduler.tick()
    offers_before = rent.offers
    creates_before = rent.creates
    prefer = scheduler.quote([RunRequest(job=job())], pool="lab", rent="l4")
    assert prefer.machines[0].machine == "m2"
    assert rent.offers == offers_before
    (on_m2,) = scheduler.launch(job(), pool="lab", rent="l4")
    assert scheduler.read(on_m2).machine == "m2"
    assert rent.creates == creates_before


def test_explicit_launch_on_owned_ssh_pool_confirms_pre_commit_and_replans_on_refusal(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = open_database(tmp_path / "state.db")
    mini1 = FakeService(clock, seconds=10)
    mini2 = FakeService(clock, seconds=10)
    mini3 = FakeService(clock, seconds=10)
    rent = FakeService(clock, seconds=10)
    owned = {"mini-1": mini1, "mini-2": mini2, "mini-3": mini3}
    scheduler = Scheduler(
        store,
        {"ssh": lambda held: owned[held.name], "runpod": lambda held: rent},
        clock=clock,
    )
    scheduler.add_storage(
        Storage(name="west", bucket="b", prefix="p", provider="s3", region="us-west-2")
    )
    scheduler.set_provider(
        Provider(name="fake", kind="runpod", registry="ghcr.io/test", storage="west")
    )
    scheduler.add_ondemand(OnDemand(name="gpu", template=L4, max_machines=2, setup_seconds=0))
    scheduler.add_pool(Pool(name="lab"))
    scheduler.add_machine("mini-1", "mini-1.local", pool="lab")

    # Solo refusing pool rejects before committing any run row.
    mini1.refuse = 1
    with pytest.raises(LauncherError, match=r"machine refused \(mini-1\)"):
        scheduler.launch(job(), pool="lab")
    assert store.runs() == ()
    assert mini1.creates == 1

    # Adding a healthy sibling lets launch fail over pre-commit from refusing mini-1 to mini-2.
    clock.advance(61)  # mini-1 cooldown expired so it is tried first
    scheduler.add_machine("mini-2", "mini-2.local", pool="lab")
    mini1.refuse = 1
    (run_a,) = scheduler.launch(job(), pool="lab")
    assert scheduler.read(run_a).machine == "mini-2"
    assert mini1.creates == 2
    assert mini2.creates == 1
    clock.advance(10)
    scheduler.tick()
    assert scheduler.read(run_a).outcome == "succeeded"

    # When every owned host refuses, `--pool lab --rent gpu` re-plans onto gpu pre-commit.
    clock.advance(61)
    mini1.refuse = 1
    mini2.refuse = 1
    (run_b,) = scheduler.launch(job(), pool="lab", rent="gpu")
    assert (scheduler.read(run_b).machine or "").startswith("gpu-")
    assert rent.creates == 1
    clock.advance(10)
    scheduler.tick()
    assert scheduler.read(run_b).outcome == "succeeded"

    # Multi-run launch: confirmed mini-1 is not double-booked when mini-2 refuses; mini-3 takes run 2.
    clock.advance(61)
    scheduler.add_machine("mini-3", "mini-3.local", pool="lab")
    mini2.refuse = 1
    first, second = scheduler.launch(job().runs(1, 2), pool="lab")
    assert scheduler.read(first).machine == "mini-1"
    assert scheduler.read(second).machine == "mini-3"
    clock.advance(10)
    scheduler.tick()
    assert scheduler.read(first).outcome == "succeeded"
    assert scheduler.read(second).outcome == "succeeded"

    # Multi-run launch with no sibling left after mini-2 refuses keeps mini-1 and queues run 2.
    clock.advance(61)
    mini2.refuse = 1
    mini3.refuse = 1
    kept, waiting = scheduler.launch(job().runs(3, 4), pool="lab")
    assert scheduler.read(kept).machine == "mini-1"
    assert scheduler.read(waiting).phase == "queued"


def test_cancelled_owned_session_observe_error_does_not_end_or_double_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    store = open_database(tmp_path / "state.db")
    host = FakeService(clock, seconds=600)
    scheduler = Scheduler(store, {"ssh": lambda held: host}, clock=clock)
    scheduler.add_pool(Pool(name="lab", slots=2))
    scheduler.add_machine("mini", "mini.local", pool="lab")

    # Part 1: StorageUnavailable during observe on a cancelled owned session does not stamp
    # finish_requested_at or end the session; recovery settles cleanly via graceful stop.
    (run,) = scheduler.launch(job(max_attempts=1), pool="lab")
    session_id = scheduler.attempts(run)[0].session
    scheduler.tick()
    assert store.session(session_id).started_at is not None

    real_observe = host.observe
    real_end = host.end
    real_stop = host.stop
    observe_calls = 0
    end_calls = 0
    fail_observe: Exception | None = StorageUnavailable("R2 503")
    refuse_stop = False

    def wrapped_observe(session_id: str) -> Observation:
        nonlocal observe_calls
        observe_calls += 1
        if fail_observe is not None:
            raise fail_observe
        return real_observe(session_id)

    def wrapped_end(session_id: str) -> object:
        nonlocal end_calls
        end_calls += 1
        return real_end(session_id)

    def wrapped_stop(session_id: str, attempt: str) -> None:
        if refuse_stop:
            raise Refused("OSError: [Errno 28] No space left on device")
        real_stop(session_id, attempt)

    monkeypatch.setattr(host, "observe", wrapped_observe)
    monkeypatch.setattr(host, "end", wrapped_end)
    monkeypatch.setattr(host, "stop", wrapped_stop)

    status = scheduler.cancel(run)
    assert status.phase == "stopping"
    assert store.session(session_id).finish_requested_at is None
    assert store.session(session_id).ended_at is None
    assert end_calls == 0
    assert observe_calls == 1

    # Part 2: An asleep host (Unreachable on observe) costs exactly 1 observe call per tick,
    # never calling end or stamping finish_requested_at.
    observe_calls = 0
    fail_observe = Unreachable("SSH wait exceeded 30 seconds")
    scheduler.tick()
    assert observe_calls == 1
    assert end_calls == 0
    assert store.session(session_id).finish_requested_at is None
    assert store.session(session_id).ended_at is None

    # Once the host answers again, graceful stop finishes and settles the cancelled run.
    fail_observe = None
    scheduler.tick()  # delivers graceful stop
    assert scheduler.read(run).phase == "stopping"
    scheduler.tick()  # observes graceful cancellation and settles
    assert scheduler.read(run).outcome == "cancelled"
    assert store.session(session_id).ended_at is not None

    # Part 3: Refused from stop escalates to _end_session only when every open attempt is cancelled.
    end_calls = 0
    first, second = scheduler.launch(job(max_attempts=1).runs(1, 2), pool="lab")
    shared = scheduler.attempts(first)[0].session
    assert scheduler.attempts(second)[0].session == shared
    clock.advance(1)
    scheduler.tick()
    assert scheduler.read(first).phase == "running"
    assert scheduler.read(second).phase == "running"
    refuse_stop = True

    # Cancelling only one of two attempts does not kill the sibling.
    scheduler.cancel(first)
    assert end_calls == 0
    assert store.session(shared).ended_at is None
    assert scheduler.read(second).phase == "running"

    # Cancelling the remaining attempt makes _all_cancelled True -> escalates to _end_session,
    # settling both runs `cancelled` even with max_attempts=1.
    scheduler.cancel(second)
    assert end_calls == 1
    assert store.session(shared).ended_at is not None
    assert scheduler.read(first).outcome == "cancelled"
    assert scheduler.read(second).outcome == "cancelled"
