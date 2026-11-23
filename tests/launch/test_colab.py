"""Controlled Google Colab units, conversions, recovery, WebSocket bootstrap and effect counts."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import socket
import struct
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch import database
from etude.launch.aws import Keys
from etude.launch.client import _open
from etude.launch.database import launcher_file, migrations, open_home, upgrade
from etude.launch.errors import ProviderUnavailable
from etude.launch.executor import Execution, Executor, ProviderService
from etude.launch.listing import show_availability, show_machines, show_quote
from etude.launch.provisioner.machines import MachineRequest, Refused
from etude.launch.provisioner.providers.colab.api import (
    ColabAPI,
    resolve_template,
    validate_template,
)
from etude.launch.provisioner.providers.colab.http import (
    XSSI_PREFIX,
    ColabCredentials,
    execute_in_kernel,
    load_credentials,
)
from etude.launch.provisioner.providers.colab.machines import (
    ColabMachines,
    Receipt,
    _kernel_bootstrap_code,
)
from etude.launch.records import (
    CPU,
    GPU,
    BootstrapLinks,
    Image,
    Machine,
    MachineTemplate,
    OnDemand,
    Provider,
)
from etude.launch.records import Storage as Place
from etude.launch.scheduler.loop import Scheduler
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket
from etude.launch.storage.transport import HTTPReply
from etude.launch.worker.courier import run as run_courier
from etude.spec import Job
from tests.launch.fake import open_database
from tests.launch.test_runpod_machines import Storage, released, repository

ENDPOINT = "gpu-l4-s-demo-123456"
PROXY_URL = "https://8080-gpu-l4-s-demo-123456.prod.r.colab.dev"
PROXY_TOKEN = "proxy-token-secret"
TEMPLATE = MachineTemplate(provider="colab", gpu="L4")


def _xssi(payload: object) -> bytes:
    return XSSI_PREFIX + json.dumps(payload).encode()


def _bucket(storage: Storage) -> Bucket:
    return Bucket("journal", "us-west-2", Keys(access_key="fixture", secret_key="fixture"), storage)


class FakeColab:
    def __init__(self, *, balance: float = 200.0, burn_rate: float = 1.94) -> None:
        self.balance = balance
        self.burn_rate = burn_rate
        self.rate = 0.0
        self.external_assignments = 0
        self.assigned: dict[str, dict[str, object]] = {}
        self.by_endpoint: dict[str, dict[str, object]] = {}
        self.counts: Counter[str] = Counter()
        self.lost_create_reply = False
        self.refuse_status: int | None = None
        self.delete_pending = False
        self.executed: list[tuple[str, str, str, str]] = []
        self.fail_execute = False
        self.kernel_counter = 0
        self.ccu_lag_polls = 0
        self._lag_remaining = 0

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> HTTPReply:
        parsed = urlsplit(url)
        path = parsed.path
        params = parse_qs(parsed.query)
        self.counts[f"{method} {path}"] += 1

        if parsed.hostname == "oauth2.googleapis.com":
            return HTTPReply(
                status=200,
                body=json.dumps({"access_token": "refreshed-token", "expires_in": 3600}).encode(),
            )
        if path == "/api/kernels":
            assert headers.get("X-Colab-Runtime-Proxy-Token") == PROXY_TOKEN
            self.kernel_counter += 1
            return HTTPReply(
                status=201,
                body=json.dumps(
                    {"id": f"kernel-{self.kernel_counter}", "execution_state": "starting"}
                ).encode(),
            )
        assert headers.get("Authorization", "").startswith("Bearer ")
        if path == "/tun/m/ccu-info":
            if self._lag_remaining > 0:
                self._lag_remaining -= 1
                return HTTPReply(
                    status=200,
                    body=_xssi(
                        {
                            "currentBalance": self.balance,
                            "consumptionRateHourly": max(0.0, self.rate - self.burn_rate),
                            "assignmentsCount": max(
                                0, len(self.by_endpoint) - 1 + self.external_assignments
                            ),
                        }
                    ),
                )
            return HTTPReply(
                status=200,
                body=_xssi(
                    {
                        "currentBalance": self.balance,
                        "consumptionRateHourly": self.rate,
                        "assignmentsCount": len(self.by_endpoint) + self.external_assignments,
                    }
                ),
            )
        if path == "/tun/m/assignments":
            return HTTPReply(
                status=200,
                body=_xssi({"assignments": list(self.by_endpoint.values())}),
            )
        if path == "/tun/m/assign":
            nbh = params["nbh"][0]
            if method == "GET":
                if nbh in self.assigned:
                    return HTTPReply(status=200, body=_xssi(self.assigned[nbh]))
                return HTTPReply(
                    status=200,
                    body=_xssi(
                        {
                            "acc": params.get("accelerator", ["NONE"])[0],
                            "nbh": nbh,
                            "token": "xsrf-assign-token",
                            "variant": params.get("variant", ["DEFAULT"])[0],
                        }
                    ),
                )
            if method == "POST":
                assert headers.get("X-Goog-Colab-Token") == "xsrf-assign-token"
                if self.refuse_status is not None:
                    return HTTPReply(status=self.refuse_status, body=b"refused")
                accelerator = params.get("accelerator", ["NONE"])[0]
                variant = 1 if params.get("variant", ["DEFAULT"])[0] == "GPU" else 0
                shape = 1 if params.get("shape") == ["hm"] else 0
                record: dict[str, object] = {
                    "accelerator": accelerator,
                    "endpoint": ENDPOINT,
                    "variant": variant,
                    "machineShape": shape,
                    "runtimeProxyInfo": {
                        "token": PROXY_TOKEN,
                        "tokenExpiresInSeconds": 3600,
                        "url": PROXY_URL,
                    },
                }
                self.assigned[nbh] = record
                self.by_endpoint[ENDPOINT] = record
                self.rate += self.burn_rate
                self._lag_remaining = self.ccu_lag_polls
                if self.lost_create_reply:
                    raise TimeoutError("lost POST reply")
                return HTTPReply(status=200, body=_xssi(record))
        if path.startswith("/tun/m/unassign/"):
            endpoint = path.removeprefix("/tun/m/unassign/")
            if endpoint not in self.by_endpoint:
                return HTTPReply(status=404, body=b"gone")
            if method == "GET":
                return HTTPReply(status=200, body=_xssi({"token": "xsrf-unassign-token"}))
            if method == "POST":
                assert headers.get("X-Goog-Colab-Token") == "xsrf-unassign-token"
                if not self.delete_pending:
                    self.by_endpoint.pop(endpoint, None)
                    for key, value in list(self.assigned.items()):
                        if value["endpoint"] == endpoint:
                            del self.assigned[key]
                    self.rate = max(0.0, self.rate - self.burn_rate)
                return HTTPReply(status=200, body=b"")
        raise AssertionError(f"unexpected request {method} {url}")

    def execute(self, url: str, token: str, kernel_id: str, code: str) -> None:
        self.counts["WS /api/kernels/channels"] += 1
        if self.fail_execute:
            raise ProviderUnavailable("kernel websocket failed")
        self.executed.append((url, token, kernel_id, code))


def _credentials(*, expired: bool = False) -> ColabCredentials:
    now = datetime.now(UTC)
    return ColabCredentials(
        access_token="initial-token",
        expiry=now - timedelta(minutes=1) if expired else now + timedelta(hours=1),
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
    )


def _request(session: str = "s1", *, hours: float = 1.0) -> MachineRequest:
    now = datetime.now(UTC)
    return MachineRequest(
        session=session,
        deadline=now + timedelta(hours=hours),
        startup_deadline=now + timedelta(minutes=15),
        cap_usd=Decimal("1.00"),
        bootstrap=BootstrapLinks(
            url="https://bucket.example/bootstrap",
            log_url="https://bucket.example/bootstrap.log",
            worker_log_url="https://bucket.example/worker.log",
        ),
    )


def _scheduler(
    tmp_path: Path,
    colab: FakeColab,
    storage: Storage,
    *,
    usd_per_unit: Decimal | None = None,
    template: MachineTemplate = TEMPLATE,
    max_usd_per_hour: Decimal | None = None,
) -> Scheduler:
    store = open_database(launcher_file(tmp_path))
    place = Place(
        name="evidence", bucket="journal", prefix="etude/launch", provider="s3", region="us-west-2"
    )
    store.add_storage(place)
    bucket = _bucket(storage)
    api = ColabAPI(_credentials(), send=colab, execute=colab.execute)

    def compose(held: Machine | OnDemand) -> Execution:
        assert isinstance(held, OnDemand)
        return Executor(
            ColabMachines(held.template, api, bucket, place.prefix),
            None,
            BucketJournal(place, bucket),
        )

    service = ProviderService(compose, ColabMachines.profile, validate=validate_template)
    scheduler = Scheduler(store, {"colab": service})
    scheduler.set_provider(
        Provider(
            name="colab",
            kind="colab",
            registry="",
            storage="evidence",
            usd_per_unit=usd_per_unit,
        )
    )
    scheduler.add_ondemand(
        OnDemand(
            name="colab-l4",
            template=template,
            max_machines=1,
            max_usd_per_hour=max_usd_per_hour,
        )
    )
    return scheduler


def _bootstrap_env(code: str) -> dict[str, str]:
    payload_line = [line for line in code.splitlines() if line.startswith("payload = json.loads(")][
        0
    ]
    payload = json.loads(ast.literal_eval(payload_line.removeprefix("payload = json.loads(")[:-1]))
    env = payload["env"]
    assert isinstance(env, dict)
    return {str(k): str(v) for k, v in env.items()}


def test_template_resolution_has_no_fabricated_hardware_specs() -> None:
    l4 = resolve_template(MachineTemplate(provider="colab", gpu="l4"))
    assert not isinstance(l4, str)
    assert l4.variant == "GPU" and l4.accelerator == "L4" and l4.shape_param is None
    assert isinstance(l4.hardware, GPU)
    assert l4.hardware.min_vcpus is None and l4.hardware.min_memory_gb is None
    assert str(l4.hardware) == "L4"

    t4_high = resolve_template(MachineTemplate(provider="colab", gpu="T4", memory_gb=32))
    assert not isinstance(t4_high, str)
    assert t4_high.high_mem and t4_high.shape_param == "hm"
    assert str(t4_high.hardware) == "High-RAM: T4"

    cpu = resolve_template(MachineTemplate(provider="colab"))
    assert not isinstance(cpu, str)
    assert cpu.variant == "DEFAULT" and isinstance(cpu.hardware, CPU) and cpu.hardware.vcpus is None
    assert str(cpu.hardware) == "colab-standard" and not cpu.needs_compute_units

    assert "spot" in str(resolve_template(MachineTemplate(provider="colab", gpu="L4", spot=True)))
    assert "one GPU" in str(
        resolve_template(MachineTemplate(provider="colab", gpu="L4", gpu_count=2))
    )
    assert "--vcpus" in str(resolve_template(MachineTemplate(provider="colab", gpu="L4", vcpus=8)))
    assert "--disk-gb" in str(
        resolve_template(MachineTemplate(provider="colab", gpu="L4", disk_gb=100))
    )
    assert "--image" in str(
        resolve_template(
            MachineTemplate(
                provider="colab",
                gpu="L4",
                image=Image(
                    requested="x@sha256:" + "0" * 64,
                    resolved="x@sha256:" + "0" * 64,
                ),
            )
        )
    )
    assert "--data-center" in str(
        resolve_template(MachineTemplate(provider="colab", gpu="L4", data_center="us-central1"))
    )
    unknown = resolve_template(MachineTemplate(provider="colab", gpu="H200"))
    assert isinstance(unknown, str) and "L4" in unknown and "A100" in unknown


def test_ondemand_add_and_image_validate_colab_template_immediately(tmp_path: Path) -> None:
    colab, storage = FakeColab(), Storage()
    scheduler = _scheduler(tmp_path, colab, storage)
    for bad in (
        MachineTemplate(provider="colab", gpu="L4", spot=True),
        MachineTemplate(provider="colab", gpu="H200"),
        MachineTemplate(provider="colab", gpu="L4", gpu_count=2),
        MachineTemplate(provider="colab", gpu="L4", vcpus=8),
        MachineTemplate(provider="colab", gpu="L4", data_center="us-west-1"),
    ):
        with pytest.raises(LauncherError):
            scheduler.add_ondemand(OnDemand(name="bad", template=bad))
    with pytest.raises(LauncherError, match="--image"):
        scheduler.set_ondemand_image(
            "colab-l4",
            Image(requested="img@sha256:" + "0" * 64, resolved="img@sha256:" + "0" * 64),
        )


def test_oauth_token_refresh_and_credential_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    colab = FakeColab()
    api = ColabAPI(_credentials(expired=True), send=colab, execute=colab.execute)
    usage = api.ccu_info()
    assert usage.current_balance == 200.0
    assert colab.counts["POST /token"] == 1
    api.ccu_info()
    assert colab.counts["POST /token"] == 1

    # Loads ETUDE_HOME/colab-token.json without env vars or OS keychain prompts.
    home = tmp_path / "etude-home"
    home.mkdir()
    (home / "colab-token.json").write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "refresh_token": "home-rtok",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    monkeypatch.setenv("ETUDE_HOME", str(home))
    monkeypatch.delenv("COLAB_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("COLAB_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("COLAB_OAUTH_TOKEN_FILE", raising=False)
    assert load_credentials().refresh_token == "home-rtok"

    token_file = tmp_path / "creds.json"
    token_file.write_text(
        json.dumps(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "refresh_token": "file-rtok",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    monkeypatch.setenv("COLAB_OAUTH_TOKEN_FILE", str(token_file))
    assert load_credentials().refresh_token == "file-rtok"

    monkeypatch.setenv("COLAB_ACCESS_TOKEN", "direct-bearer")
    assert load_credentials().access_token == "direct-bearer"


def test_offer_units_balance_unknown_quote_and_live_provider_updates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    colab, storage = FakeColab(balance=199.25), Storage()
    scheduler = _scheduler(
        tmp_path,
        colab,
        storage,
        usd_per_unit=Decimal("0.10"),
        max_usd_per_hour=Decimal("0.30"),
    )
    answer = scheduler.availability("colab-l4")
    assert answer.rentable
    assert answer.offer.unit == "cu"
    assert answer.offer.units_per_hour is None
    assert answer.offer.usd_per_unit == Decimal("0.10")
    # Positive conversion with unobserved burn rate is explicitly unknown, never $0/hr.
    assert answer.offer.usd_per_hour is None
    assert answer.offer.balance == Decimal("199.25")
    assert answer.offer.active == 0

    show_availability(answer)
    out = capsys.readouterr().out
    assert "in CU at $0.1/CU (rate observed on assign)" in out
    assert "[199.25 CU balance, 0 active]" in out
    assert "(ceiling $0.30 checked on assign)" in out

    # Quote reports unknown planned cost when usd_per_unit > 0 and rate is observed on assign.
    repo = repository(tmp_path)
    quote = scheduler.quote(
        Job(command="true", repo=str(repo), max_hours=1, max_usd=Decimal("2")).runs(1),
        rent="colab-l4",
    )
    assert quote.max_usd is None
    assert quote.machines[0].usd_per_hour is None and quote.machines[0].max_usd is None
    show_quote(quote)
    quoted_out = capsys.readouterr().out
    assert "in CU at $0.1/CU (rate observed on assign)" in quoted_out
    assert "planned rental cost unknown until assign" in quoted_out
    assert "planned first-wave rental cost: unknown until assign; nothing rented" in quoted_out

    # Updating provider usd_per_unit takes effect immediately (no stale _reached cache).
    scheduler.set_provider(
        Provider(
            name="colab", kind="colab", registry="", storage="evidence", usd_per_unit=Decimal("0")
        )
    )
    zeroed = scheduler.availability("colab-l4")
    assert zeroed.offer.usd_per_unit == Decimal("0")
    assert zeroed.offer.usd_per_hour == Decimal("0")
    zero_quote = scheduler.quote(
        Job(command="true", repo=str(repo), max_hours=1, max_usd=Decimal("2")).runs(1),
        rent="colab-l4",
    )
    assert zero_quote.max_usd == Decimal("0")


def test_offer_rejects_paid_gpu_without_balance_and_allows_cpu() -> None:
    bucket = _bucket(Storage())
    empty = FakeColab(balance=0.0)
    machines = ColabMachines(
        TEMPLATE, ColabAPI(_credentials(), send=empty, execute=empty.execute), bucket, "etude"
    )
    offer = machines.offer()
    assert not offer.available and offer.reason and "compute units" in offer.reason
    assert empty.counts["GET /tun/m/ccu-info"] == 1

    cpu_machines = ColabMachines(
        MachineTemplate(provider="colab"),
        ColabAPI(_credentials(), send=empty, execute=empty.execute),
        bucket,
        "etude",
    )
    cpu_offer = cpu_machines.offer()
    assert cpu_offer.available and cpu_offer.unit == "cu" and cpu_offer.usd_per_hour is None


def test_live_burn_rate_observed_with_lag_and_concurrent_sessions_guarded() -> None:
    bucket = _bucket(Storage())
    colab = FakeColab(burn_rate=1.94)
    colab.ccu_lag_polls = 1  # simulate one stale ccu-info poll right after POST /tun/m/assign
    machines = ColabMachines(
        TEMPLATE,
        ColabAPI(_credentials(), send=colab, execute=colab.execute),
        bucket,
        "etude",
    )
    alloc = machines.create(_request("s1"))
    assert alloc.unit == "cu"
    assert alloc.units_per_hour == Decimal("1.94")
    assert alloc.usd_per_hour is None
    assert colab.counts["GET /tun/m/ccu-info"] == 3  # 1 before + 1 lagged + 1 updated

    # If a concurrent assignment changes count unexpectedly, ignore noisy delta.
    bucket2 = _bucket(Storage())

    class RacyColab(FakeColab):
        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> HTTPReply:
            if urlsplit(url).path == "/tun/m/assign" and method == "POST":
                self.external_assignments += 1
                self.rate += 10.0  # another notebook started an A100 concurrently
            return super().__call__(method, url, body, headers)

    racy = RacyColab(burn_rate=1.94)
    machines2 = ColabMachines(
        TEMPLATE,
        ColabAPI(_credentials(), send=racy, execute=racy.execute),
        bucket2,
        "etude",
    )
    alloc2 = machines2.create(_request("s2"))
    assert alloc2.units_per_hour is None
    assert alloc2.usd_per_hour is None


def test_create_lost_reply_recovers_via_deterministic_nbh_rate_and_bootstraps_once() -> None:
    bucket = _bucket(Storage())
    colab = FakeColab(burn_rate=1.94)
    colab.lost_create_reply = True
    api = ColabAPI(_credentials(), send=colab, execute=colab.execute)
    machines = ColabMachines(TEMPLATE, api, bucket, "etude")

    with pytest.raises(ProviderUnavailable):
        machines.create(_request("s1"))

    assert colab.counts["POST /tun/m/assign"] == 1
    assert colab.counts["WS /api/kernels/channels"] == 0

    colab.lost_create_reply = False
    assert machines.find("s1") == ENDPOINT
    assert colab.counts["POST /tun/m/assign"] == 1

    allocation = machines.allocation("s1")
    assert allocation is not None and allocation.id == ENDPOINT
    # Sole active assignment on the account recovers exact CU/hr burn rate.
    assert allocation.units_per_hour == Decimal("1.94")
    assert colab.counts["POST /api/kernels"] == 1
    assert colab.counts["WS /api/kernels/channels"] == 1
    assert len(colab.executed) == 1
    assert "boot-claim" in colab.executed[0][3]
    assert "etude.launch.provisioner.providers.watchdog" in colab.executed[0][3]

    machines.allocation("s1")
    assert colab.counts["POST /api/kernels"] == 1
    assert colab.counts["WS /api/kernels/channels"] == 1


def test_create_refusal_412_and_400_record_no_machine() -> None:
    for status, fragment in ((412, "too many active sessions"), (400, "entitlement")):
        bucket = _bucket(Storage())
        colab = FakeColab()
        colab.refuse_status = status
        machines = ColabMachines(
            TEMPLATE, ColabAPI(_credentials(), send=colab, execute=colab.execute), bucket, "etude"
        )
        with pytest.raises(Refused, match=fragment):
            machines.create(_request("s-refused"))
        assert machines.find("s-refused") is None


def test_end_confirms_absence_and_alive_tracks_assignments() -> None:
    bucket = _bucket(Storage())
    colab = FakeColab()
    machines = ColabMachines(
        TEMPLATE, ColabAPI(_credentials(), send=colab, execute=colab.execute), bucket, "etude"
    )
    machines.create(_request("s1"))
    assert machines.alive("s1") is True

    colab.delete_pending = True
    assert machines.end("s1") is None
    assert machines.alive("s1") is True

    colab.delete_pending = False
    ended = machines.end("s1")
    assert ended is not None
    assert machines.alive("s1") is False
    raw = bucket.get("etude/sessions/s1/receipt.json")
    assert raw is not None and Receipt.model_validate_json(raw).endpoint == ENDPOINT


def test_websocket_execute_in_kernel_loopback() -> None:
    received: dict[str, object] = {}
    ready = threading.Event()

    def serve(server: socket.socket) -> None:
        ready.set()
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            headers: dict[str, str] = {}
            lines = data.decode().split("\r\n")
            received["request_line"] = lines[0]
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            accept = base64.b64encode(
                hashlib.sha1(
                    (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()
            ).decode()
            conn.sendall(
                f"HTTP/1.1 101 Switching Protocols\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
            )
            head = conn.recv(2)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", conn.recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", conn.recv(8))[0]
            mask = conn.recv(4)
            payload = bytearray()
            while len(payload) < length:
                payload.extend(conn.recv(length - len(payload)))
            decoded = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            msg = json.loads(decoded)
            received["msg"] = msg
            conn.sendall(bytes([0x89, 2]) + b"hi")
            pong_head = conn.recv(6)
            pong_len = pong_head[1] & 0x7F
            conn.recv(pong_len)
            reply = json.dumps(
                {
                    "msg_type": "status",
                    "parent_header": {"msg_id": msg["header"]["msg_id"]},
                    "content": {"execution_state": "busy"},
                }
            ).encode()
            if len(reply) < 126:
                frame = bytes([0x81, len(reply)]) + reply
            else:
                frame = bytes([0x81, 126]) + struct.pack("!H", len(reply)) + reply
            conn.sendall(frame)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        thread = threading.Thread(target=serve, args=(server,), daemon=True)
        thread.start()
        ready.wait(timeout=5)
        execute_in_kernel(f"http://127.0.0.1:{port}", PROXY_TOKEN, "kid-1", "print(1)")
        thread.join(timeout=5)

    assert "/api/kernels/kid-1/channels" in str(received["request_line"])
    msg = received["msg"]
    assert isinstance(msg, dict)
    assert msg["header"]["msg_type"] == "execute_request"
    assert msg["content"]["code"] == "print(1)"


def test_colab_scheduler_lifecycle_updates_cap_and_displays_cu_and_usd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))
    colab, storage = FakeColab(burn_rate=2.0), Storage()
    scheduler = _scheduler(tmp_path, colab, storage, usd_per_unit=Decimal("0.10"))

    availability = scheduler.availability("colab-l4")
    assert availability.rentable and availability.offer.units_per_hour is None
    assert availability.offer.usd_per_hour is None

    repo = repository(tmp_path)
    run_id = scheduler.launch(
        Job(
            command='test -f train.sh && echo retained > "$ETUDE_OUTPUT_DIR/ok.txt"',
            repo=str(repo),
            max_hours=5.0,
            # Observed rate is 2.0 CU/hr * $0.10/CU = $0.20/hr -> $0.50 cap trims deadline to 2.5h!
            max_usd=Decimal("0.50"),
        ),
        rent="colab-l4",
    )[0]

    machines = scheduler.machines("colab-l4")
    assert len(machines) == 1
    assert machines[0].image is None
    assert machines[0].provider_id == ENDPOINT
    assert isinstance(machines[0].hardware, GPU) and str(machines[0].hardware) == "L4"

    rental = scheduler.rentals()[0].rental
    assert rental.unit == "cu"
    assert rental.units_per_hour == Decimal("2.0")
    assert rental.usd_per_hour == Decimal("0.200")
    assert rental.cap_usd == Decimal("0.50")

    # Attempt limit AND VM watchdog deadline both reflect the observed 2.5h budget window.
    (attempt,) = scheduler.attempts(run_id)
    assert attempt.limit_seconds == 9000
    assert attempt.limit_kind == "over_budget"

    show_machines(scheduler, ended=False)
    listed = capsys.readouterr().out
    assert machines[0].name in listed and "CU (" in listed and "L4" in listed

    assert len(colab.executed) == 1
    env = _bootstrap_env(colab.executed[0][3])
    vm_deadline = datetime.fromisoformat(env["ETUDE_DEADLINE"])
    assert abs((vm_deadline - rental.deadline).total_seconds()) < 1.0
    bootstrap_url = env["ETUDE_BOOTSTRAP_URL"]

    def leave_deletion_to_server() -> None:
        pass

    errors: list[Exception] = []

    def worker() -> None:
        try:
            run_courier(
                bootstrap_url,
                leave_deletion_to_server,
                root=tmp_path / "colab-vm",
                send=storage,
                every=0.05,
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        statuses = scheduler.wait(run_id, every=0.05, timeout=20)
        assert not errors
        assert statuses[0].outcome == "succeeded"
        assert statuses[0].unit == "cu" and statuses[0].units is not None
        assert "CU (" in str(statuses[0])
        assert released(scheduler, run_id).rental_ended
        assert not colab.by_endpoint
        assert scheduler.machines("colab-l4")[0].deleted_at is not None
        assert colab.counts["POST /tun/m/assign"] == 1
        assert colab.counts[f"POST /tun/m/unassign/{ENDPOINT}"] == 1
        fetched = scheduler.fetch(run_id, "ok.txt", tmp_path / "fetched")
        assert fetched.read_text() == "retained\n"
    finally:
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_colab_manual_rent_respects_max_usd_and_terminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    colab, storage = FakeColab(burn_rate=2.0), Storage()
    scheduler = _scheduler(tmp_path, colab, storage, usd_per_unit=Decimal("0.10"))
    # 2.0 CU/hr * $0.10/CU = $0.20/hr; --max-usd 0.20 trims a 2h request to 1h before boot.
    before = datetime.now(UTC)
    machine_name = scheduler.rent("colab-l4", hours=2.0, max_usd=Decimal("0.20"))
    (cost,) = scheduler.rentals()
    assert cost.rental.cap_usd == Decimal("0.20")
    assert cost.rental.usd_per_hour == Decimal("0.200")
    assert 3500 <= (cost.rental.deadline - before).total_seconds() <= 3700
    env = _bootstrap_env(colab.executed[0][3])
    vm_deadline = datetime.fromisoformat(env["ETUDE_DEADLINE"])
    assert abs((vm_deadline - cost.rental.deadline).total_seconds()) < 1.0

    show_machines(scheduler, ended=False)
    out = capsys.readouterr().out
    assert machine_name in out and "L4" in out and "CU (" in out
    assert colab.counts["POST /tun/m/assign"] == 1

    ended = scheduler.terminate(machine_name)
    assert ended.deleted_at is not None
    assert not colab.by_endpoint
    assert colab.counts[f"POST /tun/m/unassign/{ENDPOINT}"] == 1


def test_colab_ceiling_exceeded_unassigns_before_booting_kernel(tmp_path: Path) -> None:
    colab, storage = FakeColab(burn_rate=1.94), Storage()
    scheduler = _scheduler(
        tmp_path,
        colab,
        storage,
        usd_per_unit=Decimal("0.10"),
        max_usd_per_hour=Decimal("0.15"),  # 1.94 * 0.10 = $0.194/hr > $0.15/hr
    )
    repo = repository(tmp_path)
    with pytest.raises(LauncherError, match="exceeds ceiling"):
        scheduler.launch(
            Job(command="true", repo=str(repo), max_hours=1.0, max_usd=Decimal("1.00")),
            rent="colab-l4",
        )
    # Runtime unassigned immediately; no kernel started; zero endpoints held.
    assert not colab.by_endpoint
    assert colab.counts["POST /tun/m/assign"] == 1
    assert colab.counts[f"POST /tun/m/unassign/{ENDPOINT}"] == 1
    assert colab.counts["POST /api/kernels"] == 0
    assert colab.counts["WS /api/kernels/channels"] == 0


def test_colab_multi_attempt_sums_cu_and_marks_estimated_at_zero_usd(tmp_path: Path) -> None:
    # Start 30m before wall clock so ColabMachines.end() (wall UTC) lands 30m after attempt 1's claim.
    now = [datetime.now(UTC) - timedelta(minutes=30)]
    store = open_database(launcher_file(tmp_path))
    place = Place(
        name="evidence", bucket="journal", prefix="etude/launch", provider="s3", region="us-west-2"
    )
    store.add_storage(place)
    colab, storage = FakeColab(burn_rate=2.0), Storage()
    bucket = _bucket(storage)
    api = ColabAPI(_credentials(), send=colab, execute=colab.execute)

    def compose(held: Machine | OnDemand) -> Execution:
        assert isinstance(held, OnDemand)
        return Executor(
            ColabMachines(held.template, api, bucket, place.prefix),
            None,
            BucketJournal(place, bucket),
        )

    service = ProviderService(compose, ColabMachines.profile, validate=validate_template)
    scheduler = Scheduler(store, {"colab": service}, clock=lambda: now[0])
    scheduler.set_provider(
        Provider(
            name="colab", kind="colab", registry="", storage="evidence", usd_per_unit=Decimal("0")
        )
    )
    scheduler.add_ondemand(OnDemand(name="colab-l4", template=TEMPLATE, max_machines=1))

    repo = repository(tmp_path)
    (run_id,) = scheduler.launch(
        Job(command="true", repo=str(repo), max_hours=2.0, max_usd=Decimal("1.00"), max_attempts=2),
        rent="colab-l4",
    )
    # Attempt 1 runs for 30 minutes (1.00 CU), then Colab reclaims the runtime.
    now[0] += timedelta(minutes=30)
    colab.by_endpoint.clear()
    colab.assigned.clear()
    colab.rate = 0.0
    scheduler.tick()
    # Attempt 2 is assigned on a fresh runtime and runs for 15 minutes (0.50 CU).
    now[0] += timedelta(minutes=15)
    status = scheduler.read(run_id)
    assert status.unit == "cu"
    assert status.units is not None and abs(status.units - Decimal("1.50")) < Decimal("0.01")
    assert status.usd == Decimal("0")
    assert status.estimated is True
    assert "~1.50 CU ($0.00)" in str(status)


def test_colab_cli_configuration_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    monkeypatch.delenv("COLAB_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("COLAB_ACCESS_TOKEN", raising=False)
    _open.cache_clear()
    try:
        upgrade(home, development=True)
        assert (
            main(
                [
                    "storage",
                    "add",
                    "evidence",
                    "--bucket",
                    "b",
                    "--prefix",
                    "p",
                    "--provider",
                    "s3",
                    "--region",
                    "us-west-2",
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "provider",
                    "set",
                    "colab",
                    "--kind",
                    "colab",
                    "--storage",
                    "evidence",
                    "--usd-per-unit",
                    "0.10",
                ]
            )
            == 0
        )
        providers_out = capsys.readouterr().out
        assert "$0.10/CU" in providers_out

        # Invalid Colab template rejected at ondemand add.
        assert (
            main(
                [
                    "ondemand",
                    "add",
                    "bad",
                    "--provider",
                    "colab",
                    "--gpu",
                    "H200",
                ]
            )
            != 0
        )
        capsys.readouterr()

        assert (
            main(
                [
                    "ondemand",
                    "add",
                    "colab-l4",
                    "--provider",
                    "colab",
                    "--gpu",
                    "L4",
                    "--max",
                    "1",
                ]
            )
            == 0
        )
        capsys.readouterr()
        assert main(["ondemands"]) == 0
        out = capsys.readouterr().out
        assert "colab-l4" in out and "L4 on colab" in out
    finally:
        _open.cache_clear()


def test_provider_units_migration_preserves_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = migrations()
    preceding = known[: next(i for i, m in enumerate(known) if m.name == "provider_units")]
    with monkeypatch.context() as old:
        old.setattr(database, "migrations", lambda: preceding)
        upgrade(tmp_path)
        store = open_home(tmp_path)
        store.add_storage(
            Place(name="west", bucket="b", prefix="p", provider="s3", region="us-west-2")
        )
        store._db.execute(
            "INSERT INTO providers(name, kind, registry, storage) VALUES (?, ?, ?, ?)",
            ("runpod", "runpod", "", "west"),
        )
        store._db.commit()
        store.close()
    monkeypatch.setattr(database, "_merged", lambda: {m.name: m.sha256 for m in known})
    result = upgrade(tmp_path)
    assert "provider_units" in result.applied
    store = open_home(tmp_path)
    saved = store.provider("runpod")
    assert saved.kind == "runpod" and saved.usd_per_unit is None
    store.close()


def test_kernel_bootstrap_code_is_valid_python() -> None:
    code = _kernel_bootstrap_code({"ETUDE_SESSION": "s1"}, "print('hi')")
    compile(code, "<colab-bootstrap>", "exec")
    assert "boot-claim" in code and "start_new_session=True" in code
