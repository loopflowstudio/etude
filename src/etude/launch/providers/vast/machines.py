"""Vast acquisition and ownership receipts on the existing session evidence path.

A durable offer intent precedes the single create request. Lost replies reconcile
by label; a retained intent never authorizes a second paid create.

A rental's disk holds every undelivered output, and declaring it lost destroys
that disk. Hosts drop offline and return, so one unhealthy read only opens a
durable `Outage`; the rental is lost once the outage outlasts `OUTAGE_SECONDS`.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etude.deadline import limit_time
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Refused, check_creation
from etude.launch.machines import Allocation, Bill, MachineRequest, Safeguards
from etude.launch.providers.program import Program, boot_source
from etude.launch.providers.vast.api import Create, Instance, VastAPI, VastOffer
from etude.launch.records import STOCK_IMAGE, MachineTemplate, Offer
from etude.launch.storage.s3 import Bucket
from etude.spec import Record

# Fifteen minutes of a single-GPU rental costs cents; its disk can hold days of work.
OUTAGE_SECONDS = 900
_DOWN = {"exited", "offline", "error", "stopped"}


class Outage(Record):
    """Why and since when a rental has looked unhealthy; retained after it ends."""

    status: str | None  # None: the provider reported no such instance
    first_seen: datetime


class VastMachines:
    safeguards = Safeguards(start_command=True)
    owned = False

    def __init__(
        self,
        template: MachineTemplate,
        api: VastAPI,
        bucket: Bucket,
        prefix: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._template, self._api, self._bucket, self._prefix = template, api, bucket, prefix
        self._now = now
        self._offers: tuple[VastOffer, ...] | None = None
        self._program = Program(bucket, prefix)

    def offer(self) -> Offer:
        self._offers = self._api.offers(self._template)
        selected = self._offers[0] if self._offers else None
        return Offer(
            hardware=None if selected is None else selected.hardware,
            usd_per_hour=Decimal(0) if selected is None else selected.dph_total,
            available=selected is not None,
        )

    def _key(self, session: str, name: str) -> str:
        return f"{self._prefix}/sessions/{session}/{name}.json"

    def _stored_instance(self, session: str) -> Instance | None:
        raw = self._bucket.get(self._key(session, "receipt"))
        return None if raw is None else Instance.model_validate_json(raw)

    def _keep(self, session: str, instance: Instance) -> None:
        if instance.label != f"etude-{session}":
            raise ProviderUnavailable("Vast instance label does not match this session")
        self._bucket.put(self._key(session, "receipt"), instance.model_dump_json().encode())

    def create(self, request: MachineRequest) -> Allocation:
        if request.deadline is None or request.bootstrap is None:
            raise Refused("Vast rentals need a deadline and a storage bootstrap link")
        if self._bucket.get(self._key(request.session, "offer")) is not None:
            existing = self.find(request.session)
            if existing is not None:
                return existing
            raise ProviderUnavailable("Vast create already attempted; awaiting reconciliation")
        offers = self._offers
        self._offers = None  # the next acquisition needs current capacity
        if offers is None:
            offers = self._api.offers(self._template)
        if not offers:
            raise Refused("no compatible Vast offer with free upload and download")
        # Equivalent offers can have different contract expiry dates. Select one
        # covering this window without exceeding the price admitted by the scheduler.
        offer = next(
            (
                candidate
                for candidate in offers
                if candidate.dph_total <= offers[0].dph_total
                and (
                    candidate.end_date is None or candidate.end_date >= request.deadline.timestamp()
                )
            ),
            None,
        )
        if offer is None:
            raise Refused("no quoted Vast offer covers the requested rental deadline")
        image = self._template.image or STOCK_IMAGE
        env = self._program.prepare(request, image)
        creation_deadline = min(request.deadline, request.startup_deadline or request.deadline)
        now = datetime.now(UTC)
        if now >= creation_deadline:
            raise Refused("startup or rental deadline expired; no Vast instance created")
        cost = offer.dph_total * Decimal(str((request.deadline - now).total_seconds())) / 3600
        if cost > request.cap_usd:
            raise Refused("Vast offer exceeds the reserved rental budget")
        # Receipt of intent is not a receipt of creation. Never retry the PUT,
        # even if its connection or the subsequent storage write is lost.
        self._bucket.put(
            self._key(request.session, "offer"), offer.model_dump_json().encode(), once=True
        )
        check_creation()
        with limit_time((creation_deadline - datetime.now(UTC)).total_seconds()):
            id = self._api.create(
                offer,
                Create(
                    image=image.resolved,
                    disk=self._template.disk_gb or 40,
                    label=f"etude-{request.session}",
                    env=env,
                    onstart="exec python3.12 -c "
                    + shlex.quote(boot_source("etude.launch.providers.vast.instance")),
                ),
            )
            instance = self._api.instance(id)
        if instance is None:
            raise ProviderUnavailable("Vast accepted creation but instance is not yet visible")
        self._keep(request.session, instance)
        return self._allocation(instance)

    @staticmethod
    def _allocation(instance: Instance) -> Allocation:
        address = (
            f"ssh://root@{instance.ssh_host}:{instance.ssh_port}"
            if instance.ssh_host and instance.ssh_port
            else None
        )
        return Allocation(
            id=str(instance.id),
            address=address,
            usd_per_hour=instance.dph_total,
            created_at=instance.created_at,
            hardware=instance.hardware,
        )

    def find(self, session_id: str) -> Allocation | None:
        instance = self._stored_instance(session_id)
        if instance is None:
            if self._bucket.get(self._key(session_id, "offer")) is None:
                return None
            instance = self._api.named(f"etude-{session_id}")
            if instance is None:
                # Inventory can lag creation, even beyond the local rental deadline.
                # Retain reconciliation duty until the paid outcome is known.
                raise ProviderUnavailable(
                    "Vast create already attempted; instance not yet visible; "
                    "awaiting reconciliation"
                )
            self._keep(session_id, instance)
        return self._allocation(instance)

    def alive(self, session_id: str) -> bool:
        instance = self._stored_instance(session_id)
        if instance is None:
            return True
        try:
            current = self._api.instance(instance.id)
        except ProviderUnavailable:
            return True
        key = self._key(session_id, "outage")
        raw = self._bucket.get(key)
        if current is not None and current.actual_status == "destroyed":
            return False
        if current is not None and current.actual_status not in _DOWN:
            if raw is not None:
                self._bucket.delete(key)
            return True
        now = self._now()
        if raw is None:
            status = None if current is None else current.actual_status
            outage = Outage(status=status, first_seen=now)
            self._bucket.put(key, outage.model_dump_json().encode())
            return True
        first_seen = Outage.model_validate_json(raw).first_seen
        return now - first_seen < timedelta(seconds=OUTAGE_SECONDS)

    def end(self, session_id: str) -> datetime | None:
        receipt = self._stored_instance(session_id)
        if receipt is None and self._bucket.get(self._key(session_id, "offer")) is None:
            return None
        current = (
            self._api.instance(receipt.id)
            if receipt is not None
            else self._api.named(f"etude-{session_id}")
        )
        if current is None:
            return datetime.now(UTC) if receipt is not None else None
        if current.label != f"etude-{session_id}":
            raise ProviderUnavailable("refusing to destroy a Vast instance with a different label")
        if receipt is None:
            self._keep(session_id, current)
        self._api.delete_self(str(current.id))
        # DELETE success acknowledges the operation. Confirm absence before
        # declaring the rental ended and releasing its reservation.
        return datetime.now(UTC) if self._api.instance(current.id) is None else None

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        # Vast's daily charge totals do not establish contiguous settled
        # coverage. Keep estimates until the provider supplies that evidence;
        # even confirmed deletion cannot turn a daily date into a watermark.
        return None
