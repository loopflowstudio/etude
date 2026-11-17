"""EC2 rental ownership by session tag. A durable intent precedes the paid request."""

from __future__ import annotations

import base64
import hashlib
import math
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
from etude.launch.provisioner.providers.ec2.api import EC2API, Instance
from etude.launch.provisioner.providers.ec2.host import user_data
from etude.launch.provisioner.providers.program import Program, boot_source
from etude.launch.records import GPU, STOCK_IMAGE, MachineTemplate, Offer
from etude.launch.storage.s3 import AlreadyThere, Bucket
from etude.spec import Record
from etude.storage_timeout import StorageUnavailable


class Intent(Record):
    creation_deadline: datetime
    rental_deadline: datetime
    hardware: GPU
    usd_per_hour: Decimal


class EC2Machines:
    owned = False
    safeguards = Safeguards(machine_timer=True)

    def __init__(
        self, template: MachineTemplate, api: EC2API, zone: str | None, bucket: Bucket, prefix: str
    ) -> None:
        self.template, self.api, self.zone = template, api, zone
        self.bucket, self.prefix = bucket, prefix
        self.program = Program(bucket, prefix)
        self._quote: Offer | None = None

    def offer(self) -> Offer:
        self._quote = self.api.offer(self.template, self.zone)
        return self._quote

    def _key(self, session: str) -> str:
        return f"{self.prefix}/sessions/{session}/intent.json"

    def _intent(self, session: str) -> Intent | None:
        raw = self.bucket.get(self._key(session))
        return None if raw is None else Intent.model_validate_json(raw)

    def _prepare(self, request: MachineRequest) -> tuple[Intent, dict[str, str]]:
        if request.deadline is None or request.bootstrap is None:
            raise Refused("EC2 needs a deadline and storage bootstrap")
        quote = self._quote or self.offer()
        self._quote = None
        gpu = quote.hardware
        if not quote.available or not isinstance(gpu, GPU) or not gpu.flavor:
            raise Refused(quote.reason or "EC2 has no matching GPU")
        now = datetime.now(UTC)
        seconds = (request.deadline - now).total_seconds()
        cutoff = min(request.deadline, request.startup_deadline or request.deadline)
        if (
            now >= cutoff
            or quote.usd_per_hour * Decimal(str(max(60, seconds))) / 3600 > request.cap_usd
        ):
            raise Refused("EC2 deadline or reserved budget does not cover this rental")

        image = self.template.image or STOCK_IMAGE
        env = self.program.prepare(request, image)
        # Only names go in docker's argv; all capabilities travel in the environment.
        exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.items())
        command = ["docker", "run", "--rm", "--init", "--gpus", "all"]
        for key in env:
            command.extend(["--env", key])
        command.extend(
            [
                "--entrypoint",
                "python3.12",
                image.resolved,
                "-c",
                boot_source("etude.launch.provisioner.providers.watchdog"),
            ]
        )
        boot = f"set -eu\n{exports}\nexec {shlex.join(command)}\n".encode()
        sha = hashlib.sha256(boot).hexdigest()
        key = f"{self.prefix}/sessions/{request.session}/boot/{sha}"
        self.bucket.put(key, boot)
        script = user_data(
            request.deadline, self.bucket.get_url(key, math.ceil(seconds)), sha
        ).encode()
        if len(script) > 16 * 1024:
            raise Refused("EC2 user data exceeds 16 KiB")
        fields = {
            "ClientToken": request.session,
            "MinCount": "1",
            "MaxCount": "1",
            "InstanceType": gpu.flavor,
            "ImageId": self.api.image(),
            "UserData": base64.b64encode(script).decode(),
            "InstanceInitiatedShutdownBehavior": "terminate",
            "MetadataOptions.HttpTokens": "required",
            "BlockDeviceMapping.1.DeviceName": "/dev/sda1",
            "BlockDeviceMapping.1.Ebs.VolumeType": "gp3",
            "BlockDeviceMapping.1.Ebs.VolumeSize": str(gpu.disk_gb),
            "BlockDeviceMapping.1.Ebs.DeleteOnTermination": "true",
            "NetworkInterface.1.DeviceIndex": "0",
            "NetworkInterface.1.AssociatePublicIpAddress": "true",
        }
        # A region-wide request lets EC2 pick a default subnet with capacity.
        if self.zone:
            fields["Placement.AvailabilityZone"] = self.zone
        for i, resource in enumerate(("instance", "volume"), start=1):
            tag = f"TagSpecification.{i}"
            fields.update(
                {
                    f"{tag}.ResourceType": resource,
                    f"{tag}.Tag.1.Key": "etude-session",
                    f"{tag}.Tag.1.Value": request.session,
                    f"{tag}.Tag.2.Key": "Name",
                    f"{tag}.Tag.2.Value": f"etude-{request.session}",
                }
            )
        intent = Intent(
            creation_deadline=cutoff,
            rental_deadline=request.deadline,
            hardware=gpu,
            usd_per_hour=quote.usd_per_hour,
        )
        return intent, fields

    def _launch(self, intent: Intent, fields: dict[str, str]) -> Allocation:
        if datetime.now(UTC) >= intent.creation_deadline:
            raise ProviderUnavailable("EC2 create window ended; awaiting inventory reconciliation")
        check_creation()
        return self._allocation(self.api.launch(fields), intent)

    def create(self, request: MachineRequest) -> Allocation:
        prior = self.find(request.session)
        if prior is not None:
            return prior
        try:
            intent, fields = self._prepare(request)
        except (OSError, ProviderUnavailable, StorageUnavailable) as error:
            # No RunInstances request can have left during boot preparation.
            raise Refused(f"EC2 preparation failed: {sanitize(str(error))}") from None
        if datetime.now(UTC) >= intent.creation_deadline:
            raise Refused("EC2 create window expired")
        check_creation()
        try:
            self.bucket.put(
                self._key(request.session), intent.model_dump_json().encode(), once=True
            )
        except AlreadyThere:
            raise ProviderUnavailable("EC2 launch already claimed; reconciling by tag") from None
        try:
            allocation = self._launch(intent, fields)
        except OSError:
            # EC2 may have accepted the request. Replay the identical token, subnet,
            # and signed user data while the original placement fence still holds.
            try:
                allocation = self._launch(intent, fields)
            except Refused:
                raise ProviderUnavailable("EC2 launch reply lost; reconciling by tag") from None
        return self._keep(request.session, allocation)

    @staticmethod
    def _allocation(instance: Instance, intent: Intent) -> Allocation:
        return Allocation(
            id=instance.id,
            address=None,
            created_at=instance.launched,
            hardware=intent.hardware,
            usd_per_hour=intent.usd_per_hour,
        )

    def _receipt_key(self, session: str) -> str:
        return f"{self.prefix}/sessions/{session}/receipt.json"

    def _keep(self, session: str, allocation: Allocation) -> Allocation:
        self.bucket.put(self._receipt_key(session), allocation.model_dump_json().encode())
        return allocation

    def _receipt(self, session: str) -> Allocation | None:
        raw = self.bucket.get(self._receipt_key(session))
        return None if raw is None else Allocation.model_validate_json(raw)

    def find(self, session_id: str) -> Allocation | None:
        receipt = self._receipt(session_id)
        if receipt is not None:
            return receipt
        intent = self._intent(session_id)
        if intent is None:
            return None
        instances = self.api.instances(session_id)
        if instances:
            return self._keep(session_id, self._allocation(instances[0], intent))
        raise ProviderUnavailable("EC2 launch intent exists; awaiting tagged instance")

    def alive(self, session_id: str) -> bool:
        instances = self.api.instances(session_id)
        # EC2 may not list a newly launched instance yet. Its deadline still
        # leads to end() if the worker never checks in.
        return not instances or any(i.state in {"running", "pending"} for i in instances)

    def end(self, session_id: str) -> datetime | None:
        instances = self.api.instances(session_id)
        live = [i.id for i in instances if i.state not in {"shutting-down", "terminated"}]
        if live:
            self.api.terminate(live)
            return None
        if any(i.state == "shutting-down" for i in instances):
            return None
        if not instances:
            # Empty inventory before expiration is ambiguous after a lost reply.
            intent = self._intent(session_id)
            if intent is None or datetime.now(UTC) < intent.rental_deadline:
                return None
        if self.api.volumes_remain(session_id):
            return None
        return datetime.now(UTC)

    def bill(self, session_id: str, now: datetime) -> Bill | None:
        return None
