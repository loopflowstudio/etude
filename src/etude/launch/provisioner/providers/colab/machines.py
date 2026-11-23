"""Google Colab runtime acquisition, Jupyter kernel bootstrap, and reconciliation.

Deterministic `nbh` hashes make assignment idempotent and recoverable across lost
replies. Account OAuth credentials stay on the launcher; the Colab VM receives
only signed storage bootstrap links and runs a local deadline watchdog inside an
active Jupyter kernel cell. Colab reports native Compute Units (`cu`) only; the
scheduler owns USD conversion and budget accounting.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from etude.deadline import limit_time, remaining
from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.machines import (
    Allocation,
    Bill,
    MachineRequest,
    ProviderProfile,
    Refused,
    Safeguards,
    check_creation,
)
from etude.launch.provisioner.providers.colab.api import (
    Assigned,
    CcuInfo,
    ColabAPI,
    ColabSpec,
    RuntimeProxyInfo,
    resolve_template,
)
from etude.launch.provisioner.providers.program import Program, boot_source
from etude.launch.records import MachineTemplate, Offer, provider_unit
from etude.launch.storage.s3 import AlreadyThere, Bucket
from etude.spec import Record

UNIT = provider_unit("colab")
_CCU_POLL_ATTEMPTS = 3
_CCU_POLL_INTERVAL = 0.1


class Intent(Record):
    request: MachineRequest
    spec: ColabSpec
    requested_at: datetime


class Receipt(Record):
    endpoint: str
    url: str
    created_at: datetime
    units_per_hour: Decimal | None = None
    deadline: datetime | None = None


def _session_nbh(session: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"etude-colab:{session}")


def _observed_rate(before: CcuInfo, after: CcuInfo | None) -> Decimal | None:
    """Isolate this runtime's CU/hr burn rate when no other assignment changed concurrently."""
    if after is None or after.assignments_count != before.assignments_count + 1:
        return None
    delta = round(after.consumption_rate_hourly - before.consumption_rate_hourly, 4)
    return Decimal(str(delta)) if delta > 0 else None


def _cap_deadline(
    origin: datetime,
    deadline: datetime,
    units_per_hour: Decimal | None,
    cap_units: Decimal | None,
) -> datetime:
    """Bound a Colab rental's deadline against its CU cap once the hourly burn rate is observed."""
    if units_per_hour is None or units_per_hour <= 0 or cap_units is None or cap_units <= 0:
        return deadline
    micros = (cap_units * 3_600_000_000 / units_per_hour).to_integral_value(rounding=ROUND_FLOOR)
    funded = max(1.0, float(micros / 1_000_000))
    return min(deadline, origin + timedelta(seconds=funded))


# A retried bootstrap never starts a second worker: the atomic claim lives on
# the VM. Waiting on the child subprocess keeps the Colab Jupyter kernel `busy`
# for the life of the worker, while `start_new_session=True` isolates the
# watchdog's `killpg` from the Jupyter server.
def _kernel_bootstrap_code(env: dict[str, str], code: str) -> str:
    payload = json.dumps({"env": env, "code": code})
    return (
        "import json, os, pathlib, shutil, subprocess, sys\n"
        f"payload = json.loads({payload!r})\n"
        "root = pathlib.Path('/run/etude')\n"
        "root.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "try:\n"
        "    (root / 'boot-claim').open('x').close()\n"
        "except FileExistsError:\n"
        "    raise SystemExit(0)\n"
        "python = shutil.which('python3.12') or sys.executable\n"
        "subprocess.run(\n"
        "    [python, '-c', payload['code']],\n"
        "    env={**os.environ, **payload['env']},\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    start_new_session=True,\n"
        ")\n"
    )


class ColabMachines:
    safeguards = Safeguards()
    profile = ProviderProfile(unit=UNIT, safeguards=safeguards, stock_image=False)
    owned = False

    def __init__(
        self,
        template: MachineTemplate,
        api: ColabAPI,
        bucket: Bucket,
        prefix: str,
    ) -> None:
        self._template, self._api, self._bucket, self._prefix = template, api, bucket, prefix
        self._program = Program(bucket, prefix)

    def _key(self, session: str, name: str) -> str:
        return f"{self._prefix}/sessions/{session}/{name}.json"

    def _read[T: Record](self, session: str, name: str, model: type[T]) -> T | None:
        raw = self._bucket.get(self._key(session, name))
        return None if raw is None else model.model_validate_json(raw)

    def _keep_receipt(self, session: str, receipt: Receipt) -> None:
        self._bucket.put(self._key(session, "receipt"), receipt.model_dump_json().encode())

    def _poll_rate(self, before: CcuInfo) -> Decimal | None:
        """Read post-assign ccu-info, retrying briefly if Colab lags reflecting the new session."""
        for attempt in range(_CCU_POLL_ATTEMPTS):
            try:
                after = self._api.ccu_info()
            except ProviderUnavailable:
                return None
            if after.assignments_count != before.assignments_count:
                return _observed_rate(before, after)
            if attempt + 1 < _CCU_POLL_ATTEMPTS:
                left = remaining()
                if left is not None and left <= _CCU_POLL_INTERVAL:
                    break
                time.sleep(_CCU_POLL_INTERVAL)
        return None

    def offer(self) -> Offer:
        spec = resolve_template(self._template)
        if isinstance(spec, str):
            return Offer.unavailable(spec)
        usage = self._api.ccu_info()
        if spec.needs_compute_units and usage.current_balance <= 0:
            return Offer.unavailable("Colab account has no paid compute units remaining")
        return Offer(
            hardware=spec.hardware,
            usd_per_hour=None,
            unit=UNIT,
            units_per_hour=None,
            usd_per_unit=Decimal(0),
            balance=Decimal(str(round(usage.current_balance, 2))),
            active=usage.assignments_count,
            available=True,
        )

    def create(self, request: MachineRequest) -> Allocation:
        if request.deadline is None or request.bootstrap is None:
            raise Refused("Colab rentals need a deadline and storage bootstrap")
        if self._read(request.session, "receipt", Receipt) is not None:
            found = self.allocation(request.session)
            if found is None:
                raise ProviderUnavailable("Colab accepted creation; runtime not yet visible")
            return found
        spec = resolve_template(self._template)
        if isinstance(spec, str):
            raise Refused(spec)
        before = self._api.ccu_info()
        if spec.needs_compute_units and before.current_balance <= 0:
            raise Refused("Colab account has no paid compute units remaining")
        now = datetime.now(UTC)
        cutoff = min(request.deadline, request.startup_deadline or request.deadline)
        if now >= cutoff:
            raise Refused("Colab rental or startup deadline already passed")
        intent = Intent(
            request=request,
            spec=spec,
            requested_at=now,
        )
        check_creation()
        try:
            self._bucket.put(
                self._key(request.session, "intent"),
                intent.model_dump_json().encode(),
                once=True,
            )
        except AlreadyThere:
            raise ProviderUnavailable(
                "Colab assignment already claimed; reconciling by notebook hash"
            ) from None
        check_creation()
        with limit_time((cutoff - datetime.now(UTC)).total_seconds()):
            assigned = self._api.assign(_session_nbh(request.session), spec)
            created_at = datetime.now(UTC)
            units_per_hour = self._poll_rate(before)
            if (
                units_per_hour is not None
                and request.max_units_per_hour is not None
                and units_per_hour > request.max_units_per_hour
            ):
                self._api.unassign(assigned.endpoint)
                if any(item.endpoint == assigned.endpoint for item in self._api.assignments()):
                    raise ProviderUnavailable(
                        "Colab observed rate exceeded ceiling; unassign unconfirmed"
                    )
                raise Refused(
                    f"Colab observed rate {units_per_hour} CU/hour exceeds ceiling "
                    f"{request.max_units_per_hour} CU/hour"
                )
            effective_deadline = _cap_deadline(
                created_at, request.deadline, units_per_hour, request.cap_units
            )
            receipt = Receipt(
                endpoint=assigned.endpoint,
                url=assigned.runtime_proxy_info.url,
                created_at=created_at,
                units_per_hour=units_per_hour,
                deadline=effective_deadline,
            )
            self._keep_receipt(request.session, receipt)
            return self._resume(intent, receipt, assigned.runtime_proxy_info)

    def _resume(
        self, intent: Intent, receipt: Receipt, proxy: RuntimeProxyInfo | None
    ) -> Allocation:
        request = intent.request
        assert request.deadline is not None
        effective_deadline = receipt.deadline or request.deadline
        bounded_request = request.model_copy(update={"deadline": effective_deadline})
        cutoff = min(effective_deadline, request.startup_deadline or effective_deadline)
        if (
            datetime.now(UTC) < cutoff
            and self._bucket.get(self._key(request.session, "booted")) is None
        ):
            if proxy is None:
                proxy = self._proxy_for(receipt.endpoint)
            if proxy is not None:
                env = self._program.prepare(bounded_request, None)
                code = _kernel_bootstrap_code(
                    env, boot_source("etude.launch.provisioner.providers.watchdog")
                )
                with limit_time((cutoff - datetime.now(UTC)).total_seconds()):
                    kernel = self._api.start_kernel(proxy)
                    self._api.execute(proxy, kernel.id, code)
                self._bucket.put(self._key(request.session, "booted"), b"true")
        return Allocation(
            id=receipt.endpoint,
            address=None,
            usd_per_hour=None,
            created_at=receipt.created_at,
            hardware=intent.spec.hardware,
            unit=UNIT,
            units_per_hour=receipt.units_per_hour,
            deadline=effective_deadline,
        )

    def _proxy_for(self, endpoint: str) -> RuntimeProxyInfo | None:
        for active in self._api.assignments():
            if active.endpoint == endpoint:
                return active.runtime_proxy_info
        return None

    def _receipt(self, session_id: str) -> tuple[Receipt | None, RuntimeProxyInfo | None]:
        receipt = self._read(session_id, "receipt", Receipt)
        if receipt is not None:
            return receipt, None
        intent = self._read(session_id, "intent", Intent)
        if intent is None:
            return None, None
        found = self._api.lookup(_session_nbh(session_id), intent.spec)
        if not isinstance(found, Assigned):
            return None, None
        units_per_hour: Decimal | None = None
        try:
            info = self._api.ccu_info()
            if info.assignments_count == 1 and info.consumption_rate_hourly > 0:
                units_per_hour = Decimal(str(round(info.consumption_rate_hourly, 4)))
        except ProviderUnavailable:
            pass
        assert intent.request.deadline is not None
        effective_deadline = _cap_deadline(
            intent.requested_at,
            intent.request.deadline,
            units_per_hour,
            intent.request.cap_units,
        )
        receipt = Receipt(
            endpoint=found.endpoint,
            url=found.runtime_proxy_info.url,
            created_at=intent.requested_at,
            units_per_hour=units_per_hour,
            deadline=effective_deadline,
        )
        self._keep_receipt(session_id, receipt)
        return receipt, found.runtime_proxy_info

    def find(self, session_id: str) -> str | None:
        receipt, _ = self._receipt(session_id)
        return None if receipt is None else receipt.endpoint

    def allocation(self, session_id: str) -> Allocation | None:
        receipt, proxy = self._receipt(session_id)
        if receipt is None:
            return None
        intent = self._read(session_id, "intent", Intent)
        if intent is None:
            raise ProviderUnavailable("Colab rental is missing its creation intent")
        return self._resume(intent, receipt, proxy)

    def alive(self, session_id: str) -> bool:
        receipt = self._read(session_id, "receipt", Receipt)
        if receipt is None:
            return True
        try:
            active = self._api.assignments()
        except ProviderUnavailable:
            return True
        return any(item.endpoint == receipt.endpoint for item in active)

    def end(self, session_id: str) -> datetime | None:
        receipt, _ = self._receipt(session_id)
        if receipt is None:
            return None
        self._api.unassign(receipt.endpoint)
        active = self._api.assignments()
        if any(item.endpoint == receipt.endpoint for item in active):
            return None
        return datetime.now(UTC)

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
