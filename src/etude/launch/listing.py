"""Print runs the way every command and the controller lists them."""

import os
from collections.abc import Sequence

from etude.launch.errors import LauncherError
from etude.launch.records import Availability, Machine, Quote
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


def _marks(scheduler: Scheduler, name: str) -> str:
    chosen = scheduler.default_target()
    return " (default)" if name in (chosen.pool, chosen.rent) else ""


def show_pools(scheduler: Scheduler) -> None:
    """One line per pool: the machines you own in it."""
    for each in scheduler.pools():
        owned = ", ".join(_machine_name(m) for m in scheduler.machines(each.name))
        marks = _marks(scheduler, each.name) + (" (off)" if each.off else "")
        print(each.name, (owned or "no machines") + marks, sep="  ")


def show_ondemands(scheduler: Scheduler) -> None:
    """One line per on-demand: what it rents, its budget and the machines it holds now."""
    for each in scheduler.ondemands():
        template = each.template
        kind = template.gpu or "CPU"
        if template.vcpus or template.memory_gb:
            kind += f" {template.vcpus or 'any'} vCPUs, {template.memory_gb or 'any'} GB"
        source = template.provider + (f" in {template.data_center}" if template.data_center else "")
        ceiling = (
            f" under ${each.max_usd_per_hour}/hour" if each.max_usd_per_hour is not None else ""
        )
        parts = [f"{kind} on {source}", f"up to {each.max_machines} at once{ceiling}"]
        rented = [m.name for m in scheduler.machines(each.name) if m.deleted_at is None]
        if rented:
            parts.append("rented: " + ", ".join(rented))
        parts.append("safeguards: " + ", ".join(scheduler.safeguards(each.name)))
        if each.storage:
            parts.append(f"records in {each.storage}")
        print(each.name, "; ".join(parts) + _marks(scheduler, each.name), sep="  ")


def show_machines(scheduler: Scheduler, *, ended: bool, images: bool = False) -> None:
    """One line per machine: owned with its cost and expiry, or rented with its window and cost."""
    windows = {rental.machine: (rental, usd) for rental, usd in scheduler.rentals()}
    now = scheduler._clock()
    for each in scheduler.pools():
        for machine in scheduler.machines(each.name):
            where = f" ({_here()})" if machine.kind == "local" else ""
            if machine.expires_at is None:
                expiry = "never expires"
            elif machine.expired(now):
                expiry = f"expired {machine.expires_at:%Y-%m-%d %H:%M} UTC"
            else:
                expiry = f"expires {machine.expires_at:%Y-%m-%d %H:%M} UTC"
            print(
                machine.name,
                each.name,
                f"owned{where}, ${machine.cost_usd:.2f} committed, {expiry}",
                sep="  ",
            )
    for ondemand in scheduler.ondemands():
        for machine in scheduler.machines(ondemand.name):
            if machine.name not in windows or (machine.deleted_at is not None and not ended):
                continue
            rental, usd = windows[machine.name]
            estimated = scheduler._estimated(rental, now)
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
            print(machine.name, ondemand.name, f"{hardware}{window}, {cost}", sep="  ")
            if images and machine.image is not None:
                image = machine.image
                print(
                    f"  image: {image.requested} -> {image.resolved} ({image.platform or 'platform unchecked'})"
                )
            if rental.billed_usd > rental.cap_usd:
                print(f"warning: {machine.name} bill exceeds its ${rental.cap_usd:.4f} cap")


def show_quote(quote: Quote) -> None:
    """One line per machine the first wave would take, then the most it could cost."""
    for each in quote.machines:
        if each.machine is not None:
            where = f"machine {each.machine}" + (
                f" at ${each.usd_per_hour}/hour" if each.usd_per_hour else ""
            )
        else:
            where = f"rent {each.hardware or 'a machine'} at ${each.usd_per_hour}/hour"
        print(each.run, where, f"at most ${each.max_usd:.2f}", sep="  ")
    print(f"maximum spend: ${quote.max_usd:.2f}; nothing rented")
    if quote.limit_usd is not None:
        print(f"the whole launch is capped at ${quote.limit_usd:.2f} by its job and cohort limits")


def show_availability(answer: Availability) -> None:
    offer = answer.offer
    ceiling = (
        "no ceiling" if answer.max_usd_per_hour is None else f"ceiling ${answer.max_usd_per_hour}"
    )
    print(
        answer.ondemand,
        f"{offer.hardware or 'a machine'} at ${offer.usd_per_hour}/hour ({ceiling})",
        "available" if offer.available else "none available",
        f"{answer.in_use} of {answer.max_machines} in use",
        "rentable now" if answer.rentable else "not rentable now",
        f"asked {answer.observed_at:%Y-%m-%d %H:%M:%S} UTC",
        sep="  ",
    )
