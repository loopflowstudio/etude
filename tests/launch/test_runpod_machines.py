"""RunPod end to end with nothing real outside this machine.

Storage and the provider are stand-ins that speak the same HTTP the real ones
do. What runs "in the pod" is the real pod code: the courier in a thread, and
the real session process it starts.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import ValidationError

from etude.jobs import continue_from
from etude.launch.database import Database
from etude.launch.executor import Executor
from etude.launch.listing import show_machines, show_runs
from etude.launch.logging import CLOSED, log_to, write_log
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
from etude.launch.schemas import (
    WORKER_LOG,
    AttemptRecord,
    Bootstrap,
    Packet,
    Work,
    WorkerLog,
    attempt_log,
    log_piece,
)
from etude.launch.storage.bucket import BucketJournal
from etude.launch.storage.s3 import Bucket, Grant, Keys, Reply, StorageUnavailable
from etude.launch.storage.transport import emit
from etude.launch.worker import courier as courier_module
from etude.launch.worker.courier import Courier
from etude.launch.worker.courier import run as run_in_pod
from etude.run.records import ArtifactRef
from etude.train.records import Step
from etude.train.run import Run as TrackedRun
from etude.train.run import track
from tests.launch.counting import Counting, Counts, job_text

TRAIN = """
echo "seed $ETUDE_SEED to $ETUDE_UNTIL_VALUE"
echo "trained to $ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/weights.pt"
printf '{"clocks": {"steps": %s}}' "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"
"""

# A tracked job that resumes from its run's latest checkpoint, or its parent's.
TRACKED = """
import os
from pathlib import Path
from urllib.parse import urlparse
from etude.train.records import Step
from etude.train.run import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
latest = run.latest_checkpoint()
start = 0 if latest is None else int(latest.at["steps"])
kept = "" if latest is None else Path(urlparse(latest.artifact.uri).path).read_text()
target = int(os.environ["ETUDE_UNTIL_VALUE"])
print(f"seed {os.environ['ETUDE_SEED']} from {start} to {target}")
for step in range(start + 1, target + 1):
    run.log({"loss": 1.0 / step}, steps=step)
weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.pt"
weights.write_text(kept + f"trained to {target}\\n")
run.checkpoint(weights, steps=target)
run.finish()
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
    # Pods delete themselves from their own threads while the client reads them.
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        with self.lock:
            return self._answer(method, url, body)

    def _delete(self, id: str) -> None:
        with self.lock:
            self.pods.pop(id, None)
            self._deleted(id)

    def _deleted(self, id: str) -> None:
        with self.storage.lock:  # on the same timeline as storage requests and creates
            self.storage.asked.append(("TERMINATE", id))

    def _answer(self, method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
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
            pod["createdAt"] = datetime.now(UTC).isoformat()
            self.pods[id] = pod
            env = asked["env"]
            assert not any("KEY" in name or "SECRET" in name for name in env)
            assert asked["ports"] == ["22/tcp"] and asked["startSsh"] is True
            self.envs.append(env)
            thread = threading.Thread(
                target=run_in_pod,
                args=(env["ETUDE_BOOTSTRAP_URL"], lambda: self._delete(id)),
                kwargs={"root": self.root, "send": self.storage, "every": 0.05},
                daemon=True,
            )
            thread.start()
            if self.lose_reply:
                self.lose_reply = False
                raise OSError("the reply never arrived")
            return 200, json.dumps(pod).encode()
        if path == "pods":
            paging = {"hasNextPage": False, "nextCursor": None}
            return 200, json.dumps(
                {"pods": list(self.pods.values()), "pagination": paging}
            ).encode()
        id = path.removeprefix("pods/")
        if method == "DELETE":
            self.pods.pop(id, None)
            self._deleted(id)
            return 200, b""
        return (200, json.dumps(self.pods[id]).encode()) if id in self.pods else (404, b"")


class BucketObjects:
    """The in-memory bucket as tracking's readers see a bucket (`etude.run.storage.Storage`)."""

    def __init__(self, storage: Storage, bucket: str) -> None:
        self.storage, self.bucket = storage, bucket
        self.name = f"s3-{bucket}"

    def put(self, key: str, data: bytes) -> None:
        self.storage.objects[key] = data

    def put_file(self, key: str, path: Path) -> None:
        self.storage.objects[key] = path.read_bytes()

    def delete(self, key: str) -> bool:
        return self.storage.objects.pop(key, None) is not None

    def get(self, key: str) -> bytes:
        if key not in self.storage.objects:
            raise FileNotFoundError(key)
        return self.storage.objects[key]

    def get_range(self, key: str, start: int, length: int) -> bytes:
        return self.get(key)[start : start + length]

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.versions(prefix))

    def versions(self, prefix: str) -> dict[str, str]:
        found = self.storage.objects.items()
        return {
            key: hashlib.sha256(body).hexdigest() for key, body in found if key.startswith(prefix)
        }


@dataclass
class Cloud:
    scheduler: Scheduler
    storage: Storage
    provider: RunPod
    store: Database


def cloud(
    tmp_path: Path,
    *,
    delivery_seconds: float | None = None,
    gpu: str | None = "NVIDIA L4",
    r2: bool = False,
) -> Cloud:
    """A RunPod account and its storage: S3, or with `r2` an R2 bucket that has no API token."""
    storage = Storage()
    provider = RunPod(storage, tmp_path / "pod")
    store = Database(tmp_path / "launcher.db")
    account = Provider(name="runpod", kind="runpod", registry="reg1", storage="west")
    place = (
        Place(name="west", bucket="journals", prefix="etude/t", provider="r2")
        if r2
        else Place(
            name="west", bucket="journals", prefix="etude/t", provider="s3", region="us-west-2"
        )
    )
    template = MachineTemplate(
        provider="runpod",
        gpu=gpu,
        vcpus=6,
        memory_gb=48,
        disk_gb=40,
        image=Image(requested="r/e:fixture", resolved="r/e@sha256:" + "a" * 64),
        data_center="EU-RO-1",
    )
    keys = Keys(access_key="access", secret_key="secret")
    bucket = (
        Bucket("journals", "auto", keys, storage, account="account")
        if r2
        else Bucket("journals", "us-west-2", keys, storage)
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


def released(scheduler: Scheduler, run: str) -> Status:
    """The done run's status once its pod has delivered its last files and deleted itself."""
    for _ in range(400):
        scheduler.tick()
        status = scheduler.status(run)
        if status.rental_ended:
            return status
        time.sleep(0.05)
    raise AssertionError(f"the rental never ended: {status}")


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
    released(c.scheduler, refused)
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
    repo = repository(tmp_path)
    (repo / "train.py").write_text(TRACKED)
    subprocess.run(["git", "-C", str(repo), "add", "train.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        + ["commit", "-qm", "track"],
        check=True,
    )
    job = Job(
        command=f"{sys.executable} train.py",
        repo=str(repo),
        until="steps=3",
        max_hours=0.05,
        max_usd=Decimal(1),
    )
    first = c.scheduler.launch(Run(job=job, seed=7), rent="l4")
    c.scheduler.wait(first, every=0.05, timeout=30)
    status = released(c.scheduler, first)
    assert (status.outcome, status.clocks) == ("succeeded", {"steps": 3})
    assert job_text(c.scheduler.logs(run=first)) == "seed 7 from 0 to 3\n"
    assert c.provider.pods == {}  # the pod deleted itself once its work was delivered

    # The parent's checkpoint, resolved from the bucket, reaches the next pod as an input.
    parent = c.scheduler.record(first)
    second = c.scheduler.launch(
        continue_from(parent, "steps=7", source=BucketObjects(c.storage, "journals")), rent="l4"
    )
    c.scheduler.wait(second, every=0.05, timeout=30)
    status = released(c.scheduler, second)
    assert (status.outcome, status.clocks) == ("succeeded", {"steps": 7})
    assert job_text(c.scheduler.logs(run=second)) == "seed 7 from 3 to 7\n"
    copy = c.scheduler.fetch(second, "weights.pt", tmp_path / "out")
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
    # The pod was deleted after its end note and the tail of its log arrived.
    assert {"packet.json", "bootstrap.json", "inbox.json", "receipt.json"} <= set(keys)
    assert {"out/ended", "out/worker-log.json"} <= set(keys)
    # Everything about the run is in its own folder, whichever machine wrote it.
    attempt = c.scheduler.attempts(second)[0].id
    folder = f"etude/t/{second}/"
    keys = [k.removeprefix(folder) for k in c.storage.objects if k.startswith(folder)]
    assert {f"attempts/{attempt}.json", log_piece(attempt_log(attempt), 0)} <= set(keys)
    assert {"run.json", "checkpoints/steps-7.json", "checkpoints/steps-7.pt"} <= set(keys)


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


def test_an_r2_storage_that_cannot_grant_keys_fails_the_launch_before_renting(
    tmp_path: Path,
) -> None:
    c = cloud(tmp_path, r2=True)
    with pytest.raises(LauncherError, match="R2_API_TOKEN"):
        c.scheduler.launch(Job(command="true", max_hours=0.05, max_usd=Decimal(1)), rent="l4")
    assert (c.provider.creates, c.scheduler.runs(done=True)) == (0, ())
    # Refused, not unknown: no pod was asked for, so none is looked for later.
    (session,) = c.store.sessions()
    assert session.refused_at is not None and session.receipt is None
    assert not any(key.endswith("packet.json") for key in c.storage.objects)


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


def test_shared_inputs_are_prepared_once(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    storage = Storage()
    counting = Counting(Database(tmp_path / "counting.db"))
    journal = BucketJournal(
        Place(name="west", bucket="journal", prefix="test", provider="s3", region="us-west-2"),
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
        config=None,
        repo=str(repo),
        commit=commit,
        run="one",
    )
    packet = Packet(
        session="shared",
        deadline=None,
        work=(work, work.model_copy(update={"attempt": "b", "run": "two"})),
    )
    with counting.record():
        journal.prepare(packet)
    # Packet, one source listing/upload, inbox, bootstrap.
    assert counting.snapshot() == Counts(bucket=5, git=1)
    bootstrap = Bootstrap.model_validate_json(
        storage.objects["test/sessions/shared/bootstrap.json"]
    )
    assert len(bootstrap.fetch) == 1
    source = bootstrap.fetch[f"shared/sources/{commit}.tar"]
    archive = storage.objects[f"test/sources/{source.sha256}.tar"]  # shared by every session
    assert (source.sha256, source.size_bytes) == (hashlib.sha256(archive).hexdigest(), len(archive))
    with counting.record():
        journal.stage(str(repo), commit)  # already staged: no git, no bucket
    assert counting.snapshot() == Counts()


def test_a_rented_sessions_grants_last_until_its_deadline(tmp_path: Path) -> None:
    class Federation(Storage):
        def __init__(self) -> None:
            super().__init__()
            self.durations: list[int] = []

        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> Reply:
            if urlsplit(url).netloc.startswith("sts."):
                self.durations.append(int(parse_qs((body or b"").decode())["DurationSeconds"][0]))
            return super().__call__(method, url, body, headers)

    storage = Federation()
    journal = BucketJournal(
        Place(name="west", bucket="journal", prefix="test", provider="s3", region="us-west-2"),
        Bucket("journal", "us-west-2", Keys(access_key="access", secret_key="secret"), storage),
    )
    work = Work(
        attempt="a",
        command="true",
        limit_seconds=30 * 3600,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
        run="one",
    )
    deadline = datetime.now(UTC) + timedelta(hours=30)
    journal.prepare(Packet(session="long", deadline=deadline, work=(work,)))
    # The session's own folder and its run's, each for the whole 30 hours.
    assert len(storage.durations) == 2
    assert all(30 * 3600 - 60 <= seconds <= 30 * 3600 for seconds in storage.durations)


def test_lost_start_receipt_reply_cannot_leave_unreported_running_code(tmp_path: Path) -> None:
    class LostReceipt(Storage):
        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> Reply:
            response = super().__call__(method, url, body, headers)
            if method in ("POST", "PUT") and "test/sessions/r/out/receipt" in self.objects:
                return Reply(status=503, body=b"")
            return response

    storage = LostReceipt()
    bucket = Bucket("journal", "us-west-2", Keys(access_key="access", secret_key="secret"), storage)
    journal = BucketJournal(
        Place(name="west", bucket="journal", prefix="test", provider="s3", region="us-west-2"),
        bucket,
    )
    work = Work(
        attempt="a",
        command="echo ran",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
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
    access = held.grant("etude/run1/", 3600, "run1")
    home = tmp_path / "pod/sessions/r"
    attempt = home / "attempts/a1"
    (attempt / "outputs").mkdir(parents=True)
    (attempt / "run").write_text("run1")
    track = tmp_path / "pod/track/run1"
    (track / "metrics").mkdir(parents=True)
    session = Grant(
        prefix="etude/sessions/r/out/", form=held.post_form("etude/sessions/r/out/", 3600)
    )
    courier = Courier(home, session, "unused", storage, {"run1": access}, home / "worker.log")
    return courier, attempt, track


def test_a_twelve_hour_log_is_sent_once_in_a_few_hundred_pieces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage()
    courier, attempt, _ = machine(tmp_path, storage)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    log = attempt / "log"
    log.write_bytes(b"")
    # A line a second, carried every 15 seconds (the machine's loop carries every 3).
    with log.open("ab") as job:
        for second in range(12 * 3600):
            job.write(f"step {second}\n".encode())
            if second % 15 == 0:
                job.flush()
                now[0] = float(second)
                courier.carry_out()
    (attempt / "finished.json").write_text("{}")
    (courier._home / "session.log").touch()
    courier.carry_out(final=True)
    folder = "etude/run1/attempts/"
    pieces = sorted(k for k in storage.objects if k.startswith(folder + "a1.log/"))
    puts = [key for method, key in storage.asked if method in ("PUT", "POST")]
    # Each piece is written once, so the bytes sent are the log's bytes, once.
    assert sorted(key for key in puts if key in pieces) == pieces
    assert b"".join(storage.objects[key] for key in pieces) == log.read_bytes()
    # The record is rewritten only to name a new piece, the last with its ending.
    assert (len(pieces), puts.count(folder + "a1.json")) == (197, 197)
    record = AttemptRecord.model_validate_json(storage.objects[folder + "a1.json"])
    assert ["etude/run1/" + key for key in record.log_pieces("a1")] == pieces


def test_a_growing_worker_log_is_sent_once_in_appended_pieces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Writes(Storage):
        """Counts the times each object is written, whether by PUT or by form."""

        def __init__(self) -> None:
            super().__init__()
            self.writes: Counter[str] = Counter()

        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> Reply:
            before = dict(self.objects)
            reply = super().__call__(method, url, body, headers)
            self.writes.update(k for k, v in self.objects.items() if before.get(k) is not v)
            return reply

    storage = Writes()
    courier, _, _ = machine(tmp_path, storage)
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    log = courier._home / "worker.log"
    # A line every ten seconds for two hours, carried every fifteen.
    with log_to(log):
        for second in range(0, 2 * 3600, 5):
            if second % 10 == 0:
                write_log("inbox", f"{second} work, 0 to stop")
            if second % 15 == 0:
                now[0] = float(second)
                courier.carry_out()
        write_log("worker", CLOSED)
        courier.carry_out(final=True)
    out = "etude/sessions/r/out/"
    pieces = sorted(k for k in storage.objects if k.startswith(out + WORKER_LOG + "/"))
    # Each piece is sent once, so the bytes sent are the log's new bytes, each once,
    # where a whole upload every fifteen seconds sent the log hundreds of times over.
    assert all(storage.writes[key] == 1 for key in pieces)
    assert b"".join(storage.objects[key] for key in pieces) == log.read_bytes()
    # The record is rewritten only to name a new piece.
    assert (len(pieces), storage.writes[out + "worker-log.json"]) == (77, 77)
    record = WorkerLog.model_validate_json(storage.objects[out + "worker-log.json"])
    assert [out + key for key in record.pieces()] == pieces


def test_a_finished_rental_delivers_its_end_and_log_tail_before_it_is_deleted(
    tmp_path: Path,
) -> None:
    c = cloud(tmp_path)
    run = c.scheduler.launch(
        Job(command="echo done", max_hours=0.05, max_usd=Decimal(1)), rent="l4"
    )
    c.scheduler.wait(run, every=0.05, timeout=30)
    released(c.scheduler, run)
    timeline = [(method, key.partition("?")[0]) for method, key in c.storage.asked]
    session = c.scheduler.attempts(run)[0].session
    out = f"etude/t/sessions/{session}/out/"
    sent = [i for i, (method, key) in enumerate(timeline) if method in ("PUT", "POST")]
    (terminated,) = [i for i, (method, _) in enumerate(timeline) if method == "TERMINATE"]
    (ended,) = [i for i in sent if timeline[i][1] == out + "ended"]
    tail = max(i for i in sent if timeline[i][1].startswith(out + WORKER_LOG + "/"))
    # The log's last piece, and the record naming it, arrive before the end receipt,
    # and the machine is deleted only after that.
    named = max(i for i in sent if timeline[i][1] == out + "worker-log.json")
    assert tail < named < ended < terminated
    worker = next(s for s in c.scheduler.logs(run=run).streams if s.producer == "worker")
    assert worker.complete and worker.text.rstrip().endswith(CLOSED)


def test_output_printed_during_an_upload_waits_for_the_next_piece(tmp_path: Path) -> None:
    class Printing(Storage):
        """The job prints a line while each piece of its log is on the way."""

        def __call__(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> Reply:
            if method == "PUT" and ".log/" in url:
                with log.open("ab") as job:
                    job.write(b"printed meanwhile\n")
            return super().__call__(method, url, body, headers)

    storage = Printing()
    courier, attempt, _ = machine(tmp_path, storage)
    log = attempt / "log"
    log.write_bytes(b"first\n")
    courier.carry_out()
    pieces = [k for k in storage.objects if ".log/" in k]
    assert [storage.objects[k] for k in pieces] == [b"first\n"]


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
    courier, _, track = machine(tmp_path, storage)
    (track / "checkpoints").mkdir()
    (track / "checkpoints/steps-1.pt").write_bytes(b"0123456789")
    courier.carry_out()
    assert storage.objects["etude/run1/checkpoints/steps-1.pt"] == b"0123456789"
    assert storage.uploads == {}  # the upload was finished, not left open
    sent = [what for method, what in storage.asked if method == "PUT" and "steps-1.pt" in what]
    assert len(sent) == 3  # 4 + 4 + 2 bytes


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


def test_finished_outputs_go_under_the_attempt_and_checkpoints_under_the_run(
    tmp_path: Path,
) -> None:
    storage = Storage()
    courier, attempt, track = machine(tmp_path, storage)
    outputs = attempt / "outputs"
    (outputs / "weights.pt").write_bytes(b"weights")
    (track / "checkpoints").mkdir()
    (track / "checkpoints/steps-10.pt").write_bytes(b"weights")
    (outputs / "etude-result.json").write_text(json.dumps({"clocks": {"steps": 10}}))
    (attempt / "finished.json").write_text("{}")
    courier.carry_out()
    stored = sorted(key.removeprefix("etude/run1/") for key in storage.objects)
    assert stored == ["attempts/a1.json", "attempts/a1/weights.pt", "checkpoints/steps-10.pt"]
    recorded = json.loads(storage.objects["etude/run1/attempts/a1.json"])
    assert recorded["result"] == {"clocks": {"steps": 10.0}}
    courier.carry_out()
    assert storage.asked.count(("PUT", "etude/run1/checkpoints/steps-10.pt")) == 1


def _tracked(root: Path, run: str = "run1") -> TrackedRun:
    """A tracked run writing into a machine's track folder, as a launched job's would."""
    return track(
        experiment="capacity",
        clocks=[Step("steps", "training steps")],
        run=run,
        directory=root,
        segment_seconds=0.05,
    )


def _kept(folder: Path, records: int) -> None:
    """Wait for the tracker's writer to have kept this many checkpoint records."""
    deadline = time.monotonic() + 10
    while len(list((folder / "checkpoints").glob("*.json"))) < records:
        assert time.monotonic() < deadline, "the tracker never kept its record"
        time.sleep(0.01)


def _sent(storage: Storage, part: str) -> list[str]:
    """Keys written under the run's checkpoints, in the order they were sent."""
    return [
        what.removeprefix("etude/run1/")
        for method, what in storage.asked
        if method in ("PUT", "POST") and part in what
    ]


class Observed(Storage):
    """The bucket, reporting each request as the real transport does, so usage sees it."""

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        reply = super().__call__(method, url, body, headers)
        emit(method, url, supplied=len(body or b""), status=reply.status, completed=True)
        return reply


def test_each_checkpoint_reaches_storage_while_the_attempt_runs(tmp_path: Path) -> None:
    storage = Observed()
    courier, attempt, folder = machine(tmp_path, storage)
    run = _tracked(folder.parent)
    weights = tmp_path / "weights.pt"
    for step in (1, 2, 3):
        weights.write_bytes(f"weights at {step}".encode())
        run.checkpoint(weights, steps=step)
        _kept(folder, step)
        courier.carry_out()
        assert storage.objects[f"etude/run1/checkpoints/steps-{step}.pt"] == weights.read_bytes()
    assert not (attempt / "finished.json").exists()
    # One upload per file and per record, each file before its record.
    assert _sent(storage, "/checkpoints/") == [
        name
        for step in (1, 2, 3)
        for name in (f"checkpoints/steps-{step}.pt", f"checkpoints/steps-{step}.json")
    ]
    journal = (courier._home / "usage.jsonl").read_text().splitlines()
    usage = [json.loads(line) for line in journal]
    purposes = {
        u["key"].rpartition("/")[2]: u["purpose"] for u in usage if "/checkpoints/" in u["key"]
    }
    assert purposes == {
        **{f"steps-{step}.pt": "checkpoint" for step in (1, 2, 3)},
        **{f"steps-{step}.json": "report" for step in (1, 2, 3)},
    }

    # The same model again changes nothing; another at a recorded coordinate is a new version.
    first = storage.objects["etude/run1/checkpoints/steps-3.json"]
    again = run.checkpoint(weights, steps=3)
    courier.carry_out()
    assert len(_sent(storage, "/checkpoints/")) == 6
    latest = run.latest_checkpoint()
    assert latest is not None and (latest.ordinal, latest.artifact.sha256) == (3, again.sha256)
    weights.write_bytes(b"retrained")
    run.checkpoint(weights, steps=3)
    _kept(folder, 4)
    courier.carry_out()
    assert _sent(storage, "/checkpoints/")[6:] == [
        "checkpoints/steps-3.ordinal-4.pt",
        "checkpoints/steps-3.ordinal-4.json",
    ]
    assert storage.objects["etude/run1/checkpoints/steps-3.pt"] == b"weights at 3"
    assert storage.objects["etude/run1/checkpoints/steps-3.json"] == first
    assert storage.objects["etude/run1/checkpoints/steps-3.ordinal-4.pt"] == b"retrained"

    # Interrupted: the job never finishes, and storage already holds every checkpoint.
    (courier._home / "session.log").touch()
    courier.carry_out(final=True)
    kept = [k for k in storage.objects if k.startswith("etude/run1/checkpoints/")]
    assert len(kept) == 8
    run.finish()


def test_a_retry_on_a_fresh_machine_resumes_from_the_runs_latest_checkpoint(
    tmp_path: Path,
) -> None:
    storage = Storage()
    courier, _, folder = machine(tmp_path / "one", storage)
    run = _tracked(folder.parent)
    weights = tmp_path / "weights.pt"
    for step in (1, 2, 3):
        weights.write_bytes(f"weights at {step}".encode() * 100)
        run.checkpoint(weights, steps=step)
    _kept(folder, 3)
    run.finish()
    courier.carry_out()
    written = sum(len(body) for key, body in storage.objects.items() if "/checkpoints/" in key)

    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
    )
    retry = Work(
        attempt="a2",
        command="true",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
        run="run1",
        resumes=True,
    )
    asked = len(storage.asked)
    journal.prepare(Packet(session="r2", deadline=None, work=(retry,)))
    listed = [what for _, what in storage.asked[asked:] if "list-type" in what]
    assert len(listed) == 1  # links are made, not read
    bootstrap = Bootstrap.model_validate_json(storage.objects["etude/sessions/r2/bootstrap.json"])

    fresh, _, there = machine(tmp_path / "two", storage)
    asked = len(storage.asked)
    fresh.restore(bootstrap.restore)
    reads = [what.partition("?")[0] for method, what in storage.asked[asked:] if method == "GET"]
    # The declaration and every record, then only the file the latest record names.
    assert reads == [
        "etude/run1/checkpoints/steps-1.json",
        "etude/run1/checkpoints/steps-2.json",
        "etude/run1/checkpoints/steps-3.json",
        "etude/run1/run.json",
        "etude/run1/checkpoints/steps-3.pt",
    ]
    assert sum(len(storage.objects[key]) for key in reads) <= 2 * written

    resumed = _tracked(there.parent)
    latest = resumed.latest_checkpoint()
    assert latest is not None and latest.ordinal == 3
    assert Path(urlsplit(latest.artifact.uri).path).read_bytes() == b"weights at 3" * 100
    weights.write_bytes(b"weights at 4")
    resumed.checkpoint(weights, steps=4)
    _kept(there, 4)
    resumed.finish()
    asked = len(storage.asked)
    fresh.carry_out()
    sent = [
        what
        for method, what in storage.asked[asked:]
        if method == "PUT" and "/checkpoints/" in what
    ]
    # Nothing restored goes back, and ordinals continue.
    assert sent == ["etude/run1/checkpoints/steps-4.pt", "etude/run1/checkpoints/steps-4.json"]
    assert json.loads(storage.objects["etude/run1/checkpoints/steps-4.json"])["ordinal"] == 4


def _orphaned(tmp_path: Path, storage: Storage) -> tuple[Courier, Path, Path]:
    """Two checkpoints recorded, then a writer that died after keeping steps-3.pt, before its record."""
    courier, _, folder = machine(tmp_path, storage)
    run = _tracked(folder.parent)
    weights = tmp_path / "weights.pt"
    for step in (1, 2):
        weights.write_bytes(f"weights at {step}".encode())
        run.checkpoint(weights, steps=step)
    _kept(folder, 2)
    run.finish()
    (folder / "checkpoints/steps-3.pt").write_bytes(b"orphan")
    courier.carry_out()
    assert storage.objects["etude/run1/checkpoints/steps-3.pt"] == b"orphan"
    return courier, folder, weights


def _retrained(courier: Courier, folder: Path, weights: Path) -> ArtifactRef:
    """The next attempt resumes from steps 2 and reaches steps 3 with different bytes."""
    resumed = _tracked(folder.parent)
    latest = resumed.latest_checkpoint()
    assert latest is not None and latest.ordinal == 2
    weights.write_bytes(b"weights at 3")
    reference = resumed.checkpoint(weights, steps=3)
    _kept(folder, 3)
    resumed.finish()
    courier.carry_out()
    return reference


def _intact(storage: Storage) -> None:
    """The orphan's key holds its one upload, and the new record names bytes storage holds."""
    assert storage.asked.count(("PUT", "etude/run1/checkpoints/steps-3.pt")) == 1
    assert storage.objects["etude/run1/checkpoints/steps-3.pt"] == b"orphan"
    record = json.loads(storage.objects["etude/run1/checkpoints/steps-3.ordinal-3.json"])
    kept = storage.objects["etude/run1/checkpoints/steps-3.ordinal-3.pt"]
    assert (record["ordinal"], record["artifact"]["sha256"]) == (
        3,
        hashlib.sha256(kept).hexdigest(),
    )


def test_a_retry_on_the_same_host_never_reuses_a_file_left_without_its_record(
    tmp_path: Path,
) -> None:
    storage = Storage()
    courier, folder, weights = _orphaned(tmp_path, storage)
    reference = _retrained(courier, folder, weights)
    assert reference.uri.endswith("/steps-3.ordinal-3.pt")
    assert (folder / "checkpoints/steps-3.pt").read_bytes() == b"orphan"
    _intact(storage)


def test_a_retry_on_a_fresh_machine_never_reuses_a_name_storage_holds(tmp_path: Path) -> None:
    storage = Storage()
    _orphaned(tmp_path / "one", storage)
    journal = BucketJournal(
        Place(name="east", bucket="b", prefix="etude", provider="s3", region="us-east-1"),
        Bucket("b", "us-east-1", Keys(access_key="AKIA", secret_key="secret"), storage),
    )
    retry = Work(
        attempt="a2",
        command="true",
        limit_seconds=30,
        limit_kind="out_of_time",
        seed=None,
        until=None,
        config=None,
        repo=None,
        commit=None,
        run="run1",
        resumes=True,
    )
    journal.prepare(Packet(session="r2", deadline=None, work=(retry,)))
    bootstrap = Bootstrap.model_validate_json(storage.objects["etude/sessions/r2/bootstrap.json"])
    fresh, _, there = machine(tmp_path / "two", storage)
    fresh.restore(bootstrap.restore)
    assert not (there / "checkpoints/steps-3.pt").exists()  # listed, never read
    _retrained(fresh, there, tmp_path / "one" / "weights.pt")
    _intact(storage)
