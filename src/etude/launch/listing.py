"""Print runs the way every command and the server lists them."""

import os
from collections.abc import Sequence
from decimal import Decimal

from etude.identity import run_address
from etude.launch.records import Availability, Machine, Quote
from etude.launch.scheduler.loop import Scheduler
from etude.launch.server import PythonClient, selected
from etude.launch.source import line


def show_runs(scheduler: Scheduler | PythonClient, ids: Sequence[str]) -> None:
    for run in ids:
        status = scheduler.read(run)
        where = f", machine {status.machine}" if status.machine else ""
        where += f" ({status.hardware})" if status.hardware else ""
        record = scheduler.record(run)
        if status.attempts:
            where += f", {status.attempts} of {record.launch.spec.max_attempts} attempts used"
        print(run, record.name, str(status) + where, sep="  ")
        if record.launch.spec.repo is not None:
            print("  source: " + line(record.launch.source, record.launch.spec.revision or "HEAD"))
        if record.launch.smoke.outcome != "not_smoked":
            print(f"  smoke: {record.launch.smoke}")


def resolve_runs(
    scheduler: Scheduler,
    names: Sequence[str],
    *,
    experiment: str | None = None,
    job: str | None = None,
) -> list[str]:
    """Resolve addresses or select runs by their experiment and job labels."""
    if names:
        return [run_address(name, experiment) for name in names]
    return [
        r.id
        for r in scheduler.runs(
            done=experiment is not None or job is not None, experiment=experiment, job=job
        )
    ]


def _here() -> str:
    """What to call the machine that holds the database, as its owner would read it."""
    return "the server" if (os.environ.get("ETUDE_SERVER_LOCAL") or selected()) else "this computer"


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
        slots = f"; {each.slots} runs at once on each" if each.slots > 1 else ""
        print(each.name, (owned or "no machines") + slots + marks, sep="  ")


def show_ondemands(scheduler: Scheduler) -> None:
    """One line per on-demand: what it rents, its budget and the machines it holds now."""
    for each in scheduler.ondemands():
        template = each.template
        kind = template.gpu or "CPU"
        if template.vcpus or template.memory_gb:
            kind += f" {template.vcpus or 'any'} vCPUs, {template.memory_gb or 'any'} GB"
        source = template.provider + (f" in {template.data_center}" if template.data_center else "")
        if template.spot:
            source += ", spot"
        ceiling = (
            f" under ${each.max_usd_per_hour}/hour" if each.max_usd_per_hour is not None else ""
        )
        parts = [f"{kind} on {source}", f"up to {each.max_machines} at once{ceiling}"]
        if each.slots > 1:
            parts.append(f"{each.slots} runs at once on each")
        rented = [m.name for m in scheduler.machines(each.name) if m.held]
        if rented:
            parts.append("rented: " + ", ".join(rented))
        parts.append("safeguards: " + ", ".join(scheduler.safeguards(each.name)))
        if each.storage:
            parts.append(f"records in {each.storage}")
        print(each.name, "; ".join(parts) + _marks(scheduler, each.name), sep="  ")


def show_machines(scheduler: Scheduler, *, ended: bool) -> None:
    """One line per machine: owned with its cost and expiry, or rented with its window and cost."""
    windows = {cost.rental.machine: cost for cost in scheduler.rentals()}
    now = scheduler.now()
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
                f"{machine.state(now, off=each.off)}, owned{where}, ${machine.cost_usd:.2f} committed, {expiry}",
                sep="  ",
            )
    for ondemand in scheduler.ondemands():
        for machine in scheduler.machines(ondemand.name):
            if not machine.held and not ended:
                continue
            if machine.refused_at is not None:
                print(
                    machine.name,
                    ondemand.name,
                    f"unavailable, nothing rented {machine.refused_at:%Y-%m-%d %H:%M} UTC",
                    sep="  ",
                )
                continue
            if machine.name not in windows:
                continue
            spent = windows[machine.name]
            rental = spent.rental
            cost = f"{'~' if spent.estimated else ''}${spent.usd:.2f}"
            if rental.billed_through is not None:
                cost = f"${rental.billed_usd:.4f} billed" + (
                    f" + ~${spent.unbilled_usd:.4f}" if spent.estimated else ""
                )
            elif rental.unit != "usd" and spent.units is not None:
                dollars = f"{'~' if spent.estimated and spent.usd > 0 else ''}${spent.usd:.2f}"
                cost = f"~{spent.units:.2f} {rental.unit.upper()} ({dollars})"
            if machine.deleted_at is not None:
                window = f"rented, ended {machine.deleted_at:%Y-%m-%d %H:%M} UTC"
            else:
                window = f"rented until {rental.deadline:%Y-%m-%d %H:%M} UTC"
            if ondemand.template.spot:
                window += ", spot"
            hardware = f"{machine.hardware}, " if machine.hardware else ""
            print(
                machine.name,
                ondemand.name,
                f"{machine.state(now)}, {hardware}{window}, {cost}",
                sep="  ",
            )
            if spent.over_cap:
                print(f"warning: {machine.name} bill exceeds its ${rental.cap_usd:.4f} cap")


def _rate(
    unit: str,
    usd_per_hour: Decimal | None,
    units_per_hour: Decimal | None,
    usd_per_unit: Decimal,
) -> str:
    if unit == "usd":
        assert usd_per_hour is not None
        return f"at {_per_hour(usd_per_hour)}"
    label = unit.upper()
    conv = f"${usd_per_unit.quantize(Decimal('0.0001')).normalize():f}/{label}"
    if units_per_hour is None:
        return f"in {label} at {conv} (rate observed on assign)"
    rate = f"{units_per_hour.quantize(Decimal('0.0001')).normalize():f} {label}/hour"
    hourly = usd_per_hour if usd_per_hour is not None else units_per_hour * usd_per_unit
    return f"at {rate} ({_per_hour(hourly)} at {conv})"


def show_quote(quote: Quote) -> None:
    """First-wave placements and planned rental costs under the quoted windows."""
    for each in quote.machines:
        rate = _rate(each.unit, each.usd_per_hour, each.units_per_hour, each.usd_per_unit)
        if each.machine is not None:
            where = f"machine {each.machine}" + (
                f" {rate}" if (each.usd_per_hour or each.unit != "usd") else ""
            )
        else:
            where = f"rent {each.hardware or 'a machine'} {rate}"
        if each.spot:
            where += ", spot"
        if each.slots > 1:
            where += f", {each.slots} slots"
            if each.alongside:
                where += " with " + ", ".join(each.alongside)
        planned = (
            "planned rental cost unknown until assign"
            if each.max_usd is None
            else f"planned rental cost ${each.max_usd:.2f}"
        )
        print(each.run, where, planned, sep="  ")
    total = "unknown until assign" if quote.max_usd is None else f"${quote.max_usd:.2f}"
    print(f"planned first-wave rental cost: {total}; nothing rented")
    if quote.limit_usd is not None:
        print(f"job rental budget: ${quote.limit_usd:.2f}")
    print(
        "Rental costs assume deletion on time. Provider delays can exceed the budget; "
        "external storage charges are excluded."
    )


def _per_hour(usd: Decimal) -> str:
    """A provider's hourly price to four decimal places: Vast's float sums carry binary noise."""
    return f"${usd.quantize(Decimal('0.0001')).normalize():f}/hour"


def show_availability(answer: Availability) -> None:
    offer = answer.offer
    if answer.max_usd_per_hour is None:
        ceiling = "no ceiling"
    elif offer.usd_per_hour is None:
        ceiling = f"ceiling ${answer.max_usd_per_hour} checked on assign"
    else:
        ceiling = f"ceiling ${answer.max_usd_per_hour}"
    market = ", spot" if answer.spot else ""
    if offer.available:
        rate = _rate(offer.unit, offer.usd_per_hour, offer.units_per_hour, offer.usd_per_unit)
        extras: list[str] = []
        if offer.balance is not None:
            extras.append(f"{offer.balance:.2f} {offer.unit.upper()} balance")
        if offer.active is not None:
            extras.append(f"{offer.active} active")
        meta = f" [{', '.join(extras)}]" if extras else ""
        description = f"{offer.hardware or 'a machine'} {rate}{market}{meta} ({ceiling})  available"
    else:
        description = f"none available: {offer.reason}" if offer.reason else "none available"
    print(
        answer.ondemand,
        description,
        f"{answer.in_use} of {answer.max_machines} in use",
        "rentable now" if answer.rentable else "not rentable now",
        f"asked {answer.observed_at:%Y-%m-%d %H:%M:%S} UTC",
        sep="  ",
    )
