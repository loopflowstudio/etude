"""RunPod's REST API, reduced to the facts the launcher needs.

The account key stays with this client on the launching pool. Nothing a
provider returns is logged or stored whole: each reply is narrowed to a few
checked fields. A create is sent once and never retried here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from etude.launch.errors import LauncherError
from etude.launch.providers.runpod.http import _ID, PodHTTP
from etude.launch.providers.runpod.http import ProviderUnavailable as ProviderUnavailable
from etude.launch.records import Record


class GPU(Record):
    """One GPU of a type, on a host with at least this much CPU and memory."""

    id: str  # RunPod's GPU type id, such as "NVIDIA L4"
    min_vcpus: int
    min_memory_gb: int


class CPU(Record):
    """A CPU pod: RunPod sizes it by vCPU count, and memory follows from the flavor."""

    flavor: str  # RunPod's CPU flavor id, such as "cpu3c"
    vcpus: int  # a power of two within the flavor's range


class Quote(Record):
    hardware: GPU | CPU  # what was priced; create asks for exactly this
    usd_per_hour: Decimal
    available: bool  # False only when the catalog positively reports none


class PodFact(Record):
    id: str
    name: str  # the session's identity, set by us at create
    usd_per_hour: Decimal
    created_at: datetime
    status: str
    address: str | None = None  # retained for historical SSH receipts


class HourBill(Record):
    hour: datetime
    usd: Decimal


class RunPodAPI(PodHTTP):
    @staticmethod
    def _pod(raw: Any) -> PodFact:
        try:
            fact = PodFact(
                id=str(raw["id"]),
                name=str(raw["name"]),
                usd_per_hour=Decimal(str(raw["cost"])),
                created_at=datetime.fromisoformat(str(raw["createdAt"])),
                status=str(raw["status"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ProviderUnavailable("a pod record was missing its expected fields") from None
        if not _ID.fullmatch(fact.id) or fact.created_at.tzinfo is None:
            raise ProviderUnavailable("a pod record was missing its expected fields")
        return fact

    def quote(
        self, *, gpu: str | None, vcpus: int, memory_gb: int, data_center: str | None = None
    ) -> Quote:
        """The secure-cloud hourly price for one machine, and whether any can be had.

        With a GPU named, one of that type on a host with at least `vcpus` and
        `memory_gb`. Without, the cheapest CPU pod with at least both, preferring
        flavors in stock. With a data center named, only stock there counts.
        """
        if gpu is None:
            return self._cpu_quote(vcpus, memory_gb, data_center)
        catalog = self._ask(
            "GET",
            "catalog/gpus",
            params={"include": "AVAILABILITY", "product": "POD", "cloud": "SECURE"},
        )
        try:
            row = next(row for row in catalog["gpus"] if row["id"] == gpu)
            price = Decimal(str(row["price"]["secure"]))
            available = _available(row, data_center)
        except (KeyError, TypeError, StopIteration, InvalidOperation):
            raise ProviderUnavailable(f"no price for {gpu}") from None
        if price <= 0:
            raise ProviderUnavailable(f"no price for {gpu}")
        hardware = GPU(id=gpu, min_vcpus=vcpus, min_memory_gb=memory_gb)
        return Quote(hardware=hardware, usd_per_hour=price, available=available)

    def _cpu_quote(self, vcpus: int, memory_gb: int, data_center: str | None) -> Quote:
        # RunPod rents CPU pods in power-of-two vCPU counts, with memory a fixed
        # multiple of them per flavor, and prices them per vCPU.
        smallest = 2
        while smallest < vcpus:
            smallest *= 2
        catalog = self._ask(
            "GET",
            "catalog/cpus",
            params={"include": "AVAILABILITY", "product": "POD", "vcpuCount": str(smallest)},
        )
        quotes: list[Quote] = []
        try:
            for row in catalog["cpus"]:
                size = max(smallest, int(row["vcpu"]["min"]))
                while size * Decimal(str(row["ramGbPerVcpu"])) < memory_gb:
                    size *= 2
                price = Decimal(str(row["price"]["securePerVcpu"])) * size
                if size > int(row["vcpu"]["max"]) or price <= 0:
                    continue
                quotes.append(
                    Quote(
                        hardware=CPU(flavor=str(row["id"]), vcpus=size),
                        usd_per_hour=price,
                        available=_available(row, data_center),
                    )
                )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ProviderUnavailable("the CPU catalog was unreadable") from None
        if not quotes:
            raise LauncherError(f"no RunPod CPU pod has {vcpus} vCPUs and {memory_gb} GB")
        return min(quotes, key=lambda quote: (not quote.available, quote.usd_per_hour))

    def pod(self, id: str) -> PodFact | None:
        """One pod by the provider's id, or None when the provider says it does not exist."""
        if not _ID.fullmatch(id):
            raise ValueError("not a provider id")
        raw = self._ask("GET", f"pods/{id}", absent_ok=True)
        return None if raw is None else self._pod(raw)

    def named(self, name: str) -> PodFact | None:
        """The pod carrying one session's identity, from the whole inventory.

        None means every page was read and no pod has that name.
        """
        params = {"limit": "1000"}
        seen: set[str] = set()
        for _ in range(100):
            page = self._ask("GET", "pods", params=params)
            try:
                pods = [self._pod(raw) for raw in page["pods"]]
                more, cursor = page["pagination"]["hasNextPage"], page["pagination"]["nextCursor"]
            except (KeyError, TypeError):
                raise ProviderUnavailable("the pod inventory was unreadable") from None
            found = next((pod for pod in pods if pod.name == name), None)
            if found is not None or not more:
                return found
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
            params["cursor"] = cursor
        raise ProviderUnavailable("the pod inventory did not end")

    def create(
        self,
        *,
        name: str,
        hardware: GPU | CPU,
        image: str,
        registry: str,
        env: dict[str, str],
        disk_gb: int,
        data_center: str | None = None,
        entrypoint: tuple[str, ...],
    ) -> PodFact:
        """Ask for one on-demand pod. Any error leaves it unknown whether one exists."""
        if "@sha256:" not in image or "RUNPOD_API_KEY" in env:
            raise ValueError("the image must be pinned by digest and the account key kept out")
        payload: dict[str, Any] = {
            "name": name,
            "image": image,
            "registry": registry,
            "cloud": "SECURE",
            "disk": disk_gb,
            "startSsh": True,
            "ports": ["22/tcp"],
            "mounts": {},
            "entrypoint": list(entrypoint),
            "env": env,
        }
        if isinstance(hardware, GPU):
            payload["gpu"] = {
                "id": hardware.id,
                "count": 1,
                "minRamPerGpu": hardware.min_memory_gb,
                "minVcpuCountPerGpu": hardware.min_vcpus,
            }
        else:
            payload["cpu"] = {"id": hardware.flavor, "vcpuCount": hardware.vcpus}
        if data_center is not None:
            payload["dataCenterIds"] = [data_center]
        pod = self._pod(self._ask("POST", "pods", payload=payload))
        if pod.name != name or pod.usd_per_hour <= 0 or pod.status in {"EXITED", "TERMINATED"}:
            raise ProviderUnavailable("the created pod is not the one asked for")
        return pod

    def delete(self, pod: PodFact) -> datetime:
        """Delete exactly this pod and return when the provider confirmed it.

        Refuses when the id now belongs to something else, and when the pod is
        already absent: an absent pod gives no date to record.
        """
        current = self.pod(pod.id)
        if current is None:
            raise ProviderUnavailable("the pod is absent, so its deletion cannot be dated")
        if current.name != pod.name or current.created_at != pod.created_at:
            raise ProviderUnavailable("the pod id now names a different pod")
        self._ask("DELETE", f"pods/{pod.id}")
        return datetime.now(UTC)

    def bills(self, pod: PodFact, now: datetime) -> tuple[HourBill, ...]:
        """Whole clock hours RunPod has billed for one pod, oldest first."""
        first = pod.created_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        if end <= first:
            return ()
        raw = self._ask(
            "GET",
            "billing/pods",
            params={
                "podId": pod.id,
                "bucketSize": "hour",
                "startTime": first.isoformat(),
                "endTime": end.isoformat(),
            },
        )
        bills: dict[datetime, Decimal] = {}
        try:
            for row in raw["records"]:
                hour = datetime.fromisoformat(str(row["startTime"]))
                ends = datetime.fromisoformat(str(row["endTime"]))
                if (
                    str(row["podId"]) != pod.id
                    or hour in bills
                    or not first <= hour < end
                    or ends != hour + timedelta(hours=1)
                ):
                    raise ValueError
                bills[hour] = Decimal(str(row["totalAmount"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ProviderUnavailable("the billing records were incomplete") from None
        return tuple(HourBill(hour=hour, usd=usd) for hour, usd in sorted(bills.items()))


def _available(row: Any, data_center: str | None) -> bool:
    """False only when a catalog row positively reports none, there or anywhere."""
    if data_center is None:
        return bool(row.get("availability") != "NONE")
    # A place the catalog does not list has none.
    places = {place["id"]: place.get("availability") for place in row["dataCenters"]}
    return bool(places.get(data_center, "NONE") != "NONE")
