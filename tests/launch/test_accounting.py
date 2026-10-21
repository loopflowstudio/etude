"""Late bills correct evidence, while estimates keep deciding admission."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from etude.launch.machines import Allocation, Bill
from etude.launch.records import Job, LauncherError, MachineTemplate, Pool, Storage
from etude.launch.schemas import Observation
from tests.launch.fake import bench


def test_bills_cover_only_their_hours_and_do_not_change_admission(tmp_path: Path) -> None:
    b = bench(tmp_path, usd_per_hour="1")
    run = b.scheduler.queue(Job(command="train", max_hours=2, max_usd=Decimal(2)), "l4")
    b.scheduler.tick()
    held = b.store.session(b.scheduler.attempts(run)[0].session)
    b.service.seconds = 5000
    b.clock.advance(4000)
    b.service.bill = lambda session_id, now: Bill(  # type: ignore[method-assign]
        usd=Decimal("1.2"), through=held.planned_at + timedelta(hours=1)
    )
    b.scheduler.tick()
    status = b.scheduler.read(run)
    assert abs(status.usd - (Decimal("1.2") + Decimal(400) / 3600)) < Decimal("0.00001")
    assert status.estimated
    b.run(1100)
    ended = b.store.session(held.id)
    assert ended.ended_at is not None
    b.service.bill = lambda session_id, now: Bill(  # type: ignore[method-assign]
        usd=Decimal("1.6"), through=held.planned_at + timedelta(hours=2)
    )
    b.clock.advance(3600)
    b.scheduler.tick()
    assert not b.scheduler.read(run).estimated
    assert abs(b.scheduler.read(run).usd - Decimal("1.6") * 5000 / 5100) < Decimal("0.00001")
    # Idle time between completion and controller cleanup belongs to the rental.
    assert b.scheduler.rentals()[0][1] == Decimal("1.6")
    assert b.store.rental(held.machine).cap_usd == Decimal(2)


def test_unstarted_machine_is_deleted_after_configured_startup_allowance(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.store.set_pool_startup("l4", 600)

    b.service.observe = lambda session_id: Observation(  # type: ignore[method-assign]
        alive=b.service._alive(b.service.sessions[session_id]), progress=()
    )
    b.service.started = lambda session_id: False  # type: ignore[method-assign]
    run = b.scheduler.launch(
        Job(command="train", max_hours=1, max_usd=Decimal(1), max_attempts=1), "l4"
    )
    held = b.store.session(b.scheduler.attempts(run)[0].session)
    assert held.started_at is None
    b.run(301)
    assert b.store.session(held.id).ended_at is None
    b.run(300)
    assert b.store.session(held.id).ended_at is not None
    assert b.scheduler.read(run).cause == "machine_lost_before_start"


def test_actual_create_price_replaces_quote_without_raising_cap(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.service.allocation = lambda session_id: Allocation(  # type: ignore[method-assign]
        id=session_id, address=None, usd_per_hour=Decimal("0.7"), created_at=b.clock()
    )
    run = b.scheduler.launch(Job(command="train", max_hours=1, max_usd=Decimal(1)), "l4")
    held = b.store.session(b.scheduler.attempts(run)[0].session)
    assert b.store.rental(held.machine).usd_per_hour == Decimal("0.7")
    assert b.store.rental(held.machine).cap_usd == Decimal("0.59")


def test_rented_machine_requires_storage_but_not_a_private_image(tmp_path: Path) -> None:
    b = bench(tmp_path)
    job = Job(command="train", max_hours=1, max_usd=Decimal(1))
    b.scheduler.add_pool(
        Pool(name="stock", on_demand_template=MachineTemplate(provider="ssh"), max_on_demand=2)
    )
    with pytest.raises(LauncherError, match="pool storage"):
        b.scheduler.launch(job, "stock")
    with pytest.raises(LauncherError, match="pool storage"):
        b.scheduler.rent("stock", hours=2)
    assert not b.store.runs()
    b.scheduler.add_storage(
        Storage(name="stock-store", bucket="journal", region="us-west-2", prefix="test")
    )
    b.scheduler.add_pool(
        Pool(
            name="stock-ready",
            on_demand_template=MachineTemplate(provider="ssh"),
            storage="stock-store",
            max_on_demand=2,
        )
    )
    machine = b.scheduler.rent("stock-ready", hours=2)
    held = b.store.rented_session(machine).id
    run = b.scheduler.launch(job, "stock-ready")
    assert b.scheduler.attempts(run)[0].session == held
    assert b.store.session(held).autorun


def test_missing_autorun_setup_does_not_exclude_an_owned_host(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.scheduler.add_pool(
        Pool(name="mixed", on_demand_template=MachineTemplate(provider="ssh"), max_on_demand=1)
    )
    b.scheduler.add_machine("host", "owned", pool="mixed")
    first = b.scheduler.launch(Job(command="train", max_hours=1, max_usd=Decimal(1)), "mixed")
    second = b.scheduler.queue(Job(command="train", max_hours=1, max_usd=Decimal(1)), "mixed")
    b.scheduler.tick()
    assert not b.store.session(b.scheduler.attempts(first)[0].session).autorun
    assert "pool storage" in str(b.scheduler.read(second))
    assert b.service.creates == 0
    b.run(601)
    assert b.scheduler.read(second).phase == "setting_up"


def test_unreported_start_intent_prevents_retry_after_machine_loss(tmp_path: Path) -> None:
    b = bench(tmp_path)
    run = b.scheduler.launch(Job(command="train", max_hours=1, max_usd=Decimal(1)), "l4")
    held = b.scheduler.attempts(run)[0].session
    observe = b.service.observe
    b.service.observe = lambda session_id: observe(session_id).model_copy(  # type: ignore[method-assign]
        update={"start_unknown": True}
    )
    b.service.kill(held)
    b.scheduler.tick()
    assert b.scheduler.read(run).cause == "create_unconfirmed"
    assert b.service.creates == 1
