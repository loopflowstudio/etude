"""Lambda capacity, one paid launch across a lost reply, and confirmed cleanup."""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import pytest

from etude.launch.aws import Keys
from etude.launch.errors import ProviderUnavailable, Refused
from etude.launch.provisioner.machines import MachineRequest
from etude.launch.provisioner.providers.lambda_cloud.api import LambdaAPI
from etude.launch.provisioner.providers.lambda_cloud.machines import LambdaMachines
from etude.launch.records import BootstrapLinks, MachineTemplate
from etude.launch.storage.s3 import Bucket
from etude.launch.storage.transport import HTTPReply
from tests.launch.test_runpod_machines import Storage


class Workspace:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.state: str | None = None

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> HTTPReply:
        path = urlsplit(url).path.removeprefix("/api/v1/")
        self.calls[path] += 1
        data: object
        if path == "instance-types":
            data = {
                "gpu_1x_a10": {
                    "instance_type": {
                        "name": "gpu_1x_a10",
                        "price_cents_per_hour": 129,
                        "gpu_description": "A10 (24 GB PCIe)",
                        "specs": {"gpus": 1, "vcpus": 30, "memory_gib": 200, "storage_gib": 1400},
                    },
                    "regions_with_capacity_available": [{"name": "us-west-1"}],
                },
                "cpu_4x_general": {
                    "instance_type": {
                        "name": "cpu_4x_general",
                        "price_cents_per_hour": 10,
                        "gpu_description": "N/A",
                        "specs": {"gpus": 0, "vcpus": 4, "memory_gib": 16, "storage_gib": 100},
                    },
                    "regions_with_capacity_available": [{"name": "us-west-1"}],
                },
            }
        elif path == "ssh-keys":
            data = [{"name": "test"}]
        elif path == "instance-operations/launch":
            self.state = "active"
            raise OSError("reply lost")
        elif path == "instances/one":
            if self.state is None:
                return HTTPReply(404, b'{"error":{"code":"global/object-does-not-exist"}}')
            data = {"id": "one", "name": "renamed", "status": self.state}
        elif path == "instances":
            page = (
                {"data": [], "page_token": "next"}
                if not urlsplit(url).query
                else {
                    "data": [{"id": "one", "name": "etude-test", "status": self.state}]
                    if self.state
                    else [],
                    "page_token": None,
                }
            )
            return HTTPReply(200, json.dumps(page).encode())
        elif path == "instance-operations/terminate":
            assert body is not None
            if json.loads(body)["instance_ids"] == ["one"]:
                self.state = "terminating"
            data = {}
        else:
            raise AssertionError(path)
        return HTTPReply(200, json.dumps({"data": data}).encode())


def test_catalog_names_and_region_capacity_cost_one_read_each() -> None:
    workspace = Workspace()
    api = LambdaAPI("account", workspace)
    template = MachineTemplate(provider="lambda", gpu="A10")
    assert api.offer(template).reason == "Lambda has no GPU named 'A10'; it names: A10 (24 GB PCIe)"
    template = template.model_copy(update={"gpu": "A10 (24 GB PCIe)"})
    offer = api.offer(template)
    assert offer.available and offer.usd_per_hour == Decimal("1.29") and offer.region == "us-west-1"
    assert not api.offer(template.model_copy(update={"data_center": "elsewhere"})).available
    assert (
        api.offer(template.model_copy(update={"spot": True})).reason
        == "Etude rents no spot machines from Lambda"
    )
    assert workspace.calls == {"instance-types": 3}


def test_lost_launch_reply_recovers_by_name_and_waits_for_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LAMBDA_SSH_KEY_NAME", raising=False)
    workspace = Workspace()
    bucket = Bucket(
        "test", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), Storage()
    )
    machines = LambdaMachines(
        MachineTemplate(provider="lambda", gpu="A10 (24 GB PCIe)"),
        LambdaAPI("account", workspace),
        "guardian",
        bucket,
        "test",
    )
    request = MachineRequest(
        session="test",
        deadline=datetime.now(UTC) + timedelta(minutes=4),
        cap_usd=Decimal("1"),
        bootstrap=BootstrapLinks(url="read", log_url="log", worker_log_url="worker"),
    )
    with pytest.raises(Refused, match="exceeds the reserved budget"):
        machines.create(request.model_copy(update={"cap_usd": Decimal("0.01")}))
    assert machines.find("test") is None
    assert workspace.calls == {"instance-types": 1}
    with pytest.raises(ProviderUnavailable, match="reply unconfirmed"):
        machines.create(request)
    assert machines.find("test") == "one"
    allocation = machines.create(request)
    assert allocation.id == "one" and allocation.usd_per_hour == Decimal("1.29")
    # The account key can still end an acquired rental when the guardian credential is removed.
    machines = LambdaMachines(machines.template, machines.api, None, bucket, "test")
    assert machines.offer().reason == "LAMBDA_TERMINATE_KEY is not set"
    assert machines.end("test") is None
    assert workspace.state == "terminating"
    assert machines.end("test") is None
    workspace.state = None
    assert machines.end("test") is not None
    assert workspace.calls == {
        "instance-types": 2,
        "ssh-keys": 1,
        "instance-operations/launch": 1,
        "instances": 2,
        "instances/one": 3,
        "instance-operations/terminate": 1,
    }
