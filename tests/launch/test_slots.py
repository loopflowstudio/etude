"""Several runs at once on one machine: placement, cost split, cancel and outputs."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

from etude.job import Job
from etude.launch.database import Database, launcher_file
from etude.launch.executor import local as local_service
from etude.launch.records import Machine, Pool
from etude.launch.scheduler import Scheduler
from tests.launch.fake import bench, output_bytes

JOB = Job(command="train", max_hours=1, max_usd=Decimal(1))


def _hours_usd(seconds: int, price: str = "0.59") -> Decimal:
    return Decimal(price) * seconds / 3600


def test_a_machine_takes_waiting_runs_up_to_its_slots(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=2, slots=2)
    runs = [
        b.scheduler.queue(JOB.model_copy(update={"name": f"r{i}"}), rent="l4") for i in range(3)
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
    runs = [b.scheduler.queue(JOB, rent="l4") for _ in range(3)]
    b.scheduler.tick()
    assert [b.scheduler.read(r).machine for r in runs] == [machine, machine, None]
    assert b.scheduler.read(runs[2]).phase == "queued"
    b.run(601)  # the first two finish; the third takes a freed slot
    assert b.scheduler.read(runs[2]).machine == machine
    b.run(601)
    assert [b.scheduler.read(r).outcome for r in runs] == ["succeeded"] * 3
    assert b.service.creates == 1


def test_runs_sharing_a_machine_split_its_cost_and_cancel_independently(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    first, second = (b.scheduler.queue(JOB, rent="l4") for _ in range(2))
    b.scheduler.tick()
    b.run(300)
    b.scheduler.cancel(first)
    b.run(1)
    assert b.scheduler.read(first).outcome == "cancelled"
    assert b.scheduler.read(second).phase == "running"
    b.run(400)
    assert b.scheduler.read(second).outcome == "succeeded"
    # Together for 300 s, each pays half; then the second pays its 300 s alone.
    assert b.scheduler.read(first).usd == _hours_usd(300) / 2
    assert b.scheduler.read(second).usd == _hours_usd(300) / 2 + _hours_usd(300)
    # The runs share what they occupied; idle time before the machine ended is the rental's.
    occupied = b.scheduler.read(first).usd + b.scheduler.read(second).usd
    assert round(occupied, 12) == round(_hours_usd(600), 12) < b.scheduler.rentals()[0].usd


def test_each_run_on_a_shared_machine_publishes_its_own_outputs(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    runs = [b.scheduler.queue(JOB, rent="l4") for _ in range(2)]
    b.scheduler.tick()
    b.run(601)
    for run in runs:
        (attempt,) = b.scheduler.attempts(run)
        assert [o.name for o in attempt.outputs] == ["weights.pt"]
        fetched = b.scheduler.fetch(run, "weights.pt", tmp_path / run)
        assert fetched.read_bytes() == output_bytes(attempt.id)


def test_quote_shows_runs_sharing_one_rental(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=2, slots=2)
    quote = b.scheduler.quote(JOB.runs(1, 2, 3), rent="l4", cohort="trial", parallel=2)
    assert [(q.run, q.alongside) for q in quote.machines] == [
        ("seed-1", ("seed-2",)),
        ("seed-2", ("seed-1",)),
        ("seed-3", ()),
    ]
    assert {q.slots for q in quote.machines} == {2}
    assert quote.machines[0].max_usd == quote.machines[1].max_usd
    assert quote.machines[0].max_usd + quote.machines[1].max_usd == quote.machines[2].max_usd


def test_launch_places_a_cohort_on_one_machine_when_its_slots_hold_it(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1, slots=2)
    runs = b.scheduler.launch_cohort("pair", JOB.runs(1, 2), rent="l4")
    assert len({b.scheduler.read(r).machine for r in runs}) == 1
    b.run(601)
    assert [b.scheduler.read(r).outcome for r in runs] == ["succeeded"] * 2
    assert b.service.creates == 1


def test_one_slot_keeps_running_runs_in_turn(tmp_path: Path) -> None:
    b = bench(tmp_path, max_machines=1)
    machine = b.scheduler.rent("l4", 3)
    runs = [b.scheduler.queue(JOB, rent="l4") for _ in range(2)]
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
    scheduler = Scheduler(Database(launcher_file(tmp_path)), {"local": lambda held: service})
    scheduler.add_pool(Pool(name="local", slots=2))
    scheduler._store.add_machine(Machine(name="local", pool="local", kind="local"))
    job = Job(command=RENDEZVOUS.format(meet=meet), max_hours=0.1)
    runs = scheduler.queue_cohort("pair", job.runs(1, 2), "local", parallel=1)
    for _ in range(400):
        statuses = [scheduler.status(run) for run in runs]
        if all(status.done for status in statuses):
            break
        time.sleep(0.05)
    assert [s.outcome for s in statuses] == ["succeeded"] * 2
    for seed, run in enumerate(runs, start=1):
        fetched = scheduler.fetch(run, "seed.txt", tmp_path / run)
        assert fetched.read_text() == f"seed {seed}\n"
