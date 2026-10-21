"""RunPod end to end with nothing real outside this machine.

Storage and the provider are stand-ins that speak the same HTTP the real ones
do. What runs "in the pod" is the real pod code: the courier in a thread, and
the real session process it starts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import ValidationError

from etude.launch.database import Database
from etude.launch.executor import Executor
from etude.launch.listing import show_machines, show_runs
from etude.launch.providers.runpod.api import RunPodAPI
from etude.launch.providers.runpod.machines import RunPodMachines
from etude.launch.providers.runpod.pod import GuardianAPI, guard
from etude.launch.records import (
    Image,
    Input,
    Job,
    LauncherError,
    MachineTemplate,
    OnDemand,
    Provider,
    Run,
    Status,
)
from etude.launch.records import Storage as Place
from etude.launch.scheduler import Scheduler
from etude.launch.schemas import Bootstrap, BucketAccess, CheckpointRef, Packet, RunAccess, Work
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Keys, Reply, StorageUnavailable
from etude.launch.worker import courier as courier_module
from etude.launch.worker.courier import Courier
from etude.launch.worker.courier import run as run_in_pod
from tests.launch.counting import Counting, Counts, job_text

TRAIN = """
echo "seed $ETUDE_SEED from ${ETUDE_FROM_VALUE:-0} to $ETUDE_UNTIL_VALUE"
cat "${ETUDE_CHECKPOINT:-/dev/null}" > "$ETUDE_OUTPUT_DIR/checkpoint.pt"
echo "trained to $ETUDE_UNTIL_VALUE" >> "$ETUDE_OUTPUT_DIR/checkpoint.pt"
printf '{"clocks": {"steps": %s}, "checkpoint": "checkpoint.pt"}' "$ETUDE_UNTIL_VALUE" \\
  > "$ETUDE_OUTPUT_DIR/etude-result.json"
"""


@pytest.fixture(autouse=True)
def isolate_download_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "client"))


class Storage:
    """A bucket in memory, speaking enough of S3's HTTP for the client and the pod.

    It also answers as STS, handing out temporary keys unless `grants` is off.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.asked: list[tuple[str, str]] = []
        self.grants = True
        self.lock = threading.Lock()

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        parts = urlsplit(url)
        key = unquote(parts.path.lstrip("/"))
        query = parse_qs(parts.query, keep_blank_values=True)
        with self.lock:
            self.asked.append((method, key + ("?" + parts.query if parts.query else "")))
            if parts.netloc.startswith("sts."):
                if not self.grants:
                    return Reply(status=403, body=b"<ErrorResponse/>")
                return Reply(
                    status=200,
                    body=b"<GetFederationTokenResponse><GetFederationTokenResult><Credentials><AccessKeyId>ASIATEMPORARY</AccessKeyId><SecretAccessKey>s</SecretAccessKey><SessionToken>t</SessionToken></Credentials></GetFederationTokenResult></GetFederationTokenResponse>",
                )
            if method == "POST" and "uploads" in query:
                upload = f"upload{len(self.uploads)}"
                self.uploads[upload] = {}
                return Reply(
                    status=200, body=f"<Result><UploadId>{upload}</UploadId></Result>".encode()
                )
            if "uploadId" in query:
                held = self.uploads[query["uploadId"][0]]
                if method == "PUT":
                    number = int(query["partNumber"][0])
                    held[number] = body or b""
                    return Reply(status=200, body=b"", headers={"etag": f'"tag{number}"'})
                if method == "POST":
                    assert all(f'"tag{n}"' in (body or b"").decode() for n in held)
                    self.objects[key] = b"".join(held[n] for n in sorted(held))
                del self.uploads[query["uploadId"][0]]
                return Reply(status=200, body=b"")
            if method == "POST":
                boundary = headers["Content-Type"].split("boundary=")[1].encode()
                fields: dict[str, bytes] = {}
                for part in (body or b"").split(b"--" + boundary)[1:-1]:
                    head, _, value = part.partition(b"\r\n\r\n")
                    name = head.split(b'name="')[1].split(b'"')[0].decode()
                    fields[name] = value.removesuffix(b"\r\n")
                self.objects[fields["key"].decode()] = fields["file"]
                return Reply(status=204, body=b"")
            if method == "PUT":
                if headers.get("if-none-match") == "*" and key in self.objects:
                    return Reply(status=412, body=b"")
                self.objects[key] = body or b""
                return Reply(status=200, body=b"")
            if method == "DELETE":
                self.objects.pop(key, None)
                return Reply(status=204, body=b"")
            if "list-type" in query:
                found = sorted(k for k in self.objects if k.startswith(query["prefix"][0]))
                keys = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in found)
                page = (
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<IsTruncated>false</IsTruncated>{keys}</ListBucketResult>"
                )
                return Reply(status=200, body=page.encode())
            if key in self.objects:
                value = self.objects[key]
                if "Range" in headers:
                    start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                    return Reply(
                        status=206,
                        body=value[start : end + 1],
                        headers={
                            "content-range": f"bytes {start}-{end}/{len(value)}",
                            "content-length": str(end - start + 1),
                            "etag": hashlib.sha256(value).hexdigest(),
                        },
                    )
                return Reply(status=200, body=value)
            return Reply(status=404, body=b"")


@dataclass
class RunPod:
    """The provider: it keeps pods, and "boots" each one by running the real pod code."""

    storage: Storage
    root: Path
    pods: dict[str, dict[str, Any]] = field(default_factory=dict)
    creates: int = 0
    lose_reply: bool = False
    available: str = "HIGH"
    envs: list[dict[str, str]] = field(default_factory=list)

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        path = urlsplit(url).path.removeprefix("/v2/")
        if path == "catalog/gpus":
            here = [{"id": "EU-RO-1", "availability": self.available}]
            gpu = {"id": "NVIDIA L4", "price": {"secure": 0.59}, "dataCenters": here}
            return 200, json.dumps({"gpus": [gpu]}).encode()
        if path == "catalog/cpus":
            here = [{"id": "EU-RO-1", "availability": self.available}]
            cpu = {
                "id": "cpu3g",
                "price": {"securePerVcpu": 0.04},
                "ramGbPerVcpu": 4,
                "vcpu": {"min": 2, "max": 32},
                "dataCenters": here,
            }
            return 200, json.dumps({"cpus": [cpu]}).encode()
        if method == "POST":
            asked = json.loads(body or b"{}")
            assert asked["dataCenterIds"] == ["EU-RO-1"]  # the pool's place is the one asked for
            self.creates += 1
            with self.storage.lock:  # one timeline of storage requests and pod creates
                self.storage.asked.append(("CREATE", asked["name"]))
            id = f"pod{self.creates}"
            pod = {"id": id, "name": asked["name"], "cost": 0.59, "status": "RUNNING"}
            self.pods[id] = {**pod, "createdAt": datetime.now(UTC).isoformat()}
            env = asked["env"]
            assert not any("KEY" in name or "SECRET" in name for name in env)
            assert asked["ports"] == ["22/tcp"] and asked["startSsh"] is True
            self.envs.append(env)
            thread = threading.Thread(
                target=run_in_pod,
                args=(env["ETUDE_BOOTSTRAP_URL"], lambda: self.pods.pop(id, None)),
                kwargs={"root": self.root, "send": self.storage, "every": 0.05},
                daemon=True,
            )
            thread.start()
            if self.lose_reply:
                self.lose_reply = False
                raise OSError("the reply never arrived")
            return 200, json.dumps(self.pods[id]).encode()
        if path == "pods":
            paging = {"hasNextPage": False, "nextCursor": None}
            return 200, json.dumps(
                {"pods": list(self.pods.values()), "pagination": paging}
            ).encode()
        id = path.removeprefix("pods/")
        if method == "DELETE":
            self.pods.pop(id, None)
            return 200, b""
        return (200, json.dumps(self.pods[id]).encode()) if id in self.pods else (404, b"")


@dataclass
class Cloud:
    scheduler: Scheduler
    storage: Storage
    provider: RunPod
    store: Database


def cloud(
    tmp_path: Path, *, delivery_seconds: float | None = None, gpu: str | None = "NVIDIA L4"
) -> Cloud:
    storage = Storage()
    provider = RunPod(storage, tmp_path / "pod")
    store = Database(tmp_path / "launcher.db")
    account = Provider(name="runpod", kind="runpod", registry="reg1", storage="west")
    place = Place(name="west", bucket="journals", prefix="etude/t", region="us-west-2")
    template = MachineTemplate(
        provider="runpod",
        gpu=gpu,
        vcpus=6,
        memory_gb=48,
        disk_gb=40,
        image=Image(requested="r/e:fixture", resolved="r/e@sha256:" + "a" * 64),
        data_center="EU-RO-1",
    )
    bucket = Bucket(
        "journals", "us-west-2", Keys(access_key="access", secret_key="secret"), storage
    )
    service = Executor(
        RunPodMachines(account, template, RunPodAPI("key", provider), bucket, place.prefix),
        None,
        BucketJournal(place, bucket),
    )
    scheduler = Scheduler(store, {"runpod": lambda held: service})
    scheduler.add_storage(place)
    scheduler.set_provider(account)
    scheduler.add_ondemand(
        OnDemand(
            name="l4",
            template=template,
            max_machines=1,
            setup_seconds=60,
            delivery_seconds=delivery_seconds,
        )
    )
    return Cloud(scheduler, storage, provider, store)


def until(scheduler: Scheduler, run: str, phase: str) -> Status:
    for _ in range(400):
        status = scheduler.status(run)
        if status.phase == phase:
            return status
        time.sleep(0.05)
    raise AssertionError(f"run never reached {phase}: {status}")


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.sh").write_text(TRAIN)
    for command in (
        ["init", "-q"],
        ["add", "train.sh"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "train"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True)
    return repo


@pytest.mark.parametrize("queued", [False, True])
def test_a_runs_source_is_in_storage_before_any_pod_is_rented_for_it(
    tmp_path: Path, queued: bool
) -> None:
    c = cloud(tmp_path)
    c.scheduler.set_ondemand_max("l4", 2)
    job = Job(
        command="sh train.sh", repo=str(repository(tmp_path)), max_hours=0.05, max_usd=Decimal(1)
    )
    if queued:
        runs = c.scheduler.queue_cohort("pair", job.runs(1, 2), rent="l4", parallel=2)
        c.scheduler.tick()
    else:
        runs = c.scheduler.launch_cohort("pair", job.runs(1, 2), rent="l4", parallel=2)
    timeline = [(method, key.partition("?")[0]) for method, key in c.storage.asked]
    uploads = [
        i for i, (method, key) in enumerate(timeline) if method == "PUT" and "/sources/" in key
    ]
    creates = [i for i, (method, _) in enumerate(timeline) if method == "CREATE"]
    assert len(uploads) == 1 and len(creates) == 2  # one archive for the cohort, then the pods
    assert uploads[0] < creates[0]
    (key,) = {timeline[i][1] for i in uploads}
    assert key.startswith("etude/t/sources/") and key.endswith(".tar")  # named by digest
    statuses = c.scheduler.wait(*runs, every=0.05, timeout=30)
    assert [s.outcome for s in statuses] == ["succeeded", "succeeded"]


def test_inputs_reach_the_job_by_name_once_and_wrong_bytes_are_refused(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    weights = b"weights from an earlier tournament\n"
    c.storage.objects["shared/weights.pt"] = weights
    given = Input(
        name="weights.pt",
        key="shared/weights.pt",
        sha256=hashlib.sha256(weights).hexdigest(),
        size_bytes=len(weights),
    )
    job = Job(
        command='cat "$ETUDE_INPUTS_DIR/weights.pt"',
        inputs=(given,),
        max_hours=0.05,
        max_usd=Decimal(1),
        max_attempts=1,
    )
    run = c.scheduler.launch(job, rent="l4")
    (status,) = c.scheduler.wait(run, every=0.05, timeout=30)
    assert status.outcome == "succeeded"
    assert job_text(c.scheduler.logs(run=run)) == weights.decode()
    reads = [k for m, k in c.storage.asked if m == "GET" and k.startswith("shared/weights.pt")]
    assert len(reads) == 1  # one verified download, by the worker, through a scoped link

    wrong = given.model_copy(update={"sha256": hashlib.sha256(b"other").hexdigest()})
    refused = c.scheduler.launch(job.model_copy(update={"inputs": (wrong,)}), rent="l4")
    (status,) = c.scheduler.wait(refused, every=0.05, timeout=30)
    assert (status.outcome, status.cause) == ("lost", "setup_failed")
    said = job_text(c.scheduler.logs(run=refused))  # the job's command never started
    assert said.startswith("source preparation failed: ValueError: input weights.pt was not")
    assert c.scheduler.record(refused).job != c.scheduler.record(run).job  # inputs are identity
    assert c.provider.pods == {}
    with pytest.raises(ValidationError, match="plain file name"):
        given.model_validate({**given.model_dump(), "name": "../weights.pt"})
    with pytest.raises(ValidationError, match="its own name"):
        job.model_validate({**job.model_dump(), "inputs": [given.model_dump()] * 2})


@pytest.mark.parametrize("delivery_seconds", [None, 60])
def test_a_run_from_a_commit_runs_in_a_pod_and_continues_in_another(
    tmp_path: Path, delivery_seconds: float | None
) -> None:
    c = cloud(tmp_path, delivery_seconds=delivery_seconds)
    job = Job(
        command="sh train.sh",
        repo=str(repository(tmp_path)),
        until="steps=3",
        max_hours=0.05,
        max_usd=Decimal(1),
    )
    first = c.scheduler.launch(Run(job=job, seed=7), rent="l4")
    (status,) = c.scheduler.wait(first, every=0.05, timeout=30)
    assert (status.outcome, status.clocks, status.rental_ended) == (
        "succeeded",
        {"steps": 3},
        True,
    )
    assert job_text(c.scheduler.logs(run=first)) == "seed 7 from 0 to 3\n"
    assert c.provider.pods == {}  # the pod deleted itself once its work was delivered

    second = c.scheduler.launch(c.scheduler.continue_from(first, "steps=7"), rent="l4")
    (status,) = c.scheduler.wait(second, every=0.05, timeout=30)
    assert (status.outcome, status.clocks) == ("succeeded", {"steps": 7})
    assert job_text(c.scheduler.logs(run=second)) == "seed 7 from 3 to 7\n"
    copy = c.scheduler.fetch(second, "checkpoint.pt", tmp_path / "out")
    assert copy.read_text() == "trained to 3\ntrained to 7\n"
    assert c.provider.creates == 2 and c.provider.pods == {}

    # Only an explicit delivery allowance reserves time before rental termination.
    session = c.scheduler.attempts(second)[-1].session
    sent = json.loads(c.storage.objects[f"etude/t/sessions/{session}/packet.json"])
    pod_deadline = datetime.fromisoformat(c.provider.envs[-1]["ETUDE_DEADLINE"])
    assert (pod_deadline - datetime.fromisoformat(sent["deadline"])).total_seconds() == (
        delivery_seconds or 0
    )
    assert sent["delivery_seconds"] == delivery_seconds

    held = c.store.rental(status.machine or "")
    machine = c.store.machine(held.machine)
    assert ((machine.kind != "rented"), machine.provider_id, machine.deleted_at is not None) == (
        False,
        "pod2",
        True,
    )
    folder = f"etude/t/sessions/{session}/"
    keys = [k.removeprefix(folder) for k in c.storage.objects if k.startswith(folder)]
    # The launcher can confirm deletion after completion before the courier writes its end note.
    assert {"packet.json", "bootstrap.json", "inbox.json", "receipt.json"} <= set(keys)
    # Everything about the run is in its own folder, whichever machine wrote it.
    attempt = c.scheduler.attempts(second)[0].id
    folder = f"etude/t/{second}/"
    keys = [k.removeprefix(folder) for k in c.storage.objects if k.startswith(folder)]
    assert {f"attempts/{attempt}.json", f"attempts/{attempt}.log"} <= set(keys)
    assert "checkpoints/checkpoint.pt" in keys


def test_a_lost_create_reply_finds_the_pod_by_name_and_never_asks_twice(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    c.provider.lose_reply = True
    one = c.scheduler.queue(Job(command="echo hi", max_hours=0.05, max_usd=Decimal(1)), rent="l4")
    c.scheduler.tick()
    assert c.scheduler.read(one).phase == "renting"
    (status,) = c.scheduler.wait(one, every=0.05, timeout=30)
    assert status.outcome == "succeeded" and c.provider.creates == 1


@pytest.mark.parametrize(
    ("gpu", "shown"),
    [("NVIDIA L4", "NVIDIA L4 with at least 6 vCPUs, 48 GB"), (None, "cpu3g 16 vCPUs")],
)
def test_machines_and_status_show_the_hardware_rented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], gpu: str | None, shown: str
) -> None:
    # Without a GPU, 6 vCPUs and 48 GB round up to the 16 vCPU size of a 4 GB/vCPU flavor.
    c = cloud(tmp_path, gpu=gpu)
    one = c.scheduler.launch(Job(command="true", max_hours=0.05, max_usd=Decimal(1)), rent="l4")
    (status,) = c.scheduler.wait(one, every=0.05, timeout=30)
    assert status.outcome == "succeeded"
    capsys.readouterr()
    show_runs(c.scheduler, [one])
    show_machines(c.scheduler, ended=True)
    run_line, machine_line = capsys.readouterr().out.splitlines()
    assert f"machine {status.machine} ({shown})" in run_line
    assert machine_line.startswith(f"{status.machine}  l4  {shown}, rented")


def test_no_stock_is_a_refusal_and_cancel_reaches_a_running_pod(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    c.provider.available = "NONE"

    with pytest.raises(LauncherError, match="no available machines.*etude queue"):
        c.scheduler.launch(Job(command="true", max_hours=0.05, max_usd=Decimal(1)), rent="l4")
    assert not c.scheduler.runs(done=True)
    one = c.scheduler.queue(
        Job(command="sleep 30", max_hours=0.05, max_usd=Decimal(1), max_attempts=2),
        rent="l4",
    )
    assert c.provider.creates == 0
    assert not c.scheduler.attempts(one)
    assert "no available machines" in str(c.scheduler.read(one))
    c.provider.available = "HIGH"
    until(c.scheduler, one, "running")
    c.scheduler.cancel(one)
    status = until(c.scheduler, one, "done")
    assert (status.outcome, status.attempts, c.provider.creates) == ("cancelled", 1, 1)
    until_gone = time.monotonic() + 10
    while c.provider.pods and time.monotonic() < until_gone:
        c.scheduler.tick()
        time.sleep(0.05)
    assert c.provider.pods == {}


def test_a_kept_session_takes_work_through_its_inbox(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    machine = c.scheduler.rent("l4", hours=0.05)
    first = c.scheduler.launch(
        Job(command="echo one", max_hours=0.01, max_usd=Decimal(1)), rent="l4"
    )
    (status,) = c.scheduler.wait(first, every=0.05, timeout=30)
    second = c.scheduler.launch(
        Job(command="echo two", max_hours=0.01, max_usd=Decimal(1)), rent="l4"
    )
    (again,) = c.scheduler.wait(second, every=0.05, timeout=30)
    assert (status.outcome, again.outcome) == ("succeeded", "succeeded")
    assert status.machine == again.machine == machine and c.provider.creates == 1
    assert job_text(c.scheduler.logs(run=second)) == "two\n" and len(c.provider.pods) == 1
    assert c.scheduler.terminate(machine).deleted_at is not None and c.provider.pods == {}


def test_the_guardian_deletes_the_pod_at_the_deadline_or_when_the_dollars_run_out() -> None:
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def deleted_at(cost: float, max_usd: str, reply: int = 200) -> float:
        """Seconds after the pod was created at which the guardian deleted it."""
        elapsed = [0.0]
        deletes: list[float] = []
        fact = {"id": "pod1", "name": "etude-r", "cost": cost, "status": "RUNNING"}
        fact["createdAt"] = start.isoformat()

        def send(
            method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> tuple[int, bytes]:
            if method == "DELETE":
                deletes.append(elapsed[0])
                if len(deletes) < 3:
                    raise OSError("the provider is unreachable")  # it keeps trying
                return 200, b""
            return reply, json.dumps(fact).encode()

        guard(
            GuardianAPI("pod-key", send),
            "pod1",
            start + timedelta(hours=1),
            Decimal(max_usd),
            sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
            now=lambda: start + timedelta(seconds=elapsed[0]),
            monotonic=lambda: elapsed[0],
        )
        assert len(deletes) == 3
        return deletes[0]

    assert deleted_at(0.59, "5") == 3600  # dollars to spare: the deadline decides
    assert 1790 <= deleted_at(1.18, "0.59") <= 1800  # dearer than quoted: half the time
    assert deleted_at(0.59, "5", reply=503) == 3600  # price unknown: the deadline still holds


def test_checkpoint_reaches_storage_before_attempt_ends(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    run = c.scheduler.launch(
        Job(
            command='echo retained > "$ETUDE_OUTPUT_DIR/checkpoint.pt"; '
            'echo \'{"clocks":{"steps":3},"checkpoint":"checkpoint.pt"}\' > "$ETUDE_OUTPUT_DIR/etude-result.json"; sleep 30',
            max_hours=0.05,
            max_usd=Decimal(1),
        ),
        rent="l4",
    )
    until(c.scheduler, run, "running")
    key = f"etude/t/{run}/checkpoints/checkpoint.pt"
    deadline = time.monotonic() + 10
    while key not in c.storage.objects and time.monotonic() < deadline:
        time.sleep(0.05)
    assert c.storage.objects[key] == b"retained\n"
    assert c.scheduler.read(run).phase == "running"
    c.scheduler.cancel(run)
    assert c.scheduler.wait(run, every=0.05, timeout=10)[0].outcome == "cancelled"


def test_shared_inputs_are_prepared_once(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    checkpoint = b"shared weights"
    storage = Storage()
    counting = Counting(Database(tmp_path / "counting.db"))
    journal = BucketJournal(
        Place(name="west", bucket="journal", prefix="test", region="us-west-2"),
        Bucket(
            "journal",
            "us-west-2",
            Keys(access_key="access", secret_key="secret", session_token="temporary"),
            counting.transport("bucket", storage),
        ),
    )
    work = Work(
        attempt="a",
        command="true",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        start=None,
        config=None,
        repo=str(repo),
        commit=commit,
        checkpoint=CheckpointRef(
            run="parent",
            session="previous",
            attempt="parent-attempt",
            name="weights.pt",
            sha256=hashlib.sha256(checkpoint).hexdigest(),
            size_bytes=len(checkpoint),
            content=base64.b64encode(checkpoint).decode(),
        ),
        run="one",
    )
    packet = Packet(
        session="shared",
        deadline=None,
        work=(work, work.model_copy(update={"attempt": "b", "run": "two"})),
    )
    with counting.record():
        journal.prepare(packet)
    # Packet, one source listing/upload, one checkpoint upload, inbox, bootstrap.
    assert counting.snapshot() == Counts(bucket=6, git=1)
    bootstrap = Bootstrap.model_validate_json(
        storage.objects["test/sessions/shared/bootstrap.json"]
    )
    assert len(bootstrap.fetch) == 2
    source = bootstrap.fetch[f"shared/sources/{commit}.tar"]
    archive = storage.objects[f"test/sources/{source.sha256}.tar"]  # shared by every session
    assert (source.sha256, source.size_bytes) == (hashlib.sha256(archive).hexdigest(), len(archive))
    with counting.record():
        journal.stage(str(repo), commit)  # already staged: no git, no bucket
    assert counting.snapshot() == Counts()
    held = bootstrap.fetch["previous/attempts/parent-attempt/outputs/weights.pt"]
    assert storage.objects[f"test/sessions/shared/inputs/{held.sha256}"] == checkpoint


def test_lost_start_receipt_reply_cannot_leave_unreported_running_code(tmp_path: Path) -> None:
    class LostReceipt(Storage):
        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> Reply:
            response = super().__call__(method, url, body, headers)
            if method == "POST" and "test/sessions/r/out/receipt" in self.objects:
                return Reply(status=503, body=b"")
            return response

    storage = LostReceipt()
    bucket = Bucket("journal", "us-west-2", Keys(access_key="access", secret_key="secret"), storage)
    journal = BucketJournal(
        Place(name="west", bucket="journal", prefix="test", region="us-west-2"), bucket
    )
    work = Work(
        attempt="a",
        command="echo ran",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        start=None,
        config=None,
        repo=None,
        commit=None,
        checkpoint=None,
        run="run-a",
    )
    packet = Packet(session="r", deadline=datetime.now(UTC) + timedelta(minutes=1), work=(work,))
    bootstrap = journal.prepare(packet)
    with pytest.raises(StorageUnavailable):
        run_in_pod(bootstrap.url, lambda: None, tmp_path, send=storage, every=0.01)
    assert "test/sessions/r/out/receipt" in storage.objects
    assert not (tmp_path / "r" / "attempts").exists()


def test_tracking_files_reach_storage_while_the_job_runs(tmp_path: Path) -> None:
    c = cloud(tmp_path)
    script = tmp_path / "train.py"
    script.write_text(
        "import time\n"
        "from etude.train.records import Step\n"
        "from etude.train.run import track\n"
        'run = track(experiment="capacity", clocks=[Step("steps", "training steps")],\n'
        "           segment_seconds=0.1)\n"
        'run.log({"loss": 0.5}, steps=1)\n'
        "time.sleep(30)\n"
    )
    run = c.scheduler.launch(
        Job(command=f"{sys.executable} {script}", max_hours=0.05, max_usd=Decimal(1)), rent="l4"
    )
    until(c.scheduler, run, "running")
    rows = f"etude/t/{run}/metrics/"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        sealed = [key for key in c.storage.objects if key.startswith(rows) and key.endswith(".blk")]
        if sealed:
            break
        time.sleep(0.05)
    assert sealed
    assert b'"loss"' in c.storage.objects[sealed[0]]
    assert c.scheduler.read(run).phase == "running"
    c.scheduler.cancel(run)
    assert c.scheduler.wait(run, every=0.05, timeout=10)[0].outcome == "cancelled"


def machine(tmp_path: Path, storage: Storage) -> tuple[Courier, Path, Path]:
    """A courier on a rented machine with one running attempt, and that run's tracking folder."""
    held = Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage)
    keys = held.grant("etude/run1/", 3600, "run1")
    access = RunAccess(
        form=held.post_form("etude/run1/", 3600),
        bucket=None
        if keys is None
        else BucketAccess(name="b", region="us-east-1", host=held.host, keys=keys),
    )
    home = tmp_path / "pod/sessions/r"
    attempt = home / "attempts/a1"
    (attempt / "outputs").mkdir(parents=True)
    (attempt / "run").write_text("run1")
    track = tmp_path / "pod/track/run1"
    (track / "metrics").mkdir(parents=True)
    session = held.post_form("etude/sessions/r/out/", 3600)
    courier = Courier(home, session, "unused", storage, {"run1": access})
    return courier, attempt, track


def test_a_machine_deletes_only_uploaded_files_in_the_removal_note(tmp_path: Path) -> None:
    storage = Storage()
    courier, _, track = machine(tmp_path, storage)
    # Names are deliberately not tracking block names: only the job knows them.
    recent = ["metrics/first", "metrics/second"]
    for name in recent:
        (track / name).write_bytes(b"recent")
    courier.carry_out()
    out = "etude/run1/"
    assert {out + name for name in recent} <= set(storage.objects)
    # Tracking joins the two into one permanent block and removes them.
    joined = "metrics/joined"
    (track / joined).write_bytes(b"joined")
    for name in recent:
        (track / name).unlink()
    (track / "metrics/newer").write_bytes(b"newer")
    storage.objects[out + "not-ours"] = b"another uploader"
    (track / "metrics/w.removed").write_text(
        "\n".join([*recent, "not-ours", "../other-run/file", ""])
    )
    courier.carry_out()
    blocks = sorted(
        key.removeprefix(out)
        for key in storage.objects
        if key.startswith(out) and not key.startswith(out + "attempts/")
    )
    assert blocks == [joined, "metrics/newer", "metrics/w.removed", "not-ours"]
    assert sum(method == "DELETE" for method, _ in storage.asked) == 2
    requests = len(storage.asked)
    courier.carry_out()
    assert len(storage.asked) == requests  # successfully deleted paths are forgotten


def test_a_machine_without_keys_keeps_every_block_and_still_delivers(tmp_path: Path) -> None:
    storage = Storage()
    storage.grants = False  # AWS would not issue keys; the machine has the form alone
    courier, attempt, track = machine(tmp_path, storage)
    writer = "0123456789ab"
    (track / f"metrics/1-00000000-00000007-{writer}.blk").write_bytes(b"recent")
    courier.carry_out()
    (track / f"metrics/2-00000000-00000007-{writer}.blk").write_bytes(b"joined")
    (track / "metrics/w.removed").write_text(f"metrics/1-00000000-00000007-{writer}.blk\n")
    courier.carry_out()
    out = "etude/run1/metrics/"
    assert (
        len([key for key in storage.objects if key.startswith(out) and key.endswith(".blk")]) == 2
    )
    assert not any(method == "DELETE" for method, _ in storage.asked)


def test_courier_defers_removals_added_during_an_upload_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage()
    courier, _, track = machine(tmp_path, storage)
    old = track / "metrics/first"
    old.write_bytes(b"first")
    courier.carry_out()
    # The first pass must ignore this torn line until the writer finishes it.
    (track / "metrics/w.removed").write_text("metrics/first")
    (track / "run.json").write_bytes(b"identity")
    put = courier._put

    def compact_during_upload(path: Path, run: str, key: str) -> None:
        put(path, run, key)
        if path.name == "run.json":
            (track / "metrics/joined").write_bytes(b"joined")
            (track / "metrics/w.removed").write_text("metrics/first\n")
            old.unlink()

    monkeypatch.setattr(courier, "_put", compact_during_upload)
    courier.carry_out()
    out = "etude/run1/"
    assert storage.objects[out + "metrics/first"] == b"first"
    assert out + "metrics/joined" not in storage.objects
    assert sum(method == "DELETE" for method, _ in storage.asked) == 0
    monkeypatch.setattr(courier, "_put", put)
    courier.carry_out()
    assert out + "metrics/first" not in storage.objects
    assert storage.objects[out + "metrics/joined"] == b"joined"
    assert sum(method == "DELETE" for method, _ in storage.asked) == 1


def test_courier_keeps_old_files_when_replacement_upload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage()
    courier, _, track = machine(tmp_path, storage)
    (track / "metrics/first").write_bytes(b"first")
    courier.carry_out()
    (track / "metrics/joined").write_bytes(b"joined")
    (track / "metrics/w.removed").write_text("metrics/first\n")
    (track / "metrics/first").unlink()
    put = courier._put

    def unavailable(path: Path, run: str, key: str) -> None:
        if path.name == "joined":
            raise StorageUnavailable("offline")
        put(path, run, key)

    monkeypatch.setattr(courier, "_put", unavailable)
    with pytest.raises(StorageUnavailable):
        courier.carry_out()
    out = "etude/run1/"
    assert storage.objects[out + "metrics/first"] == b"first"
    assert sum(method == "DELETE" for method, _ in storage.asked) == 0
    monkeypatch.setattr(courier, "_put", put)
    courier.carry_out()
    assert out + "metrics/first" not in storage.objects
    assert storage.objects[out + "metrics/joined"] == b"joined"
    assert sum(method == "DELETE" for method, _ in storage.asked) == 1


def test_a_large_checkpoint_goes_in_parts_and_arrives_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(courier_module, "PART_BYTES", 4)
    storage = Storage()
    courier, attempt, _ = machine(tmp_path, storage)
    (attempt / "outputs/model.pt").write_bytes(b"0123456789")
    (attempt / "outputs/etude-result.json").write_text(
        json.dumps({"clocks": {"steps": 1}, "checkpoint": "model.pt"})
    )
    courier.carry_out()
    assert storage.objects["etude/run1/checkpoints/model.pt"] == b"0123456789"
    assert storage.uploads == {}  # the upload was finished, not left open
    sent = [what for method, what in storage.asked if method == "PUT" and "model.pt" in what]
    assert len(sent) == 3  # 4 + 4 + 2 bytes
    recorded = json.loads(storage.objects["etude/run1/attempts/a1.json"])
    assert recorded["result"]["outputs"][0]["size_bytes"] == 10


def test_a_file_that_vanishes_mid_pass_does_not_stop_the_others(tmp_path: Path) -> None:
    storage = Storage()
    courier, _, track = machine(tmp_path, storage)
    # Training removes its open log and joined blocks while the courier walks
    # the folder. A link to nothing stands in for a file gone since the listing.
    (track / "metrics/00000001-0123456789ab.gone").symlink_to(track / "metrics/absent")
    (track / "metrics/2-00000000-00000007-0123456789ab.blk").write_bytes(b"kept")
    (track / "state.json").write_text("{}")
    courier.carry_out()
    out = "etude/run1/"
    assert storage.objects[out + "metrics/2-00000000-00000007-0123456789ab.blk"] == b"kept"
    assert out + "state.json" in storage.objects  # sorted after the vanished file


def test_replaced_files_are_deleted_before_the_attempt_is_reported_finished(
    tmp_path: Path,
) -> None:
    storage = Storage()
    courier, attempt, track = machine(tmp_path, storage)
    recent = "metrics/1-00000000-00000007-0123456789ab.blk"
    (track / recent).write_bytes(b"recent")
    courier.carry_out()
    (track / "metrics/2-00000000-00000007-0123456789ab.blk").write_bytes(b"joined")
    (track / recent).unlink()
    (track / "metrics/w.removed").write_text(recent + "\n")
    (attempt / "finished.json").write_text("{}")
    courier.carry_out()
    assert f"etude/run1/{recent}" not in storage.objects
    # The launcher may release the machine once it reads the finished note, so
    # nothing may still be owed to storage when that note goes.
    asked = [what for _, what in storage.asked]
    deleted = max(i for i, (method, _) in enumerate(storage.asked) if method == "DELETE")
    finished = max(i for i, what in enumerate(asked) if what.endswith("attempts/a1.json"))
    assert deleted < finished


def test_finished_outputs_do_not_repeat_the_reported_checkpoint(tmp_path: Path) -> None:
    storage = Storage()
    courier, attempt, track = machine(tmp_path, storage)
    outputs = attempt / "outputs"
    (outputs / "checkpoint.pt").write_bytes(b"weights")
    (outputs / "steps-10.pt").hardlink_to(outputs / "checkpoint.pt")  # tracking's name for it
    (track / "checkpoints").mkdir()
    (track / "checkpoints/steps-10.pt").hardlink_to(outputs / "steps-10.pt")
    (outputs / "notes.txt").write_text("kept")
    (outputs / "etude-result.json").write_text(
        json.dumps({"clocks": {"steps": 10}, "checkpoint": "steps-10.pt"})
    )
    (attempt / "finished.json").write_text("{}")
    courier.carry_out()
    stored = sorted(key.removeprefix("etude/run1/") for key in storage.objects)
    assert stored == [
        "attempts/a1.json",
        "attempts/a1/notes.txt",
        "checkpoints/steps-10.pt",
    ]
    assert storage.objects["etude/run1/checkpoints/steps-10.pt"] == b"weights"
    assert storage.asked.count(("PUT", "etude/run1/checkpoints/steps-10.pt")) == 1
    courier.carry_out()
    assert storage.asked.count(("PUT", "etude/run1/checkpoints/steps-10.pt")) == 1
