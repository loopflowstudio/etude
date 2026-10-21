"""Print runs the way every command and the controller lists them."""

import os
from collections.abc import Sequence

from etude.launch.errors import LauncherError
from etude.launch.records import Machine
from etude.launch.scheduler import Scheduler
from etude.launch.source import line


def show_runs(scheduler: Scheduler, ids: Sequence[str]) -> None:
    for run in ids:
        status = scheduler.read(run)
        where = f", machine {status.machine}" if status.machine else ""
        where += f" ({status.hardware})" if status.hardware else ""
        record = scheduler.record(run)
        print(run, record.name, str(status) + where, sep="  ")
        if record.spec.repo is not None:
            print("  source: " + line(record.source, record.spec.revision or "HEAD"))


def resolve_runs(scheduler: Scheduler, names: Sequence[str]) -> list[str]:
    """Run ids for what was typed: a cohort's name stands for all of its runs."""

    ids: list[str] = []
    for name in names:
        try:
            ids += [run.id for run in scheduler.cohort_runs(name)]
        except LauncherError:
            ids.append(name)
    return ids


def _here() -> str:
    """What to call the machine that holds the database, as its owner would read it."""
    return "the controller" if os.environ.get("ETUDE_CONTROLLER_LOCAL") else "this computer"


def _machine_name(machine: Machine) -> str:
    return _here() if machine.kind == "local" else machine.name


def show_pools(scheduler: Scheduler) -> None:
    """One line per pool: its machines, whether it can make more, and where records go."""
    chosen = scheduler.default_pool()
    for each in scheduler.pools():
        parts = []
        added = [_machine_name(m) for m in scheduler.machines(each.name)]
        rental = [
            m.name for m in scheduler.machines(each.name, rented=True) if m.deleted_at is None
        ]
        if added:
            parts.append(", ".join(added))
        if rental:
            parts.append("rented: " + ", ".join(rental))
        if each.on_demand_template:
            kind = each.on_demand_template.gpu or "machine"
            source = each.on_demand_template.provider
            if each.on_demand_template.data_center:
                source += f" in {each.on_demand_template.data_center}"
            parts.append(f"makes up to {each.max_on_demand} {kind} on {source}")
            parts.append("safeguards: " + ", ".join(scheduler.safeguards(each.name)))
            if each.storage:
                parts.append(f"records in {each.storage}")
        marks = (" (default)" if each.name == chosen else "") + (" (off)" if each.off else "")
        print(each.name, ("; ".join(parts) or "no machines") + marks, sep="  ")


def show_machines(scheduler: Scheduler, *, ended: bool, images: bool = False) -> None:
    """One line per machine: added, or rented with its window and cost."""
    windows = {rental.machine: (rental, usd) for rental, usd in scheduler.rentals()}
    for each in scheduler.pools():
        for machine in scheduler.machines(each.name):
            where = f" ({_here()})" if machine.kind == "local" else ""
            print(machine.name, each.name, "added" + where, sep="  ")
        for machine in scheduler.machines(each.name, rented=True):
            if machine.name not in windows or (machine.deleted_at is not None and not ended):
                continue
            rental, usd = windows[machine.name]
            estimated = scheduler._estimated(rental, scheduler._clock())
            cost = f"{'~' if estimated else ''}${usd:.2f}"
            if rental.billed_through is not None:
                cost = f"${rental.billed_usd:.4f} billed" + (
                    f" + ~${usd - rental.billed_usd:.4f}" if estimated else ""
                )
            if machine.deleted_at is not None:
                window = f"rented, ended {machine.deleted_at:%Y-%m-%d %H:%M} UTC"
            else:
                window = f"rented until {rental.deadline:%Y-%m-%d %H:%M} UTC"
            hardware = f"{machine.hardware}, " if machine.hardware else ""
            print(machine.name, each.name, f"{hardware}{window}, {cost}", sep="  ")
            if images and machine.image is not None:
                image = machine.image
                print(
                    f"  image: {image.requested} -> {image.resolved} ({image.platform or 'platform unchecked'})"
                )
            if rental.billed_usd > rental.cap_usd:
                print(f"warning: {machine.name} bill exceeds its ${rental.cap_usd:.4f} cap")
