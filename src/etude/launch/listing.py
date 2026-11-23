"""Print runs the way every command and the server lists them."""

import os
from collections.abc import Sequence
from decimal import Decimal

from etude.identity import run_address
from etude.launch.records import Availability, Machine, MachineStatus, Quote, Status
from etude.launch.scheduler.loop import Scheduler
from etude.launch.server import PythonClient
from etude.launch.source import line
from etude.records import RunRecord


def launcher_facts(
    status: Status, max_attempts: int, *, on_machine: str | None = None
) -> list[str]:
    """Launcher details shared by a CLI status and an indexed run line."""
    facts = status.facts()
    if status.machine and status.machine != on_machine:
        facts.append(f"machine {status.machine}")
        if status.hardware:
            facts[-1] += f" ({status.hardware})"
    elif status.hardware:
        facts.append(str(status.hardware))
    if status.attempts:
        facts.append(f"{status.attempts} of {max_attempts} attempts used")
    return facts


def describe_status(status: Status, record: RunRecord) -> str:
    return ", ".join(
        [status.phase_and_clock(), *launcher_facts(status, record.launch.spec.max_attempts)]
    )


def show_runs(scheduler: Scheduler | PythonClient, ids: Sequence[str]) -> None:
    for run in ids:
        status = scheduler.read(run)
        record = scheduler.record(run)
        print(run, record.name, describe_status(status, record), sep="  ")
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
    return "the server" if os.environ.get("ETUDE_SERVER_LOCAL") else "this computer"


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


def describe_machine(status: MachineStatus, *, available: str = "available") -> str:
    """Acquisition state, rental or owned window, and cost for CLI and web listings."""
    machine = status.machine
    state = status.state
    readiness = available if state == "available" else state
    if machine.kind != "rented":
        where = f" ({_here()})" if machine.kind == "local" else ""
        expiry = "never expires"
        if machine.expires_at is not None:
            verb = "expired" if machine.expired(status.observed_at) else "expires"
            expiry = f"{verb} {machine.expires_at:%Y-%m-%d %H:%M} UTC"
        return f"{readiness}, owned{where}, ${machine.cost_usd:.2f} committed, {expiry}"
    if machine.refused_at is not None:
        return f"unavailable, nothing rented {machine.refused_at:%Y-%m-%d %H:%M} UTC"
    spent = status.cost
    assert spent is not None  # A listed rental has either a refusal or a paid window.
    rental = spent.rental
    cost = f"{'~' if spent.estimated else ''}${spent.usd:.2f}"
    if rental.billed_through is not None:
        cost = f"${rental.billed_usd:.4f} billed" + (
            f" + ~${spent.unbilled_usd:.4f}" if spent.estimated else ""
        )
    window = (
        f"rented, ended {machine.deleted_at:%Y-%m-%d %H:%M} UTC"
        if machine.deleted_at
        else f"rented until {rental.deadline:%Y-%m-%d %H:%M} UTC"
    )
    if status.spot:
        window += ", spot"
    hardware = f"{machine.hardware}, " if machine.hardware else ""
    return f"{readiness}, {hardware}{window}, {cost}"


def machine_warning(status: MachineStatus) -> str | None:
    spent = status.cost
    if spent and spent.over_cap:
        return f"warning: {status.machine.name} bill exceeds its ${spent.rental.cap_usd:.4f} cap"
    return None


def show_machines(scheduler: Scheduler, *, ended: bool) -> None:
    for status in scheduler.machine_statuses(ended=ended):
        print(status.machine.name, status.machine.supply, describe_machine(status), sep="  ")
        if warning := machine_warning(status):
            print(warning)


def show_quote(quote: Quote) -> None:
    """First-wave placements and planned rental costs under the quoted windows."""
    for each in quote.machines:
        if each.machine is not None:
            where = f"machine {each.machine}" + (
                f" at {_per_hour(each.usd_per_hour)}" if each.usd_per_hour else ""
            )
        else:
            where = f"rent {each.hardware or 'a machine'} at {_per_hour(each.usd_per_hour)}"
        if each.spot:
            where += ", spot"
        if each.slots > 1:
            where += f", {each.slots} slots"
            if each.alongside:
                where += " with " + ", ".join(each.alongside)
        print(each.run, where, f"planned rental cost ${each.max_usd:.2f}", sep="  ")
    print(f"planned first-wave rental cost: ${quote.max_usd:.2f}; nothing rented")
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
    ceiling = (
        "no ceiling" if answer.max_usd_per_hour is None else f"ceiling ${answer.max_usd_per_hour}"
    )
    market = ", spot" if answer.spot else ""
    if offer.available:
        description = f"{offer.hardware or 'a machine'} at {_per_hour(offer.usd_per_hour)}{market} ({ceiling})  available"
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
