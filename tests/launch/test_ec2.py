"""EC2 pricing, idempotent acquisition and confirmed teardown over the signed transport."""

import base64
import hashlib
import json
import os
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from etude.cli import main
from etude.launch.aws import Keys
from etude.launch.database import upgrade
from etude.launch.provisioner.machines import MachineRequest, Refused
from etude.launch.provisioner.providers.ec2.api import EC2API
from etude.launch.provisioner.providers.ec2.host import user_data
from etude.launch.provisioner.providers.ec2.machines import EC2Machines
from etude.launch.records import BootstrapLinks, MachineTemplate
from etude.launch.storage.s3 import Bucket
from etude.launch.storage.transport import HTTPReply
from tests.launch.test_runpod_machines import Storage


def _product(rate: str, flavor: str | None = None) -> str:
    dimension = {"one": {"pricePerUnit": {"USD": rate}}}
    return json.dumps(
        {
            "product": {"attributes": {"instanceType": flavor}},
            "terms": {"OnDemand": {"standard": {"priceDimensions": dimension}}},
        }
    )


class Region:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.state: str | None = None
        self.volume = True
        self.image_exists = True
        self.quota = 4
        self.launches: dict[str, bytes] = {}
        self.boot = ""

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> HTTPReply:
        assert body
        target = headers.get("x-amz-target", "").split(".")[-1]
        instance = f"""<item><instanceId>i-l4</instanceId><launchTime>2026-11-17T00:00:00Z</launchTime>
        <instanceState><name>{self.state}</name></instanceState></item>"""
        if target:
            self.calls[target] += 1
            payload: object
            if target == "GetParameter":
                if not self.image_exists:
                    return HTTPReply(400, b"{}", {"x-amzn-errortype": "ParameterNotFound"})
                payload = {"Parameter": {"Value": "ami-gpu"}}
            elif target == "GetServiceQuota":
                payload = {
                    "Quota": {
                        "QuotaName": "Running On-Demand G and VT instances",
                        "Value": self.quota,
                    }
                }
            else:
                filters = {f["Field"]: f["Value"] for f in json.loads(body)["Filters"]}
                if "instanceType" in filters:
                    rates = {"g6.2xlarge": "0.9776", "g6.xlarge": "0.8048"}
                    products = [
                        _product(rates[name], name) for name in filters["instanceType"].split(",")
                    ]
                else:
                    products = [_product("0.08" if "volumeApiName" in filters else "0.005")]
                payload = {"PriceList": products}
            return HTTPReply(200, json.dumps(payload).encode())
        fields = parse_qs(body.decode())
        action = fields["Action"][0]
        self.calls[action] += 1
        if action == "DescribeInstanceTypes":
            items = [
                f"""<item><instanceType>{name}</instanceType>
                <gpuInfo><gpus><item><manufacturer>NVIDIA</manufacturer><name>L4</name><count>1</count></item></gpus></gpuInfo>
                <vCpuInfo><defaultVCpus>{cpus}</defaultVCpus></vCpuInfo>
                <memoryInfo><sizeInMiB>{cpus * 4096}</sizeInMiB></memoryInfo></item>"""
                for name, cpus in (("g6.2xlarge", 8), ("g6.xlarge", 4))
            ]
            xml = "<instanceTypeSet>" + "".join(items) + "</instanceTypeSet>"
        elif action == "RunInstances":
            token = fields["ClientToken"][0]
            if token not in self.launches:
                self.launches[token] = body
                self.state = "running"
                self.boot = base64.b64decode(fields["UserData"][0]).decode()
                raise OSError("launch reply lost")
            if self.launches[token] != body:
                raise AssertionError("same token submitted with different EC2 launch parameters")
            xml = f"<instancesSet>{instance}</instancesSet>"
        elif action == "TerminateInstances":
            self.state = "shutting-down"
            xml = ""
        elif action == "DescribeVolumes":
            xml = "<volumeSet><item/></volumeSet>" if self.volume else "<volumeSet/>"
        elif action == "DescribeInstances":
            xml = (
                f"<reservationSet><item><instancesSet>{instance}</instancesSet></item></reservationSet>"
                if self.state
                else "<reservationSet/>"
            )
        else:
            raise AssertionError(action)
        return HTTPReply(200, f'<response xmlns="urn:ec2">{xml}</response>'.encode())


def test_availability_quotes_a_gpu_and_explains_quota_without_creating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "fixture")
    upgrade(tmp_path, development=True)
    region = Region()
    monkeypatch.setattr("etude.launch.client._send", region)
    assert (
        main(
            [
                "storage",
                "add",
                "west",
                "--bucket",
                "test",
                "--prefix",
                "etude",
                "--provider",
                "s3",
                "--region",
                "us-west-2",
            ]
        )
        == 0
    )
    assert main(["provider", "set", "aws", "--kind", "ec2", "--storage", "west"]) == 0
    assert main(["ondemand", "add", "l4", "--gpu", "L4", "--provider", "aws", "--max", "1"]) == 0
    assert main(["availability", "l4"]) == 0
    assert "g6.xlarge: L4 with at least 4 vCPUs, 16 GB at $0.8226/hour" in capsys.readouterr().out
    assert region.calls == {"DescribeInstanceTypes": 1, "GetProducts": 3, "GetServiceQuota": 1}
    region.quota = 0
    assert main(["availability", "l4"]) == 0
    line = capsys.readouterr().out
    assert "none available: AWS quota 'Running On-Demand G and VT instances' is 0 vCPUs" in line
    assert "not rentable now" in line
    assert region.calls["RunInstances"] == 0


def test_unknown_gpu_lists_catalog_names_and_missing_region_needs_no_read() -> None:
    region = Region()
    keys = Keys(access_key="fixture", secret_key="fixture")
    template = MachineTemplate(provider="aws", gpu="NVIDIA L4")
    answer = EC2API(keys, "", region).offer(template, None)
    assert not answer.available and answer.reason and "--data-center" in answer.reason
    assert not region.calls
    answer = EC2API(keys, "us-west-2", region).offer(template, None)
    assert not answer.available and answer.hardware is None
    assert answer.reason == "EC2 has no GPU named 'NVIDIA L4'; it names: L4"
    assert region.calls == {"DescribeInstanceTypes": 1}


def test_retry_recovers_one_rental_and_waits_for_guest_shutdown() -> None:
    region, storage = Region(), Storage()
    keys = Keys(access_key="fixture", secret_key="fixture")
    api = EC2API(keys, "us-west-2", region)
    bucket = Bucket("test", "us-west-2", keys, storage)
    template = MachineTemplate(provider="aws", gpu="L4")
    machine = EC2Machines(template, api, None, bucket, "test")
    request = MachineRequest(
        session="one",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        cap_usd=Decimal("1"),
        bootstrap=BootstrapLinks(url="read", log_url="log", worker_log_url="worker"),
    )
    allocation = machine.create(request)
    assert allocation.id == "i-l4"
    assert region.calls["RunInstances"] == 2 and len(region.launches) == 1
    assert len(region.boot.encode()) < 16384
    (boot,) = [v for k, v in storage.objects.items() if "/boot/" in k]
    assert b"--gpus all" in boot and b"providers.watchdog" in boot
    # A process crash between EC2 accepting the launch and receipt publication
    # leaves only the intent and the tagged instance.
    del storage.objects["test/sessions/one/receipt.json"]
    recovered = EC2Machines(template, api, None, bucket, "test")
    assert recovered.create(request) == allocation
    assert region.calls["RunInstances"] == 2
    assert recovered.alive("one")
    assert recovered.end("one") is None
    assert recovered.end("one") is None
    region.state = "terminated"
    assert not recovered.alive("one")
    assert recovered.end("one") is None
    region.volume = False
    assert recovered.end("one") is not None
    assert region.calls["TerminateInstances"] == 1


def test_missing_host_image_refuses_before_acquisition() -> None:
    region = Region()
    region.image_exists = False
    keys = Keys(access_key="fixture", secret_key="fixture")
    machine = EC2Machines(
        MachineTemplate(provider="aws", gpu="L4"),
        EC2API(keys, "us-west-2", region),
        None,
        Bucket("test", "us-west-2", keys, Storage()),
        "test",
    )
    request = MachineRequest(
        session="one",
        deadline=datetime.now(UTC) + timedelta(hours=1),
        cap_usd=Decimal("1"),
        bootstrap=BootstrapLinks(url="read", log_url="log", worker_log_url="worker"),
    )
    with pytest.raises(Refused, match="ParameterNotFound"):
        machine.create(request)
    assert machine.find("one") is None
    assert region.calls["RunInstances"] == 0


@pytest.mark.parametrize(
    ("seconds", "intact", "started"), [(60, True, True), (60, False, False), (-1, True, False)]
)
def test_host_powers_off_after_work_or_failed_boot(
    tmp_path: Path, seconds: int, intact: bool, started: bool
) -> None:
    payload = b'test -e "$PROOF/armed" && touch "$PROOF/started"\n'
    (tmp_path / "payload").write_bytes(payload)
    # Every host effect is redirected into this directory. The timer and
    # payload run as separate commands, as they do under cloud-init.
    for name, command in {
        "date": "echo 1700000000",
        "systemd-run": 'for arg do case "$arg" in --on-active=*) echo "$arg" > "$PROOF/armed";; esac; done',
        "systemctl": 'test "$*" = "--no-block poweroff" && touch "$PROOF/off"',
        "mktemp": 'echo "$PROOF"',
        "curl": 'cp "$PROOF/payload" boot',
    }.items():
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + command + "\n")
        path.chmod(0o700)
    digest = hashlib.sha256(payload if intact else b"changed").hexdigest()
    script = user_data(
        datetime.fromtimestamp(1700000000 + seconds, UTC), "https://signed.invalid/boot", digest
    )
    env = {**os.environ, "PROOF": str(tmp_path), "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["/bin/bash"],
        input=script,
        text=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=5,
    )
    assert (result.returncode == 0) == intact
    assert (tmp_path / "off").exists()
    if seconds > 0:
        assert (tmp_path / "armed").read_text().strip() == f"--on-active={seconds}s"
    assert (tmp_path / "started").exists() == started
    assert (tmp_path / "boot").exists() == (seconds > 0)
