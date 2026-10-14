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
from etude.launch.records import MachineTemplate
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
    assert api.quote("NVIDIA L4").usd_per_hour == Decimal("0.59")
    assert api.quote("NVIDIA L4").available and not api.quote("NVIDIA A40").available
    with pytest.raises(ProviderUnavailable):
        api.quote("NVIDIA H100")


def test_a_quote_for_one_data_center_counts_only_stock_there() -> None:
    places = [{"id": "EU-RO-1", "availability": "LOW"}, {"id": "US-TX-3", "availability": "NONE"}]
    l4 = {
        "id": "NVIDIA L4",
        "price": {"secure": 0.59},
        "availability": "LOW",
        "dataCenters": places,
    }
    api = RunPodAPI("key", Provider(replies={"GET catalog/gpus": (200, {"gpus": [l4]})}))
    assert api.quote("NVIDIA L4", "EU-RO-1").available
    assert not api.quote("NVIDIA L4", "US-TX-3").available
    assert not api.quote("NVIDIA L4", "EU-SE-1").available  # not listed: none there


def test_create_sends_one_pinned_request_and_keeps_only_checked_facts() -> None:
    provider = Provider(replies={"POST pods": (200, POD)})
    api = RunPodAPI("key", provider)
    wanted: dict[str, Any] = {
        "name": "etude-7f3a",
        "gpu": "NVIDIA L4",
        "registry": "reg1",
        "env": {"ETUDE_SESSION": "etude-7f3a"},
        "vcpus": 6,
        "memory_gb": 48,
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
    )
    (asked,) = provider.asked
    assert asked[2]["gpu"] == {
        "id": "NVIDIA L4",
        "count": 1,
        "minRamPerGpu": 48,
        "minVcpuCountPerGpu": 6,
    }
    assert asked[2]["cloud"] == "SECURE" and asked[2]["ports"] == []
    with pytest.raises(ValueError, match="pinned by digest"):
        api.create(image="registry.example/etude:latest", **wanted)
    assert len(provider.asked) == 1


def test_a_failed_or_mismatched_create_is_unknown_not_refused() -> None:
    wanted: dict[str, Any] = {
        "name": "etude-7f3a",
        "gpu": "NVIDIA L4",
        "image": "registry.example/etude@sha256:" + "a" * 64,
        "registry": "reg1",
        "env": {},
        "vcpus": 6,
        "memory_gb": 48,
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


def test_stock_create_counts_package_reuse_without_ssh(tmp_path: Path) -> None:
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
        bootstrap="https://fixture/bootstrap",
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
        assert payload["ports"] == [] and "PUBLIC_KEY" not in payload["env"]
        assert payload["entrypoint"] == ["python3.12", "-c", _BOOT]
        assert len(_BOOT.encode()) < 512  # below the previously accepted small bootstrap
        assert len(base64.b64decode(payload["env"]["ETUDE_PROGRAM"])) < 16_000
