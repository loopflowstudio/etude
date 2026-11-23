"""A directory worker's first sealed point reaches a real hosted Charts page before exit."""

from __future__ import annotations

import json
import os
import secrets
import select
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import psycopg
import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.executor import Executor
from etude.launch.provisioner.machines import OwnedMachine
from etude.launch.scheduler.loop import Scheduler
from etude.launch.schemas import Packet
from etude.launch.ssh import SSH, SSHJournal, SSHStart
from etude.observations import MAX_FILE_BYTES, MAX_OBSERVATION_BYTES, ViewGrant, ViewGrantTarget
from etude.page import DEFAULT_REPORT, Page, run_chart_data
from etude.spec import Job, RunRequest, Until
from etude.storage import DirectoryStorage
from etude.train.read import read
from tests.launch.fake import open_database
from tests.launch.test_runpod_machines import cloud
from tests.launch.test_worker_attempts import local


@pytest.fixture
def postgres(tmp_path: Path) -> Iterator[str]:
    # The test wheel supplies PostgreSQL locally even when the host has only libpq.
    binaries = Path(str(distribution("pgserver").locate_file("pgserver/pginstall/bin")))
    data = tmp_path / "postgres"
    subprocess.run(
        [
            binaries / "initdb",
            "-D",
            data,
            "-U",
            "etude",
            "-A",
            "trust",
            "--no-locale",
            "--encoding=UTF8",
        ],
        check=True,
        capture_output=True,
    )
    # A short, private socket path also fits Darwin's sockaddr_un.
    with tempfile.TemporaryDirectory(dir="/tmp") as socket:
        subprocess.run(
            [
                binaries / "pg_ctl",
                "-D",
                data,
                "-l",
                tmp_path / "sql.log",
                "-o",
                f"-k {socket} -c listen_addresses='' -c log_statement=all "
                "-c log_parameter_max_length=0",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        try:
            yield "postgresql://etude@/postgres?" + urlencode({"host": socket})
        finally:
            subprocess.run(
                [binaries / "pg_ctl", "-D", data, "-m", "immediate", "-w", "stop"],
                check=True,
                capture_output=True,
            )


@contextmanager
def _view(probe: Path, port: int = 0, *, file: Path | None = None) -> Iterator[str]:
    with subprocess.Popen(
        [
            "uv",
            "run",
            "--no-sync",
            "etude",
            "view",
            *([str(file)] if file else []),
            "--push",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(probe), os.getenv("PYTHONPATH", "")]),
        },
    ) as process:
        try:
            assert process.stdout and select.select([process.stdout], [], [], 20)[0]
            origin = process.stdout.readline().strip().rstrip("/")
            assert origin.startswith("http://127.0.0.1:")
            yield origin
        finally:
            process.terminate()
            process.wait(timeout=10)


def _get(url: str) -> str:
    with urlopen(url, timeout=5) as response:
        return bytes(response.read()).decode()


def _training(html: str) -> Page:
    payload = html.split('<script type="application/json" id="data">')[1].split("</script>")[0]
    return Page.model_validate_json(payload, extra="ignore")


def _post(
    origin: str, token: str, key: str | None, body: bytes, *, length: int | None = None
) -> int:
    request = Request(
        origin + ("/api/observations/file" if key else "/api/observations"),
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/octet-stream" if key else "application/json",
            "Content-Length": str(len(body) if length is None else length),
            "X-Etude-Key": key or "",
            "X-Etude-Sha256": sha256(body).hexdigest(),
            "X-Etude-Modified-At": datetime.now(UTC).isoformat(),
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            return int(response.status)
    except HTTPError as error:
        error.close()
        return error.code


def test_worker_publishes_live_curve(
    postgres: str, tmp_path: Path, command_python: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "lab/coin@0"
    route = "/runs/" + address + "?tab=charts"
    secret = secrets.token_hex()
    monkeypatch.setenv("ETUDE_VIEW_SECRET", secret)
    monkeypatch.setenv("ETUDE_VIEW_DATABASE_URL", postgres)
    monkeypatch.delenv("ETUDE_VIEW_URL", raising=False)
    view_home = tmp_path / "view-home"
    monkeypatch.setenv("ETUDE_HOME", str(view_home))
    view_home.mkdir()
    (view_home / "view").write_text("local view")
    # Observe reads in the actual hosted CLI process, including startup and refresh.
    probe = tmp_path / "probe"
    probe.mkdir()
    reads = probe / "reads"
    (probe / "sitecustomize.py").write_text(f"""
import sys
from pathlib import Path

def audit(event: str, args: tuple[object, ...]) -> None:
    if event in ("sqlite3.connect", "socket.connect", "subprocess.Popen"):
        with Path({str(reads)!r}).open("a") as log:
            log.write(event + "\\n")

with Path({str(reads)!r}).open("a") as log:
    log.write("view started\\n")
sys.addaudithook(audit)
""")
    assert main(["view-upgrade"]) == 0
    barrier = tmp_path / "release"
    sealed = tmp_path / "sealed"
    finish = tmp_path / "finish"
    recipe = tmp_path / "trainer.py"
    recipe.write_text(f"""
import os
import time
from pathlib import Path
from etude.train.tracking import track

assert not any(k.startswith("ETUDE_VIEW_") for k in os.environ)
print("trainer ready", flush=True)
print("token=dummy-", end="", flush=True)
with track(clocks=["steps"]) as run:
    run.log({{"loss": 7.0}}, steps=1)
    while not Path({str(barrier)!r}).exists():
        time.sleep(0.05)
    print("credential", flush=True)
    run.log({{"loss": 3.0}}, steps=2)
    weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.bin"
    weights.write_bytes(b"canonical checkpoint")
    run.checkpoint(weights, steps=2)
Path({str(sealed)!r}).touch()
while not Path({str(finish)!r}).exists():
    time.sleep(0.05)
""")
    host = tmp_path / "host"
    host.mkdir()
    scheduler = local(host)
    job = Job(
        command=shlex.join([command_python, str(recipe)]),
        experiment="lab",
        config="batch=2",
        max_hours=0.05,
        max_usd=Decimal(1),
        max_attempts=3,
    )
    request = RunRequest(id="coin@0", job=job, seed=0)
    with pytest.raises(LauncherError, match="together"):
        scheduler.queue(request)
    assert not scheduler.runs()
    source = DirectoryStorage(host / "track")
    sql_log = tmp_path / "sql.log"
    with ExitStack() as live:
        origin = live.enter_context(_view(probe))
        port = urlsplit(origin).port
        assert port
        monkeypatch.setenv("ETUDE_VIEW_URL", origin)
        assert _get(origin + "/healthz") == "ok"
        assert "waiting" in _get(origin + route + "&observe=1")
        assert scheduler.launch(request) == (address,)
        try:
            (attempt,) = scheduler.attempts(address)
            session = host / "sessions" / attempt.session
            handoff = session / ".view" / (attempt.id + ".json")
            token = ViewGrantTarget.model_validate_json(handoff.read_bytes()).token
            assert stat.S_IMODE(handoff.stat().st_mode) == 0o600
            assert token not in (session / "packet.json").read_text()
            grant = ViewGrant.verify(token, secret.encode(), origin)
            assert (grant.run, grant.attempt, grant.attempt_number) == (address, attempt.id, 1)
            assert _post(origin, "invalid", "run.json", b"{}") == 401
            assert _post(origin, "invalid", None, b"{}") == 401
            assert _post(origin, token, None, b"{}", length=MAX_OBSERVATION_BYTES + 1) == 413
            assert _post(origin, token, None, b"{}") == 422
            expired = grant.model_copy(update={"exp": 0}).sign(secret.encode())
            assert _post(origin, expired, "run.json", b"{}") == 403
            assert _post(origin, token, "run.json", b"", length=MAX_FILE_BYTES + 1) == 413
            assert _post(origin, token, "out/weights", b"abc") == 422
            assert _post(origin, token, "run.json", b'{"id":"other"}') == 422
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if "waiting" not in _get(origin + route + "&observe=1"):
                    html = _get(origin + route)
                    overview = _get(origin + "/runs/" + address)
                    if (
                        'id="data"' in html
                        and "loss" in (page := _training(html)).training.metrics
                        and "trainer ready" in overview
                    ):
                        break
                time.sleep(0.1)
            else:
                pytest.fail("first sealed point and console did not reach the view")
            assert "running" in overview and "batch=2" in overview and "dummy-" not in overview
            assert page.training.series["steps"]["loss"][0].mean == [7.0]
            cards = _get(origin + "/")
            assert "/experiments/lab" in cards and '"loss"' in cards and 'class="run-dot"' in cards
            experiment = _get(origin + "/experiments/lab?run=coin@0")
            assert "loss 7" in experiment and "trainer ready" in experiment
            assert "Machine inventory unavailable" in _get(origin + "/machines")
            assert _training(_get(origin + "/experiments/lab/report")).training.series["steps"][
                "loss"
            ][0].mean == [7.0]
            tail = next((source.root / address / "metrics").glob("*.tail"))
            tail_key, tail_body = (
                tail.relative_to(source.root / address).as_posix(),
                tail.read_bytes(),
            )
            with psycopg.connect(postgres, autocommit=True) as db:
                db.execute(
                    "UPDATE runs SET last_push_at=now()-interval '11 minutes' WHERE address=%s",
                    (address,),
                )
                cards = _get(origin + "/status")
                assert "1 no end recorded" in cards and "No open experiments" in cards
                assert "no end recorded" in _get(origin + route + "&observe=1")
            live.close()
            barrier.touch()
            deadline = time.monotonic() + 20
            while not sealed.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert sealed.exists(), (
                "checkpoint and compacted metrics must finish with receiver offline"
            )
            while (
                "hosted view:" not in (session / "session.log").read_text()
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            assert "hosted view:" in (session / "session.log").read_text()
            assert live.enter_context(_view(probe, port)) == origin
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if _training(_get(origin + route)).training.series["steps"]["loss"][0].mean == [
                    7.0,
                    3.0,
                ]:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("publisher did not catch up while the worker remained live")
        finally:
            barrier.touch(exist_ok=True)
            finish.touch()
            (status,) = scheduler.wait(address, every=0.05, timeout=30)
        assert status.outcome == "succeeded"
        assert (
            scheduler.fetch(address, "weights.bin", tmp_path / "download").read_bytes()
            == b"canonical checkpoint"
        )
        overview = _get(origin + "/runs/" + address)
        assert "succeeded" in overview and "Final output" in overview
        assert (
            "steps-2.bin" in overview
            and "token=[redacted]" in overview
            and "dummy-" not in overview
        )
        with psycopg.connect(postgres) as db:
            assert db.execute("SELECT key FROM run_files WHERE writer IS NOT NULL").fetchall() == [
                (
                    next((source.root / address / "metrics").glob("*.blk"))
                    .relative_to(source.root / address)
                    .as_posix(),
                )
            ]
            before = db.execute("SELECT accepted_rev,last_push_at FROM runs").fetchall()
            assert _post(origin, token, tail_key, tail_body) == 200
            assert db.execute("SELECT accepted_rev,last_push_at FROM runs").fetchall() == before
            assert db.execute(
                "SELECT count(*) FROM run_files WHERE writer IS NOT NULL"
            ).fetchone() == (1,)
    expected = run_chart_data(source, read(source, "lab", "coin@0"), DEFAULT_REPORT, "steps")
    canonical = Page.model_validate_json(expected.payload, extra="ignore").training
    assert canonical.series["steps"]["loss"][0].mean == [7.0, 3.0]
    with _view(probe, port) as origin:
        assert _training(_get(origin + route)).training == canonical
        mark = sql_log.stat().st_size
        started = time.monotonic()
        html = _get(origin + route)
        elapsed = time.monotonic() - started
        assert _training(html).training.model_dump() == canonical.model_dump()
        print(f"Charts: {len(html.encode())} bytes, 2 points, {elapsed:.3f} s")
        sql = sql_log.read_bytes()[mark:].decode()
        assert sql.count("LEFT JOIN run_charts") == 1
        assert "run_files" not in sql
        mark = sql_log.stat().st_size
        assert "Last push" in _get(origin + route + "&observe=1")
        sql = sql_log.read_bytes()[mark:].decode()
        # One run observation and one open-experiment rail in the same snapshot.
        assert sql.count("FROM runs") == 2
        assert "run_charts" not in sql and "run_files" not in sql
        mark = sql_log.stat().st_size
        for page_path in (
            "/",
            "/reports",
            "/machines",
            "/experiments/lab?tab=curves",
            "/experiments/lab/report",
            "/runs/" + address + "?tab=files",
        ):
            join = "&" if "?" in page_path else "?"
            assert 'id="view-rail"' in _get(origin + page_path + join + "observe=1")
        polls = sql_log.read_bytes()[mark:].decode()
        assert "run_charts" not in polls and "run_files" not in polls
        files = _get(origin + "/runs/" + address + "?tab=files")
        assert tail_key not in files and "run.json" in files and "canonical storage" in files
        with psycopg.connect(postgres) as db:
            old = db.execute("SELECT observation FROM attempts WHERE number=1").fetchone()
            assert old
            assert _post(origin, token, None, old[0].encode()) == 200
            prior = json.loads(old[0])
            assert (
                _post(origin, token, None, json.dumps({**prior, "producer_seq": 1}).encode()) == 409
            )
            regressed = {**prior, "producer_seq": prior["producer_seq"] + 1, "console": None}
            assert _post(origin, token, None, json.dumps(regressed).encode()) == 409
            assert db.execute("SELECT observation FROM attempts WHERE number=1").fetchone() == old
        # A resumed job can publish a complete console without importing tracking.
        replacement = request.model_copy(
            update={
                "id": address,
                "resume": True,
                "job": job.model_copy(
                    update={"command": "echo replacement-console", "until": Until.parse("steps=3")}
                ),
            }
        )
        assert scheduler.launch(replacement) == (address,)
        assert scheduler.wait(address, every=0.05, timeout=30)[0].outcome == "succeeded"
        assert _post(origin, token, None, old[0].encode()) == 409
        assert (
            _post(origin, token, "run.json", (source.root / address / "run.json").read_bytes())
            == 409
        )
        overview = _get(origin + "/runs/" + address)
        assert "replacement-console" in overview and "Attempt 2" in overview
        assert "trainer ready" not in overview
    assert reads.read_text().splitlines() == ["view started"] * 3
    assert (view_home / "view").read_text() == "local view"
    assert not list(view_home.rglob("*.sqlite"))


@pytest.mark.parametrize("queued", [True, False], ids=["bootstrap", "kept-inbox"])
def test_bucket_delivery_finishes_with_view_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    account = cloud(tmp_path)
    secret = secrets.token_hex()
    with socket.socket() as offline:
        offline.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{offline.getsockname()[1]}"
        monkeypatch.setenv("ETUDE_VIEW_URL", origin)
        monkeypatch.setenv("ETUDE_VIEW_SECRET", secret)
        job = Job(
            command='echo canonical > "$ETUDE_OUTPUT_DIR/artifact"; echo done',
            max_hours=0.01,
            max_usd=Decimal(1),
        )
        submit = account.scheduler.queue if queued else account.scheduler.launch
        (run,) = submit(job, rent="l4")
        (status,) = account.scheduler.wait(run, every=0.05, timeout=30)
        assert status.outcome == "succeeded" and status.rental_ended
        (attempt,) = account.scheduler.attempts(run)
        session = account.provider.root / attempt.session
        private = session / ".view" / f"{attempt.id}.json"
        assert stat.S_IMODE(private.stat().st_mode) == 0o600
        grant = ViewGrantTarget.model_validate_json(private.read_bytes())
        assert ViewGrant.verify(grant.token, secret.encode(), origin).run == run
        packet = Packet.model_validate_json((session / "packet.json").read_bytes())
        assert bool(packet.work) == queued
        assert all(
            grant.token not in path.read_text()
            for path in [session / "packet.json", *(session / "work").glob("*.json")]
        )
        assert (
            account.scheduler.fetch(run, "artifact", tmp_path / "download").read_text()
            == "canonical\n"
        )
        assert not account.provider.pods


def test_ssh_worker_receives_private_grant_for_packet_and_kept_work(
    postgres: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ssh_command = tmp_path / "ssh"
    ssh_command.write_text('#!/bin/sh\nshift 3\nexec sh -c "$1"\n')
    ssh_command.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ETUDE_REMOTE_PYTHON", sys.executable)
    monkeypatch.setenv("ETUDE_VIEW_SECRET", secrets.token_hex())
    monkeypatch.setenv("ETUDE_VIEW_DATABASE_URL", postgres)
    monkeypatch.delenv("ETUDE_VIEW_URL", raising=False)
    sessions = tmp_path / "remote/sessions"
    ssh = SSH("bench", str(sessions))
    service = Executor(OwnedMachine("bench"), SSHStart(ssh), SSHJournal(ssh))
    scheduler = Scheduler(open_database(tmp_path / "launcher.db"), {"ssh": lambda _: service})
    scheduler.add_machine("bench", "bench")
    assert main(["view-upgrade"]) == 0
    with _view(tmp_path) as origin:
        monkeypatch.setenv("ETUDE_VIEW_URL", origin)
        job = Job(command="echo ssh-console", max_hours=0.02, max_usd=Decimal(1))
        for submit in (scheduler.queue, scheduler.launch):
            (run,) = submit(job, "bench")
            (status,) = scheduler.wait(run, every=0.05, timeout=30)
            assert status.outcome == "succeeded"
            (attempt,) = scheduler.attempts(run)
            session = sessions / attempt.session
            private = session / ".view" / (attempt.id + ".json")
            assert stat.S_IMODE(private.stat().st_mode) == 0o600
            token = ViewGrantTarget.model_validate_json(private.read_bytes()).token
            for path in [session / "packet.json", *(session / "work").glob("*.json")]:
                assert token not in path.read_text()
            overview = _get(origin + "/runs/" + run)
            assert "ssh-console" in overview and "Final output" in overview and "bench" in overview


def test_group_results_and_edited_report_survive_restart(
    postgres: str, tmp_path: Path, command_python: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = secrets.token_hex()
    monkeypatch.setenv("ETUDE_VIEW_SECRET", secret)
    monkeypatch.setenv("ETUDE_VIEW_DATABASE_URL", postgres)
    monkeypatch.delenv("ETUDE_VIEW_URL", raising=False)
    assert main(["view-upgrade"]) == 0
    probe = tmp_path / "offline"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text("""
import sys

def offline(event: str, args: tuple[object, ...]) -> None:
    if event in ("sqlite3.connect","socket.connect"):
        raise RuntimeError("hosted reports must use Postgres evidence")

sys.addaudithook(offline)
""")
    file = tmp_path / "grouped.py"
    declaration = """from etude.report import Report, Score, Series

def report() -> Report:
    return Report(score=Score(table="scores",value="score"),baseline="first",
                  ml=(Series(title="Progress",keys=("first",)),))
"""
    file.write_text(declaration)
    recipe = tmp_path / "evaluate.py"
    recipe.write_text("""from etude.jobs import hold_group
from tests.test_eval_groups import group, job

if __name__ == "__main__":
    hold_group(group(job("duel",None)))
""")
    host = tmp_path / "host"
    host.mkdir()
    scheduler = local(host)
    job = Job(
        command=shlex.join([command_python, str(recipe)]),
        experiment="grouped",
        max_hours=0.05,
        max_usd=Decimal(1),
    )
    request = RunRequest(id="games@0", job=job, seed=0)
    report = "/experiments/grouped/report"
    with _view(probe, file=file) as origin:
        monkeypatch.setenv("ETUDE_VIEW_URL", origin)
        (address,) = scheduler.launch(request)
        (status,) = scheduler.wait(address, every=0.05, timeout=45)
        assert status.outcome == "succeeded"
        (attempt,) = scheduler.attempts(address)
        private = host / "sessions" / attempt.session / ".view" / (attempt.id + ".json")
        token = ViewGrantTarget.model_validate_json(private.read_bytes()).token
        assert address in _get(origin + "/experiments/grouped")
        page = _training(_get(origin + report))
        assert page.report.baseline == "first" and any(cell.value == 7 for cell in page.cells)
        assert "Eval only" in _get(origin + "/reports")
        root = host / "track" / address
        index = next(root.rglob("block.json"))
        table = next(index.parent.glob("*.parquet"))
        key = table.relative_to(root).as_posix()
        # A different seal time names a second block. Its staged table is hidden until the index commits.
        name = index.parent.name
        written, writer = name.split(".")
        later = f"{int(written) + 1:020d}.{writer}"
        later_key = key.replace(name, later)
        index_key = index.relative_to(root).as_posix().replace(name, later)
        body = json.dumps(
            {**json.loads(index.read_bytes()), "written_ns": int(written) + 1}
        ).encode()
        assert _post(origin, token, index_key, body) == 409
        assert _post(origin, token, later_key, b"invalid parquet") == 422
        for path in index.parent.glob("*.parquet"):
            assert (
                _post(
                    origin,
                    token,
                    path.relative_to(root).as_posix().replace(name, later),
                    path.read_bytes(),
                )
                == 200
            )
        files_url = origin + "/runs/" + address + "?tab=files"
        assert later_key not in _get(files_url)
        assert _post(origin, token, index_key, body) == 200
        assert later_key in _get(files_url)
        train = tmp_path / "training.py"
        train.write_text("""from etude.train.tracking import track

with track(clocks=["steps"]) as run:
    run.log({"first": 3, "second": 4}, steps=1)
    run.log_table("examples", [{"sample": 2}], steps=1)
""")
        training_job = job.model_copy(update={"command": shlex.join([command_python, str(train)])})
        (trained,) = scheduler.launch(RunRequest(id="train@0", job=training_job, seed=0))
        assert scheduler.wait(trained, every=0.05, timeout=30)[0].outcome == "succeeded"
        table_url = origin + "/experiments/grouped/status"
        assert "first 3" in _get(table_url)
        artifacts = list((host / "track" / trained / "artifacts").glob("*.json"))
        assert len(artifacts) == 1 and artifacts[0].name in _get(
            origin + "/runs/" + trained + "?tab=files"
        )
        file.write_text(declaration.replace("first", "second"))
        assert _training(_get(origin + report)).report.baseline == "first"
        with urlopen(Request(origin + report, data=b""), timeout=10) as response:
            assert _training(response.read().decode()).report.baseline == "second"
        assert "second 4" in _get(table_url)
        file.write_text("raise ValueError('invalid declaration')\n")
        with pytest.raises(HTTPError) as failed:
            urlopen(Request(origin + report, data=b""), timeout=10)
        assert "invalid declaration" in failed.value.read().decode()
        failed.value.close()
    with _view(probe, file=file) as origin:
        restored = _training(_get(origin + report))
        assert restored.report.baseline == "second" and any(
            cell.value == 7 for cell in restored.cells
        )
