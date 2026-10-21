"""The RunPod client against recorded reply shapes; nothing here reaches the network."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from etude.launch.database import Database
from etude.launch.machines import MachineRequest
from etude.launch.providers.runpod.api import PodFact, ProviderUnavailable, RunPodAPI
from etude.launch.providers.runpod.machines import _BOOT, STOCK_IMAGE, RunPodMachines
from etude.launch.records import CPU, GPU, BootstrapLinks, LauncherError, MachineTemplate
from etude.launch.records import Provider as Account
from etude.launch.storage.s3 import Bucket, Keys
from tests.launch.counting import Counting, Counts
from tests.launch.test_runpod_machines import Storage

POD = {
    "id": "abc123",
    "name": "etude-7f3a",
    "cost": 0.59,
    "createdAt": "2026-10-11T12:10:00+00:00",
    "status": "RUNNING",
    "env": {"SECRET": "never kept"},
    "ssh": {"direct": {"host": "127.0.0.1", "port": 22}},  # irrelevant to self-start
}


class Provider:
    """Replies by method and path, and remembers what it was asked."""

    def __init__(self, replies: dict[str, tuple[int, Any]]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, str, Any]] = []

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        assert headers["Authorization"] == "Bearer key"
        path = url.removeprefix("https://api.runpod.io/v2/")
        self.asked.append((method, path, None if body is None else json.loads(body)))
        reply = self.replies.get(f"{method} {path.split('?')[0]}")
        if reply is None:
            raise OSError("no route to host")
        status, value = reply
        return status, b"" if value is None else json.dumps(value).encode()


def test_a_quote_reads_price_and_positive_absence_of_stock() -> None:
    gpus = [
        {"id": "NVIDIA L4", "price": {"secure": 0.59}, "availability": "HIGH"},
        {"id": "NVIDIA A40", "price": {"secure": 0.4}, "availability": "NONE"},
    ]
    api = RunPodAPI("key", Provider(replies={"GET catalog/gpus": (200, {"gpus": gpus})}))
    l4 = api.quote(gpu="NVIDIA L4", vcpus=6, memory_gb=48)
    assert l4.usd_per_hour == Decimal("0.59") and l4.available
    assert l4.hardware == GPU(id="NVIDIA L4", min_vcpus=6, min_memory_gb=48)
    assert not api.quote(gpu="NVIDIA A40", vcpus=6, memory_gb=48).available
    with pytest.raises(ProviderUnavailable):
        api.quote(gpu="NVIDIA H100", vcpus=6, memory_gb=48)


def test_a_quote_for_one_data_center_counts_only_stock_there() -> None:
    places = [{"id": "EU-RO-1", "availability": "LOW"}, {"id": "US-TX-3", "availability": "NONE"}]
    l4 = {
        "id": "NVIDIA L4",
        "price": {"secure": 0.59},
        "availability": "LOW",
        "dataCenters": places,
    }
    api = RunPodAPI("key", Provider(replies={"GET catalog/gpus": (200, {"gpus": [l4]})}))
    for place, available in (("EU-RO-1", True), ("US-TX-3", False), ("EU-SE-1", False)):
        quote = api.quote(gpu="NVIDIA L4", vcpus=4, memory_gb=16, data_center=place)
        assert quote.available == available  # a place not listed has none


def cpu_flavor(
    id: str, per_vcpu: float, ram_per_vcpu: int, stock: dict[str, str]
) -> dict[str, Any]:
    return {
        "id": id,
        "price": {"securePerVcpu": per_vcpu},
        "ramGbPerVcpu": ram_per_vcpu,
        "vcpu": {"min": 2, "max": 32},
        "availability": "HIGH" if set(stock.values()) - {"NONE"} else "NONE",
        "dataCenters": [{"id": place, "availability": level} for place, level in stock.items()],
    }


def test_without_a_gpu_the_cheapest_fitting_cpu_pod_is_quoted() -> None:
    cpus = [
        cpu_flavor("cpu3c", 0.03, 2, {"US-KS-2": "HIGH", "EU-RO-1": "NONE"}),
        cpu_flavor("cpu3g", 0.04, 4, {"US-KS-2": "HIGH", "EU-RO-1": "LOW"}),
        cpu_flavor("cpu5c", 0.02, 2, {}),  # cheapest, but out of stock everywhere
    ]
    provider = Provider(replies={"GET catalog/cpus": (200, {"cpus": cpus})})
    api = RunPodAPI("key", provider)
    # 8 vCPUs with 16 GB fits the compute flavor exactly.
    quote = api.quote(gpu=None, vcpus=8, memory_gb=16)
    assert quote.hardware == CPU(flavor="cpu3c", vcpus=8) and quote.available
    assert quote.usd_per_hour == Decimal("0.24")
    # Sizes round up to a power of two, and grow until the memory fits.
    assert api.quote(gpu=None, vcpus=6, memory_gb=48).hardware == CPU(flavor="cpu3g", vcpus=16)
    assert api.quote(gpu=None, vcpus=4, memory_gb=16).hardware == CPU(flavor="cpu3g", vcpus=4)
    # Only stock in a named place counts.
    there = api.quote(gpu=None, vcpus=8, memory_gb=16, data_center="EU-RO-1")
    assert there.hardware == CPU(flavor="cpu3g", vcpus=8) and there.available
    nowhere = api.quote(gpu=None, vcpus=8, memory_gb=16, data_center="US-TX-3")
    assert not nowhere.available
    with pytest.raises(LauncherError, match="no RunPod CPU pod"):
        api.quote(gpu=None, vcpus=8, memory_gb=512)


def test_a_cpu_pod_is_asked_for_by_flavor_and_size_without_a_gpu() -> None:
    provider = Provider(replies={"POST pods": (200, {**POD, "cost": 0.24})})
    api = RunPodAPI("key", provider)
    api.create(
        name="etude-7f3a",
        hardware=CPU(flavor="cpu3c", vcpus=8),
        image="registry.example/etude@sha256:" + "a" * 64,
        registry="",
        env={},
        disk_gb=40,
        entrypoint=("python3.12", "-c", "boot"),
    )
    ((_, _, asked),) = provider.asked
    assert asked["cpu"] == {"id": "cpu3c", "vcpuCount": 8} and "gpu" not in asked


def test_create_sends_one_pinned_request_and_keeps_only_checked_facts() -> None:
    provider = Provider(replies={"POST pods": (200, POD)})
    api = RunPodAPI("key", provider)
    wanted: dict[str, Any] = {
        "name": "etude-7f3a",
        "hardware": GPU(id="NVIDIA L4", min_vcpus=6, min_memory_gb=48),
        "registry": "reg1",
        "env": {"ETUDE_SESSION": "etude-7f3a"},
        "disk_gb": 40,
        "entrypoint": ("python3.12", "-c", "boot"),
    }
    pod = api.create(image="registry.example/etude@sha256:" + "a" * 64, **wanted)
    assert pod == PodFact(
        id="abc123",
        name="etude-7f3a",
        usd_per_hour=Decimal("0.59"),
        created_at=datetime(2026, 10, 11, 12, 10, tzinfo=UTC),
        status="RUNNING",
        hardware=wanted["hardware"],
    )
    (asked,) = provider.asked
    assert asked[2]["gpu"] == {
        "id": "NVIDIA L4",
        "count": 1,
        "minRamPerGpu": 48,
        "minVcpuCountPerGpu": 6,
    }
    assert asked[2]["cloud"] == "SECURE" and asked[2]["ports"] == ["22/tcp"]
    assert asked[2]["startSsh"] is True
    with pytest.raises(ValueError, match="pinned by digest"):
        api.create(image="registry.example/etude:latest", **wanted)
    assert len(provider.asked) == 1


def test_a_failed_or_mismatched_create_is_unknown_not_refused() -> None:
    wanted: dict[str, Any] = {
        "name": "etude-7f3a",
        "hardware": GPU(id="NVIDIA L4", min_vcpus=6, min_memory_gb=48),
        "image": "registry.example/etude@sha256:" + "a" * 64,
        "registry": "reg1",
        "env": {},
        "disk_gb": 40,
        "entrypoint": ("python3.12", "-c", "boot"),
    }
    for replies in ({}, {"POST pods": (500, None)}, {"POST pods": (200, {**POD, "name": "other"})}):
        with pytest.raises(ProviderUnavailable):
            RunPodAPI("key", Provider(replies=replies)).create(**wanted)


def test_a_pod_is_found_by_name_across_pages_and_absence_needs_the_last_page() -> None:
    other = {**POD, "id": "zzz", "name": "etude-other"}
    one_page = {"pods": [other, POD], "pagination": {"hasNextPage": False, "nextCursor": None}}
    api = RunPodAPI("key", Provider(replies={"GET pods": (200, one_page)}))
    found = api.named("etude-7f3a")
    assert found is not None and found.id == "abc123"
    assert api.named("etude-none") is None
    endless = {"pods": [other], "pagination": {"hasNextPage": True, "nextCursor": "same"}}
    with pytest.raises(ProviderUnavailable, match="did not end"):
        RunPodAPI("key", Provider(replies={"GET pods": (200, endless)})).named("etude-none")
    with pytest.raises(ProviderUnavailable):
        RunPodAPI("key", Provider(replies={})).named("etude-7f3a")


def test_delete_dates_only_the_exact_live_pod() -> None:
    fact = RunPodAPI._pod(POD)
    live = Provider(replies={"GET pods/abc123": (200, POD), "DELETE pods/abc123": (200, None)})
    assert RunPodAPI("key", live).delete(fact).tzinfo is not None
    assert [method for method, _, _ in live.asked] == ["GET", "DELETE"]
    reused = Provider(
        replies={"GET pods/abc123": (200, {**POD, "createdAt": "2026-10-12T00:00:00+00:00"})}
    )
    with pytest.raises(ProviderUnavailable, match="different pod"):
        RunPodAPI("key", reused).delete(fact)
    gone = Provider(replies={"GET pods/abc123": (404, None)})
    with pytest.raises(ProviderUnavailable, match="cannot be dated"):
        RunPodAPI("key", gone).delete(fact)
    assert all(method == "GET" for method, _, _ in reused.asked + gone.asked)


def test_bills_are_whole_hours_for_this_pod_only() -> None:
    fact = RunPodAPI._pod(POD)
    now = datetime(2026, 10, 11, 14, 30, tzinfo=UTC)
    hour = {
        "podId": "abc123",
        "startTime": "2026-10-11T12:00:00+00:00",
        "endTime": "2026-10-11T13:00:00+00:00",
        "totalAmount": 0.49,
    }
    later = {**hour, "startTime": hour["endTime"], "endTime": "2026-10-11T14:00:00+00:00"}
    api = RunPodAPI(
        "key", Provider(replies={"GET billing/pods": (200, {"records": [later, hour]})})
    )
    assert [(b.hour.hour, b.usd) for b in api.bills(fact, now)] == [
        (12, Decimal("0.49")),
        (13, Decimal("0.49")),
    ]
    wrong = {"records": [{**hour, "podId": "someone-else"}]}
    with pytest.raises(ProviderUnavailable, match="incomplete"):
        RunPodAPI("key", Provider(replies={"GET billing/pods": (200, wrong)})).bills(fact, now)
    assert api.bills(fact, datetime(2026, 10, 11, 12, 50, tzinfo=UTC)) == ()


def test_stock_create_counts_package_reuse_without_waiting_for_ssh(tmp_path: Path) -> None:
    storage = Storage()
    provider = Provider(
        replies={
            "GET catalog/gpus": (200, {"gpus": [{"id": "NVIDIA L4", "price": {"secure": 0.59}}]}),
            "POST pods": (200, POD),
        }
    )
    counting = Counting(Database(tmp_path / "launcher.db"))
    bucket = Bucket(
        "journal",
        "us-west-2",
        Keys(access_key="fixture", secret_key="fixture"),
        counting.transport("bucket", storage),
    )
    api = RunPodAPI("key", counting.transport("provider", provider))
    account = Account(
        name="runpod", kind="runpod", registry="unused-private-registry", storage="west"
    )
    template = MachineTemplate(provider="runpod", gpu="NVIDIA L4")
    machines = RunPodMachines(account, template, api, bucket, "test")
    request = MachineRequest(
        session="7f3a",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        cap_usd=Decimal(1),
        bootstrap=BootstrapLinks(
            url="https://fixture/bootstrap",
            log_url="https://fixture/bootstrap.log",
            worker_log_url="https://fixture/worker.log",
        ),
    )
    with counting.record():
        machines.create(request)
        assert counting.snapshot() == Counts(provider=2, bucket=3)
    with counting.record():
        machines.create(request)
        assert counting.snapshot() == Counts(provider=2, bucket=1)
    # A new command also reuses the same digest object, after one existence read.
    with counting.record():
        RunPodMachines(account, template, api, bucket, "test").create(request)
        assert counting.snapshot() == Counts(provider=2, bucket=2)
    # Receipt lookup neither polls for an SSH address nor rewrites frozen facts.
    receipt_key = "test/sessions/7f3a/receipt.json"
    for address in (None, "ssh://root@127.0.0.1:22"):
        receipt = RunPodAPI._pod(POD).model_copy(update={"address": address})
        body = receipt.model_dump_json().encode()
        storage.objects[receipt_key] = body
        with counting.record():
            found = machines.find("7f3a")
            assert found is not None and found.usd_per_hour == receipt.usd_per_hour
            assert counting.snapshot() == Counts(bucket=1)
        assert storage.objects[receipt_key] == body
    # A lost create reply is still recovered by name and retained exactly once.
    del storage.objects[receipt_key]
    provider.replies["GET pods"] = (
        200,
        {"pods": [POD], "pagination": {"hasNextPage": False, "nextCursor": None}},
    )
    with counting.record():
        found = machines.find("7f3a")
        assert found is not None and found.id == POD["id"]
        assert counting.snapshot() == Counts(provider=1, bucket=2)
    payloads = [payload for method, path, payload in provider.asked if method == "POST"]
    assert len([key for key in storage.objects if "/programs/" in key]) == 1
    for payload in payloads:
        assert payload["image"] == STOCK_IMAGE and payload["registry"] == ""
        assert payload["ports"] == ["22/tcp"] and payload["startSsh"] is True
        assert "PUBLIC_KEY" not in payload["env"]  # RunPod injects the account's keys.
        assert payload["entrypoint"] == ["python3.12", "-c", _BOOT]
        assert len(_BOOT.encode()) < 512  # below the previously accepted small bootstrap
        assert len(base64.b64decode(payload["env"]["ETUDE_PROGRAM"])) < 16_000


def test_one_cpu_rental_quotes_creates_and_deletes_with_pinned_provider_calls(
    tmp_path: Path,
) -> None:
    cpu_pod = {**POD, "cost": 0.24}
    provider = Provider(
        replies={
            "GET catalog/cpus": (
                200,
                {"cpus": [cpu_flavor("cpu3c", 0.03, 2, {"US-KS-2": "HIGH"})]},
            ),
            "POST pods": (200, cpu_pod),
            "GET pods/abc123": (200, cpu_pod),
            "DELETE pods/abc123": (200, None),
        }
    )
    counting = Counting(Database(tmp_path / "launcher.db"))
    bucket = Bucket(
        "journal",
        "us-west-2",
        Keys(access_key="fixture", secret_key="fixture"),
        counting.transport("bucket", Storage()),
    )
    api = RunPodAPI("key", counting.transport("provider", provider))
    account = Account(name="runpod", kind="runpod", registry="unused", storage="west")
    template = MachineTemplate(provider="runpod", vcpus=8, memory_gb=16)
    machines = RunPodMachines(account, template, api, bucket, "test")
    request = MachineRequest(
        session="7f3a",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        cap_usd=Decimal(1),
        bootstrap=BootstrapLinks(
            url="https://fixture/bootstrap",
            log_url="https://fixture/bootstrap.log",
            worker_log_url="https://fixture/worker.log",
        ),
    )
    with counting.record():
        assert machines.price() == Decimal("0.24")
        assert counting.snapshot() == Counts(provider=1)
    with counting.record():
        allocation = machines.create(request)
        assert counting.snapshot() == Counts(provider=2, bucket=3)
    assert allocation.usd_per_hour == Decimal("0.24")
    assert allocation.hardware == CPU(flavor="cpu3c", vcpus=8)
    with counting.record():
        assert machines.end("7f3a") is not None
        assert counting.snapshot() == Counts(provider=3, bucket=1)
    (created,) = [payload for method, _, payload in provider.asked if method == "POST"]
    assert created["cpu"] == {"id": "cpu3c", "vcpuCount": 8} and "gpu" not in created
    assert created["disk"] == 40 and created["image"] == STOCK_IMAGE
