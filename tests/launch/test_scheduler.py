"""The launcher's rules, against a provider that runs on a test clock."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch.database import Database
from etude.launch.records import (
    Job,
    LauncherError,
    MachineTemplate,
    Offer,
    OnDemand,
    Output,
    Pool,
    Run,
    Stale,
    Target,
)
from etude.launch.schemas import Observation
from etude.launch.storage.s3 import StorageUnavailable
from tests.launch.counting import Counting, job_text
from tests.launch.fake import L4, Bench, bench, checkpoint_bytes

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
    one = b.scheduler.queue(job(until="steps=26000"), rent="l4")
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
    assert job_text(b.scheduler.logs(run=one)).startswith("log of ")


def test_a_lost_create_reply_is_looked_up_and_never_asked_twice(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.lose_reply = 1
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.read(one).phase == "renting"
    b.run(1)
    assert b.scheduler.read(one).phase == "setting_up"
    b.run(700)
    assert b.scheduler.read(one).outcome == "succeeded"
    assert b.service.creates == 1


def test_a_create_that_never_appears_is_lost_only_after_the_rental_deadline(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.service.lose_reply = 1
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    del b.service.sessions[b.scheduler.attempts(one)[0].session]  # the provider has no such session
    b.run(3599)
    assert b.scheduler.read(one).phase == "renting"
    b.run(1)
    status = b.scheduler.read(one)
    assert (status.outcome, status.cause, status.rental_ended) == (
        "lost",
        "create_unconfirmed",
        False,
    )
    assert b.service.creates == 1 and status.attempts == 1


def test_refused_capacity_is_retried_up_to_the_jobs_allowed_attempts(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.refuse = 1
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert str(b.scheduler.read(one)) == "queued (retrying after capacity_refused), $0.00"
    b.run(1)
    b.run(601)
    status = b.scheduler.read(one)
    assert (status.outcome, status.attempts) == ("succeeded", 2)

    b.service.refuse = 9
    two = b.scheduler.queue(job(max_attempts=2), rent="l4")
    b.scheduler.tick()
    b.run(1)
    b.run(1)
    status = b.scheduler.read(two)
    assert (status.phase, status.outcome, status.cause) == ("done", "lost", "capacity_refused")
    assert status.attempts == 2 and status.usd == 0


def test_a_lost_machine_retries_the_same_run_with_remaining_hours(tmp_path: Path) -> None:
    b = bench(tmp_path)
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(100)
    first = b.scheduler.attempts(one)[0]
    b.service.kill(first.session)
    b.run(1)
    attempts = b.scheduler.attempts(one)
    assert len(attempts) == 2
    assert attempts[0].cause == "machine_lost"
    assert attempts[1].limit_seconds == 3500
    assert attempts[1].machine != first.machine
    b.run(601)
    assert b.scheduler.read(one).outcome == "succeeded"


def test_a_lost_machine_is_settled_even_when_its_final_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = bench(tmp_path)
    one = b.scheduler.queue(job(), rent="l4")
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
    running = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    queued = b.scheduler.queue(job(), rent="l4")
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
    slow = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    dear = b.scheduler.queue(job(max_usd=Decimal("0.10")), rent="l4")
    b.scheduler.tick()
    b.run(3600)
    assert b.scheduler.read(slow).outcome == "out_of_time"
    status = b.scheduler.read(dear)
    assert status.outcome == "over_budget" and status.usd <= Decimal("0.10")


def test_a_cohort_runs_one_at_a_time_and_stops_at_its_dollar_ceiling(tmp_path: Path) -> None:
    b = bench(tmp_path)
    ids = b.scheduler.queue_cohort(
        "capacity", job().runs(1, 2, 3), rent="l4", parallel=1, max_usd=USD_PER_RUN * 2
    )
    b.scheduler.tick()
    assert str(b.scheduler.read(ids[1])) == "queued (cohort runs 1 at a time), $0.00"
    statuses = b.scheduler.wait(*ids, every=60)
    assert [s.outcome for s in statuses] == ["succeeded", "succeeded", "over_budget"]
    assert statuses[2].attempts == 0
    assert sum(s.usd for s in statuses) <= USD_PER_RUN * 2
    assert b.service.most_live == 1 and b.service.creates == 1
    with pytest.raises(LauncherError, match="already exists"):
        b.scheduler.queue_cohort("capacity", [Run(job=job())], rent="l4")


def test_several_runs_share_one_session_and_split_its_cost(tmp_path: Path) -> None:
    b = bench(tmp_path)
    ids = b.scheduler.queue_cohort("packed", job().runs(1, 2, 3), rent="l4")
    b.scheduler.tick()
    statuses = b.scheduler.wait(*ids, every=60)
    assert [s.outcome for s in statuses] == ["succeeded"] * 3
    assert b.service.creates == 1 and len({s.machine for s in statuses}) == 1
    assert all(abs(s.usd - USD_PER_RUN) < Decimal("0.001") for s in statuses)
    assert all(s.rental_ended for s in statuses)


def test_a_write_from_an_old_read_is_refused(tmp_path: Path) -> None:
    b = bench(tmp_path)
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    attempt = b.scheduler.attempts(one)[0]
    session = b.store.session(attempt.session)
    b.run(1)
    with pytest.raises(Stale):
        b.store.start(attempt, b.clock())
    b.run(600)
    with pytest.raises(Stale):
        b.store.confirm_ended(session, b.clock())


def test_manabot_cohort_shape(tmp_path: Path) -> None:
    """Two jobs by three seeds, one run at a time on an L4, continued at absolute positions on its clock."""
    b = bench(tmp_path)
    configs = {"small": "hidden = 256\n", "large": "hidden = 1024\n"}
    jobs = [
        Job(
            command="python -m manabot.train",
            name=name,
            config=config,
            until="updates=26000",
            max_hours=11.9,
            max_usd=Decimal(5),
        )
        for name, config in configs.items()
    ]
    first = [run for each in jobs for run in each.runs(1, 2, 3)]
    assert len({r.job for r in first}) == 2
    stages = [b.scheduler.queue_cohort("capacity-26k", first, rent="l4", max_usd=Decimal(30))]
    b.scheduler.tick()
    for target in (63_000, 100_000):
        assert all(s.outcome == "succeeded" for s in b.scheduler.wait(*stages[-1], every=60))
        continued = [b.scheduler.continue_from(p, f"updates={target}") for p in stages[-1]]
        stages.append(
            b.scheduler.queue_cohort(
                f"capacity-{target // 1000}k", continued, rent="l4", max_usd=Decimal(30)
            )
        )
        b.scheduler.tick()
    last = b.scheduler.wait(*stages[-1], every=60)

    assert [s.clocks for s in last] == [{"updates": 100_000}] * 6
    assert b.service.most_live == 1 and b.service.creates == 3
    for root, middle, final in zip(*stages, strict=True):
        records = [b.store.run(r) for r in (root, middle, final)]
        assert len({(r.spec.name, r.seed, r.spec.config) for r in records}) == 1
        assert [r.spec.until for r in records] == [
            "updates=26000",
            "updates=63000",
            "updates=100000",
        ]
        assert len(b.store.job_runs(records[0].job)) == 3  # one job, its three seeds
        held = records[2].checkpoint
        assert held is not None and (held.run, held.clocks) == (middle, {"updates": 63_000})
        parent_attempt = b.scheduler.attempts(middle)[0].id
        assert held.sha256 == hashlib.sha256(checkpoint_bytes(parent_attempt)).hexdigest()
        attempt = b.scheduler.attempts(final)[0]
        work = next(
            w for w, _ in b.service.sessions[attempt.session].work() if w.attempt == attempt.id
        )
        assert (work.start, work.until) == ({"updates": 63_000}, "updates=100000")
        assert work.checkpoint is not None and work.checkpoint.attempt == parent_attempt
    spent = sum(b.scheduler.read(j).usd for stage in stages for j in stage)
    assert abs(spent - USD_PER_RUN * 18) < Decimal("0.01")
    assert all(b.scheduler.read(j).rental_ended for stage in stages for j in stage)


def test_a_continuation_names_a_clock_its_parent_reported_and_moves_forward(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    parent = b.scheduler.queue(job(until="steps=26000"), rent="l4")
    b.scheduler.tick()
    with pytest.raises(LauncherError, match="no succeeded attempt"):
        b.scheduler.continue_from(parent, "steps=63000")
    b.run(601)
    for target, problem in (
        ("steps=26000", "not beyond the checkpoint's steps=26000"),
        ("agent_steps=90000", "a clock its parent reported: steps"),
        ("steps", "not a target; write it as clock=value"),
        ("steps=many", "not a target"),
    ):
        with pytest.raises(LauncherError, match=problem):
            b.scheduler.queue(b.scheduler.continue_from(parent, target), rent="l4")
    further = b.scheduler.queue(b.scheduler.continue_from(parent, "steps=63000.0"), rent="l4")
    b.scheduler.tick()
    assert b.scheduler.record(further).spec.until == "steps=63000"  # one way to write a target


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
    one = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    assert (b.scheduler.record(one).pool, b.scheduler.record(one).rent) == (None, "l4")
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
    first, second, third = (b.scheduler.queue(job(), "lab", rent="l4") for _ in range(3))
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
    assert len(b.store.machines("l4")) == 2


def test_a_pool_alone_never_rents_and_rent_alone_never_takes_owned_machines(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.scheduler.add_machine("shed", "owned")
    owned = [b.scheduler.queue(job(), "shed") for _ in range(2)]
    rented = b.scheduler.queue(job(), rent="l4")
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
    run = b.scheduler.queue(job(), "lab", rent="l4")
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
    later = b.scheduler.queue(job(), "lab")  # the pool has no machine left to give
    b.run(1)
    assert b.scheduler.read(later).phase == "queued"
    assert b.store.machine("window").deleted_at is None  # owned machines are never deleted


def test_quote_prints_the_first_wave_and_its_most_spend_and_rents_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path, max_machines=1)
    b.scheduler.add_pool(Pool(name="lab"))
    b.scheduler.add_machine("mini", "owned", pool="lab")
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    path = tmp_path / "job.py"
    path.write_text(
        "from etude.launch.records import Job\n"
        "def runs(): return Job(command='train', max_hours=1, max_usd=2).runs(1, 2, 3)\n"
    )
    counting = Counting(b.store, b.service)
    with counting.record():
        code = main(
            ["launch", str(path), "--pool", "lab", "--rent", "l4"]
            + ["--cohort", "three", "--parallel", "2", "--quote"]
        )
    assert code == 0
    assert counting.snapshot().provider == 1  # one price; nothing created
    assert capsys.readouterr().out.splitlines() == [
        "seed1  machine mini  at most $0.00",
        "seed2  rent NVIDIA L4 with at least 6 vCPUs, 48 GB at $0.59/hour  at most $0.88",
        "maximum spend: $0.88; nothing rented",
        "the whole launch is capped at $6.00 by its job and cohort limits",
    ]
    assert b.service.creates == 0 and not b.store.runs() and not b.store.sessions()
    assert not b.store.cohort_exists("three")
    b.scheduler.set_ondemand_max("l4", 0)
    with pytest.raises(LauncherError, match="at its machine limit"):
        b.scheduler.quote(job().runs(1, 2), "lab", rent="l4", cohort="three", parallel=2)


def test_availability_asks_the_provider_once_and_rents_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    b = bench(tmp_path, max_machines=2)
    b.scheduler.rent("l4", hours=1)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    counting = Counting(b.store, b.service)
    with counting.record():
        assert main(["availability", "--rent", "l4"]) == 0
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
        "mini  lab  owned, $0.00 committed, never expires",
        "window  lab  owned, $12.50 committed, expires 2026-10-12 00:00 UTC",
    ]
    b.clock.advance(24 * 3600)
    assert main(["machines"]) == 0
    assert "window  lab  owned, $12.50 committed, expired 2026-10-12 00:00 UTC" in (
        capsys.readouterr().out
    )


def test_a_job_is_what_decides_the_result_not_its_name_or_limits(tmp_path: Path) -> None:
    b = bench(tmp_path)
    first = b.scheduler.queue(Run(job=job(config="hidden = 256", name="small"), seed=1), rent="l4")
    b.scheduler.tick()
    richer = job(config="hidden = 256", name="small again", max_usd=Decimal(9), max_hours=2.0)
    second = b.scheduler.queue(Run(job=richer, seed=2), rent="l4")
    b.scheduler.tick()
    other = b.scheduler.queue(Run(job=job(config="hidden = 1024"), seed=1), rent="l4")
    b.scheduler.tick()
    same, also, different = (b.scheduler.record(r) for r in (first, second, other))
    assert same.job == also.job != different.job
    assert [r.seed for r in b.store.job_runs(same.job)] == [1, 2]
    assert (also.spec.max_usd, also.name) == (Decimal(9), "small again-seed2")
    assert same.spec.max_usd == Decimal(5)


def test_queue_returns_its_id_even_when_the_provider_cannot_quote_a_price(tmp_path: Path) -> None:
    b = bench(tmp_path)
    quote = b.service.offer

    def no_quote() -> Offer:
        raise ConnectionError("pricing is down")

    b.service.offer = no_quote  # type: ignore[method-assign]
    one = b.scheduler.queue(job(), rent="l4")
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
    one = b.scheduler.queue(job(), rent="capped")  # the fake provider asks $0.59
    b.scheduler.tick()
    assert b.scheduler.read(one).phase == "queued" and b.service.creates == 0
    b.service.usd_per_hour = Decimal("0.49")
    b.run(1)
    held = b.store.session(b.scheduler.attempts(one)[0].session)
    assert b.store.rental(held.machine).usd_per_hour == Decimal("0.49")


def test_a_rental_made_by_hand_takes_several_launches_and_one_setup(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", hours=2)
    held = b.store.rented_session(machine).id
    assert b.service.creates == 1 and b.store.rental(machine).kept
    assert b.service.sessions[held].packet.autorun

    first = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    b.run(1)  # the work is handed over
    b.run(1)
    assert b.scheduler.read(first).phase == "running"
    b.run(600)
    assert b.scheduler.read(first).outcome == "succeeded"
    b.run(100)  # idle: the rental stays, and nobody's run pays for it
    second = b.scheduler.queue(job(), rent="l4")
    b.scheduler.tick()
    too_long = b.scheduler.queue(job(max_hours=3.0), rent="l4")  # more time than it has left
    b.scheduler.tick()
    b.run(1)
    b.run(601)
    statuses = [b.scheduler.read(r) for r in (first, second)]
    assert [s.outcome for s in statuses] == ["succeeded", "succeeded"]
    assert {s.machine for s in statuses} == {machine} and b.service.creates == 1
    assert all(abs(s.usd - USD_PER_RUN) < Decimal("0.001") for s in statuses)
    assert str(b.scheduler.read(too_long)) == "queued (waiting for l4: at its machine limit), $0.00"

    ((still, cost),) = b.scheduler.rentals()
    assert still.machine == machine and cost > USD_PER_RUN * 2  # idle time is the rental's
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
    assert b.store.rental(machine).deadline == b.clock.now + timedelta(minutes=30)
    one = b.scheduler.queue(job(max_hours=0.2), rent="l4")
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
    waiting = b.scheduler.queue(job(), rent="l4")
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
    one = b.scheduler.queue(job())
    b.scheduler.tick()
    assert (b.scheduler.record(one).pool, b.scheduler.record(one).rent) == ("shed", "l4")
    assert Database(tmp_path / "launcher.db").default_target() == Target(pool="shed", rent="l4")
    b.scheduler.turn_pool("shed", off=True)
    assert b.scheduler.record(b.scheduler.queue(job())).pool is None  # rents only
    b.scheduler.set_default("shed")
    with pytest.raises(LauncherError, match="name a pool"):
        b.scheduler.queue(job())


def test_completion_between_progress_and_machine_reads_uses_final_journal(tmp_path: Path) -> None:
    b = bench(tmp_path)
    run = b.scheduler.queue(job(until="steps=42"), rent="l4")
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


def test_recovery_uses_last_checkpoint_without_creating_a_parent_run(tmp_path: Path) -> None:
    b = bench(tmp_path)
    run = b.scheduler.queue(job(until="steps=100"), rent="l4")
    b.scheduler.tick()
    first = b.scheduler.attempts(run)[0]
    original = b.service.observe
    data = checkpoint_bytes(first.id)
    output = Output(
        name="checkpoint.pt", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    )

    def observe(session_id: str) -> Observation:
        observation = original(session_id)
        if session_id == first.session:
            if first.planned_at is not None and b.clock() > first.planned_at + timedelta(
                seconds=100
            ):
                return observation.model_copy(
                    update={
                        "progress": tuple(
                            p.model_copy(update={"clocks": {"steps": 60}})
                            for p in observation.progress
                        )
                    }
                )
            return observation.model_copy(
                update={
                    "progress": tuple(
                        p.model_copy(
                            update={
                                "clocks": {"steps": 40},
                                "checkpoint": output.name,
                                "outputs": (output,),
                            }
                        )
                        for p in observation.progress
                    )
                }
            )
        return observation

    b.service.observe = observe  # type: ignore[method-assign]
    b.run(100)
    b.run(1)  # later progress without a checkpoint must not advance its resume coordinate
    b.service.kill(first.session)
    b.run(1)
    second = b.scheduler.attempts(run)[1]
    work = b.service.sessions[second.session].packet.work[0]
    assert work.start == {"steps": 40}
    assert work.checkpoint is not None and work.checkpoint.sha256 == output.sha256
    assert b.scheduler.record(run).checkpoint is None
    b.run(601)
    assert b.scheduler.read(run).outcome == "succeeded"
    assert len(b.scheduler.runs(done=True)) == 1


def test_machine_window_retries_but_run_hours_do_not_reset(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_ondemand(
        b.store.ondemand("l4").model_copy(update={"name": "short", "max_rental_seconds": 300})
    )
    b.service.seconds = 7200
    run = b.scheduler.queue(job(max_hours=500 / 3600), rent="short")
    b.scheduler.tick()
    b.run(300)
    assert len(b.scheduler.attempts(run)) == 2
    assert b.scheduler.attempts(run)[1].limit_seconds == 200
    b.run(200)
    assert b.scheduler.read(run).outcome == "out_of_time"


def test_retries_share_the_runs_dollar_limit(tmp_path: Path) -> None:
    b = bench(tmp_path, usd_per_hour="1")
    b.service.seconds = 1000
    run = b.scheduler.queue(job(max_usd=Decimal(200) / 3600), rent="l4")
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
    run = b.scheduler.queue(free, "mine")
    assert b.scheduler.record(run).spec.max_usd is None
    with pytest.raises(LauncherError, match="more than zero"):
        b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(0)), "mine")
