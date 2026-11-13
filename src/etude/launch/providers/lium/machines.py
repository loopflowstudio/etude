"""Lium acquisition, timer acknowledgement, SSH bootstrap and reconciliation.

Intent precedes one paid request. Every subsequent pass recovers that request;
only confirmed deletion releases ownership. No account credential leaves here.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
from datetime import UTC, datetime
from decimal import Decimal

from etude.deadline import limit_time, remaining
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Refused, check_creation
from etude.launch.machines import Allocation, Bill, MachineRequest, Safeguards
from etude.launch.measurement import timed
from etude.launch.providers.lium.api import Create, LiumAPI, LiumOffer, Pod
from etude.launch.providers.program import Program, boot_source
from etude.launch.records import STOCK_IMAGE, MachineTemplate, Offer, Record
from etude.launch.storage.s3 import Bucket


class Intent(Record):
    request: MachineRequest
    offer: LiumOffer


# A lost SSH reply can be retried, but never starts a second bootstrap. The
# claim lives on the pod, and the child owns a process group for its watchdog.
_START = """import json,os,pathlib,sys
payload=json.load(sys.stdin)
root=pathlib.Path('/run/etude')
root.mkdir(parents=True,exist_ok=True,mode=0o700)
try:
    (root/'boot-claim').open('x').close()
except FileExistsError:
    sys.exit(0)
if os.fork():
    sys.exit(0)
os.setsid()
with open(os.devnull,'r+b') as sink:
    for fd in (0,1,2): os.dup2(sink.fileno(),fd)
os.environ.update(payload['env'])
exec(payload['code'])
"""


@timed("ssh.call")
def _start(pod: Pod, env: dict[str, str]) -> None:
    address = pod.address
    if address is None:
        raise ProviderUnavailable("running Lium pod has no SSH address")
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", address, "python3.12 -c " + shlex.quote(_START)],
            input=json.dumps(
                {"env": env, "code": boot_source("etude.launch.providers.lium.pod")}
            ).encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining(),
        )
    except (OSError, subprocess.SubprocessError):
        raise ProviderUnavailable("Lium SSH bootstrap reply unconfirmed") from None
    if result.returncode:
        raise ProviderUnavailable("Lium SSH bootstrap failed; check controller SSH configuration")


class LiumMachines:
    safeguards = Safeguards(lifetime=True, reservation_hours=1)
    owned = False

    def __init__(
        self, template: MachineTemplate, api: LiumAPI, bucket: Bucket, prefix: str
    ) -> None:
        self._template, self._api, self._bucket, self._prefix = template, api, bucket, prefix
        self._program = Program(bucket, prefix)

    def offer(self) -> Offer:
        offers = self._api.offers(self._template)
        selected = offers[0] if offers else None
        return Offer(
            hardware=None if selected is None else selected.hardware,
            usd_per_hour=Decimal(0) if selected is None else selected.price,
            available=selected is not None,
        )

    def _key(self, session: str, name: str) -> str:
        return f"{self._prefix}/sessions/{session}/{name}.json"

    def _read[T: Record](self, session: str, name: str, model: type[T]) -> T | None:
        raw = self._bucket.get(self._key(session, name))
        return None if raw is None else model.model_validate_json(raw)

    def _keep(self, session: str, pod: Pod) -> None:
        if pod.pod_name != f"etude-{session}":
            raise ProviderUnavailable("Lium pod name does not match this session")
        self._bucket.put(self._key(session, "receipt"), pod.model_dump_json().encode())

    def create(self, request: MachineRequest) -> Allocation:
        if request.deadline is None or request.bootstrap is None:
            raise Refused("Lium rentals need a deadline and storage bootstrap")
        found = self.find(request.session)
        if found is not None:
            return found
        public_key = os.environ.get("LIUM_SSH_PUBLIC_KEY", "").strip()
        if (
            not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-"))
            or "\n" in public_key
        ):
            raise Refused("LIUM_SSH_PUBLIC_KEY must contain the controller's public SSH key")
        offers = self._api.offers(self._template)  # revalidate current stock, resources and price
        if not offers:
            raise Refused("no compatible secure Lium allocation")
        offer = offers[0]
        cutoff = min(request.deadline, request.startup_deadline or request.deadline)
        now = datetime.now(UTC)
        hours = math.ceil((request.deadline - now).total_seconds() / 3600)
        if now >= cutoff or not 1 <= hours <= 720:
            raise Refused("Lium rental or startup deadline is outside its supported window")
        if offer.price * hours > request.cap_usd:
            raise Refused("Lium whole-hour fallback exceeds the reserved rental budget")
        image = self._template.image or STOCK_IMAGE
        creation = Create(
            pod_name=f"etude-{request.session}",
            gpu_count=self._template.gpu_count,
            user_public_key=public_key,
            dockerfile_content=f'FROM {image.resolved}\nEXPOSE 22\nENTRYPOINT []\nCMD ["sleep", "infinity"]\n',
            termination_hours=hours,
            expected_price_per_gpu_hour=offer.node.price_per_gpu_hour,
        )
        intent = Intent(request=request, offer=offer)
        check_creation()
        self._bucket.put(
            self._key(request.session, "intent"), intent.model_dump_json().encode(), once=True
        )
        check_creation()
        with limit_time((cutoff - datetime.now(UTC)).total_seconds()):
            id = self._api.create(offer, creation, request.session)
            pod = self._api.pod(id)
            if pod is None:
                raise ProviderUnavailable("Lium accepted creation; pod not yet visible")
            self._keep(request.session, pod)
            return self._resume(intent, pod)

    def _resume(self, intent: Intent, pod: Pod) -> Allocation:
        request = intent.request
        assert request.deadline is not None
        if pod.executor_id != intent.offer.node.id or pod.gpu_count != intent.offer.quote.gpu_count:
            self._api.delete(pod.id)
            raise ProviderUnavailable("Lium allocated a different node or GPU count")
        cutoff = min(request.deadline, request.startup_deadline or request.deadline)
        if datetime.now(UTC) < cutoff:
            if pod.removal_scheduled_at is None or pod.removal_scheduled_at > request.deadline:
                pod = self._api.schedule(pod.id, request.deadline)
                self._keep(request.session, pod)
            if (
                pod.status == "RUNNING"
                and datetime.now(UTC) < cutoff
                and self._bucket.get(self._key(request.session, "booted")) is None
            ):
                if pod.price > intent.offer.price:
                    self._api.delete(pod.id)
                    raise ProviderUnavailable("Lium allocated above the admitted hourly price")
                with limit_time((cutoff - datetime.now(UTC)).total_seconds()):
                    _start(pod, self._program.prepare(request, self._template.image or STOCK_IMAGE))
                self._bucket.put(self._key(request.session, "booted"), b"true")
        return self._allocation(intent, pod)

    @staticmethod
    def _allocation(intent: Intent, pod: Pod) -> Allocation:
        # Retain the admitted tolerance until billing supplies actual charges.
        # Lowering the rate here would also lower downstream budget reservations.
        return Allocation(
            id=str(pod.id),
            address=pod.address,
            usd_per_hour=intent.offer.price,
            created_at=pod.created_at,
            hardware=intent.offer.hardware,
        )

    def find(self, session_id: str) -> Allocation | None:
        intent = self._read(session_id, "intent", Intent)
        if intent is None:
            return None
        receipt = self._read(session_id, "receipt", Pod)
        pod = (
            self._api.named(f"etude-{session_id}") if receipt is None else self._api.pod(receipt.id)
        )
        if pod is None:
            if receipt is not None:
                return self._allocation(intent, receipt)
            raise ProviderUnavailable("Lium create already attempted; awaiting reconciliation")
        self._keep(session_id, pod)
        return self._resume(intent, pod)

    def alive(self, session_id: str) -> bool:
        receipt = self._read(session_id, "receipt", Pod)
        if receipt is None:
            return True
        pod = self._api.pod(receipt.id)
        return pod is not None and pod.status not in {"ERROR", "FAILED", "TERMINATED"}

    def end(self, session_id: str) -> datetime | None:
        receipt = self._read(session_id, "receipt", Pod)
        if receipt is None and self._read(session_id, "intent", Intent) is None:
            return None
        pod = (
            self._api.named(f"etude-{session_id}") if receipt is None else self._api.pod(receipt.id)
        )
        if pod is None:
            return datetime.now(UTC) if receipt is not None else None
        self._keep(session_id, pod)
        self._api.delete(pod.id)
        return datetime.now(UTC) if self._api.pod(pod.id) is None else None

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        receipt = self._read(session_id, "receipt", Pod)
        if receipt is None:
            return None
        statement = self._api.statement(receipt.id)
        if statement is None or statement.billed_until is None:
            return None
        watermarks = [row.billed_until for row in statement.days]
        if (
            statement.created_at != receipt.created_at
            or not receipt.created_at <= statement.billed_until <= now
            or not watermarks
            or watermarks != sorted(set(watermarks))
            or watermarks[0] < receipt.created_at
            or watermarks[-1] != statement.billed_until
            or sum(row.amount for row in statement.days) != statement.total
            or (statement.removed and statement.removed_at != statement.billed_until)
        ):
            raise ProviderUnavailable("Lium statement does not establish settled coverage")
        # This endpoint promises the entire ledger, including paused/free time;
        # billed_until is its explicit contiguous settlement watermark.
        self._bucket.put(self._key(session_id, "statement"), statement.model_dump_json().encode())
        # A final settled removal proves zero compute charges after removal,
        # including the gap before the controller observed absence.
        through = now if statement.removed else statement.billed_until
        return Bill(usd=statement.total, through=through)
