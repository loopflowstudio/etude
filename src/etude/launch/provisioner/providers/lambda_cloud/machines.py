"""Lambda GPU rentals. A durable name claims each non-idempotent launch once."""

import os
import shlex
from datetime import UTC, datetime
from decimal import Decimal

from etude.errors import sanitize
from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.machines import (
    Allocation,
    Bill,
    MachineRequest,
    Refused,
    Safeguards,
    check_creation,
)
from etude.launch.provisioner.providers.host import container_boot, user_data
from etude.launch.provisioner.providers.lambda_cloud.api import (
    Instance,
    LambdaAPI,
    LambdaOffer,
    Launch,
)
from etude.launch.provisioner.providers.program import Program
from etude.launch.records import GPU, STOCK_IMAGE, MachineTemplate, Offer
from etude.launch.storage.s3 import AlreadyThere, Bucket
from etude.spec import Record
from etude.storage_timeout import StorageUnavailable


class Receipt(Record):
    id: str


class Intent(Record):
    requested_at: datetime
    deadline: datetime
    hardware: GPU
    usd_per_hour: Decimal

    def allocation(self, instance_id: str) -> Allocation:
        return Allocation(
            id=instance_id,
            address=None,
            created_at=self.requested_at,
            hardware=self.hardware,
            usd_per_hour=self.usd_per_hour,
        )


def _setup(key: str) -> str:
    # /run is mounted noexec on Lambda Stack. The host invokes this file with bash.
    return f"""printf '%s\\n' {shlex.quote("Authorization: Bearer " + key)} >/run/etude-lambda-auth
cat >/run/etude-terminate <<'END'
set -eu -o pipefail
id=$(cloud-init query instance_id | tr -d '-')
[[ $id =~ ^[a-fA-F0-9]{{32}}$ ]]
curl -fsS --max-time 20 -H @/run/etude-lambda-auth -H 'Content-Type: application/json' \\
    -d "{{\\"instance_ids\\":[\\"$id\\"]}}" https://cloud.lambda.com/api/v1/instance-operations/terminate >/dev/null
END"""


class LambdaMachines:
    safeguards = Safeguards(machine_timer=True)
    owned = False

    def __init__(
        self,
        template: MachineTemplate,
        api: LambdaAPI,
        key: str | None,
        bucket: Bucket,
        prefix: str,
    ) -> None:
        self.template, self.api, self.key = template, api, key
        self.bucket, self.prefix = bucket, prefix
        self.program = Program(bucket, prefix)
        self._quote: LambdaOffer | None = None

    def offer(self) -> Offer:
        if not self.key:
            return LambdaOffer.unavailable("LAMBDA_TERMINATE_KEY is not set")
        self._quote = self.api.offer(self.template)
        return self._quote

    def _key(self, session: str, record: str) -> str:
        return f"{self.prefix}/sessions/{session}/{record}.json"

    def _intent(self, session: str) -> Intent | None:
        data = self.bucket.get(self._key(session, "intent"))
        return None if data is None else Intent.model_validate_json(data)

    def _receipt(self, session: str) -> Receipt | None:
        data = self.bucket.get(self._key(session, "receipt"))
        return None if data is None else Receipt.model_validate_json(data)

    def _keep(self, session: str, instance_id: str) -> str:
        self.bucket.put(
            self._key(session, "receipt"), Receipt(id=instance_id).model_dump_json().encode()
        )
        return instance_id

    def _prepare(self, request: MachineRequest) -> tuple[Intent, Launch]:
        if not request.deadline or not request.bootstrap:
            raise Refused("Lambda needs a deadline and storage bootstrap")
        if not self.key:
            raise Refused("LAMBDA_TERMINATE_KEY is not set")
        quote = self._quote or self.api.offer(self.template)
        self._quote = None
        gpu = quote.hardware
        if not quote.available or not isinstance(gpu, GPU) or not gpu.flavor or not quote.region:
            raise Refused(quote.reason or "Lambda has no matching GPU")
        now = datetime.now(UTC)
        if (
            Decimal(str((request.deadline - now).total_seconds())) * quote.usd_per_hour / 3600
            > request.cap_usd
        ):
            raise Refused("Lambda rental exceeds the reserved budget")
        image = self.template.image or STOCK_IMAGE
        boot = container_boot(image.resolved, self.program.prepare(request, image))
        launch = Launch(
            name=f"etude-{request.session}",
            region_name=quote.region,
            instance_type_name=gpu.flavor,
            ssh_key_names=(os.environ.get("LAMBDA_SSH_KEY_NAME") or self.api.ssh_key(),),
            user_data=user_data(
                request.deadline,
                "/bin/bash /run/etude-terminate",
                boot,
                _setup(self.key),
            ),
        )
        intent = Intent(
            requested_at=now,
            deadline=request.deadline,
            hardware=gpu,
            usd_per_hour=quote.usd_per_hour,
        )
        return intent, launch

    def create(self, request: MachineRequest) -> Allocation:
        prior = self.allocation(request.session)
        if prior:
            return prior
        try:
            intent, launch = self._prepare(request)
        except (OSError, ProviderUnavailable, StorageUnavailable) as error:
            raise Refused(f"Lambda preparation failed: {sanitize(str(error))}") from None
        cutoff = min(intent.deadline, request.startup_deadline or intent.deadline)
        if datetime.now(UTC) >= cutoff:
            raise Refused("Lambda create window expired")
        check_creation()
        try:
            self.bucket.put(
                self._key(request.session, "intent"), intent.model_dump_json().encode(), once=True
            )
        except AlreadyThere:
            raise ProviderUnavailable(
                "Lambda launch already claimed; reconciling by name"
            ) from None
        check_creation()
        if datetime.now(UTC) >= cutoff:
            raise Refused("Lambda create window expired")
        instance = self.api.launch(launch)
        return intent.allocation(self._keep(request.session, instance))

    def find(self, session_id: str) -> str | None:
        receipt = self._receipt(session_id)
        if receipt:
            return receipt.id
        if self.bucket.get(self._key(session_id, "intent")) is None:
            return None
        instances = self.api.instances("etude-" + session_id)
        if not instances:
            raise ProviderUnavailable("Lambda launch intent exists; awaiting named instance")
        return self._keep(session_id, instances[0].id)

    def allocation(self, session_id: str) -> Allocation | None:
        instance_id = self.find(session_id)
        if instance_id is None:
            return None
        intent = self._intent(session_id)
        if intent is None:
            raise ProviderUnavailable("Lambda rental is missing its pricing intent")
        return intent.allocation(instance_id)

    def _inventory(self, session: str, receipt: Receipt | None) -> list[Instance]:
        if receipt is None:
            return self.api.instances("etude-" + session)
        instance = self.api.instance(receipt.id)
        return [instance] if instance is not None else []

    def alive(self, session_id: str) -> bool:
        return any(
            i.status in {"booting", "active"}
            for i in self._inventory(session_id, self._receipt(session_id))
        )

    def end(self, session_id: str) -> datetime | None:
        receipt = self._receipt(session_id)
        instances = self._inventory(session_id, receipt)
        ids = tuple(i.id for i in instances if i.status not in {"terminating", "terminated"})
        if ids:
            self.api.terminate(ids)
        if any(i.status != "terminated" for i in instances):
            return None
        if not instances and receipt is None:
            intent = self._intent(session_id)
            if intent is None or datetime.now(UTC) < intent.deadline:
                return None
        return datetime.now(UTC)

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
