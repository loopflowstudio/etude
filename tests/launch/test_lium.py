"""Controlled Lium inventory, rental recovery, timers, accounting and effect counts."""

from __future__ import annotations

import io
import json
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import _open
from etude.launch.database import Database, launcher_file
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Execution, Executor, Refused
from etude.launch.machines import MachineRequest
from etude.launch.providers import bootstrap as bootstrap_module
from etude.launch.providers.bootstrap import guard
from etude.launch.providers.lium.api import Create, LiumAPI, Pod
from etude.launch.providers.lium.machines import _START, LiumMachines
from etude.launch.providers.lium.pod import Watchdog
from etude.launch.providers.lium.pod import main as pod_main
from etude.launch.records import GPU, BootstrapLinks, Machine, MachineTemplate, OnDemand, Provider
from etude.launch.records import Storage as Place
from etude.launch.scheduler import Scheduler
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys
from etude.launch.storage.transport import HTTPReply
from etude.launch.worker.courier import run as run_courier
from etude.spec import Job
from tests.launch.test_runpod_machines import Storage, released, repository, until

NODE = "89044090-3d31-4c5a-a388-0d0a30aef107"
POD = "34044090-3d31-4c5a-a388-0d0a30aef107"
TEMPLATE = MachineTemplate(provider="lium", gpu="RTX 5090", gpu_count=2)


class Marketplace:
    def __init__(self) -> None:
        self.nodes: list[dict[str, object]] = [
            {
                "id": NODE,
                "gpu_model": "RTX 5090",
                "gpu_count": 8,
                "available_gpu_count": 4,
                "available_now": True,
                "min_rentable_gpu_count": 1,
                "price_per_gpu_hour": 0.75,
                "tier": "secure",
                "country_code": "US",
                "cpu_count": 128,
                "ram_gb": 512,
            }
        ]
        self.quote: dict[str, object] = {
            "executor_id": NODE,
            "gpu_count": 2,
            "hours": 1,
            "price_per_hour": 1.5,
            "total_usd": 1.5,
            "allocation": {
                "cpu_count": 32,
                "ram_gb": 126,
                "local_volume_gb": 100,
                "storage_gb": 50,
            },
            "lines": [
                {"item": "compute", "quantity": 1, "amount_usd": 1.5},
                {"item": "volume", "quantity": 0, "amount_usd": 0},
                {"item": "backup", "quantity": 0, "amount_usd": 0},
                {"item": "egress", "quantity": 0, "amount_usd": None},
            ],
        }
        self.pod: dict[str, object] | None = None
        self.created: Create | None = None
        self.counts: Counter[str] = Counter()
        self.lost_reply = False
        self.visible = True
        self.acknowledge_timer = True
        self.delete_pending = False
        self.statement: dict[str, object] | None = None

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> HTTPReply:
        path = urlsplit(url).path.removeprefix("/api/")
        self.counts[f"{method} {path}"] += 1
        reply: object
        if path == "public/v1/nodes":
            reply = {
                "nodes": self.nodes,
                "count": len(self.nodes),
                "generated_at": datetime.now(UTC).isoformat(),
            }
        elif path == f"executors/{NODE}/quote":
            reply = self.quote
        elif path == f"executors/{NODE}/rent":
            assert body is not None
            self.created = Create.model_validate_json(body)
            self.pod = {
                "id": POD,
                "executor_id": NODE,
                "pod_name": self.created.pod_name,
                "status": "PENDING",
                "created_at": datetime.now(UTC).isoformat(),
                "price": 1.5,
                "gpu_count": "2",
                "ssh_connect_cmd": "",
                "removal_scheduled_at": None,
            }
            if self.lost_reply:
                raise OSError("fixture lost create reply")
            reply = {"pod_id": POD}
        elif path == "pods":
            reply = [self.pod] if self.pod and self.visible else []
        elif path == f"pods/{POD}/schedule-removal":
            assert self.pod is not None and body is not None
            if self.acknowledge_timer:
                self.pod["removal_scheduled_at"] = json.loads(body)["removal_scheduled_at"]
            reply = {}
        elif path == f"pods/{POD}/statement":
            if self.statement is None:
                return HTTPReply(404, b"")
            reply = self.statement
        elif path == f"pods/{POD}":
            if method == "DELETE":
                if not self.delete_pending:
                    self.pod = None
                reply = {}
            elif self.pod is None or not self.visible:
                return HTTPReply(404, b"")
            else:
                reply = self.pod
        else:
            raise AssertionError(f"unhandled fixture path: {method} {path}")
        return HTTPReply(200, json.dumps(reply).encode())


def machines(market: Marketplace, storage: Storage) -> LiumMachines:
    bucket = Bucket(
        "journal", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), storage
    )
    return LiumMachines(TEMPLATE, LiumAPI("fixture-account-key", market), bucket, "test")


def request() -> MachineRequest:
    return MachineRequest(
        session="session",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        cap_usd=Decimal("1.52"),
        bootstrap=BootstrapLinks(
            url="https://fixture/bootstrap",
            log_url="https://fixture/log",
            worker_log_url="https://fixture/worker",
        ),
    )


@pytest.fixture(autouse=True)
def public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIUM_SSH_PUBLIC_KEY", "ssh-ed25519 Zml4dHVyZQ==")


def test_quote_counts_gpus_and_only_allocated_resources() -> None:
    market = Marketplace()
    offer = machines(market, Storage()).offer()
    assert offer.available and offer.usd_per_hour == Decimal("1.52")
    assert isinstance(offer.hardware, GPU)
    assert offer.hardware.count == 2
    assert offer.hardware.min_vcpus == 32 and offer.hardware.min_memory_gb == 126
    assert market.counts == {"GET public/v1/nodes": 1, f"GET executors/{NODE}/quote": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"tier": "spot"},
        {"available_gpu_count": 1},
        {"min_rentable_gpu_count": 4},
        {"available_now": False},
    ],
)
def test_explicitly_unavailable_inventory_is_empty(change: dict[str, object]) -> None:
    market = Marketplace()
    market.nodes[0].update(change)
    assert not machines(market, Storage()).offer().available
    assert market.counts == {"GET public/v1/nodes": 1}


@pytest.mark.parametrize(
    "field", ["tier", "gpu_model", "available_gpu_count", "price_per_gpu_hour"]
)
def test_unknown_inventory_is_not_empty_stock(field: str) -> None:
    market = Marketplace()
    market.nodes[0].pop(field)
    with pytest.raises(ProviderUnavailable):
        machines(market, Storage()).offer()


def test_missing_allocation_and_unquoted_storage_are_unknown() -> None:
    market = Marketplace()
    market.quote["allocation"] = None
    with pytest.raises(ProviderUnavailable):
        machines(market, Storage()).offer()
    market = Marketplace()
    market.quote["total_usd"] = 2
    with pytest.raises(ProviderUnavailable):
        machines(market, Storage()).offer()


def test_create_revalidates_price_and_reserves_whole_hour() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.offer()
    market.nodes[0]["price_per_gpu_hour"] = 1
    market.quote.update(price_per_hour=2, total_usd=2)
    market.quote["lines"] = [
        {"item": "compute", "quantity": 1, "amount_usd": 2},
        {"item": "volume", "quantity": 0, "amount_usd": 0},
        {"item": "backup", "quantity": 0, "amount_usd": 0},
        {"item": "egress", "quantity": 0, "amount_usd": None},
    ]
    with pytest.raises(Refused, match="budget"):
        machine.create(request())
    assert market.created is None
    assert market.counts == {"GET public/v1/nodes": 2, f"GET executors/{NODE}/quote": 2}


def test_create_acknowledges_timer_and_keeps_secrets_off_provider() -> None:
    market, storage = Marketplace(), Storage()
    wanted = request()
    allocation = machines(market, storage).create(wanted)
    assert allocation.id == POD and allocation.hardware is not None
    assert market.created is not None and market.pod is not None
    assert market.created.termination_hours == 1 and not market.created.enable_backups
    assert wanted.deadline is not None
    assert Pod.model_validate(market.pod).removal_scheduled_at == wanted.deadline
    assert "bootstrap" not in market.created.model_dump_json()
    assert "fixture-account-key" not in b"".join(storage.objects.values()).decode(errors="ignore")
    assert market.counts == {
        "GET public/v1/nodes": 1,
        f"GET executors/{NODE}/quote": 1,
        f"POST executors/{NODE}/rent": 1,
        f"GET pods/{POD}": 2,
        f"POST pods/{POD}/schedule-removal": 1,
    }


def test_lost_create_recovers_without_second_paid_request() -> None:
    market, storage = Marketplace(), Storage()
    market.lost_reply = True
    with pytest.raises(ProviderUnavailable):
        machines(market, storage).create(request())
    market.visible = False
    with pytest.raises(ProviderUnavailable, match="awaiting reconciliation"):
        machines(market, storage).find("session")
    market.visible = True
    found = machines(market, storage).find("session")
    assert found is not None and found.id == POD
    assert machines(market, storage).create(request()).id == POD
    assert market.pod is not None and market.pod["removal_scheduled_at"] is not None
    assert market.counts[f"POST executors/{NODE}/rent"] == 1
    assert market.counts["GET pods"] == 2


def test_unacknowledged_timer_is_retried_before_work() -> None:
    market, storage = Marketplace(), Storage()
    market.acknowledge_timer = False
    with pytest.raises(ProviderUnavailable, match="acknowledge"):
        machines(market, storage).create(request())
    assert "test/sessions/session/booted.json" not in storage.objects
    market.acknowledge_timer = True
    assert machines(market, storage).find("session") is not None
    assert market.counts[f"POST pods/{POD}/schedule-removal"] == 2
    assert market.counts[f"POST executors/{NODE}/rent"] == 1


def test_ssh_bootstrap_starts_once_after_running(monkeypatch: pytest.MonkeyPatch) -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.create(request())
    assert market.pod is not None
    market.pod.update(status="RUNNING", ssh_connect_cmd="ssh root@192.0.2.1 -p 2200")
    environments: list[dict[str, str]] = []

    def start(pod: Pod, env: dict[str, str]) -> None:
        environments.append(env)

    monkeypatch.setattr("etude.launch.providers.lium.machines._start", start)
    machine.find("session")
    machines(market, storage).find("session")
    assert len(environments) == 1
    assert environments[0]["ETUDE_BOOTSTRAP_URL"] == "https://fixture/bootstrap"
    assert not any("KEY" in name for name in environments[0])
    assert market.counts[f"POST pods/{POD}/schedule-removal"] == 1


def test_delete_requires_absence_and_retries_pending_removal() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.create(request())
    market.delete_pending = True
    assert machine.end("session") is None
    market.delete_pending = False
    assert machine.end("session") is not None
    assert market.pod is None and market.counts[f"DELETE pods/{POD}"] == 2


def test_ledger_survives_deletion_without_inventing_settlement() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.create(request())
    assert market.pod is not None
    start = market.pod["created_at"]
    now = datetime.now(UTC)
    market.statement = {
        "pod_id": POD,
        "created_at": start,
        "removed_at": now.isoformat(),
        "removed": True,
        "billed_until": now.isoformat(),
        "total": 0.03,
        "days": [{"amount": 0.03, "billed_until": now.isoformat()}],
    }
    machine.end("session")
    bill = machine.bill("session", now)
    assert bill is not None and bill.usd == Decimal("0.03") and bill.through == now
    market.statement["total"] = 0.04
    with pytest.raises(ProviderUnavailable, match="coverage"):
        machine.bill("session", now)
    market.statement["billed_until"] = None
    assert machine.bill("session", now) is None


def test_provider_ssh_command_cannot_add_options() -> None:
    market = Marketplace()
    machine = machines(market, Storage())
    machine.create(request())
    assert market.pod is not None
    for command in ("ssh -oProxyCommand=touch root@192.0.2.1 -p 22", "ssh evil@192.0.2.1 -p 22"):
        with pytest.raises(ProviderUnavailable, match="SSH address"):
            Pod.model_validate({**market.pod, "ssh_connect_cmd": command}).address
    assert UUID(POD) == Pod.model_validate(market.pod).id


def test_offline_configuration_and_safeguards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    scheduler = _open(tmp_path)
    try:
        scheduler.add_storage(
            Place(
                name="evidence", bucket="fixture", prefix="test", provider="s3", region="us-west-2"
            )
        )
        scheduler.set_provider(Provider(name="lium", kind="lium", registry="", storage="evidence"))
        scheduler.add_ondemand(OnDemand(name="5090", template=TEMPLATE))
        assert scheduler.providers()[0].kind == "lium"
        assert scheduler.ondemands()[0].template.gpu_count == 2
        assert scheduler.safeguards("5090") == ("provider lifetime", "launcher cleanup")
    finally:
        scheduler._store.close()


def _scheduler(store: Database, market: Marketplace, storage: Storage) -> Scheduler:
    bucket = Bucket(
        "journal", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), storage
    )
    place = Place(
        name="evidence", bucket="journal", prefix="test", provider="s3", region="us-west-2"
    )
    executor = Executor(machines(market, storage), None, BucketJournal(place, bucket))

    def compose(held: Machine | OnDemand) -> Execution:
        return executor

    compose.safeguards = LiumMachines.safeguards  # type: ignore[attr-defined]
    scheduler = Scheduler(store, {"lium": compose})
    scheduler.add_storage(place)
    scheduler.set_provider(Provider(name="lium", kind="lium", registry="", storage="evidence"))
    scheduler.add_ondemand(OnDemand(name="5090", template=TEMPLATE, setup_seconds=0))
    return scheduler


def test_public_rental_budget_covers_fallback_timer(tmp_path: Path) -> None:
    market, storage = Marketplace(), Storage()
    store = Database(launcher_file(tmp_path))
    try:
        scheduler = _scheduler(store, market, storage)
        with pytest.raises(LauncherError):
            scheduler.rent("5090", hours=0.1, max_usd=Decimal("1.51"))
        assert market.created is None
        quoted = scheduler.quote(
            Job(command="true", max_hours=0.1, max_usd=Decimal(2)).runs(1), rent="5090"
        )
        assert quoted.machines[0].max_usd == Decimal("1.52")
        name = scheduler.rent("5090", hours=0.1, max_usd=Decimal(2))
        rental = store.rental_for(store.rented_session(name))
        assert rental is not None and rental.cap_usd == Decimal("1.52")
        scheduler.terminate(name)
        assert market.pod is None
        assert market.counts[f"POST executors/{NODE}/rent"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_lium_executes_and_retains_outputs_before_controller_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    market, storage = Marketplace(), Storage()
    store = Database(launcher_file(tmp_path))
    scheduler = _scheduler(store, market, storage)
    environments: list[dict[str, str]] = []

    def start(pod: Pod, env: dict[str, str]) -> None:
        environments.append(env)

    monkeypatch.setattr("etude.launch.providers.lium.machines._start", start)
    errors: list[Exception] = []

    def leave_deletion_to_controller() -> None:
        pass

    def worker() -> None:
        try:
            run_courier(
                environments[0]["ETUDE_BOOTSTRAP_URL"],
                leave_deletion_to_controller,
                root=tmp_path / "pod",
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
        max_usd=Decimal(2),
        max_attempts=1,
    )
    runs = scheduler.launch_cohort(
        "lium-cohort", job.runs(17) if cancel else job.runs(17, 18), rent="5090", max_usd=Decimal(2)
    )
    thread: threading.Thread | None = None
    try:
        assert market.pod is not None
        market.pod.update(status="RUNNING", ssh_connect_cmd="ssh -p 2200 root@192.0.2.1")
        scheduler.tick()
        assert len(environments) == 1
        thread = threading.Thread(target=worker)
        thread.start()
        if cancel:
            until(scheduler, runs[0], "running")
            scheduler.cancel(runs[0])
        statuses = scheduler.wait(*runs, every=0.05, timeout=20)
        assert statuses[0].outcome == ("cancelled" if cancel else "succeeded")
        assert released(scheduler, runs[0]).rental_ended
        assert market.pod is None and market.counts[f"POST executors/{NODE}/rent"] == 1
        assert market.counts[f"DELETE pods/{POD}"] == 1 and len(environments) == 1
        destination = scheduler.fetch(runs[0], "result.txt", tmp_path / "output")
        assert destination.read_text() == "retained\n"
    finally:
        for run in runs:
            scheduler.cancel(run)
        if thread is not None:
            thread.join(timeout=10)
            assert not thread.is_alive()
        store.close()
    assert errors == []


def test_local_watchdog_stops_work_without_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seconds = [0.0]
    start = datetime.now(UTC)
    terminated: list[int] = []

    def sleep(duration: float) -> None:
        seconds[0] += duration

    def killpg(group: int, signal: int) -> None:
        terminated.append(group)

    monkeypatch.setattr("etude.launch.providers.lium.pod.os.killpg", killpg)
    guard(
        Watchdog(),
        "4321",
        start + timedelta(seconds=3),
        Decimal(1),
        sleep=sleep,
        now=lambda: start + timedelta(seconds=seconds[0]),
        monotonic=lambda: seconds[0],
    )
    assert terminated == [4321] and seconds[0] == 3


def test_failed_ssh_reply_can_recover_without_renting_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.create(request())
    assert market.pod is not None
    market.pod.update(status="RUNNING", ssh_connect_cmd="ssh -p 2200 root@192.0.2.1")
    attempts = [0]

    def start(pod: Pod, env: dict[str, str]) -> None:
        attempts[0] += 1
        if attempts[0] == 1:
            raise ProviderUnavailable("fixture lost SSH reply")

    monkeypatch.setattr("etude.launch.providers.lium.machines._start", start)
    with pytest.raises(ProviderUnavailable):
        machine.find("session")
    assert machines(market, storage).find("session") is not None
    assert attempts[0] == 2 and market.counts[f"POST executors/{NODE}/rent"] == 1
    assert market.counts[f"POST pods/{POD}/schedule-removal"] == 1


def test_lium_cli_configuration_is_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
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
    assert main(["provider", "set", "lium", "--kind", "lium", "--storage", "evidence"]) == 0
    assert (
        main(
            [
                "ondemand",
                "add",
                "5090",
                "--provider",
                "lium",
                "--gpu",
                "RTX 5090",
                "--gpu-count",
                "2",
            ]
        )
        == 0
    )
    scheduler = _open(tmp_path)
    try:
        assert scheduler.ondemands()[0].template.gpu_count == 2
    finally:
        scheduler._store.close()


@pytest.mark.parametrize("guardian_ready", [False, True])
def test_lium_bootstrap_requires_watchdog_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guardian_ready: bool,
) -> None:
    marker = tmp_path / "guardian-started"
    monkeypatch.setattr(bootstrap_module, "GUARDIAN_MARKER", marker)
    monkeypatch.setenv("ETUDE_SESSION", "fixture")
    monkeypatch.delenv("ETUDE_BOOTSTRAP_LOG_URL", raising=False)
    events: list[str] = []

    class Timer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            events.append("guardian spawned")

        def poll(self) -> int | None:
            if not guardian_ready:
                return 1
            marker.write_text("started")
            return None

    def ensure_uv() -> None:
        assert marker.exists()
        events.append("install")
        raise OSError("fixture installation failure")

    monkeypatch.setattr("subprocess.Popen", Timer)
    monkeypatch.setattr(bootstrap_module, "ensure_uv", ensure_uv)
    assert pod_main(["rental", "boot"]) == 1
    assert events == (["guardian spawned", "install"] if guardian_ready else ["guardian spawned"])
    log = (tmp_path / "sessions/fixture/bootstrap.log").read_text()
    assert "provider timer and controller own machine deletion" in log
    assert "bootstrap exit" in log


def test_replayed_remote_start_cannot_start_a_second_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Replace only OS process boundaries; execute the exact claim/start script.
    monkeypatch.setattr("os.fork", lambda: 0)
    monkeypatch.setattr("os.setsid", lambda: None)
    monkeypatch.setattr("os.dup2", lambda source, destination: None)
    output = tmp_path / "starts"
    payload = json.dumps(
        {"env": {}, "code": f"pathlib.Path({str(output)!r}).write_text('started')"}
    )
    code = _START.replace("'/run/etude'", repr(str(tmp_path / "run")))
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    exec(code, {})
    assert output.read_text() == "started"
    output.write_text("first worker still owns the claim")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as stopped:
        exec(code, {})
    assert stopped.value.code == 0
    assert output.read_text() == "first worker still owns the claim"


def test_unacknowledged_timer_still_allows_cleanup_at_startup_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market, storage = Marketplace(), Storage()
    market.acknowledge_timer = False
    clock = [datetime.now(UTC)]

    class Time(datetime):
        @classmethod
        def now(cls, tz: object = None) -> Time:
            return cls.fromtimestamp(clock[0].timestamp(), UTC)

    monkeypatch.setattr("etude.launch.providers.lium.machines.datetime", Time)
    store = Database(launcher_file(tmp_path))
    scheduler = _scheduler(store, market, storage)
    monkeypatch.setattr(scheduler, "_clock", lambda: clock[0])
    scheduler.set_startup("5090", 10)
    try:
        name = scheduler.rent("5090", hours=0.1, max_usd=Decimal(2))
        assert market.pod is not None
        clock[0] += timedelta(seconds=11)
        scheduler.tick()  # recover the receipt without trying to start expired work
        scheduler.tick()  # controller deletes despite the failed schedule acknowledgement
        assert market.pod is None
        assert store.machine(name).deleted_at is not None
        assert market.counts[f"POST executors/{NODE}/rent"] == 1
        assert market.counts[f"DELETE pods/{POD}"] == 1
    finally:
        store.close()


def test_incompatible_paid_allocation_is_deleted_without_starting_work() -> None:
    market, storage = Marketplace(), Storage()
    machine = machines(market, storage)
    machine.create(request())
    assert market.pod is not None
    market.pod["gpu_count"] = 1
    with pytest.raises(ProviderUnavailable, match="different node or GPU count"):
        machine.find("session")
    assert market.pod is None and market.counts[f"DELETE pods/{POD}"] == 1
    assert "test/sessions/session/booted.json" not in storage.objects
    assert machine.end("session") is not None
