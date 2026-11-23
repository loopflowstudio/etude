"""Google Colab assignment, usage, and kernel wire models.

Validates Colab tunnel responses and maps Etude machine templates onto Colab's
variant, accelerator, and machine shape parameters.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.machines import Refused
from etude.launch.provisioner.providers.colab.http import (
    XSSI_PREFIX,
    ColabCredentials,
    ColabHTTP,
    KernelChannel,
    execute_in_kernel,
)
from etude.launch.provisioner.providers.http import send_to_provider
from etude.launch.records import CPU, GPU, Hardware, MachineTemplate
from etude.launch.storage.transport import Send
from etude.spec import Record

ExecuteKernel = Callable[[str, str, str, str], None]


class ChannelHandle(Protocol):
    def update(self, base_url: str, proxy_token: str) -> None: ...
    def close(self) -> None: ...


WatchKernel = Callable[[str, str, str], ChannelHandle]

AcceleratorName = Literal["NONE", "T4", "L4", "G4", "A100", "H100"]
VariantName = Literal["DEFAULT", "GPU"]

_GPU_NAMES: tuple[AcceleratorName, ...] = ("T4", "L4", "G4", "A100", "H100")
# Accelerators with only one machine shape; Colab ignores `shape=hm` for them.
_SINGLE_SHAPE_ACCELERATORS = frozenset({"L4", "G4"})
# Standard CPU/T4 memory threshold above which Colab's High-RAM shape is requested.
_STANDARD_RAM_GB = 16


class ColabSpec(Record):
    """Resolved Colab assignment parameters for one MachineTemplate."""

    accelerator: AcceleratorName
    high_mem: bool

    @property
    def variant(self) -> VariantName:
        return "DEFAULT" if self.accelerator == "NONE" else "GPU"

    @property
    def hardware(self) -> Hardware:
        if self.accelerator == "NONE":
            return CPU(flavor="colab-high-ram" if self.high_mem else "colab-standard")
        return GPU(id=self.accelerator, flavor="High-RAM" if self.high_mem else None)

    @property
    def needs_compute_units(self) -> bool:
        return self.high_mem or self.accelerator not in {"NONE", "T4"}


def resolve_template(template: MachineTemplate) -> ColabSpec | str:
    """Map a MachineTemplate to Colab assignment parameters, or return a refusal reason."""
    if template.spot:
        return "Etude rents no spot machines from Colab"
    if template.gpu_count > 1:
        return "Colab allocates one GPU per runtime"
    if template.image is not None:
        return "Colab supplies its own runtime image; omit --image"
    if template.data_center is not None:
        return "Colab chooses the runtime region; omit --data-center"
    if template.vcpus is not None or template.disk_gb is not None:
        return "Colab does not select vCPU or disk sizes; omit --vcpus and --disk-gb"

    high_mem = template.memory_gb is not None and template.memory_gb > _STANDARD_RAM_GB
    if template.gpu is None:
        return ColabSpec(accelerator="NONE", high_mem=high_mem)
    name = template.gpu.strip().upper()
    if name not in _GPU_NAMES:
        return f"Colab has no GPU {template.gpu!r}; supported: {', '.join(_GPU_NAMES)}"
    return ColabSpec(accelerator=name, high_mem=high_mem and name not in _SINGLE_SHAPE_ACCELERATORS)


def validate_template(template: MachineTemplate) -> str | None:
    """Offline static template check invoked when adding a Colab on-demand."""
    result = resolve_template(template)
    return result if isinstance(result, str) else None


def notebook_hash(value: UUID) -> str:
    """Encode a UUID the way Colab's `/tun/m/assign` `nbh` parameter expects."""
    return str(value).replace("-", "_").ljust(44, ".")


class CcuInfo(Record):
    current_balance: Decimal = Field(alias="currentBalance")
    consumption_rate_hourly: Decimal = Field(alias="consumptionRateHourly")
    assignments_count: int = Field(alias="assignmentsCount")


class RuntimeProxyInfo(Record):
    token: str
    url: str


class Assigned(Record):
    endpoint: str
    runtime_proxy_info: RuntimeProxyInfo = Field(alias="runtimeProxyInfo")


class _ListedAssignments(Record):
    assignments: tuple[Assigned, ...] = ()


class ColabToken(Record):
    token: str


class Kernel(Record):
    id: str
    execution_state: str | None = None


def _read[T: Record](model: type[T], raw: bytes | None) -> T:
    try:
        if raw is None:
            raise ValueError()
        return model.model_validate_json(raw)
    except ValueError:
        raise ProviderUnavailable("Colab reply is missing valid required fields") from None


def _assign_params(nbh: UUID, spec: ColabSpec) -> dict[str, str]:
    params = {
        "nbh": notebook_hash(nbh),
        "variant": spec.variant,
        "accelerator": spec.accelerator,
    }
    if spec.high_mem:
        params["shape"] = "hm"
    return params


class ColabAPI(ColabHTTP):
    def __init__(
        self,
        credentials: ColabCredentials,
        send: Send = send_to_provider,
        *,
        execute: ExecuteKernel = execute_in_kernel,
        watch: WatchKernel | None = None,
    ) -> None:
        super().__init__(credentials, send)
        self._execute = execute
        self._watch: WatchKernel | None = (
            watch
            if watch is not None
            else (KernelChannel if execute is execute_in_kernel else None)
        )

    def ccu_info(self) -> CcuInfo:
        return _read(CcuInfo, self.ask("GET", "/tun/m/ccu-info"))

    def assignments(self) -> tuple[Assigned, ...]:
        return _read(_ListedAssignments, self.ask("GET", "/tun/m/assignments")).assignments

    def lookup(self, nbh: UUID, spec: ColabSpec) -> Assigned | ColabToken:
        raw = self.ask("GET", "/tun/m/assign", params=_assign_params(nbh, spec))
        try:
            data = json.loads(raw or b"")
            if not isinstance(data, dict):
                raise ValueError
            if "endpoint" in data:
                return Assigned.model_validate(data)
            return ColabToken.model_validate(data)
        except ValueError:
            raise ProviderUnavailable("Colab assign lookup returned an invalid reply") from None

    def assign(self, nbh: UUID, spec: ColabSpec) -> Assigned:
        current = self.lookup(nbh, spec)
        if isinstance(current, Assigned):
            return current
        reply = self.ask_raw(
            "POST",
            "/tun/m/assign",
            params=_assign_params(nbh, spec),
            headers={"X-Goog-Colab-Token": current.token},
        )
        if reply.status == 412:
            raise Refused("Colab refused allocation: too many active sessions or capacity limit")
        if reply.status == 400 and spec.accelerator != "NONE":
            raise Refused(
                f"Colab rejected accelerator {spec.accelerator!r}; check account entitlement"
            )
        if reply.status == 503:
            # Confirm via deterministic nbh lookup that no runtime was assigned behind the 503.
            checked = self.lookup(nbh, spec)
            if isinstance(checked, Assigned):
                return checked
            raise Refused(f"Colab has no {spec.accelerator} capacity right now (HTTP 503)")
        if not 200 <= reply.status < 300:
            raise ProviderUnavailable(f"Colab POST /tun/m/assign: status {reply.status}")
        return _read(Assigned, reply.body.removeprefix(XSSI_PREFIX))

    def unassign(self, endpoint: str) -> None:
        path = f"/tun/m/unassign/{urllib.parse.quote(endpoint, safe='')}"
        raw = self.ask("GET", path, absent_ok=True)
        if raw is None:
            return
        token = _read(ColabToken, raw).token
        self.ask("POST", path, headers={"X-Goog-Colab-Token": token}, absent_ok=True)

    def kernels(self, proxy: RuntimeProxyInfo) -> tuple[Kernel, ...]:
        raw = self.list_kernels(proxy.url, proxy.token)
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError
            return tuple(Kernel.model_validate(item) for item in items)
        except ValueError:
            raise ProviderUnavailable(
                "Colab runtime GET /api/kernels returned invalid JSON"
            ) from None

    def watch_kernel(self, proxy: RuntimeProxyInfo, kernel_id: str) -> ChannelHandle | None:
        if self._watch is None:
            return None
        return self._watch(proxy.url, proxy.token, kernel_id)

    def execute(self, proxy: RuntimeProxyInfo, code: str) -> str:
        raw = self.start_kernel(proxy.url, proxy.token)
        kernel_id = _read(Kernel, raw).id
        self._execute(proxy.url, proxy.token, kernel_id, code)
        return kernel_id
