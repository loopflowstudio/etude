"""RunPod's REST API, reduced to the facts the launcher needs.

The account key stays with this client on the launching pool. Nothing a
provider returns is logged or stored whole: each reply is narrowed to a few
checked fields. A create is sent once and never retried here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from etude.launch.providers.runpod.http import _ID, PodHTTP
from etude.launch.providers.runpod.http import ProviderUnavailable as ProviderUnavailable
from etude.launch.records import Record


class Quote(Record):
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

    def quote(self, gpu: str, data_center: str | None = None) -> Quote:
        """The secure-cloud hourly price for one GPU type, and whether any can be had.

        With a data center named, only stock there counts; the price is the same everywhere.
        """
        catalog = self._ask(
            "GET",
            "catalog/gpus",
            params={"include": "AVAILABILITY", "product": "POD", "cloud": "SECURE"},
        )
        try:
            row = next(row for row in catalog["gpus"] if row["id"] == gpu)
            price = Decimal(str(row["price"]["secure"]))
            availability = row.get("availability")
            if data_center is not None:  # a place the catalog does not list has none
                places = {place["id"]: place.get("availability") for place in row["dataCenters"]}
                availability = places.get(data_center, "NONE")
        except (KeyError, TypeError, StopIteration, InvalidOperation):
            raise ProviderUnavailable(f"no price for {gpu}") from None
        if price <= 0:
            raise ProviderUnavailable(f"no price for {gpu}")
        return Quote(usd_per_hour=price, available=availability != "NONE")

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
        gpu: str,
        image: str,
        registry: str,
        env: dict[str, str],
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
        data_center: str | None = None,
        entrypoint: tuple[str, ...],
    ) -> PodFact:
        """Ask for one on-demand pod. Any error leaves it unknown whether one exists."""
        if "@sha256:" not in image or "RUNPOD_API_KEY" in env:
            raise ValueError("the image must be pinned by digest and the account key kept out")
        payload = {
            "name": name,
            "image": image,
            "registry": registry,
            "gpu": {"id": gpu, "count": 1, "minRamPerGpu": memory_gb, "minVcpuCountPerGpu": vcpus},
            "cloud": "SECURE",
            "disk": disk_gb,
            "ports": [],
            "mounts": {},
            "entrypoint": list(entrypoint),
            "env": env,
        }
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
