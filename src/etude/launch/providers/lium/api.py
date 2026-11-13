"""Lium nodes, allocation quotes, pods and ledger statements.

Validate provider facts here; never retain arbitrary replies, SSH options or
credentials. Quotes use the rent path's allocation, not whole-node resources.
"""

from __future__ import annotations

import ipaddress
import shlex
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from etude.launch.errors import ProviderUnavailable
from etude.launch.providers.lium.http import LiumHTTP
from etude.launch.records import GPU, MachineTemplate, Record


class Node(Record):
    id: UUID
    gpu_model: str
    gpu_count: int = Field(gt=0)
    available_gpu_count: int = Field(ge=0)
    available_now: bool
    min_rentable_gpu_count: int = Field(gt=0)
    price_per_gpu_hour: Decimal = Field(gt=0, allow_inf_nan=False)
    tier: Literal["secure", "spot"]
    country_code: str


class _Nodes(Record):
    count: int = Field(ge=0)
    generated_at: AwareDatetime
    nodes: tuple[Node, ...]


class Resources(Record):
    cpu_count: int = Field(gt=0)
    ram_gb: Decimal = Field(gt=0, allow_inf_nan=False)
    local_volume_gb: int = Field(gt=0)
    storage_gb: int = Field(gt=0)


class Line(Record):
    item: Literal["compute", "volume", "backup", "egress"]
    quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    amount_usd: Decimal | None = Field(ge=0, allow_inf_nan=False)


class Quote(Record):
    executor_id: UUID
    gpu_count: int = Field(gt=0)
    hours: Decimal = Field(gt=0, allow_inf_nan=False)
    price_per_hour: Decimal = Field(gt=0, allow_inf_nan=False)
    total_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    allocation: Resources
    lines: tuple[Line, ...]


class LiumOffer(Record):
    node: Node
    quote: Quote

    @property
    def price(self) -> Decimal:
        # Direct-node rent accepts a one-cent increase per GPU. Admit that
        # tolerance in the quote rather than quietly exceeding the ceiling.
        return self.quote.total_usd + Decimal("0.01") * self.quote.gpu_count

    @property
    def hardware(self) -> GPU:
        return GPU(
            id=self.node.gpu_model,
            count=self.quote.gpu_count,
            min_vcpus=self.quote.allocation.cpu_count,
            min_memory_gb=int(self.quote.allocation.ram_gb),
            disk_gb=min(self.quote.allocation.local_volume_gb, self.quote.allocation.storage_gb),
        )


class Create(Record):
    pod_name: str
    gpu_count: int
    user_public_key: str
    dockerfile_content: str
    termination_hours: int = Field(ge=1, le=720)
    expected_price_per_gpu_hour: Decimal
    enable_backups: Literal[False] = False
    enable_jupyter: Literal[False] = False
    initial_port_count: Literal[1] = 1


class _Created(Record):
    pod_id: UUID


class Pod(Record):
    id: UUID
    executor_id: UUID
    pod_name: str
    status: str
    created_at: AwareDatetime
    price: Decimal = Field(ge=0, allow_inf_nan=False)
    gpu_count: int = Field(gt=0)
    ssh_connect_cmd: str
    removal_scheduled_at: AwareDatetime | None

    @model_validator(mode="after")
    def _validate_address(self) -> Pod:
        self.address  # validate before a provider-supplied command can be retained
        return self

    @property
    def address(self) -> str | None:
        if not self.ssh_connect_cmd:
            return None
        # Provider strings are data, never shell commands. Accept only the
        # documented root/IP/port shape, with either ordering of -p.
        try:
            parts = shlex.split(self.ssh_connect_cmd)
            if len(parts) != 4 or parts.pop(0) != "ssh":
                raise ValueError()
            index = parts.index("-p")
            port = int(parts[index + 1])
            del parts[index : index + 2]
            host = parts[0].removeprefix("root@")
            if parts[0] != f"root@{host}" or not 0 < port < 65536:
                raise ValueError()
            ip = ipaddress.ip_address(host)
        except (ValueError, IndexError):
            raise ProviderUnavailable("Lium returned an unsupported SSH address") from None
        return f"ssh://root@{'[' + str(ip) + ']' if ip.version == 6 else ip}:{port}"


class _Removal(Record):
    removal_scheduled_at: datetime


class Charge(Record):
    amount: Decimal = Field(ge=0, allow_inf_nan=False)
    billed_until: AwareDatetime


class Statement(Record):
    pod_id: UUID
    created_at: AwareDatetime | None
    removed_at: AwareDatetime | None
    removed: bool
    billed_until: AwareDatetime | None
    total: Decimal = Field(ge=0, allow_inf_nan=False)
    days: tuple[Charge, ...]


def _read[T: Record](model: type[T], raw: bytes | None) -> T:
    try:
        if raw is None:
            raise ValueError()
        return model.model_validate_json(raw)
    except ValueError:
        raise ProviderUnavailable("Lium reply is missing valid required fields") from None


class LiumAPI(LiumHTTP):
    def offers(self, template: MachineTemplate) -> tuple[LiumOffer, ...]:
        if template.gpu is None:
            raise ValueError("Lium rentals need a GPU model")
        feed = _read(_Nodes, self.ask("GET", "public/v1/nodes"))
        if feed.count != len(feed.nodes):
            raise ProviderUnavailable("Lium inventory count does not match its nodes")
        offers: list[LiumOffer] = []
        for node in feed.nodes:
            if (
                node.gpu_model != template.gpu
                or node.tier != "secure"
                or not node.available_now
                or not node.min_rentable_gpu_count <= template.gpu_count <= node.available_gpu_count
                or (template.data_center and node.country_code != template.data_center)
            ):
                continue
            quote = _read(
                Quote,
                self.ask(
                    "GET",
                    f"executors/{node.id}/quote",
                    params={
                        "hours": "1",
                        "gpu_count": str(template.gpu_count),
                        "volume_gb": "0",
                        "backup_gb": "0",
                    },
                ),
            )
            if (
                quote.executor_id != node.id
                or quote.gpu_count != template.gpu_count
                or quote.hours != 1
                or quote.price_per_hour != node.price_per_gpu_hour * template.gpu_count
                or quote.total_usd != quote.price_per_hour
                or sum(line.amount_usd or Decimal(0) for line in quote.lines) != quote.total_usd
                or len(quote.lines) != 4
                or {line.item for line in quote.lines} != {"compute", "volume", "backup", "egress"}
                or any(line.quantity != 0 for line in quote.lines if line.item != "compute")
                or any(line.amount_usd is None for line in quote.lines if line.item != "egress")
            ):
                raise ProviderUnavailable(
                    "Lium quote does not establish a complete compute-only price"
                )
            allocation = quote.allocation
            if (
                allocation.cpu_count < (template.vcpus or 4)
                or allocation.ram_gb < (template.memory_gb or 16)
                or min(allocation.local_volume_gb, allocation.storage_gb) < (template.disk_gb or 40)
            ):
                continue
            offers.append(LiumOffer(node=node, quote=quote))
        return tuple(sorted(offers, key=lambda offer: (offer.price, str(offer.node.id))))

    def create(self, offer: LiumOffer, request: Create, session: str) -> UUID:
        return _read(
            _Created,
            self.ask(
                "POST",
                f"executors/{offer.node.id}/rent",
                body=request.model_dump_json(),
                idempotency_key=session,
            ),
        ).pod_id

    def pod(self, id: UUID) -> Pod | None:
        raw = self.ask("GET", f"pods/{id}", absent_ok=True)
        if raw is None:
            return None
        pod = _read(Pod, raw)
        if pod.id != id:
            raise ProviderUnavailable("Lium returned a different pod id")
        return pod

    def named(self, name: str) -> Pod | None:
        try:
            raw = self.ask("GET", "pods")
            if raw is None:
                raise ValueError()
            pods = TypeAdapter(tuple[Pod, ...]).validate_json(raw)
        except ValueError:
            raise ProviderUnavailable("Lium pod inventory is unreadable") from None
        found = [pod for pod in pods if pod.pod_name == name]
        if len(found) > 1:
            raise ProviderUnavailable("multiple Lium pods have the session name")
        return found[0] if found else None

    def schedule(self, id: UUID, deadline: datetime) -> Pod:
        self.ask(
            "POST",
            f"pods/{id}/schedule-removal",
            body=_Removal(removal_scheduled_at=deadline).model_dump_json(),
        )
        pod = self.pod(id)
        if pod is None or pod.removal_scheduled_at is None or pod.removal_scheduled_at > deadline:
            raise ProviderUnavailable("Lium did not acknowledge the rental deadline")
        return pod

    def delete(self, id: UUID) -> None:
        self.ask("DELETE", f"pods/{id}", absent_ok=True)

    def statement(self, id: UUID) -> Statement | None:
        raw = self.ask("GET", f"pods/{id}/statement", absent_ok=True)
        if raw is None:
            return None
        statement = _read(Statement, raw)
        if statement.pod_id != id:
            raise ProviderUnavailable("Lium returned a different statement id")
        return statement
