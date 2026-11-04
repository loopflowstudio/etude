"""Layer 1 RunPod machines: pricing, stock-image boot, bills and deletion.

A template naming a GPU rents a GPU pod; one naming none rents the cheapest
CPU pod with its vCPUs and memory. Both share create, receipts, bills and deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etude.deadline import limit_time
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Refused, check_creation
from etude.launch.machines import Allocation, Bill, MachineRequest, Safeguards
from etude.launch.providers.program import Program, boot_source
from etude.launch.providers.runpod.api import PodFact, Quote, RunPodAPI
from etude.launch.records import GPU, Hardware, LauncherError, MachineTemplate, Offer, Provider
from etude.launch.records import STOCK_IMAGE as DEFAULT_IMAGE
from etude.launch.storage.s3 import Bucket


class RunPodMachines:
    safeguards = Safeguards(start_command=True)
    owned = False

    def __init__(
        self,
        provider: Provider,
        template: MachineTemplate,
        api: RunPodAPI,
        bucket: Bucket,
        prefix: str,
    ) -> None:
        self._provider, self._template, self._api = provider, template, api
        self._bucket, self._prefix = bucket, prefix
        self._program = Program(bucket, prefix)

    def _quote(self) -> Quote:
        template = self._template
        return self._api.quote(
            gpu=template.gpu,
            vcpus=template.vcpus or 4,
            memory_gb=template.memory_gb or 16,
            data_center=template.data_center,
        )

    def offer(self) -> Offer:
        quote = self._quote()
        return Offer(
            hardware=quote.hardware, usd_per_hour=quote.usd_per_hour, available=quote.available
        )

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}/sessions/{session_id}/receipt.json"

    def _keep(self, session_id: str, pod: PodFact) -> None:
        self._bucket.put(self._key(session_id), pod.model_dump_json().encode())

    def _receipt(self, session_id: str) -> PodFact | None:
        raw = self._bucket.get(self._key(session_id))
        return None if raw is None else PodFact.model_validate_json(raw)

    def create(self, request: MachineRequest) -> Allocation:
        assert request.deadline is not None
        template = self._template
        quote = self._quote()
        if not quote.available:
            raise Refused(
                f"no {template.gpu or 'CPU pod'} available in"
                f" {template.data_center or 'any data center'}"
            )
        if request.bootstrap is None:
            raise LauncherError("a rented machine needs a storage bootstrap link")
        image = template.image or DEFAULT_IMAGE
        env = self._program.prepare(request, image)
        creation_deadline = min(request.deadline, request.startup_deadline or request.deadline)
        if datetime.now(UTC) >= creation_deadline:
            raise Refused(
                "startup or rental deadline expired during preparation; no machine created"
            )
        check_creation()
        seconds = (creation_deadline - datetime.now(UTC)).total_seconds()
        with limit_time(seconds):
            pod = self._api.create(
                name=f"etude-{request.session}",
                hardware=quote.hardware,
                image=image.resolved,
                registry=self._provider.registry if image.resolved != STOCK_IMAGE else "",
                env=env,
                entrypoint=("python3.12", "-c", boot_source("etude.launch.providers.runpod.pod")),
                disk_gb=template.disk_gb or _disk_gb(quote.hardware),
                data_center=template.data_center,
            )
        self._keep(request.session, pod)
        return self._allocation(pod)

    @staticmethod
    def _allocation(pod: PodFact) -> Allocation:
        return Allocation(
            id=pod.id,
            address=pod.address,
            usd_per_hour=pod.usd_per_hour,
            created_at=pod.created_at,
            hardware=pod.hardware,
        )

    def find(self, session_id: str) -> Allocation | None:
        # The immutable receipt already contains identity and charged price.
        # Health belongs to alive(); worker startup does not wait for SSH.
        pod = self._receipt(session_id)
        if pod is not None:
            return self._allocation(pod)
        pod = self._api.named(f"etude-{session_id}")
        if pod is None:
            return None
        self._keep(session_id, pod)
        return self._allocation(pod)

    def alive(self, session_id: str) -> bool:
        pod = self._receipt(session_id)
        if pod is None:
            return True
        try:
            current = self._api.pod(pod.id)
            return current is not None and current.status not in {"EXITED", "TERMINATED", "ERROR"}
        except ProviderUnavailable:
            return True

    def end(self, session_id: str) -> datetime | None:
        pod = self._receipt(session_id) or self._api.named(f"etude-{session_id}")
        if pod is None:
            return None
        return datetime.now(UTC) if self._api.pod(pod.id) is None else self._api.delete(pod)

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        pod = self._receipt(session_id)
        if pod is None:
            return None
        rows = self._api.bills(pod, now)
        through = pod.created_at.replace(minute=0, second=0, microsecond=0)
        usd = Decimal(0)
        for row in rows:
            if row.hour != through:
                break  # never estimate across a missing hour as if it had been billed
            usd += row.usd
            through += timedelta(hours=1)
        return Bill(usd=usd, through=through) if through > pod.created_at else None


def _disk_gb(hardware: Hardware) -> int:
    # RunPod caps a CPU pod's container disk at 10 GB per vCPU on its smallest flavors.
    return 40 if isinstance(hardware, GPU) else min(40, 10 * hardware.vcpus)


# Published import retained; the domain record owns the default and its provenance.
STOCK_IMAGE = DEFAULT_IMAGE.resolved
