"""Several runs at once on one machine: placement, cost split, cancel and outputs."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

from etude.errors import LauncherError
from etude.launch.database import launcher_file
from etude.launch.executor import local as local_service
from etude.launch.provisioner.machines import Safeguards
from etude.launch.records import Machine, OnDemand, Pool
from etude.launch.scheduler.loop import Scheduler
from etude.spec import Job, RunRequest
from tests.launch.fake import L4, bench, open_database, output_bytes

JOB = Job(command="train", max_hours=1, max_usd=Decimal(1))


def _hours_usd(seconds: int, price: str = "0.59") -> Decimal:
    return Decimal(price) * seconds / 3600


def test_a_machine_takes_waiting_runs_up_to_its_slots(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=2, slots=2)
    runs = [
        b.scheduler.queue(JOB.model_copy(update={"name": f"r{i}"}), rent="l4")[0] for i in range(3)
    ]
    b.scheduler.tick()
    machines = [b.scheduler.read(run).machine for run in runs]
    assert machines[0] == machines[1] != machines[2]  # the first fills both slots of one
    assert len(b.scheduler.machines("l4")) == 2
    b.run(1)
    shared = [b.scheduler.read(run) for run in runs]
    assert [s.phase for s in shared] == ["running"] * 3
    assert [s.shared for s in shared] == [2, 2, 1]
    assert "shared by 2 runs" in str(shared[0])
    b.run(600)
    assert [b.scheduler.read(run).outcome for run in runs] == ["succeeded"] * 3


def test_a_full_machine_leaves_runs_waiting_and_takes_them_as_slots_free(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    machine = b.scheduler.rent("l4", 3)
    runs = [b.scheduler.queue(JOB, rent="l4")[0] for _ in range(3)]
    b.scheduler.tick()
    assert [b.scheduler.read(r).machine for r in runs] == [machine, machine, None]
    assert b.scheduler.read(runs[2]).phase == "queued"
    b.run(601)  # the first two finish; the third takes a freed slot
    assert b.scheduler.read(runs[2]).machine == machine
    b.run(601)
    assert [b.scheduler.read(r).outcome for r in runs] == ["succeeded"] * 3
    assert b.service.creates == 1


def test_runs_sharing_a_machine_split_its_cost_and_cancel_independently(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=3)
    early, first, second = (b.scheduler.queue(JOB, rent="l4")[0] for _ in range(3))
    b.scheduler.tick()
    b.scheduler.cancel(early)  # cancelled before starting
    b.run(300)
    b.scheduler.cancel(first)
    b.run(1)
    assert b.scheduler.attempts(early)[0].started_at is None
    assert b.scheduler.read(early).outcome == "cancelled"
    assert b.scheduler.read(first).outcome == "cancelled"
    assert b.scheduler.read(second).phase == "running"
    b.run(400)
    assert b.scheduler.read(second).outcome == "succeeded"
    # Every placed run funds the survivor through its finish, even one cancelled before start.
    for run in (early, first, second):
        assert round(b.scheduler.read(run).usd, 12) == round(_hours_usd(600) / 3, 12)
    # The runs share what they occupied; idle time before the machine ended is the rental's.
    occupied = sum((b.scheduler.read(r).usd for r in (early, first, second)), Decimal(0))
    assert round(occupied, 12) == round(_hours_usd(600), 12) < b.scheduler.rentals()[0].usd


def test_finished_places_never_pay_for_the_next_batch(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2, usd_per_hour="0.60")
    machine = b.scheduler.rent("l4", 3)
    first, second, later = (b.scheduler.queue(JOB, rent="l4")[0] for _ in range(3))
    b.scheduler.tick()
    b.run(600)
    b.run(600)
    statuses = [b.scheduler.read(run) for run in (first, second, later)]
    assert all(s.machine == machine and s.outcome == "succeeded" for s in statuses)
    assert [s.usd for s in statuses] == [Decimal("0.05"), Decimal("0.05"), Decimal("0.10")]
    assert [s.shared for s in statuses] == [2, 2, 1]


def test_a_retry_on_the_same_machine_holds_one_share(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2, usd_per_hour="0.60")
    machine = b.scheduler.rent("l4", 3)
    (first,) = b.scheduler.queue(JOB, rent="l4")
    b.scheduler.tick()
    b.run(300)
    (peer,) = b.scheduler.queue(JOB, rent="l4")
    b.scheduler.tick()
    initial = b.scheduler.attempts(first)[0].id
    b.service.outcome = lambda work: "lost" if work.attempt == initial else "succeeded"
    b.run(300)
    b.run(600)

    assert [a.machine for a in b.scheduler.attempts(first)] == [machine, machine]
    assert [b.scheduler.read(r).outcome for r in (first, peer)] == ["succeeded"] * 2
    # The first run funds 300 seconds alone, then both fund the remaining 900.
    assert b.scheduler.read(first).usd == Decimal("0.125")
    assert b.scheduler.read(peer).usd == Decimal("0.075")


def test_each_run_on_a_shared_machine_publishes_its_own_outputs(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    runs = [b.scheduler.queue(JOB, rent="l4")[0] for _ in range(2)]
    b.scheduler.tick()
    b.run(601)
    for run in runs:
        (attempt,) = b.scheduler.attempts(run)
        assert [o.name for o in attempt.outputs] == ["weights.pt"]
        fetched = b.scheduler.fetch(run, "weights.pt", tmp_path / run)
        assert fetched.read_bytes() == output_bytes(attempt.id)


def test_quote_shows_runs_sharing_one_rental(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=2, slots=2)
    job = JOB.model_copy(update={"experiment": "trial", "name": "baseline", "seeds": (1, 2, 3)})
    quote = b.scheduler.quote(job.runs(), rent="l4")
    assert [(q.run, q.alongside) for q in quote.machines] == [
        ("trial/baseline@1", ("trial/baseline@2",)),
        ("trial/baseline@2", ("trial/baseline@1",)),
        ("trial/baseline@3", ()),
    ]
    assert {q.slots for q in quote.machines} == {2}
    assert quote.machines[0].max_usd == quote.machines[1].max_usd
    assert quote.machines[0].max_usd + quote.machines[1].max_usd == quote.machines[2].max_usd


def test_launch_places_a_cohort_on_one_machine_when_its_slots_hold_it(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    runs = b.scheduler.launch(JOB.runs(1, 2), rent="l4")
    assert len({b.scheduler.read(r).machine for r in runs}) == 1
    b.run(601)
    assert [b.scheduler.read(r).outcome for r in runs] == ["succeeded"] * 2
    assert b.service.creates == 1


def test_one_slot_keeps_running_runs_in_turn(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", 3)
    runs = [b.scheduler.queue(JOB, rent="l4")[0] for _ in range(2)]
    b.scheduler.tick()
    assert [b.scheduler.read(r).machine for r in runs] == [machine, None]


RENDEZVOUS = """
touch {meet}/$ETUDE_SEED
for _ in $(seq 200); do
  [ -f {meet}/1 ] && [ -f {meet}/2 ] && break
  sleep 0.05
done
[ -f {meet}/1 ] && [ -f {meet}/2 ] || exit 4
echo "seed $ETUDE_SEED" > "$ETUDE_OUTPUT_DIR/seed.txt"
"""


def test_a_real_worker_runs_two_slots_at_once_with_their_own_outputs(tmp_path: Path) -> None:
    # Each job waits until the other has started, so both pass only when they overlap.
    meet = tmp_path / "meet"
    meet.mkdir()
    service = local_service(tmp_path / "sessions")
    scheduler = Scheduler(open_database(launcher_file(tmp_path)), {"local": lambda held: service})
    scheduler.add_pool(Pool(name="local", slots=2))
    scheduler._store.add_machine(Machine(name="local", pool="local", kind="local"))
    job = Job(command=RENDEZVOUS.format(meet=meet), max_hours=0.1)
    runs = scheduler.queue(job.runs(1, 2), "local")
    for _ in range(400):
        statuses = [scheduler.status(run) for run in runs]
        if all(status.done for status in statuses):
            break
        time.sleep(0.05)
    assert [s.outcome for s in statuses] == ["succeeded"] * 2
    for seed, run in enumerate(runs, start=1):
        fetched = scheduler.fetch(run, "seed.txt", tmp_path / run)
        assert fetched.read_text() == f"seed {seed}\n"


@pytest.mark.parametrize("seeds", [1, 3, 4])
def test_strict_rental_funds_only_the_seeds_placed(tmp_path: Path, seeds: int) -> None:
    b = bench(tmp_path, slots=3, max_machines=2, usd_per_hour="0.69")
    requests = JOB.model_copy(update={"max_usd": Decimal("0.23")}).runs(*range(seeds))
    if seeds == 1:
        with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
            b.scheduler.quote(requests, rent="l4")
        with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
            b.scheduler.launch(requests, rent="l4")
        assert b.service.creates == 0
        return
    quote = b.scheduler.quote(requests, rent="l4")
    assert [q.max_usd for q in quote.machines] == [Decimal("0.23")] * 3
    assert all(len(q.alongside) == 2 for q in quote.machines)
    assert b.service.creates == 0
    runs = b.scheduler.launch(requests, rent="l4")
    assert b.service.offers == 2 * (1 if seeds == 3 else 2)
    assert b.service.creates == 1
    (rental,) = b.store.rentals()
    assert rental.cap_usd == quote.max_usd == Decimal("0.69")
    assert (rental.deadline - b.clock()).total_seconds() == 3600
    for run in runs[:3]:
        (attempt,) = b.scheduler.attempts(run)
        assert (attempt.machine, attempt.limit_seconds, attempt.limit_kind) == (
            rental.machine,
            3600,
            "out_of_time",
        )
    if seeds == 4:
        assert b.scheduler.read(runs[3]).phase == "queued"


@pytest.mark.parametrize(("increment", "share"), [(0, "0.2875"), (1, "0.46")])
def test_group_includes_setup_and_provider_rounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, increment: int, share: str
) -> None:
    b = bench(tmp_path, usd_per_hour="0.69")
    monkeypatch.setattr(
        b.scheduler._services["runpod"],
        "safeguards",
        Safeguards(reservation_hours=increment),
        raising=False,
    )
    b.scheduler.add_ondemand(
        OnDemand(name="shared", template=L4, max_machines=1, slots=3, setup_seconds=900)
    )
    job = JOB.model_copy(update={"max_usd": Decimal(share)})
    short = job.model_copy(update={"max_usd": Decimal(share) - Decimal("0.0001")})
    with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
        b.scheduler.quote(short.runs(1, 2, 3), rent="shared")
    quote = b.scheduler.quote(job.runs(1, 2, 3), rent="shared")
    assert [q.max_usd for q in quote.machines] == [Decimal(share)] * 3
    b.scheduler.launch(job.runs(1, 2, 3), rent="shared")
    (rental,) = b.store.rentals()
    assert (rental.deadline - b.clock()).total_seconds() == 4500
    assert rental.cap_usd == quote.max_usd


def test_joining_a_manual_rental_prices_the_peer_time_remaining(tmp_path: Path) -> None:
    b = bench(tmp_path, slots=3, max_machines=1, usd_per_hour="0.60")
    machine = b.scheduler.rent("l4", 2)
    # For the first half hour all three share; the newcomer funds its last half hour alone.
    b.scheduler.launch(JOB.model_copy(update={"max_hours": 0.5}).runs(1, 2), rent="l4")
    job = JOB.model_copy(update={"max_usd": Decimal("0.40")})
    with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
        b.scheduler.quote(job.model_copy(update={"max_usd": Decimal("0.39")}).runs(3), rent="l4")
    (q,) = b.scheduler.quote(job.runs(3), rent="l4").machines
    assert q.machine == machine and q.max_usd == Decimal("0.40")
    (run,) = b.scheduler.launch(job.runs(3), rent="l4")
    (attempt,) = b.scheduler.attempts(run)
    assert attempt.machine == machine and attempt.limit_seconds == 3600
    assert b.service.creates == 1


@pytest.mark.parametrize(("rate", "window"), [("0.60", 4200), ("0.69", 4121.739130)])
def test_waiting_run_funds_only_the_extension(tmp_path: Path, rate: str, window: float) -> None:
    b = bench(tmp_path, max_machines=1, usd_per_hour=rate)
    waiting = JOB.model_copy(update={"max_usd": Decimal("0.10")})
    requests = [*JOB.runs(1), *waiting.runs(2)]
    quote = b.scheduler.quote(requests, rent="l4")
    assert [q.max_usd for q in quote.machines] == [Decimal(rate)]
    assert quote.max_usd <= Decimal(rate) + Decimal("0.10")
    b.scheduler.launch(requests, rent="l4")
    (rental,) = b.store.rentals()
    assert (rental.deadline - b.clock()).total_seconds() == window
    assert rental.cap_usd == quote.max_usd


def test_unfunded_place_is_replaced_before_another_offer(tmp_path: Path) -> None:
    b = bench(tmp_path, slots=3, max_machines=1, usd_per_hour="0.69")
    poor = JOB.model_copy(update={"max_usd": Decimal("0.01")})
    funded = JOB.model_copy(update={"max_usd": Decimal("0.23")})
    runs = b.scheduler.launch([*poor.runs(0), *funded.runs(1, 2, 3)], rent="l4")
    assert b.scheduler.read(runs[0]).phase == "queued"
    assert all(b.scheduler.attempts(r)[0].limit_seconds == 3600 for r in runs[1:])
    assert b.service.creates == b.service.offers == 1


def test_resume_reserves_its_unfinished_share_only_on_other_machines(tmp_path: Path) -> None:
    b = bench(tmp_path, slots=2, max_machines=2, usd_per_hour="0.60")
    b.service.seconds = 3600
    job = JOB.model_copy(update={"max_usd": Decimal("0.30")})
    run, peer = b.scheduler.launch(job.runs(1, 2), rent="l4")
    machine = b.scheduler.read(peer).machine
    b.run(300)
    b.scheduler.cancel(run)
    b.run(1)
    assert b.scheduler.read(run).outcome == "cancelled"
    # A two-hour target needs another machine. Its $1.20 covers the remaining
    # execution there but cannot also fund the first rental's $0.30 place.
    longer = RunRequest(
        id=run,
        seed=1,
        job=job.model_copy(update={"max_hours": 2, "max_usd": Decimal("1.20")}),
        resume=True,
    )
    with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
        b.scheduler.quote([longer], rent="l4")
    with pytest.raises(LauncherError, match="job dollars do not cover the hours"):
        b.scheduler.launch(longer, rent="l4")
    # The original place can be continued on the same machine. Allow the
    # one-second gap between stopping and rejoining by shortening the target.
    same = RunRequest(id=run, seed=1, job=job.model_copy(update={"max_hours": 0.999}), resume=True)
    (q,) = b.scheduler.quote([same], rent="l4").machines
    assert q.machine == machine and Decimal("0.27") < q.max_usd < Decimal("0.28")
    b.scheduler.launch(same, rent="l4")
    assert b.scheduler.attempts(run)[-1].machine == machine
    assert b.service.creates == 1
