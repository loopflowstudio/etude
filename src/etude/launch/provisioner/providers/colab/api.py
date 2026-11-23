"""Google Colab assignment, usage, and kernel wire models.

Validates Colab tunnel responses and maps Etude machine templates onto Colab's
variant, accelerator, and machine shape parameters.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.machines import Refused
from etude.launch.provisioner.providers.colab.http import (
    COLAB_DOMAIN,
    ColabCredentials,
    ColabHTTP,
    execute_in_kernel,
)
from etude.launch.provisioner.providers.http import send_to_provider
from etude.launch.records import CPU, GPU, Hardware, MachineTemplate
from etude.launch.storage.transport import Send
from etude.spec import Record

ExecuteKernel = Callable[[str, str, str, str], None]

AcceleratorName = Literal["NONE", "T4", "L4", "G4", "A100", "H100"]
VariantName = Literal["DEFAULT", "GPU"]

_GPU_NAMES: tuple[AcceleratorName, ...] = ("T4", "L4", "G4", "A100", "H100")
_PAID_ACCELERATORS = frozenset({"L4", "G4", "A100", "H100"})
# Accelerators with only one machine shape; Colab ignores `shape=hm` for them.
_SINGLE_SHAPE_ACCELERATORS = frozenset({"L4", "G4"})
# Standard CPU/T4 memory threshold above which Colab's High-RAM shape is requested.
_STANDARD_RAM_GB = 16


class ColabSpec(Record):
    """Resolved Colab assignment parameters for one MachineTemplate."""

    variant: VariantName
    accelerator: AcceleratorName
    high_mem: bool
    hardware: Hardware

    @property
    def needs_compute_units(self) -> bool:
        return self.high_mem or self.accelerator in _PAID_ACCELERATORS

    @property
    def shape_param(self) -> str | None:
        if self.high_mem and self.accelerator not in _SINGLE_SHAPE_ACCELERATORS:
            return "hm"
        return None


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

    if template.gpu is None:
        high_mem = template.memory_gb is not None and template.memory_gb > _STANDARD_RAM_GB
        return ColabSpec(
            variant="DEFAULT",
            accelerator="NONE",
            high_mem=high_mem,
            hardware=CPU(flavor="colab-high-ram" if high_mem else "colab-standard"),
        )

    name = template.gpu.strip().upper()
    if name not in _GPU_NAMES:
        supported = ", ".join(_GPU_NAMES)
        return f"Colab has no GPU {template.gpu!r}; supported: {supported}"
    high_mem = (
        template.memory_gb is not None
        and template.memory_gb > _STANDARD_RAM_GB
        and name not in _SINGLE_SHAPE_ACCELERATORS
    )
    accelerator: AcceleratorName = name
    return ColabSpec(
        variant="GPU",
        accelerator=accelerator,
        high_mem=high_mem,
        hardware=GPU(id=name, flavor="High-RAM" if high_mem else None, count=1),
    )


def validate_template(template: MachineTemplate) -> str | None:
    """Offline static template check invoked when adding a Colab on-demand."""
    result = resolve_template(template)
    return result if isinstance(result, str) else None


def notebook_hash(value: UUID) -> str:
    """Encode a UUID the way Colab's `/tun/m/assign` `nbh` parameter expects."""
    transformed = str(value).replace("-", "_")
    return transformed + "." * (44 - len(transformed))


class _Wire(Record):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")


class CcuInfo(_Wire):
    current_balance: float = Field(alias="currentBalance")
    consumption_rate_hourly: float = Field(alias="consumptionRateHourly")
    assignments_count: int = Field(alias="assignmentsCount")


class RuntimeProxyInfo(_Wire):
    token: str
    token_expires_in_seconds: int = Field(alias="tokenExpiresInSeconds")
    url: str


class ListedAssignment(_Wire):
    accelerator: str
    endpoint: str
    variant: int
    machine_shape: int = Field(default=0, alias="machineShape")
    runtime_proxy_info: RuntimeProxyInfo = Field(alias="runtimeProxyInfo")


class _ListedAssignments(_Wire):
    assignments: tuple[ListedAssignment, ...] = ()


class Assigned(_Wire):
    endpoint: str
    runtime_proxy_info: RuntimeProxyInfo = Field(alias="runtimeProxyInfo")


class PendingAssign(_Wire):
    token: str


class _UnassignToken(_Wire):
    token: str


class Kernel(_Wire):
    id: str
    execution_state: str = "starting"


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
    if spec.shape_param is not None:
        params["shape"] = spec.shape_param
    return params


class ColabAPI(ColabHTTP):
    def __init__(
        self,
        credentials: ColabCredentials,
        send: Send = send_to_provider,
        *,
        execute: ExecuteKernel = execute_in_kernel,
        domain: str = COLAB_DOMAIN,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(credentials, send, domain=domain, now=now)
        self._execute = execute

    def ccu_info(self) -> CcuInfo:
        return _read(CcuInfo, self.ask("GET", "/tun/m/ccu-info"))

    def assignments(self) -> tuple[ListedAssignment, ...]:
        return _read(_ListedAssignments, self.ask("GET", "/tun/m/assignments")).assignments

    def lookup(self, nbh: UUID, spec: ColabSpec) -> Assigned | PendingAssign:
        raw = self.ask("GET", "/tun/m/assign", params=_assign_params(nbh, spec))
        if raw is None:
            raise ProviderUnavailable("Colab assign lookup returned an empty body")
        try:
            data = json.loads(raw)
        except ValueError:
            raise ProviderUnavailable("Colab assign lookup returned invalid JSON") from None
        if not isinstance(data, dict):
            raise ProviderUnavailable("Colab assign lookup returned a non-object reply")
        if "endpoint" in data and "runtimeProxyInfo" in data:
            return _read(Assigned, raw)
        return _read(PendingAssign, raw)

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
        if not 200 <= reply.status < 300:
            raise ProviderUnavailable(f"Colab POST /tun/m/assign: status {reply.status}")
        body = reply.body
        if body.startswith(b")]}'\n"):
            body = body[5:]
        return _read(Assigned, body)

    def unassign(self, endpoint: str) -> None:
        path = f"/tun/m/unassign/{urllib.parse.quote(endpoint, safe='')}"
        raw = self.ask("GET", path, absent_ok=True)
        if raw is None:
            return
        token = _read(_UnassignToken, raw).token
        self.ask("POST", path, headers={"X-Goog-Colab-Token": token}, absent_ok=True)

    def start_kernel(self, proxy: RuntimeProxyInfo) -> Kernel:
        raw = self.runtime_request(
            "POST",
            proxy.url,
            "/api/kernels",
            proxy.token,
            body=json.dumps({"name": "python3"}).encode(),
        )
        return _read(Kernel, raw)

    def execute(self, proxy: RuntimeProxyInfo, kernel_id: str, code: str) -> None:
        self._execute(proxy.url, proxy.token, kernel_id, code)
