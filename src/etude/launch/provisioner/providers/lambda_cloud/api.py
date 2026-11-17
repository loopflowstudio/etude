"""Lambda Cloud's catalog, paginated instance inventory and rental operations."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote, urlencode

from pydantic import Field, ValidationError

from etude.deadline import call
from etude.errors import sanitize
from etude.launch.errors import ProviderRejected, ProviderUnavailable
from etude.launch.provisioner.machines import Refused
from etude.launch.provisioner.providers.http import MAX_REPLY_BYTES, send_to_provider
from etude.launch.records import GPU, MachineTemplate, Offer
from etude.launch.storage.transport import Send
from etude.spec import Record


class _Specs(Record):
    vcpus: int
    memory_gib: int
    storage_gib: int
    gpus: int

    def fits(self, template: MachineTemplate) -> bool:
        return (
            self.gpus == template.gpu_count
            and self.vcpus >= (template.vcpus or 0)
            and self.memory_gib >= (template.memory_gb or 0)
        )


class _Type(Record):
    name: str
    gpu_description: str
    price_cents_per_hour: int = Field(gt=0)
    specs: _Specs


class _Name(Record):
    name: str


class _Stock(Record):
    instance_type: _Type
    regions_with_capacity_available: tuple[_Name, ...]


class _Catalog(Record):
    data: dict[str, _Stock]


class _Keys(Record):
    data: tuple[_Name, ...]


class Instance(Record):
    id: str
    name: str | None = None
    status: str


class _Instance(Record):
    data: Instance


class _Instances(Record):
    data: tuple[Instance, ...]
    page_token: str | None


class Launch(Record):
    name: str
    region_name: str
    instance_type_name: str
    ssh_key_names: tuple[str, ...]
    user_data: str = Field(repr=False)


class _IDs(Record):
    instance_ids: tuple[str, ...]


class _Launched(Record):
    data: _IDs


class _Error(Record):
    code: str
    message: str = ""


class _Failed(Record):
    error: _Error


class LambdaOffer(Offer):
    region: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> LambdaOffer:
        return cls(available=False, hardware=None, usd_per_hour=Decimal(0), reason=reason)


class LambdaAPI:
    def __init__(self, key: str, send: Send = send_to_provider) -> None:
        self._headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": "etude-launcher",
            "Content-Type": "application/json",
        }
        self._send = send

    def _ask(self, path: str, body: Record | None = None) -> bytes:
        data = body.model_dump_json().encode() if body else None
        try:
            reply = call(
                lambda: self._send(
                    "POST" if body else "GET",
                    "https://cloud.lambda.com/api/v1/" + path,
                    data,
                    self._headers,
                )
            )
        except OSError:
            raise ProviderUnavailable(
                f"Lambda {path.partition('?')[0]}: reply unconfirmed"
            ) from None
        if 200 <= reply.status < 300 and len(reply.body) <= MAX_REPLY_BYTES:
            return reply.body
        try:
            error = _Failed.model_validate_json(reply.body).error
        except ValueError:
            raise ProviderUnavailable(
                f"Lambda {path.partition('?')[0]} returned HTTP {reply.status}"
            ) from None
        if (
            isinstance(body, Launch)
            and 400 <= reply.status < 500
            and error.code
            in {
                "instance-operations/launch/insufficient-capacity",
                "global/quota-exceeded",
            }
        ):
            raise Refused(f"Lambda {error.code}: {sanitize(error.message)[:300]}")
        raise ProviderRejected(reply.status, sanitize(error.code))

    def _read[T: Record](self, model: type[T], path: str, body: Record | None = None) -> T:
        try:
            return model.model_validate_json(self._ask(path, body))
        except ValidationError:
            raise ProviderUnavailable(
                f"Lambda {path.partition('?')[0]}: missing or invalid response fields"
            ) from None

    def offer(self, template: MachineTemplate) -> LambdaOffer:
        if not template.gpu:
            return LambdaOffer.unavailable("Lambda needs a GPU template")
        catalog = self._read(_Catalog, "instance-types").data.values()
        matches = [
            stock for stock in catalog if stock.instance_type.gpu_description == template.gpu
        ]
        if not matches:
            names = sorted({stock.instance_type.gpu_description for stock in catalog})
            return LambdaOffer.unavailable(
                f"Lambda has no GPU named {template.gpu!r}; it names: {', '.join(names)}"
            )

        choices = [
            (stock.instance_type, region.name)
            for stock in matches
            if stock.instance_type.specs.fits(template)
            for region in stock.regions_with_capacity_available
            if template.data_center in {None, region.name}
        ]
        if not choices:
            return LambdaOffer.unavailable("Lambda reports no capacity matching this template")
        info, region = min(choices, key=lambda c: (c[0].price_cents_per_hour, c[0].name, c[1]))
        return LambdaOffer(
            region=region,
            available=True,
            usd_per_hour=Decimal(info.price_cents_per_hour) / 100,
            hardware=GPU(
                flavor=info.name,
                id=info.gpu_description,
                count=info.specs.gpus,
                min_vcpus=info.specs.vcpus,
                min_memory_gb=info.specs.memory_gib,
                disk_gb=info.specs.storage_gib,
            ),
        )

    def ssh_key(self) -> str:
        keys = self._read(_Keys, "ssh-keys").data
        if not keys:
            raise Refused("Add an SSH key to the Lambda account before renting")
        return min(k.name for k in keys)

    def instances(self, name: str) -> list[Instance]:
        path = "instances"
        matches: list[Instance] = []
        while True:
            page = self._read(_Instances, path)
            matches.extend(i for i in page.data if i.name == name)
            if not page.page_token:
                return matches
            path = "instances?" + urlencode({"page_token": page.page_token})

    def instance(self, instance_id: str) -> Instance | None:
        try:
            return self._read(_Instance, "instances/" + quote(instance_id, safe="")).data
        except ProviderRejected as error:
            if error.status == 404 and error.detail == "global/object-does-not-exist":
                return None
            raise

    def launch(self, request: Launch) -> str:
        ids = self._read(_Launched, "instance-operations/launch", request).data.instance_ids
        if len(ids) != 1:
            raise ProviderUnavailable("Lambda launch reply did not identify one instance")
        return ids[0]

    def terminate(self, ids: tuple[str, ...]) -> None:
        self._ask("instance-operations/terminate", _IDs(instance_ids=ids))
