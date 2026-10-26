"""A job's preparation on a real local pool: kept, reused, rejected and rebuilt;
and its carried directory, restored on a new machine from a bucket held in memory."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from etude.launch.logging import log_to
from etude.launch.records import Input, Job, Preparation, Runtime, Status
from etude.launch.scheduler import Scheduler
from etude.launch.storage.s3 import BucketAccess, Grant, Keys, PostForm, Reply, StorageUnavailable
from etude.launch.worker.carried import REQUEST, Locator, restore
from etude.launch.worker.courier import Courier
from etude.launch.worker.prepared import Key, Receipt, carried_home, carry, evict, home, prepare
from tests.launch.counting import job_text
from tests.launch.test_worker_attempts import local

PREPARE = """
echo built >> "{count}"
cp lock.txt "$ETUDE_PREPARED_DIR/lock"
for name in $(ls "$ETUDE_PREPARED_INPUTS" 2>/dev/null); do echo "saw $name" >> "{count}"; done
"""
RUN = 'cat "$ETUDE_PREPARED_DIR/lock"'


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lock.txt").write_text("lock 1\n")
    (repo / "other.txt").write_text("other 1\n")
    _commit(repo)
    return repo


def _commit(repo: Path) -> None:
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "change"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)


def _input(tmp_path: Path, name: str, body: bytes) -> Input:
    """An input this machine already holds in a run's folder, so no storage is needed."""
    folder = tmp_path / "track" / "given"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(body)
    return Input(
        name=name,
        key=name,
        run="given",
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
    )


def _job(repo: Path, count: Path, **changes: object) -> Job:
    job = Job(
        command=RUN,
        repo=str(repo),
        revision="HEAD",
        prepare=Preparation(command=PREPARE.format(count=count), paths=("lock.txt",)),
        max_hours=0.1,
        max_usd=Decimal(1),
        max_attempts=1,
    )
    return job.model_copy(update=changes)


def _launch(scheduler: Scheduler, job: Job) -> tuple[Status, str]:
    run = scheduler.launch(job)
    (status,) = scheduler.wait(run, every=0.05, timeout=60)
    return status, job_text(scheduler.logs(run=run))


def _outcomes(log: str) -> list[str]:
    return [line.split()[1] for line in log.splitlines() if line.startswith("prepare: ")]


def test_a_second_attempt_reuses_the_prepared_directory(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    first, log = _launch(scheduler, _job(repo, count))
    assert first.outcome == "succeeded" and _outcomes(log) == ["built"]
    second, log = _launch(scheduler, _job(repo, count))
    assert second.outcome == "succeeded" and _outcomes(log) == ["reused"]
    assert log.endswith("lock 1\n")
    assert count.read_text() == "built\n"  # the command ran once


def test_only_declared_paths_and_inputs_decide_the_key(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    engine, weights = _input(tmp_path, "engine", b"engine 1"), _input(tmp_path, "weights", b"w1")
    prepare = Preparation(
        command=PREPARE.format(count=count), paths=("lock.txt",), inputs=("engine",)
    )
    job = _job(repo, count, prepare=prepare, inputs=(engine, weights))
    assert _outcomes(_launch(scheduler, job)[1]) == ["built"]
    assert count.read_text() == "built\nsaw engine\n"  # undeclared inputs are not visible

    (repo / "other.txt").write_text("other 2\n")
    _commit(repo)
    assert _outcomes(_launch(scheduler, job)[1]) == ["reused"]

    new_weights = _input(tmp_path, "weights2", b"w2").model_copy(update={"name": "weights"})
    assert _outcomes(
        _launch(scheduler, job.model_copy(update={"inputs": (engine, new_weights)}))[1]
    ) == ["reused"]

    new_engine = _input(tmp_path, "engine2", b"engine 2").model_copy(update={"name": "engine"})
    assert _outcomes(
        _launch(scheduler, job.model_copy(update={"inputs": (new_engine, weights)}))[1]
    ) == ["built"]

    (repo / "lock.txt").write_text("lock 2\n")
    _commit(repo)
    status, log = _launch(scheduler, job)
    assert _outcomes(log) == ["built"] and log.endswith("lock 2\n")


def test_reading_an_undeclared_input_fails_the_build(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    weights = _input(tmp_path, "weights", b"w1")
    prepare = Preparation(
        command='cat "$ETUDE_INPUTS_DIR/weights" > "$ETUDE_PREPARED_DIR/w"', paths=("lock.txt",)
    )
    status, log = _launch(local(tmp_path), _job(repo, count, prepare=prepare, inputs=(weights,)))
    assert status.outcome == "failed"
    assert "/weights" in log and "phase prepare: exit 1" in log
    assert not list(home().glob("*.json"))  # no receipt


def test_an_unknown_input_name_is_refused_at_launch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not given"):
        Job(
            command="true",
            max_hours=1,
            prepare=Preparation(command="true", paths=("lock.txt",), inputs=("engine",)),
        )


def test_a_changed_prepared_file_is_rejected_and_rebuilt(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    assert _outcomes(_launch(scheduler, _job(repo, count))[1]) == ["built"]
    (prepared,) = (p for p in home().iterdir() if p.is_dir())
    (prepared / "lock").chmod(0o644)
    (prepared / "lock").write_text("lock X\n")
    status, log = _launch(scheduler, _job(repo, count))
    assert status.outcome == "succeeded" and _outcomes(log) == ["rejected", "built"]
    assert "(lock digest mismatch)" in log and log.endswith("lock 1\n")


def test_a_build_stopped_at_its_deadline_leaves_nothing(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    stuck = Preparation(
        command='touch "$ETUDE_PREPARED_DIR/partial"; sleep 30', paths=("lock.txt",)
    )
    status, log = _launch(local(tmp_path), _job(repo, count, prepare=stuck, max_hours=2 / 3600))
    assert status.outcome == "out_of_time" and "phase prepare: deadline" in log
    assert not [p for p in home().iterdir() if p.suffix != ".lock"]


def test_a_killed_build_leaves_no_receipt_and_is_built_again(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    finish = tmp_path / "finish"
    command = (
        f'if [ -f "{finish}" ]; then touch "$ETUDE_PREPARED_DIR/done";'
        ' else touch "$ETUDE_PREPARED_DIR/partial"; sleep 5; fi'
    )
    prepare = Preparation(command=command, paths=("lock.txt",))
    run = scheduler.launch(_job(repo, count, prepare=prepare, command="true", max_attempts=2))
    for _ in range(300):
        partial = [p for p in home().glob("*/partial")] if home().exists() else []
        if partial:
            break
        time.sleep(0.05)
    (leftover,) = (p.parent for p in partial)
    assert not list(home().glob("*.json"))
    finish.touch()
    session = scheduler.attempts(run)[0].session
    os.kill(int((tmp_path / "sessions" / session / "receipt").read_text()), signal.SIGKILL)
    (status,) = scheduler.wait(run, every=0.05, timeout=60)
    assert (status.outcome, status.attempts) == ("succeeded", 2)
    assert _outcomes(job_text(scheduler.logs(run=run))) == ["built"]
    assert sorted(p.name for p in leftover.iterdir()) == ["done"]


def test_a_reused_runtime_is_still_checked_against_its_pins(tmp_path: Path) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    venv = Preparation(
        command='python3 -m venv --without-pip "$ETUDE_PREPARED_DIR/.venv"', paths=("lock.txt",)
    )
    runtime = Runtime(python=".venv/bin/python")
    job = _job(repo, count, prepare=venv, runtime=runtime, command='"$ETUDE_PYTHON" -c "print(7)"')
    status, log = _launch(scheduler, job)
    assert status.outcome == "succeeded" and log.endswith("7\n")
    pinned = runtime.model_copy(update={"packages": {"etude-sdk": "0.0.0"}})
    status, log = _launch(scheduler, job.model_copy(update={"runtime": pinned}))
    assert status.outcome == "failed" and _outcomes(log) == ["reused"]
    assert "No package metadata was found for etude-sdk" in log


def test_eviction_removes_the_least_recently_used_directory_not_one_in_use(
    tmp_path: Path,
) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    for text in ("lock a\n", "lock b\n", "lock c\n"):
        (repo / "lock.txt").write_text(text)
        _commit(repo)
        _launch(scheduler, _job(repo, count))
    receipts = sorted(home().glob("*.json"), key=lambda p: p.stat().st_mtime)
    oldest, middle, newest = receipts
    assert Receipt.model_validate_json(newest.read_text()).size_bytes > 0
    with (home() / f"{middle.stem}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)  # an attempt is using it
        lines: list[str] = []
        evict(0, lines.append)
    assert [p.name for p in home().glob("*.json")] == [middle.name]
    assert not (home() / oldest.stem).exists() and not (home() / newest.stem).exists()
    assert lines[-1].endswith("bytes over allowance; the rest is in use")


CARRY = """
test -f "$ETUDE_CARRIED_DIR/wheel" && echo "found wheel" >> "{count}"
echo wheel > "$ETUDE_CARRIED_DIR/wheel"
cp lock.txt "$ETUDE_PREPARED_DIR/lock"
"""


def test_a_changed_declared_input_rebuilds_with_the_same_carried_directory(
    tmp_path: Path,
) -> None:
    repo, count = _repo(tmp_path), tmp_path / "count"
    scheduler = local(tmp_path)
    prepare = Preparation(
        command=CARRY.format(count=count), paths=("lock.txt",), inputs=("engine",), carry=True
    )
    engine = _input(tmp_path, "engine", b"engine 1")
    job = _job(repo, count, prepare=prepare, inputs=(engine,))
    assert _outcomes(_launch(scheduler, job)[1]) == ["built"]
    assert not count.exists()  # nothing carried yet

    new_engine = _input(tmp_path, "engine2", b"engine 2").model_copy(update={"name": "engine"})
    status, log = _launch(scheduler, job.model_copy(update={"inputs": (new_engine,)}))
    assert status.outcome == "succeeded" and _outcomes(log) == ["built"]
    assert count.read_text() == "found wheel\n"
    assert len([p for p in carried_home().iterdir() if p.is_dir()]) == 1

    (repo / "lock.txt").write_text("lock 2\n")  # a declared path: a new carry key too
    _commit(repo)
    assert _outcomes(_launch(scheduler, job)[1]) == ["built"]
    assert count.read_text() == "found wheel\n"
    assert len([p for p in carried_home().iterdir() if p.is_dir()]) == 2


class Shelf:
    """A bucket's `prepared/` folder, held in memory and reached as S3 is."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.refused = ""  # PUTs of keys ending in this are refused
        self.grant = Grant(
            prefix="p/prepared/",
            bucket=BucketAccess(
                name="lab",
                region="r",
                host="lab.example",
                keys=Keys(access_key="a", secret_key="s"),
            ),
        )

    def send(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        key = urlsplit(url).path[1:]
        if method == "PUT":
            if self.refused and key.endswith(self.refused):
                return Reply(status=403, body=b"")
            self.objects[key] = body or b""
            return Reply(status=200, body=b"", headers={"etag": '"stored"'})
        found = self.objects.get(key)
        if found is None:
            return Reply(status=404, body=b"")
        etag = f'"{hashlib.md5(found).hexdigest()}"'
        start, _, end = headers.get("Range", "bytes=0-").removeprefix("bytes=").partition("-")
        part = found[int(start) : int(end or len(found) - 1) + 1]
        fields = {"etag": etag, "content-length": str(len(part))}
        if "Range" not in headers:
            return Reply(status=200, body=part, headers=fields)
        fields["content-range"] = f"bytes {start}-{end}/{len(found)}"
        return Reply(status=206, body=part, headers=fields)

    def locator(self, key: str) -> Locator | None:
        raw = self.objects.get(f"p/prepared/{key}.json")
        return None if raw is None else Locator.model_validate_json(raw)


KEY = Key(command="stub", paths={"uv.lock": "1"}, inputs={}, environment="", machine={"os": "t"})


def build_on(machine: Path, shelf: Shelf, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], str]:
    """One attempt's preparation on `machine`, as a worker composes it; its log and what
    its command found carried. A build that restored nothing asks to be saved."""
    monkeypatch.setenv("ETUDE_HOME", str(machine))
    lines: list[str] = []
    found = ""

    def fetch(directory: Path) -> int | None:
        return restore(shelf.grant, KEY.carry, directory, "attempt", shelf.send)

    def build(directory: Path) -> bool:
        nonlocal found
        with carry(KEY.carry, fetch, lambda: True, lines.append) as carried:
            assert carried is not None
            wheel = carried.directory / "wheel"
            found = wheel.read_text() if wheel.exists() else ""
            wheel.write_text("wheel 1")
            (directory / "python").write_text("stub")
            if not carried.kept and carried.restored_bytes is None:
                (machine / REQUEST).write_text(KEY.carry)
        return True

    with prepare(KEY, build, lambda: True, lines.append) as ready:
        assert ready is not None
    return lines, found


def save_from(machine: Path, shelf: Shelf) -> str:
    """What the machine's courier logs as it carries out an attempt that asked to be saved."""
    session = machine / "sessions" / "s1"
    attempt = session / "attempts" / "a1"
    attempt.mkdir(parents=True)
    (attempt / "run").write_text("r1")
    if (machine / REQUEST).exists():
        (machine / REQUEST).replace(attempt / REQUEST)
    form = PostForm(url="https://lab.example/", fields={})
    courier = Courier(
        session,
        Grant(prefix="p/sessions/s1/out/", form=form),
        "https://lab.example/inbox",
        shelf.send,
        runs={"r1": Grant(prefix="p/r1/", bucket=shelf.grant.bucket)},
    )
    courier.allow_carry(shelf.grant)
    with log_to(machine / "worker.log"):
        courier.carry_out()
        courier.carry_out()  # saved once
    return (machine / "worker.log").read_text()


def test_a_new_machine_restores_the_carried_directory_then_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = Shelf()
    lines, found = build_on(tmp_path / "a", shelf, monkeypatch)
    assert _outcomes("\n".join(lines)) == ["built"] and found == ""
    log = save_from(tmp_path / "a", shelf)
    locator = shelf.locator(KEY.carry)
    assert locator is not None and f"p/prepared/{locator.archive}" in shelf.objects
    assert log.count(f"saved {KEY.carry[:12]} ({locator.size_bytes} bytes)") == 1

    lines, found = build_on(tmp_path / "b", shelf, monkeypatch)  # a new rental
    assert _outcomes("\n".join(lines)) == ["restored", "built"] and found == "wheel 1"
    assert not (tmp_path / "b" / REQUEST).exists()  # restored, so not saved again


def test_a_corrupted_carried_archive_is_refused_and_the_build_starts_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = Shelf()
    build_on(tmp_path / "a", shelf, monkeypatch)
    save_from(tmp_path / "a", shelf)
    locator = shelf.locator(KEY.carry)
    assert locator is not None
    archive = f"p/prepared/{locator.archive}"
    shelf.objects[archive] = b"X" + shelf.objects[archive][1:]

    lines, found = build_on(tmp_path / "b", shelf, monkeypatch)
    assert _outcomes("\n".join(lines)) == ["built"] and found == ""
    assert (
        f"carry: refused {KEY.carry[:12]} (Mismatch: download length or SHA256 mismatch)" in lines
    )
    assert (tmp_path / "b" / REQUEST).exists()  # a cold build is saved again


def test_a_locator_is_written_only_after_its_archive_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shelf = Shelf()
    build_on(tmp_path / "a", shelf, monkeypatch)
    shelf.refused = ".tar"
    log = save_from(tmp_path / "a", shelf)
    assert "carry: not saved (Denied: PUT p/prepared/" in log
    assert not [key for key in shelf.objects if key.startswith("p/prepared/")]


def test_a_grant_without_keys_cannot_restore(tmp_path: Path) -> None:
    form_only = Grant(prefix="p/prepared/", form=PostForm(url="https://lab.example/", fields={}))
    with pytest.raises(StorageUnavailable, match="no keys"):
        restore(form_only, KEY.carry, tmp_path / "carried", "attempt")
