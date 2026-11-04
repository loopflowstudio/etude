"""Vast acquisition and ownership receipts on the existing session evidence path.

A durable offer intent precedes the single create request. Lost replies reconcile
by label; a retained intent never authorizes a second paid create.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
from datetime import UTC, datetime
from decimal import Decimal

from etude.deadline import limit_time
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Refused, check_creation
from etude.launch.machines import Allocation, Bill, MachineRequest, Safeguards
from etude.launch.providers.package import program_archive
from etude.launch.providers.vast.api import Create, Instance, VastAPI, VastOffer
from etude.launch.records import STOCK_IMAGE, MachineTemplate, Offer
from etude.launch.storage.s3 import Bucket


class VastMachines:
    safeguards = Safeguards(start_command=True)
    owned = False

    def __init__(
        self, template: MachineTemplate, api: VastAPI, bucket: Bucket, prefix: str
    ) -> None:
        self._template, self._api, self._bucket, self._prefix = template, api, bucket, prefix
        self._offers: tuple[VastOffer, ...] | None = None
        self._package_key: str | None = None
        self._package_digest = ""

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

    def _receipt(self, session: str) -> Instance | None:
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
        if self._offers is None:
            self.offer()
        offers = self._offers
        self._offers = None  # the next acquisition needs current capacity
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
        if self._package_key is None:
            code = program_archive()
            digest = hashlib.sha256(code).hexdigest()
            key = f"{self._prefix}/programs/{digest}.zip"
            if self._bucket.get(key) is None:
                self._bucket.put(key, code, once=True)
            self._package_key, self._package_digest = key, digest
        image = self._template.image or STOCK_IMAGE
        env = {
            "ETUDE_LOG_SECONDS": "" if request.log_seconds is None else str(request.log_seconds),
            "ETUDE_IMAGE": image.model_dump_json(),
            "ETUDE_SESSION": request.session,
            "ETUDE_STARTUP_DEADLINE": request.startup_deadline.isoformat()
            if request.startup_deadline
            else "",
            "ETUDE_DEADLINE": request.deadline.isoformat(),
            "ETUDE_MAX_USD": str(request.cap_usd),
            "ETUDE_BOOTSTRAP_URL": request.bootstrap.url,
            "ETUDE_BOOTSTRAP_LOG_URL": request.bootstrap.log_url,
            "ETUDE_WORKER_LOG_URL": request.bootstrap.worker_log_url,
            "ETUDE_PACKAGE_URL": self._bucket.get_url(self._package_key, 13 * 3600),
            "ETUDE_PACKAGE_SHA256": self._package_digest,
            "ETUDE_PROGRAM": base64.b64encode(program_archive(guardian=True)).decode(),
        }
        limits = [d for d in (request.deadline, request.startup_deadline) if d is not None]
        now = datetime.now(UTC)
        if now >= min(limits):
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
        with limit_time((min(limits) - datetime.now(UTC)).total_seconds()):
            id = self._api.create(
                offer,
                Create(
                    image=image.resolved,
                    disk=self._template.disk_gb or 40,
                    label=f"etude-{request.session}",
                    env=env,
                    onstart="exec python3.12 -c " + shlex.quote(_BOOT),
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
        instance = self._receipt(session_id)
        if instance is None:
            if self._bucket.get(self._key(session_id, "offer")) is None:
                return None
            instance = self._api.named(f"etude-{session_id}")
            if instance is not None:
                self._keep(session_id, instance)
        return None if instance is None else self._allocation(instance)

    def alive(self, session_id: str) -> bool:
        instance = self._receipt(session_id)
        if instance is None:
            return True
        try:
            current = self._api.instance(instance.id)
        except ProviderUnavailable:
            return True
        return current is not None and current.actual_status not in {
            "exited",
            "offline",
            "destroyed",
            "error",
            "stopped",
        }

    def end(self, session_id: str) -> datetime | None:
        receipt = self._receipt(session_id)
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
        instance = self._receipt(session_id)
        return None if instance is None else self._api.bill(instance, now)


_BOOT = """import base64,io,os,pathlib,sys,zipfile
root=pathlib.Path('/opt/etude/src')
root.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(io.BytesIO(base64.b64decode(os.environ.pop('ETUDE_PROGRAM')))) as z:
    z.extractall(root)
sys.path.insert(0,str(root))
from etude.launch.providers.vast.instance import main
raise SystemExit(main(['instance','boot']))
"""
