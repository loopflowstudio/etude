"""Vast offers, instances and charges, narrowed once at the HTTP boundary.

Creation is sent once. Inventory pagination is exhaustive, including when a
create reply is lost. Provider bodies and environment variables are never kept.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from etude.launch.errors import ProviderUnavailable
from etude.launch.providers.vast.http import InstanceHTTP
from etude.launch.records import GPU, MachineTemplate
from etude.spec import Record


class VastOffer(Record):
    id: int = Field(gt=0)
    gpu_name: str
    num_gpus: int
    cpu_arch: str
    cpu_cores_effective: Decimal = Field(ge=0, allow_inf_nan=False)
    cpu_ram: Decimal = Field(ge=0, allow_inf_nan=False)  # allocated MB; CLI uses 1000 MB/GB
    disk_space: Decimal = Field(ge=0, allow_inf_nan=False)
    dph_total: Decimal = Field(gt=0, allow_inf_nan=False)  # includes allocated_storage
    storage_total_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    inet_up_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    inet_down_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    rentable: bool
    rented: bool
    verification: str
    geolocation: str
    end_date: float | None = Field(default=None, allow_inf_nan=False)

    @property
    def hardware(self) -> GPU:
        return GPU(
            id=self.gpu_name,
            count=self.num_gpus,
            min_vcpus=int(self.cpu_cores_effective),
            min_memory_gb=int(self.cpu_ram / 1000),
        )


class Instance(Record):
    id: int = Field(gt=0)
    label: str | None
    actual_status: str | None
    start_date: float = Field(gt=0, allow_inf_nan=False)
    dph_total: Decimal = Field(ge=0, allow_inf_nan=False)
    gpu_name: str
    num_gpus: int = 1  # previously published receipts represented exactly one GPU
    cpu_cores_effective: Decimal = Field(ge=0, allow_inf_nan=False)
    cpu_ram: Decimal = Field(ge=0, allow_inf_nan=False)
    ssh_host: str | None = None
    ssh_port: int | None = Field(default=None, gt=0, le=65535)

    @property
    def hardware(self) -> GPU:
        return GPU(
            id=self.gpu_name,
            count=self.num_gpus,
            min_vcpus=int(self.cpu_cores_effective),
            min_memory_gb=int(self.cpu_ram / 1000),
        )

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.start_date, UTC)


class _Offers(Record):
    offers: tuple[VastOffer, ...]


class _Page(Record):
    next_token: str | None  # absence of this field is not an exhausted result


class _Instances(_Page):
    instances: tuple[Instance, ...]


class _Instance(Record):
    instances: Instance | None


class _Created(Record):
    success: Literal[True]
    new_contract: int = Field(gt=0)


class Create(Record):
    client_id: Literal["me"] = "me"
    image: str
    disk: int
    label: str
    env: dict[str, str]
    onstart: str
    runtype: Literal["ssh_direct"] = "ssh_direct"
    target_state: Literal["running"] = "running"
    cancel_unavail: Literal[True] = True


class _Charge(Record):
    start: float = Field(allow_inf_nan=False)
    end: float = Field(allow_inf_nan=False)
    type: str
    source: str
    amount: Decimal = Field(ge=0, allow_inf_nan=False)


class _Charges(_Page):
    results: tuple[_Charge, ...]


class Charges(Record):
    """Reported contract total over inclusive UTC dates, not settled coverage.

    Vast can revise a day's aggregate. Neither its last date nor the time we
    read it establishes that all charges through teardown have been posted.
    """

    usd: Decimal
    first_day: date
    last_day: date
    observed_at: datetime


def _charge_day(timestamp: float) -> date:
    try:
        value = datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, OverflowError, OSError):
        raise ProviderUnavailable("Vast charge date is outside the supported range") from None
    if value.time() != datetime.min.time():
        raise ProviderUnavailable("Vast charge date is not UTC midnight")
    return value.date()


def _read[T: Record](model: type[T], raw: bytes | None) -> T:
    try:
        if raw is None:
            raise ValueError("missing body")
        return model.model_validate_json(raw)
    except ValueError:
        # Validation errors may contain whole provider replies, including secrets.
        raise ProviderUnavailable("Vast reply is missing valid required fields") from None


class VastAPI(InstanceHTTP):
    def _pages[T: _Page](self, model: type[T], path: str, params: dict[str, str]) -> Iterator[T]:
        seen: set[str] = set()
        while True:
            page = _read(model, self.ask("GET", path, params=params))
            yield page
            if page.next_token is None:
                return
            if not page.next_token or page.next_token in seen:
                raise ProviderUnavailable("Vast pagination did not advance")
            seen.add(page.next_token)
            params = {**params, "after_token": page.next_token}

    def offers(self, template: MachineTemplate) -> tuple[VastOffer, ...]:
        if template.gpu is None:
            raise ValueError("Vast rentals need a GPU model")
        vcpus = template.vcpus or 4
        memory_mb = (template.memory_gb or 16) * 1000
        disk_gb = template.disk_gb or 40
        # Search includes disk in dph_total. Zero transfer rates keep arbitrary
        # training traffic within the quoted rental rate, without a guessed byte cap.
        query = {
            "type": "ondemand",
            "order": [["dph_total", "asc"]],
            "gpu_name": {"eq": template.gpu},
            "num_gpus": {"eq": template.gpu_count},
            "cpu_arch": {"eq": "amd64"},
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "cpu_cores_effective": {"gte": vcpus},
            "cpu_ram": {"gte": memory_mb},
            "disk_space": {"gte": disk_gb},
            "allocated_storage": disk_gb,
            "inet_up_cost": {"eq": 0},
            "inet_down_cost": {"eq": 0},
            "direct_port_count": {"gte": 1},
        }
        if template.data_center:
            query["geolocation"] = {"eq": template.data_center}
        page = _read(_Offers, self.ask("POST", "api/v0/bundles/", body=json.dumps(query)))
        # Validate the selection locally as well: a server ignoring a filter must
        # not sell the caller a smaller machine or charge unbounded bandwidth.
        offers = (
            offer
            for offer in page.offers
            if offer.gpu_name == template.gpu
            and offer.num_gpus == template.gpu_count
            and offer.cpu_arch == "amd64"
            and offer.verification == "verified"
            and offer.rentable
            and not offer.rented
            and offer.cpu_cores_effective >= vcpus
            and offer.cpu_ram >= memory_mb
            and offer.disk_space >= disk_gb
            and offer.dph_total >= offer.storage_total_cost
            and offer.inet_up_cost == 0
            and offer.inet_down_cost == 0
            and (
                not template.data_center
                or offer.geolocation.split(",")[-1].strip() == template.data_center
            )
        )
        return tuple(sorted(offers, key=lambda offer: (offer.dph_total, offer.id)))

    def create(self, offer: VastOffer, request: Create) -> int:
        reply = self.ask("PUT", f"api/v0/asks/{offer.id}/", body=request.model_dump_json())
        return _read(_Created, reply).new_contract

    def instance(self, id: int) -> Instance | None:
        if id <= 0:
            raise ValueError("not a Vast instance id")
        reply = self.ask("GET", f"api/v0/instances/{id}/", absent_ok=True)
        instance = None if reply is None else _read(_Instance, reply).instances
        if instance is not None and instance.id != id:
            raise ProviderUnavailable("Vast returned a different instance id")
        return instance

    def named(self, label: str) -> Instance | None:
        params = {"limit": "25", "select_filters": json.dumps({"label": {"eq": label}})}
        found = [
            instance
            for page in self._pages(_Instances, "api/v1/instances/", params)
            for instance in page.instances
            if instance.label == label
        ]
        if len(found) > 1:
            raise ProviderUnavailable("multiple Vast instances have the session label")
        return found[0] if found else None

    def charges(self, instance: Instance, now: datetime) -> Charges | None:
        """Read all reported charges since the contract's creation day.

        The provider's `day` filter and `start`/`end` fields name UTC dates,
        including the current, incomplete day. They are not billing watermarks.
        """
        if now.tzinfo is None or now < instance.created_at:
            raise ValueError("charge observation must be aware and after creation")
        first = instance.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
        last = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        params = {
            "select_filters": json.dumps(
                {
                    "day": {"gte": int(first.timestamp()), "lte": int(last.timestamp())},
                    "type": {"in": ["instance"]},
                }
            ),
            "format": "table",
            "limit": "500",
        }
        found = [
            row
            for page in self._pages(_Charges, "api/v0/charges", params)
            for row in page.results
            if row.type == "instance" and row.source == f"instance-{instance.id}"
        ]
        if not found:
            return None
        if len(found) != 1:
            raise ProviderUnavailable("duplicate Vast contract charge totals")
        row = found[0]
        first_day, last_day = _charge_day(row.start), _charge_day(row.end)
        if not first.date() <= first_day <= last_day <= last.date():
            raise ProviderUnavailable("Vast charge dates are outside the requested range")
        return Charges(usd=row.amount, first_day=first_day, last_day=last_day, observed_at=now)
