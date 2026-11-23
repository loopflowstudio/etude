"""A directory worker's first sealed point reaches a real hosted Charts page before exit."""

from __future__ import annotations

import os
import secrets
import select
import shlex
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import distribution
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
import pytest

from etude.cli import main
from etude.launch.schemas import Finished, Packet, Work
from etude.observations import MAX_FILE_BYTES, ViewGrant, ViewGrantTarget
from etude.page import DEFAULT_REPORT, Page, run_chart_data
from etude.storage import DirectoryStorage
from etude.train.read import read


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
def _view(probe: Path) -> Iterator[str]:
    with subprocess.Popen(
        ["uv", "run", "--no-sync", "etude", "view", "--push", "--port", "0"],
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


def _post(origin: str, token: str, key: str, body: bytes, *, length: int | None = None) -> int:
    request = Request(
        origin + "/api/observations/file",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(body) if length is None else length),
            "X-Etude-Key": key,
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
    recipe = tmp_path / "trainer.py"
    recipe.write_text(f"""
import time
from pathlib import Path
from etude.train.tracking import track

with track(clocks=["steps"]) as run:
    run.log({{"loss": 7.0}}, steps=1)
    while not Path({str(barrier)!r}).exists():
        time.sleep(0.05)
    run.log({{"loss": 3.0}}, steps=2)
""")
    work = Work(
        attempt="first",
        run=address,
        experiment="lab",
        seed=0,
        command=shlex.join([command_python, str(recipe)]),
        limit_seconds=45,
        limit_kind="out_of_time",
        repo=None,
        commit=None,
        config=None,
        until=None,
    )
    session = tmp_path / "host/sessions/one"
    private = session / ".view"
    private.mkdir(parents=True, mode=0o700)
    packet = Packet(session="one", work=(work,), deadline=None)
    (session / "packet.json").write_text(packet.model_dump_json())
    source = DirectoryStorage(tmp_path / "host/track")
    sql_log = tmp_path / "sql.log"
    with _view(probe) as origin:
        assert _get(origin + "/healthz") == "ok"
        assert "waiting" in _get(origin + route + "&observe=1")
        grant = ViewGrant(
            origin=origin,
            run=address,
            attempt=work.attempt,
            attempt_number=1,
            exp=int(time.time()) + 120,
        )
        token = grant.sign(secret.encode())
        handoff = private / "first.json"
        handoff.touch(mode=0o600)
        handoff.write_text(ViewGrantTarget(origin=origin, token=token).model_dump_json())
        assert _post(origin, "invalid", "run.json", b"{}") == 401
        expired = grant.model_copy(update={"exp": 0}).sign(secret.encode())
        assert _post(origin, expired, "run.json", b"{}") == 403
        assert _post(origin, token, "run.json", b"", length=MAX_FILE_BYTES + 1) == 413
        assert _post(origin, token, "out/weights", b"abc") == 422
        assert _post(origin, token, "run.json", b'{"id":"other"}') == 422
        assert "waiting" in _get(origin + route + "&observe=1")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("ETUDE_VIEW_")}
        with subprocess.Popen(
            ["uv", "run", "--no-sync", "etude", "worker", session], env=environment
        ) as worker:
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        pytest.fail((session / "attempts/first/log").read_text())
                    if "waiting" not in _get(origin + route + "&observe=1"):
                        html = _get(origin + route)
                        if (
                            'id="data"' in html
                            and "loss" in (page := _training(html)).training.metrics
                        ):
                            break
                    time.sleep(0.15)
                else:
                    pytest.fail("first sealed point did not reach Charts while training")
                assert page.training.series["steps"]["loss"][0].mean == [7.0]
                tail = next((source.root / address / "metrics").glob("*.tail"))
                tail_key, tail_body = (
                    tail.relative_to(source.root / address).as_posix(),
                    tail.read_bytes(),
                )
                barrier.touch()
                assert worker.wait(timeout=20) == 0
            finally:
                barrier.touch(exist_ok=True)
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=10)
        finished = Finished.model_validate_json(
            (session / "attempts/first/finished.json").read_bytes()
        )
        assert finished.outcome == "succeeded"
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
    with _view(probe) as origin:
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
        assert sql.count("FROM runs") == 1
        assert "run_charts" not in sql and "run_files" not in sql
    assert reads.read_text().splitlines() == ["view started", "view started"]
    assert (view_home / "view").read_text() == "local view"
    assert not list(view_home.rglob("*.sqlite"))
