"""Google Colab runtime acquisition, Jupyter kernel bootstrap, and reconciliation.

Deterministic `nbh` hashes make assignment idempotent and recoverable across lost
replies. Account OAuth credentials stay on the launcher; the Colab VM receives
only signed storage bootstrap links and runs a local deadline watchdog inside an
active Jupyter kernel cell. Colab reports native Compute Units (`cu`) only; the
scheduler owns USD conversion and budget accounting.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from etude.deadline import limit_time, remaining
from etude.launch.errors import ProviderUnavailable, Refused
from etude.launch.provisioner.machines import (
    Allocation,
    Bill,
    MachineRequest,
    Safeguards,
    check_creation,
)
from etude.launch.provisioner.providers.colab.api import (
    Assigned,
    CcuInfo,
    ChannelHandle,
    ColabAPI,
    ColabSpec,
    RuntimeProxyInfo,
    resolve_template,
)
from etude.launch.provisioner.providers.program import Program, boot_source
from etude.launch.records import MachineTemplate, Offer, affordable_seconds, provider_unit
from etude.launch.storage.s3 import AlreadyThere, Bucket
from etude.spec import Record

UNIT = provider_unit("colab")
_CCU_POLL_ATTEMPTS = 3
_CCU_POLL_INTERVAL = 0.1
_KEEP_ALIVE_INTERVAL = 60.0


class Intent(Record):
    request: MachineRequest
    spec: ColabSpec
    requested_at: datetime


class Receipt(Record):
    endpoint: str
    created_at: datetime
    units_per_hour: Decimal | None = None
    deadline: datetime | None = None


def _session_nbh(session: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"etude-colab:{session}")


def _cap_deadline(
    origin: datetime,
    deadline: datetime,
    units_per_hour: Decimal | None,
    cap_units: Decimal | None,
) -> datetime:
    """Bound a Colab rental's deadline against its CU cap once the hourly burn rate is observed."""
    if units_per_hour is None or units_per_hour <= 0 or cap_units is None or cap_units <= 0:
        return deadline
    funded = max(1.0, affordable_seconds(units_per_hour, cap_units))
    return origin + timedelta(seconds=min(funded, (deadline - origin).total_seconds()))


# A retried bootstrap never starts a second worker: the atomic claim lives on
# the VM. Waiting on the child subprocess keeps the Colab Jupyter kernel `busy`
# for the life of the worker and emits a periodic heartbeat so Jupyter's
# `last_activity` advances, while `start_new_session=True` isolates the
# watchdog's `killpg` from the Jupyter server.
def _kernel_bootstrap_code(env: dict[str, str], code: str) -> str:
    return f"""import os, pathlib, shutil, subprocess, sys
root = pathlib.Path('/run/etude')
root.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    (root / 'boot-claim').open('x').close()
except FileExistsError:
    raise SystemExit(0)
env = {env!r}
python = shutil.which('python3.12') or sys.executable
proc = subprocess.Popen(
    [python, '-c', {code!r}],
    env={{**os.environ, **env}},
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
while True:
    try:
        proc.wait(timeout=30)
        break
    except subprocess.TimeoutExpired:
        sys.stdout.write('.')
        sys.stdout.flush()
"""


class ColabMachines:
    safeguards = Safeguards()
    owned = False

    def __init__(
        self,
        template: MachineTemplate,
        api: ColabAPI,
        bucket: Bucket,
        prefix: str,
    ) -> None:
        self._spec = resolve_template(template)
        self._api, self._bucket, self._prefix = api, bucket, prefix
        self._program = Program(bucket, prefix)
        self._checked_kernel: dict[str, float] = {}
        self._channels: dict[str, ChannelHandle] = {}
        self._booted: set[str] = set()

    def _key(self, session: str, name: str) -> str:
        return f"{self._prefix}/sessions/{session}/{name}.json"

    def _read[T: Record](self, session: str, name: str, model: type[T]) -> T | None:
        raw = self._bucket.get(self._key(session, name))
        return None if raw is None else model.model_validate_json(raw)

    def _record_assignment(
        self, intent: Intent, assigned: Assigned, created_at: datetime, rate: Decimal | None
    ) -> Receipt:
        request = intent.request
        assert request.deadline is not None
        receipt = Receipt(
            endpoint=assigned.endpoint,
            created_at=created_at,
            units_per_hour=rate,
            deadline=_cap_deadline(created_at, request.deadline, rate, request.cap_units),
        )
        self._bucket.put(self._key(request.session, "receipt"), receipt.model_dump_json().encode())
        return receipt

    def _poll_rate(self, before: CcuInfo) -> Decimal | None:
        """Read post-assign ccu-info, retrying briefly if Colab lags reflecting the new session."""
        for attempt in range(_CCU_POLL_ATTEMPTS):
            try:
                after = self._api.ccu_info()
            except ProviderUnavailable:
                return None
            difference = after.assignments_count - before.assignments_count
            if difference == 1:
                rate = round(after.consumption_rate_hourly - before.consumption_rate_hourly, 4)
                return rate if rate > 0 else None
            if difference != 0:
                return None
            if attempt + 1 < _CCU_POLL_ATTEMPTS:
                left = remaining()
                if left is not None and left <= _CCU_POLL_INTERVAL:
                    break
                time.sleep(_CCU_POLL_INTERVAL)
        return None

    def _availability(self) -> tuple[ColabSpec, CcuInfo]:
        if isinstance(self._spec, str):
            raise Refused(self._spec)
        usage = self._api.ccu_info()
        if self._spec.needs_compute_units and usage.current_balance <= 0:
            raise Refused("Colab account has no paid compute units remaining")
        return self._spec, usage

    def offer(self) -> Offer:
        try:
            spec, usage = self._availability()
        except Refused as reason:
            return Offer.unavailable(str(reason))
        return Offer(
            hardware=spec.hardware,
            usd_per_hour=None,
            unit=UNIT,
            usd_per_unit=None,
            balance=round(usage.current_balance, 2),
            active=usage.assignments_count,
            available=True,
        )

    def create(self, request: MachineRequest) -> Allocation:
        if request.deadline is None or request.bootstrap is None:
            raise Refused("Colab rentals need a deadline and storage bootstrap")
        receipt = self._read(request.session, "receipt", Receipt)
        if receipt is not None:
            return self._restore(request.session, receipt, None)
        spec, before = self._availability()
        now = datetime.now(UTC)
        cutoff = min(request.deadline, request.startup_deadline or request.deadline)
        if now >= cutoff:
            raise Refused("Colab rental or startup deadline already passed")
        intent = Intent(request=request, spec=spec, requested_at=now)
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
            self._check_rate(request, assigned.endpoint, units_per_hour)
            receipt = self._record_assignment(intent, assigned, created_at, units_per_hour)
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
                env = self._program.prepare(bounded_request)
                code = _kernel_bootstrap_code(
                    env, boot_source("etude.launch.provisioner.providers.watchdog")
                )
                with limit_time((cutoff - datetime.now(UTC)).total_seconds()):
                    kernel_id = self._api.execute(proxy, code)
                self._bucket.put(self._key(request.session, "booted"), b"true")
                self._booted.add(request.session)
                self._ensure_channel(receipt.endpoint, proxy, kernel_id)
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
                units_per_hour = round(info.consumption_rate_hourly, 4)
        except ProviderUnavailable:
            pass
        receipt = self._record_assignment(intent, found, intent.requested_at, units_per_hour)
        return receipt, found.runtime_proxy_info

    def find(self, session_id: str) -> str | None:
        receipt, _ = self._receipt(session_id)
        return None if receipt is None else receipt.endpoint

    def allocation(self, session_id: str) -> Allocation | None:
        receipt, proxy = self._receipt(session_id)
        if receipt is None:
            return None
        try:
            return self._restore(session_id, receipt, proxy)
        except Refused:
            return None  # The recovered assignment exceeded its ceiling and is confirmed gone.

    def _restore(
        self, session_id: str, receipt: Receipt, proxy: RuntimeProxyInfo | None
    ) -> Allocation:
        intent = self._read(session_id, "intent", Intent)
        if intent is None:
            raise ProviderUnavailable("Colab rental is missing its creation intent")
        self._check_rate(intent.request, receipt.endpoint, receipt.units_per_hour)
        return self._resume(intent, receipt, proxy)

    def _check_rate(self, request: MachineRequest, endpoint: str, rate: Decimal | None) -> None:
        ceiling = request.max_units_per_hour
        if rate is not None and ceiling is not None and rate > ceiling:
            if not self._unassign(endpoint):
                raise ProviderUnavailable(
                    "Colab observed rate exceeded ceiling; unassign unconfirmed"
                )
            limit = f"{ceiling.quantize(Decimal('0.0001')).normalize():f}"
            raise Refused(f"Colab observed rate {rate} CU/hour exceeds ceiling {limit} CU/hour")

    def _is_booted(self, session_id: str) -> bool:
        if session_id in self._booted:
            return True
        if self._bucket.get(self._key(session_id, "booted")) is not None:
            self._booted.add(session_id)
            return True
        return False

    def _ensure_channel(self, endpoint: str, proxy: RuntimeProxyInfo, kernel_id: str) -> None:
        channel = self._channels.get(endpoint)
        if channel is not None:
            channel.update(proxy.url, proxy.token)
            return
        handle = self._api.watch_kernel(proxy, kernel_id)
        if handle is not None:
            self._channels[endpoint] = handle

    def _close_channel(self, endpoint: str) -> None:
        self._checked_kernel.pop(endpoint, None)
        channel = self._channels.pop(endpoint, None)
        if channel is not None:
            channel.close()

    def alive(self, session_id: str) -> bool:
        receipt = self._read(session_id, "receipt", Receipt)
        if receipt is None:
            return True
        try:
            proxy = self._proxy_for(receipt.endpoint)
            if proxy is None:
                self._close_channel(receipt.endpoint)
                return False
            if not self._refresh_kernel(session_id, receipt.endpoint, proxy):
                self._close_channel(receipt.endpoint)
                return False
            return True
        except ProviderUnavailable:
            return True

    def _refresh_kernel(self, session_id: str, endpoint: str, proxy: RuntimeProxyInfo) -> bool:
        """Keep the kernel WebSocket attached with fresh tokens and detect exited bootstraps."""
        now = time.monotonic()
        if now - self._checked_kernel.get(endpoint, float("-inf")) < _KEEP_ALIVE_INTERVAL:
            return True
        kernels = self._api.kernels(proxy)
        self._checked_kernel[endpoint] = now
        active = [k for k in kernels if k.execution_state != "idle"]
        if not active:
            return not self._is_booted(session_id)
        self._ensure_channel(endpoint, proxy, active[0].id)
        return True

    def end(self, session_id: str) -> datetime | None:
        endpoint = self.find(session_id)
        if endpoint is not None:
            self._close_channel(endpoint)
        return datetime.now(UTC) if endpoint is not None and self._unassign(endpoint) else None

    def _unassign(self, endpoint: str) -> bool:
        self._api.unassign(endpoint)
        return self._proxy_for(endpoint) is None

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
