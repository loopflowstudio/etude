"""Controlled Vast lifecycle, pagination, charge coverage and effect counts."""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from etude.cli import main
from etude.launch.client import _open
from etude.launch.database import Database
from etude.launch.errors import LauncherError, ProviderUnavailable
from etude.launch.executor import Executor, Refused
from etude.launch.machines import MachineRequest
from etude.launch.providers.bootstrap import guard
from etude.launch.providers.cleanup import command
from etude.launch.providers.vast.api import Create, Instance, VastAPI
from etude.launch.providers.vast.instance import GuardianAPI
from etude.launch.providers.vast.machines import OUTAGE_SECONDS, Outage, VastMachines
from etude.launch.records import (
    GPU,
    STOCK_IMAGE,
    BootstrapLinks,
    Job,
    MachineTemplate,
    OnDemand,
    Provider,
)
from etude.launch.records import Storage as Place
from etude.launch.scheduler import Scheduler
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys
from etude.launch.worker.courier import run as run_courier
from tests.launch.fake import Clock
from tests.launch.test_runpod_machines import Storage, released, repository, until

OFFER: dict[str, object] = {
    "id": 7,
    "gpu_name": "RTX 4090",
    "num_gpus": 1,
    "cpu_arch": "amd64",
    "cpu_cores_effective": 8,
    "cpu_ram": 65536,
    "gpu_frac": 0.5,
    "disk_space": 100,
    "dph_total": 0.6,
    "storage_total_cost": 0.1,
    "inet_up_cost": 0,
    "inet_down_cost": 0,
    "rentable": True,
    "rented": False,
    "verification": "verified",
    "geolocation": "Oregon, US",
}
INSTANCE: dict[str, object] = {
    "id": 12,
    "num_gpus": 1,
    "label": "etude-session",
    "actual_status": "running",
    "start_date": 1793770000,
    "dph_total": 0.6,
    "gpu_name": "RTX 4090",
    "cpu_cores_effective": 8,
    "cpu_ram": 65536,
    "gpu_frac": 0.5,
    "ssh_host": "fixture",
    "ssh_port": 2222,
    "env": {"SECRET": "fixture-not-retained"},
}
TEMPLATE = MachineTemplate(provider="vast", gpu="RTX 4090", vcpus=4, memory_gb=16, disk_gb=40)


class Marketplace:
    def __init__(self) -> None:
        self.offers: list[dict[str, object]] = [OFFER.copy()]
        self.instances: dict[int, dict[str, object]] = {}
        self.counts: Counter[str] = Counter()
        self.created: Create | None = None
        self.lost_reply = False
        self.inventory_visible = True
        self.offline = False
        self.delete_pending = False
        self.charge: dict[str, object] | None = None

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        self.counts[method] += 1
        if self.offline:
            raise OSError("fixture unavailable")
        path = urlsplit(url).path
        reply: object
        if path == "/api/v0/bundles/":
            reply = {"offers": self.offers}
        elif path == "/api/v0/asks/7/" and method == "PUT":
            assert body is not None
            self.created = Create.model_validate_json(body)
            self.instances[12] = {
                **INSTANCE,
                "label": self.created.label,
                "start_date": datetime.now(UTC).timestamp(),
            }
            if self.lost_reply:
                raise OSError("create reply lost")
            reply = {"success": True, "new_contract": 12}
        elif path == "/api/v1/instances/":
            # A full account can put the owned instance on a later page.
            query = parse_qs(urlsplit(url).query)
            rows = (
                list(self.instances.values())
                if "after_token" in query and self.inventory_visible
                else []
            )
            reply = {
                "instances": rows,
                "next_token": None if rows or "after_token" in query else "second",
            }
        elif path == "/api/v0/instances/12/":
            if method == "DELETE":
                if not self.delete_pending:
                    self.instances.pop(12, None)
                reply = {"success": True}
            else:
                reply = {"instances": self.instances.get(12)}
        elif path == "/api/v0/charges":
            query = parse_qs(urlsplit(url).query)
            days = json.loads(query["select_filters"][0])["day"]
            rows = []
            if self.charge is not None and days["gte"] <= self.charge["start"] <= days["lte"]:
                rows.append(self.charge)
            reply = {"results": rows, "next_token": None}
        else:
            raise AssertionError(f"unhandled fixture path: {method} {path}")
        return 200, json.dumps(reply).encode()


def machines(
    market: Marketplace, storage: Storage, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> VastMachines:
    bucket = Bucket(
        "journal", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), storage
    )
    return VastMachines(TEMPLATE, VastAPI("fixture-account-key", market), bucket, "test", now)


def request() -> MachineRequest:
    return MachineRequest(
        session="session",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        cap_usd=Decimal(1),
        bootstrap=BootstrapLinks(
            url="https://fixture/bootstrap",
            log_url="https://fixture/log",
            worker_log_url="https://fixture/worker",
        ),
    )


def test_offer_prices_disk_and_filters_incompatible_or_metered_capacity() -> None:
    market = Marketplace()
    market.offers = [
        {**OFFER, "id": 1, "dph_total": 0.1, "inet_up_cost": 0.2},
        {**OFFER, "id": 2, "cpu_ram": 15999},
        {**OFFER, "id": 3, "cpu_arch": "arm64"},
        {**OFFER, "id": 4, "rented": True},
        OFFER.copy(),
    ]
    machine = machines(market, Storage())
    offer = machine.offer()
    assert offer.available and offer.usd_per_hour == Decimal("0.6")
    assert isinstance(offer.hardware, GPU) and offer.hardware.id == "RTX 4090"
    assert market.counts == {"POST": 1}
    market.offers = []
    assert not machine.offer().available
    market.offline = True
    with pytest.raises(ProviderUnavailable):
        machine.offer()


def test_rental_keeps_identity_and_package_without_account_credentials() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.offer()
    allocation = machine.create(request())
    assert allocation.id == "12" and allocation.usd_per_hour == Decimal("0.6")
    assert isinstance(allocation.hardware, GPU) and allocation.hardware.id == "RTX 4090"
    assert market.counts == {"POST": 1, "PUT": 1, "GET": 1}
    assert len(storage.asked) == 5  # intent read, package read/write, intent and receipt writes
    created = market.created
    assert created is not None and created.image == STOCK_IMAGE.resolved and created.disk == 40
    assert created.cancel_unavail and created.runtype == "ssh_direct"
    assert len(created.onstart) < 4048
    assert "fixture-account-key" not in created.model_dump_json()
    assert created.env["ETUDE_DEADLINE"]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(created.env["ETUDE_PROGRAM"]))) as archive:
        assert "etude/launch/providers/vast/instance.py" in archive.namelist()
        assert "etude/launch/worker/main.py" not in archive.namelist()
    assert b"fixture-not-retained" not in storage.objects["test/sessions/session/receipt.json"]
    market.counts.clear()
    assert machine.find("session") == allocation
    assert market.counts == {}
    assert machine.create(request()) == allocation  # the intent prohibits duplicate acquisition
    assert market.counts == {}
    market.delete_pending = True
    assert machine.end("session") is None
    assert 12 in market.instances
    market.delete_pending = False
    assert machine.end("session") is not None and market.instances == {}
    assert market.counts == {"GET": 4, "DELETE": 2}
    assert machine.bill("session", datetime.now(UTC)) is None
    assert market.counts == {"GET": 4, "DELETE": 2}  # deletion is not settlement


def test_lost_reply_recovers_across_inventory_pages_without_another_create() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    market.lost_reply = True
    with pytest.raises(ProviderUnavailable):
        machine.create(request())
    assert 12 in market.instances
    fresh = machines(market, storage)
    allocation = fresh.create(request())
    assert allocation.id == "12"
    assert market.counts == {"POST": 1, "PUT": 1, "GET": 2}
    assert fresh.end("session") is not None
    assert market.instances == {}


def test_unconfirmed_create_and_foreign_labels_never_trigger_paid_retries_or_deletion() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    market.lost_reply = True
    with pytest.raises(ProviderUnavailable):
        machine.create(request())
    market.instances.clear()  # not yet visible is not evidence of refusal
    with pytest.raises(ProviderUnavailable, match="already attempted"):
        machine.create(request())
    assert market.counts["PUT"] == 1
    market.instances[12] = INSTANCE.copy()
    assert machine.find("session") is not None
    market.instances[12]["label"] = "someone-else"
    with pytest.raises(ProviderUnavailable, match="different label"):
        machine.end("session")
    assert market.counts["DELETE"] == 0 and 12 in market.instances


def _scheduler(
    store: Database, market: Marketplace, storage: Storage, clock: Callable[[], datetime]
) -> Scheduler:
    bucket = Bucket(
        "journal", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), storage
    )
    place = Place(
        name="evidence", bucket="journal", prefix="test", provider="s3", region="us-west-2"
    )
    executor = Executor(machines(market, storage), None, BucketJournal(place, bucket))
    scheduler = Scheduler(store, {"vast": lambda held: executor}, clock=clock)
    scheduler.add_storage(place)
    scheduler.set_provider(Provider(name="vast", kind="vast", registry="", storage="evidence"))
    scheduler.add_ondemand(
        OnDemand(name="4090", template=TEMPLATE, setup_seconds=60, max_machines=1)
    )
    return scheduler


@pytest.mark.parametrize("action", ["terminate", "deadline"])
def test_scheduler_retains_uncertain_acquisition_until_owned_deletion(
    tmp_path: Path, action: str
) -> None:
    market, storage, clock = Marketplace(), Storage(), Clock()
    clock.now = datetime.now(UTC)
    market.lost_reply = True
    market.inventory_visible = False
    store = Database(tmp_path / "launcher.db")
    try:
        scheduler = _scheduler(store, market, storage, clock)
        name = scheduler.rent("4090", hours=1)
        held = store.rented_session(name)
        rental = store.rental_for(held)
        assert rental is not None
        intent = storage.objects[f"test/sessions/{held.id}/offer.json"]
        assert 12 in market.instances and market.counts == {"POST": 1, "PUT": 1}

        # Rebuild the controller from retained evidence; process-local state cannot
        # prove ownership or preserve the reservation after a lost create reply.
        store.close()
        store = Database(tmp_path / "launcher.db")
        scheduler = _scheduler(store, market, storage, clock)
        clock.advance(60 if action == "terminate" else 3601)
        if action == "terminate":
            with pytest.raises(ProviderUnavailable, match="awaiting reconciliation"):
                scheduler.terminate(name)
        else:
            scheduler.tick()
            scheduler.tick()
        assert market.counts == {"POST": 1, "PUT": 1, "GET": 4}
        pending = store.rented_session(name)
        assert (
            pending.provider_id is None and pending.refused_at is None and pending.ended_at is None
        )
        assert [s.id for s in store.unsettled_sessions()] == [held.id]
        assert store.rental_for(pending) == rental
        assert scheduler.rentals()[0].usd > 0
        assert storage.objects[f"test/sessions/{held.id}/offer.json"] == intent
        with pytest.raises(LauncherError, match="at its machine limit"):
            scheduler.rent("4090", hours=1)
        assert market.counts["PUT"] == 1 and market.counts["DELETE"] == 0

        market.inventory_visible = True
        if action == "terminate":
            scheduler.terminate(name)
        else:
            scheduler.tick()  # recover the receipt
            scheduler.tick()  # enforce the elapsed deadline
        ended = store.rented_session(name)
        assert ended.id == held.id and ended.provider_id == "12"
        assert ended.ended_at is not None and ended.refused_at is None
        assert not store.unsettled_sessions() and market.instances == {}
        assert market.counts["PUT"] == 1 and market.counts["DELETE"] == 1
        assert scheduler.rentals()[0].usd > 0
    finally:
        store.close()


def test_scheduler_releases_confirmed_vast_refusal(tmp_path: Path) -> None:
    market, storage, clock = Marketplace(), Storage(), Clock()
    clock.now = datetime.now(UTC)
    market.offers = [{**OFFER, "end_date": clock.now.timestamp() + 5}]
    store = Database(tmp_path / "launcher.db")
    try:
        scheduler = _scheduler(store, market, storage, clock)
        refused = scheduler.rent("4090", hours=1)
        assert store.machine(refused).refused_at is not None
        assert not store.unsettled_sessions()
        assert scheduler.rentals()[0].usd == 0
        assert market.counts == {"POST": 1}
        market.offers = [OFFER.copy()]
        acquired = scheduler.rent("4090", hours=1)
        assert acquired != refused and store.rented_session(acquired).provider_id == "12"
        scheduler.terminate(acquired)
        assert market.instances == {} and market.counts["PUT"] == market.counts["DELETE"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("change", ["expired", "budget", "capacity", "lifetime"])
def test_refusal_has_no_paid_effect(change: str) -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    wanted = request()
    if change == "expired":
        wanted = wanted.model_copy(update={"deadline": datetime.now(UTC) - timedelta(seconds=1)})
    elif change == "budget":
        wanted = wanted.model_copy(update={"cap_usd": Decimal("0.0001")})
    elif change == "capacity":
        market.offers = []
    else:
        market.offers = [{**OFFER, "end_date": datetime.now(UTC).timestamp() + 5}]
    with pytest.raises(Refused):
        machine.create(wanted)
    assert machine.find(wanted.session) is None
    assert market.counts["PUT"] == 0 and market.instances == {}


def test_inventory_errors_and_duplicate_labels_are_not_absence() -> None:
    market = Marketplace()
    market.instances = {12: INSTANCE.copy(), 13: {**INSTANCE, "id": 13}}
    api = VastAPI("fixture", market)
    with pytest.raises(ProviderUnavailable, match="multiple"):
        api.named("etude-session")
    assert market.counts == {"GET": 2}
    market.offline = True
    with pytest.raises(ProviderUnavailable):
        api.named("etude-session")


@pytest.mark.parametrize("elapsed_days", [0, 1, 3])
def test_charges_include_creation_day_without_claiming_settlement(elapsed_days: int) -> None:
    market = Marketplace()
    api = VastAPI("fixture", market)
    instance = Instance.model_validate(INSTANCE)
    first = instance.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
    last = first + timedelta(days=elapsed_days)
    now = instance.created_at + timedelta(days=elapsed_days, hours=2)
    assert api.charges(instance, now) is None
    market.charge = {
        "type": "instance",
        "source": "instance-12",
        "amount": 0.168,
        "start": first.timestamp(),
        "end": last.timestamp(),
    }
    charges = api.charges(instance, now)
    assert charges is not None and charges.usd == Decimal("0.168")
    assert (charges.first_day, charges.last_day) == (first.date(), last.date())
    assert charges.observed_at == now
    # A revised amount on the same dates is still a report, not an exact interval.
    market.charge["amount"] = 0.190
    revised = api.charges(instance, now + timedelta(minutes=1))
    assert revised is not None and revised.usd == Decimal("0.190")
    assert market.counts == {"GET": 3}
    machine = machines(market, Storage())
    assert machine.bill("session", now) is None
    assert market.counts == {"GET": 3}  # no query can establish an absent watermark


@pytest.mark.parametrize("invalid_end", [1793750401, 1793664000, 1793836800])
def test_charges_reject_elapsed_timestamps_reversed_and_future_dates(invalid_end: int) -> None:
    market = Marketplace()
    market.charge = {
        "type": "instance",
        "source": "instance-12",
        "amount": 0.168,
        "start": 1793750400,
        "end": invalid_end,
    }
    with pytest.raises(ProviderUnavailable, match="charge date"):
        VastAPI("fixture", market).charges(
            Instance.model_validate(INSTANCE), datetime.fromtimestamp(1793777200, UTC)
        )
    assert market.counts == {"GET": 1}


def test_charges_exhaust_pages_and_reject_duplicate_contract_totals() -> None:
    calls = 0
    duplicate = False
    row = {
        "type": "instance",
        "source": "instance-12",
        "amount": 0.168,
        "start": 1793750400,
        "end": 1793750400,
    }

    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        later = "after_token" in parse_qs(urlsplit(url).query)
        rows = [row] if later or duplicate else [{**row, "source": "instance-99"}]
        return 200, json.dumps({"results": rows, "next_token": None if later else "next"}).encode()

    api = VastAPI("fixture", send)
    instance = Instance.model_validate(INSTANCE)
    now = datetime.fromtimestamp(1793777200, UTC)
    charges = api.charges(instance, now)
    assert charges is not None and charges.usd == Decimal("0.168")
    assert calls == 2
    duplicate = True
    with pytest.raises(ProviderUnavailable, match="duplicate"):
        api.charges(instance, now)
    assert calls == 4


def test_guardian_shortens_budget_and_deletes_after_caller_loss() -> None:
    market = Marketplace()
    market.instances[12] = INSTANCE.copy()
    api = GuardianAPI("fixture-instance-key", market)
    start = datetime.fromtimestamp(float(str(INSTANCE["start_date"])), UTC)
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def now() -> datetime:
        return start + timedelta(seconds=clock[0])

    guard(
        api,
        "12",
        start + timedelta(hours=1),
        Decimal("0.001"),
        sleep=sleep,
        now=now,
        monotonic=lambda: clock[0],
    )
    assert clock[0] == 6 and market.instances == {}
    assert market.counts == {"GET": 1, "DELETE": 1}


def test_configured_vast_reads_need_no_provider_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    scheduler = _open(tmp_path)
    scheduler.add_storage(
        Place(name="evidence", bucket="fixture", prefix="test", provider="s3", region="us-west-2")
    )
    scheduler.set_provider(Provider(name="vast", kind="vast", registry="", storage="evidence"))
    scheduler.add_ondemand(OnDemand(name="4090", template=TEMPLATE))
    assert scheduler.providers()[0].kind == "vast"
    assert scheduler.ondemands()[0].template == TEMPLATE
    scheduler._store.close()


def test_default_cleanup_uses_only_the_active_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.setenv("CONTAINER_ID", "12")
    monkeypatch.setenv("CONTAINER_API_KEY", "fixture")
    assert command() == [sys.executable, "-m", "etude.launch.providers.vast.instance", "delete"]


@pytest.mark.parametrize("cancel", [False, True])
def test_vast_cohort_uses_the_worker_and_retains_outputs_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    market, storage = Marketplace(), Storage()
    store = Database(tmp_path / "launcher.db")
    scheduler = _scheduler(store, market, storage, lambda: datetime.now(UTC))
    # Only the remote host boundary is replaced; the scheduler, journal, courier,
    # worker, job subprocess and output retention all use their production paths.
    worker: threading.Thread | None = None
    errors: list[Exception] = []

    def delete() -> None:
        market.instances.pop(12, None)

    def run_worker() -> None:
        assert market.created is not None
        try:
            run_courier(
                market.created.env["ETUDE_BOOTSTRAP_URL"],
                delete,
                root=tmp_path / "instance",
                send=storage,
                every=0.05,
            )
        except Exception as error:
            errors.append(error)

    job = Job(
        repo=str(repository(tmp_path)),
        command='test -f train.sh && echo retained > "$ETUDE_OUTPUT_DIR/result.txt"; '
        + ("sleep 30" if cancel else "echo finished"),
        max_hours=0.05,
        max_usd=Decimal(1),
        max_attempts=1,
    )
    runs = scheduler.launch_cohort("vast-cohort", job.runs(17), rent="4090", max_usd=Decimal(1))
    try:
        worker = threading.Thread(target=run_worker)
        worker.start()
        if cancel:
            until(scheduler, runs[0], "running")
            scheduler.cancel(runs[0])
        statuses = scheduler.wait(*runs, every=0.05, timeout=20)
        assert statuses[0].outcome == ("cancelled" if cancel else "succeeded")
        status = released(scheduler, runs[0])
        assert status.rental_ended
        assert market.instances == {} and market.counts["PUT"] == 1
        destination = scheduler.fetch(runs[0], "result.txt", tmp_path / "outputs")
        assert destination.read_text() == "retained\n"
        held = store.machine(scheduler.session(scheduler.attempts(runs[0])[0].session).machine)
        assert held.provider == "vast" and held.image == STOCK_IMAGE
    finally:
        scheduler.cancel(*runs)
        if worker is not None:
            worker.join(timeout=10)
            assert not worker.is_alive(), "fixture worker did not terminate"
        store.close()
    assert errors == []


def rented(market: Marketplace, storage: Storage, clock: Clock) -> VastMachines:
    market.instances[12] = INSTANCE.copy()
    machine = machines(market, storage, clock)
    machine._keep("session", Instance.model_validate(INSTANCE))
    return machine


@pytest.mark.parametrize("status", ["offline", "exited", "stopped", "error"])
def test_one_unhealthy_read_keeps_a_rental_and_its_disk(status: str) -> None:
    market, clock = Marketplace(), Clock()
    machine = rented(market, Storage(), clock)
    market.instances[12]["actual_status"] = status
    assert machine.alive("session")
    clock.advance(OUTAGE_SECONDS - 1)
    assert machine.alive("session")
    market.instances[12]["actual_status"] = "running"
    assert machine.alive("session")
    assert 12 in market.instances


def test_a_transient_missing_read_keeps_a_rental() -> None:
    market, clock = Marketplace(), Clock()
    machine = rented(market, Storage(), clock)
    held = market.instances.pop(12)
    assert machine.alive("session")
    market.instances[12] = held
    assert machine.alive("session")


def test_a_rental_is_lost_once_its_outage_lasts_and_retains_the_cause() -> None:
    market, clock, storage = Marketplace(), Clock(), Storage()
    machine = rented(market, storage, clock)
    market.instances[12]["actual_status"] = "offline"
    assert machine.alive("session")
    clock.advance(OUTAGE_SECONDS)
    assert not machine.alive("session")
    outage = Outage.model_validate_json(storage.objects["test/sessions/session/outage.json"])
    assert outage.status == "offline"
    assert outage.first_seen == clock.now - timedelta(seconds=OUTAGE_SECONDS)


def test_recovery_restarts_the_outage_allowance() -> None:
    market, clock = Marketplace(), Clock()
    machine = rented(market, Storage(), clock)
    market.instances[12]["actual_status"] = "offline"
    assert machine.alive("session")
    clock.advance(OUTAGE_SECONDS - 1)
    market.instances[12]["actual_status"] = "running"
    assert machine.alive("session")
    market.instances[12]["actual_status"] = "offline"
    clock.advance(OUTAGE_SECONDS - 1)
    assert machine.alive("session")
    clock.advance(1)
    assert machine.alive("session")


def test_a_destroyed_rental_is_lost_at_once() -> None:
    market, clock = Marketplace(), Clock()
    machine = rented(market, Storage(), clock)
    market.instances[12]["actual_status"] = "destroyed"
    assert not machine.alive("session")


def test_a_matching_label_without_an_etude_acquisition_is_not_owned() -> None:
    market = Marketplace()
    market.instances[12] = INSTANCE.copy()
    machine = machines(market, Storage())
    assert machine.find("session") is None
    assert machine.end("session") is None
    assert 12 in market.instances and market.counts == {}


@pytest.mark.parametrize("reply", [b"not-json", b'{"offers":null}', b'{"error":"fixture-secret"}'])
def test_invalid_offers_are_unknown_and_do_not_disclose_provider_bodies(reply: bytes) -> None:
    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        return 200, reply

    with pytest.raises(ProviderUnavailable) as error:
        VastAPI("fixture", send).offers(TEMPLATE)
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("resource", ["inventory", "charges"])
def test_pagination_requires_a_terminal_page_and_detects_nonadvancing_cursors(
    resource: str,
) -> None:
    calls = 0

    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        field = "instances" if resource == "inventory" else "results"
        return 200, json.dumps({field: [], "next_token": "repeated"}).encode()

    with pytest.raises(ProviderUnavailable, match="pagination"):
        api = VastAPI("fixture", send)
        if resource == "inventory":
            api.named("etude-session")
        else:
            api.charges(Instance.model_validate(INSTANCE), datetime.now(UTC))
    assert calls == 2


def test_quote_prices_the_requested_disk_allocation() -> None:
    def send(
        method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        search = json.loads(body or b"{}")
        disk = search["allocated_storage"]
        return 200, json.dumps(
            {
                "offers": [
                    {**OFFER, "storage_total_cost": disk * 0.0025, "dph_total": 0.5 + disk * 0.0025}
                ]
            }
        ).encode()

    api = VastAPI("fixture", send)
    assert api.offers(TEMPLATE)[0].dph_total == Decimal("0.6")
    larger = TEMPLATE.model_copy(update={"disk_gb": 80})
    assert api.offers(larger)[0].dph_total == Decimal("0.7")


def test_vast_cli_configuration_is_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.delenv("VAST_API_KEY", raising=False)
    assert (
        main(
            [
                "storage",
                "add",
                "evidence",
                "--bucket",
                "fixture",
                "--prefix",
                "test",
                "--provider",
                "s3",
                "--region",
                "us-west-2",
            ]
        )
        == 0
    )
    assert main(["provider", "set", "vast", "--kind", "vast", "--storage", "evidence"]) == 0
    assert (
        main(
            [
                "ondemand",
                "add",
                "4090",
                "--provider",
                "vast",
                "--gpu",
                "RTX 4090",
                "--max-usd-per-hour",
                "0.80",
            ]
        )
        == 0
    )
    assert "Vast" not in capsys.readouterr().err
    assert main(["providers"]) == 0
    assert "vast  vast  storage evidence" in capsys.readouterr().out


def test_memory_uses_vasts_allocated_mb_without_scaling_by_gpu_fraction_again() -> None:
    market = Marketplace()
    market.offers = [{**OFFER, "cpu_ram": 16000, "gpu_frac": 0.25}]
    offer = machines(market, Storage()).offer()
    assert offer.available
    assert isinstance(offer.hardware, GPU) and offer.hardware.min_memory_gb == 16


def test_equal_price_offer_with_sufficient_lifetime_is_selected() -> None:
    market = Marketplace()
    market.offers = [
        {**OFFER, "id": 6, "end_date": datetime.now(UTC).timestamp() + 1},
        OFFER.copy(),
    ]
    allocation = machines(market, Storage()).create(request())
    assert allocation.id == "12" and market.counts["PUT"] == 1


def test_vast_multi_gpu_offer_preserves_allocation_count() -> None:
    market = Marketplace()
    market.offers[0]["num_gpus"] = 2
    offer = VastAPI("fixture-account-key", market).offers(
        TEMPLATE.model_copy(update={"gpu_count": 2})
    )[0]
    assert offer.hardware.count == 2 and offer.hardware.min_vcpus == 8
    assert market.counts == {"POST": 1}
