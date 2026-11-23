"""Signed EC2 Query requests and the JSON services used to price and boot GPUs."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from urllib.parse import urlencode

from pydantic import AliasPath, Field, ValidationError

from etude.errors import sanitize
from etude.launch.aws import Keys, Signer
from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.machines import Refused
from etude.launch.records import GPU, MachineTemplate, Offer
from etude.launch.storage.transport import HTTPReply, Send
from etude.spec import Record

# One week of us-west-2 L4 prices moved within this margin over a day.
SPOT_HEADROOM = Decimal("1.10")

AMI_PARAMETER = (
    "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
)

# RunInstances errors that definitely created nothing: a refusal, not an outage.
_REFUSALS = frozenset(
    {
        "InsufficientInstanceCapacity",
        "VcpuLimitExceeded",
        "MaxSpotInstanceCountExceeded",
        "SpotMaxPriceTooLow",
        "UnfulfillableCapacity",
        "Unsupported",
        "InstanceLimitExceeded",
        "VPCIdNotSpecified",
    }
)


class EC2Offer(Offer):
    # Zones to ask, in order; empty lets EC2 choose. A spot offer lists every zone
    # whose current price its bid covers, cheapest first: one zone's capacity
    # refusal says nothing about the next.
    zones: tuple[str, ...] = ()
    max_price: Decimal | None = None  # compute only; EBS and IPv4 are billed separately


class SpotPrice(Record):
    flavor: str = Field(min_length=1)
    zone: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    at: datetime


class Instance(Record):
    id: str
    launched: datetime
    state: Literal["pending", "running", "stopping", "stopped", "shutting-down", "terminated"]

    @classmethod
    def read(cls, node: ET.Element) -> Instance:
        try:
            return cls.model_validate(
                {
                    "id": node.findtext("instanceId"),
                    "launched": node.findtext("launchTime"),
                    "state": node.findtext("instanceState/name"),
                }
            )
        except ValidationError:
            raise ProviderUnavailable("EC2 returned an incomplete instance") from None


class Dimension(Record):
    usd: Decimal = Field(validation_alias=AliasPath("pricePerUnit", "USD"), gt=0)


class Term(Record):
    priceDimensions: dict[str, Dimension]


class Product(Record):
    flavor: str | None = Field(
        default=None, validation_alias=AliasPath("product", "attributes", "instanceType")
    )
    standard: dict[str, Term] = Field(validation_alias=AliasPath("terms", "OnDemand"))

    @property
    def rate(self) -> Decimal:
        rates = [
            dimension.usd
            for term in self.standard.values()
            for dimension in term.priceDimensions.values()
        ]
        if len(rates) != 1:
            raise ProviderUnavailable("AWS product has no single standard price")
        return rates[0]


class Prices(Record):
    PriceList: list[str]
    NextToken: str | None = None


class Quota(Record):
    name: str = Field(validation_alias=AliasPath("Quota", "QuotaName"))
    vcpus: Decimal = Field(validation_alias=AliasPath("Quota", "Value"), ge=0)


class Parameter(Record):
    value: str = Field(validation_alias=AliasPath("Parameter", "Value"))


def location(template: MachineTemplate, storage_region: str | None) -> tuple[str, str | None]:
    where = template.data_center or storage_region or ""
    match = re.fullmatch(r"([a-z]+(?:-[a-z]+)+-\d+)([a-z]?)", where)
    if match is None:
        return "", None
    return match[1], where if match[2] else None


class EC2API:
    def __init__(self, keys: Keys, region: str, send: Send) -> None:
        self.keys, self.region, self.send = keys, region, send

    def _post(
        self, service: str, region: str, host: str, body: bytes, headers: dict[str, str]
    ) -> HTTPReply:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        signed = Signer(self.keys, region).request(
            "POST", host, "", stamp, service, body, None, headers
        )
        return self.send("POST", signed.url, body, signed.headers)

    def _query(self, action: str, fields: dict[str, str]) -> ET.Element:
        body = urlencode({"Action": action, "Version": "2016-11-15", **fields}).encode()
        reply = self._post(
            "ec2",
            self.region,
            f"ec2.{self.region}.amazonaws.com",
            body,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            root = ET.fromstring(reply.body)
        except ET.ParseError:
            raise ProviderUnavailable(
                f"AWS {action}: unreadable HTTP {reply.status} reply"
            ) from None
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        if reply.status != 200:
            code = root.findtext(".//Error/Code") or f"HTTP {reply.status}"
            detail = sanitize(root.findtext(".//Error/Message") or "")[:300]
            if action == "RunInstances" and code in _REFUSALS:
                raise Refused(f"AWS {code}: {detail}")
            raise ProviderUnavailable(f"AWS {action}: {code}")
        return root

    def _json[T: Record](
        self, service: str, target: str, payload: dict[str, object], model: type[T]
    ) -> T:
        region = "us-east-1" if service == "pricing" else self.region
        endpoint = "api.pricing" if service == "pricing" else service
        reply = self._post(
            service,
            region,
            f"{endpoint}.{region}.amazonaws.com",
            json.dumps(payload).encode(),
            {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": target},
        )
        if reply.status != 200:
            code = reply.headers.get("x-amzn-errortype", "").split(":")[0]
            raise ProviderUnavailable(f"AWS {target}: {code or f'HTTP {reply.status}'}")
        try:
            return model.model_validate_json(reply.body)
        except ValidationError:
            raise ProviderUnavailable(f"AWS {target}: missing or invalid response fields") from None

    def launch(self, fields: dict[str, str]) -> Instance:
        instance = self._query("RunInstances", fields).find("instancesSet/item")
        if instance is None:
            raise ProviderUnavailable("EC2 launch returned no instance")
        return Instance.read(instance)

    def _items(self, action: str, fields: dict[str, str], path: str) -> Iterator[ET.Element]:
        while True:
            page = self._query(action, fields)
            if page.find(path.split("/", 1)[0]) is None:
                raise ProviderUnavailable(f"AWS {action} omitted its inventory")
            yield from page.findall(path)
            if not (token := page.findtext("nextToken")):
                return
            fields["NextToken"] = token

    def instances(self, session: str) -> list[Instance]:
        fields = {"Filter.1.Name": "tag:etude-session", "Filter.1.Value.1": session}
        return [
            Instance.read(i)
            for i in self._items(
                "DescribeInstances", fields, "reservationSet/item/instancesSet/item"
            )
        ]

    def terminate(self, ids: list[str]) -> None:
        self._query("TerminateInstances", {f"InstanceId.{n}": id for n, id in enumerate(ids, 1)})

    def volumes_remain(self, session: str) -> bool:
        fields = {"Filter.1.Name": "tag:etude-session", "Filter.1.Value.1": session}
        return next(self._items("DescribeVolumes", fields, "volumeSet/item"), None) is not None

    def image(self) -> str:
        return self._json("ssm", "AmazonSSM.GetParameter", {"Name": AMI_PARAMETER}, Parameter).value

    def _products(self, service: str, **attributes: str) -> Iterator[Product]:
        filters = [
            {"Type": "ANY_OF" if "," in v else "TERM_MATCH", "Field": k, "Value": v}
            for k, v in {"regionCode": self.region, **attributes}.items()
        ]
        payload: dict[str, object] = {"ServiceCode": service, "Filters": filters, "MaxResults": 100}
        while True:
            page = self._json("pricing", "AWSPriceListService.GetProducts", payload, Prices)
            for entry in page.PriceList:
                try:
                    yield Product.model_validate_json(entry)
                except ValidationError:
                    raise ProviderUnavailable(
                        "AWS returned an invalid price list product"
                    ) from None
            if not page.NextToken:
                return
            payload["NextToken"] = page.NextToken

    def _price(self, service: str, **attributes: str) -> Decimal:
        products = list(self._products(service, **attributes))
        if len(products) != 1:
            raise ProviderUnavailable("AWS price does not identify a single product")
        return products[0].rate

    def _spot_prices(self, flavors: list[str], zone: str | None) -> list[SpotPrice]:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = {f"InstanceType.{i}": name for i, name in enumerate(flavors, 1)}
        fields.update({"StartTime": now, "EndTime": now, "ProductDescription.1": "Linux/UNIX"})
        if zone:
            fields["AvailabilityZone"] = zone
        latest: dict[tuple[str, str], SpotPrice] = {}
        for node in self._items("DescribeSpotPriceHistory", fields, "spotPriceHistorySet/item"):
            try:
                price = SpotPrice.model_validate(
                    {
                        "flavor": node.findtext("instanceType"),
                        "zone": node.findtext("availabilityZone"),
                        "price": node.findtext("spotPrice"),
                        "at": node.findtext("timestamp"),
                    }
                )
            except ValidationError:
                raise ProviderUnavailable("EC2 returned an invalid spot price") from None
            key = (price.flavor, price.zone)
            if key not in latest or latest[key].at < price.at:
                latest[key] = price
        return list(latest.values())

    def offer(self, template: MachineTemplate, zone: str | None) -> EC2Offer:
        if not self.region:
            return EC2Offer.unavailable(
                "EC2 needs an AWS region or availability zone in --data-center"
            )
        if not template.gpu:
            return EC2Offer.unavailable("EC2 needs a GPU template")
        fields = {
            "Filter.1.Name": "supported-usage-class",
            "Filter.1.Value.1": "spot" if template.spot else "on-demand",
            "Filter.2.Name": "processor-info.supported-architecture",
            "Filter.2.Value.1": "x86_64",
            "Filter.3.Name": "instance-type",
            "Filter.3.Value.1": "g*",
            "Filter.3.Value.2": "p*",
            "MaxResults": "100",
        }
        names: set[str] = set()
        candidates: list[GPU] = []
        for item in self._items("DescribeInstanceTypes", fields, "instanceTypeSet/item"):
            gpu = item.find("gpuInfo/gpus/item")
            if gpu is None or gpu.findtext("manufacturer") != "NVIDIA":
                continue
            name = gpu.findtext("name") or ""
            names.add(name)
            count = int(gpu.findtext("count") or "0")
            cpus = int(item.findtext("vCpuInfo/defaultVCpus") or "0")
            memory = int(item.findtext("memoryInfo/sizeInMiB") or "0") // 1024
            if (
                name == template.gpu
                and count == template.gpu_count
                and cpus >= (template.vcpus or 0)
                and memory >= (template.memory_gb or 0)
            ):
                candidates.append(
                    GPU(
                        id=name,
                        flavor=item.findtext("instanceType"),
                        count=count,
                        min_vcpus=cpus,
                        min_memory_gb=memory,
                        disk_gb=75 + (template.disk_gb or 40),
                    )
                )
        if template.gpu not in names:
            return EC2Offer.unavailable(
                f"EC2 has no GPU named {template.gpu!r}; it names: {', '.join(sorted(names))}"
            )
        if zone:
            filters = {
                "LocationType": "availability-zone",
                "Filter.1.Name": "location",
                "Filter.1.Value.1": zone,
            }
            supported = {
                node.text
                for node in self._items(
                    "DescribeInstanceTypeOfferings",
                    filters,
                    "instanceTypeOfferingSet/item/instanceType",
                )
            }
            candidates = [gpu for gpu in candidates if gpu.flavor in supported]
        if not candidates:
            return EC2Offer.unavailable("EC2 has no matching GPU instance type in this location")
        flavors = {gpu.flavor: gpu for gpu in candidates if gpu.flavor}
        products = self._products(
            "AmazonEC2",
            instanceType=",".join(flavors),
            operatingSystem="Linux",
            tenancy="Shared",
            preInstalledSw="NA",
            capacitystatus="Used",
        )
        rates = {product.flavor: product.rate for product in products}
        if rates.keys() != flavors.keys():
            raise ProviderUnavailable("AWS prices do not cover the matching instance types")
        if template.spot:
            prices = self._spot_prices(list(flavors), zone)
            if not prices:
                return EC2Offer.unavailable(
                    "EC2 has no current spot price for the matching instance types"
                )

            def bid(p: SpotPrice) -> Decimal:
                # Never bid above the standard price of the same instance type.
                return min(rates[p.flavor], p.price * SPOT_HEADROOM)

            spot = min(prices, key=lambda p: (bid(p), p.price, p.zone, p.flavor))
            hardware, rate = flavors[spot.flavor], bid(spot)
            covered = sorted(
                (p for p in prices if p.flavor == spot.flavor and p.price <= rate),
                key=lambda p: (p.price, p.zone),
            )
            zones = tuple(p.zone for p in covered)
        else:
            hardware = min(candidates, key=lambda gpu: rates[gpu.flavor])
            rate = rates[hardware.flavor]
            zones = (zone,) if zone else ()
        if (hardware.flavor or "").startswith("p"):
            quota_code = "L-7212CCBC" if template.spot else "L-417A185B"
        else:
            quota_code = "L-3819A6DF" if template.spot else "L-DB2E81BA"
        quota = self._json(
            "servicequotas",
            "ServiceQuotasV20190624.GetServiceQuota",
            {"ServiceCode": "ec2", "QuotaCode": quota_code},
            Quota,
        )
        assert hardware.min_vcpus is not None
        if quota.vcpus < hardware.min_vcpus:
            return EC2Offer.unavailable(
                f"AWS quota {quota.name!r} is {quota.vcpus:g} vCPUs; {hardware.flavor} needs {hardware.min_vcpus}"
            )
        disk = self._price("AmazonEC2", volumeApiName="gp3", group="EBS Storage")
        address = self._price(
            "AmazonVPC", groupDescription="Hourly charge for In-use Public IPv4 Addresses"
        )
        return EC2Offer(
            zones=zones,
            max_price=rate if template.spot else None,
            hardware=hardware,
            available=True,
            usd_per_hour=(rate + address + disk * (hardware.disk_gb or 0) / 720).quantize(
                Decimal("0.0001"), rounding=ROUND_CEILING
            ),
        )
