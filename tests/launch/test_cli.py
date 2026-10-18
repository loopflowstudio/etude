"""The `etude` command over a job file, running real local processes."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from pathlib import Path
from threading import Thread

import pytest

from etude.cli import main
from etude.launch.client import default
from etude.launch.schemas import Packet
from tests.launch.fake import bench

JOB_FILE = r"""
from etude.launch.records import Job, Run
from decimal import Decimal
from pathlib import Path
command = '''
echo "$(cat "$ETUDE_CONFIG") seed $ETUDE_SEED from ${ETUDE_FROM_VALUE:-0} to $ETUDE_UNTIL_VALUE"
echo weights > "$ETUDE_OUTPUT_DIR/checkpoint.pt"
printf '{"clocks": {"steps": %s}, "checkpoint": "checkpoint.pt"}' "$ETUDE_UNTIL_VALUE" > "$ETUDE_OUTPUT_DIR/etude-result.json"
'''
here = Path(__file__).parent


def runs(steps: int = 26000, seeds: int = 2) -> list[Run]:
    return [run for arm in ("small", "large") for run in
            Job(command=command, max_hours=0.1, max_usd=Decimal(5), until=f"steps={steps}",
                      name=arm, config=(here / (arm + ".toml")).read_text()
                      ).runs(*range(1, seeds + 1))]


if __name__ == "__main__":
    raise SystemExit("the command must not run this block")
"""


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    assert main(argv) == 0
    return capsys.readouterr().out.splitlines()


def test_a_job_file_launches_a_cohort_and_each_verb_reads_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    for arm in ("small", "large"):
        (tmp_path / f"{arm}.toml").write_text(f"arm = {arm}")
    (tmp_path / "job.py").write_text(JOB_FILE)

    launched = run(
        capsys, "launch", str(tmp_path / "job.py"), "--cohort", "capacity-26k", "--max-usd", "20"
    )
    ids = [line.split()[0] for line in launched]
    names = [line.split()[1] for line in launched]
    assert names == ["small-seed1", "small-seed2", "large-seed1", "large-seed2"]
    assert "queued (cohort runs 1 at a time)" in launched[1]

    # Reproduce the race: a quick first run can finish before the next command.

    default().wait(ids[0], every=0.01, timeout=10)
    assert [line.split()[0] for line in run(capsys, "runs")] == ids
    done = run(capsys, "wait", *ids)
    assert all("done: succeeded, steps 26000" in line for line in done)
    assert "arm = large seed 2 from 0 to 26000" in run(
        capsys, "logs", "--run", ids[3], "--job-only"
    )

    assert run(capsys, "status") == []
    assert len(run(capsys, "runs")) == 4
    assert [line.split()[0] for line in run(capsys, "status", "capacity-26k")] == ids
    assert main(["continue", "capacity-26k", "steps=40000"]) == 1
    assert "needs --cohort" in capsys.readouterr().err
    stage = run(capsys, "continue", "capacity-26k", "steps=40000", "--cohort", "capacity-40k")
    assert len(stage) == 4 and all("queued (waiting for local)" in line for line in stage)
    assert all("steps 40000" in line for line in run(capsys, "wait"))  # none named: all of them
    assert "arm = small seed 1 from 26000 to 40000" in run(
        capsys, "logs", "--cohort", "capacity-40k", "--job-only"
    )
    (continued,) = run(capsys, "continue", ids[3], "steps=63000")
    next_id = continued.split()[0]
    assert "steps 63000" in run(capsys, "wait", next_id)[0]
    assert "arm = large seed 2 from 26000 to 63000" in run(
        capsys, "logs", "--run", next_id, "--job-only"
    )
    (copy,) = run(capsys, "fetch", next_id, "checkpoint.pt", str(tmp_path / "out"))
    assert Path(copy).read_text() == "weights\n"


@pytest.mark.parametrize(
    "definition", ["def runs(): return []", "def runs(): return 42", "runs = 42", "job = None"]
)
def test_invalid_python_file_is_refused_before_opening_the_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    definition: str,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    path = tmp_path / "job.py"
    path.write_text(definition)
    assert main(["launch", str(path)]) == 1
    assert "runs()" in capsys.readouterr().err
    assert not (tmp_path / "home" / "launcher.db").exists()


def test_machines_join_pools_and_this_computer_can_be_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    assert run(capsys, "pools") == ["local  this computer (default)"]
    assert run(capsys, "machine", "add", "bench", "bench")[-1] == "bench  bench"
    listed = run(capsys, "machine", "add", "second", "second", "--pool", "bench")
    assert listed[-1] == "bench  bench, second"
    assert main(["pool", "add", "l4", "--provider", "lambda", "--gpu", "NVIDIA L4"]) == 1
    assert "no provider named 'lambda'" in capsys.readouterr().err
    assert main(["machine", "add", "local", "localhost"]) == 1
    assert "already the local pool" in capsys.readouterr().err

    assert run(capsys, "pool", "off", "local")[0] == "local  this computer (default) (off)"
    (tmp_path / "job.py").write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="true", max_hours=1, max_usd=1)\n'
    )
    assert main(["launch", str(tmp_path / "job.py")]) == 1
    assert "pool 'local' is turned off" in capsys.readouterr().err
    assert (
        run(capsys, "pools")[0] == "local  this computer (default) (off)"
    )  # still off on the next call
    assert run(capsys, "machines") == [
        "local  local  added (this computer)",
        "bench  bench  added",
        "second  bench  added",
    ]


def test_pool_default_max_and_owned_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    run(capsys, "pool", "max", "local", "0")
    run(capsys, "pool", "default", "local")
    path = tmp_path / "job.py"
    path.write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="echo hi", max_hours=0.001, max_usd=1)\n'
    )
    (line,) = run(capsys, "launch", str(path))
    assert "machine local" in line
    assert "succeeded" in run(capsys, "wait", line.split()[0])[0]
    assert run(capsys, "machines") == ["local  local  added (this computer)"]  # no session
    path.write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="echo free", max_hours=0.001)\n'
    )
    assert "machine local" in run(capsys, "launch", str(path))[0]  # no dollars needed here
    for old in ("--on", "--reserve"):
        with pytest.raises(SystemExit):
            main(["launch", str(path), old])
        capsys.readouterr()


def test_a_second_bucket_or_place_is_a_second_pool_not_a_second_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    image = "r/e@sha256:" + "a" * 64
    assert run(capsys, "providers") == []
    assert main(
        [
            "provider",
            "set",
            "runpod",
            "--kind",
            "runpod",
            "--registry",
            "r1",
            "--storage",
            "west",
        ]
    )
    assert "no storage named 'west'" in capsys.readouterr().err
    west = ["storage", "add", "west", "--bucket", "b-west", "--prefix", "etude", "--region"]
    assert run(capsys, *west, "us-west-2") == ["west  s3://b-west/etude  us-west-2"]
    assert main([*west, "eu-west-1"]) == 1  # a storage never changes; add another instead
    assert "already exists with other settings" in capsys.readouterr().err
    run(
        capsys,
        "storage",
        "add",
        "eu",
        "--bucket",
        "b-eu",
        "--prefix",
        "etude",
        "--region",
        "eu-west-1",
    )
    account = ["provider", "set", "runpod", "--kind", "runpod", "--registry", "r1", "--storage"]
    assert run(capsys, *account, "west") == ["runpod  runpod  storage west"]

    l4 = ["--provider", "runpod", "--gpu", "NVIDIA L4", "--image", image]
    run(capsys, "pool", "add", "l4", *l4)
    listed = run(capsys, "pool", "add", "l4-eu", *l4, "--data-center", "EU-RO-1", "--storage", "eu")
    assert (
        "l4  makes up to 1 NVIDIA L4 on runpod; safeguards: machine timer, launcher cleanup; records in west"
        in listed
    )
    assert (
        "l4-eu  makes up to 1 NVIDIA L4 on runpod in EU-RO-1; safeguards: machine timer, launcher cleanup; records in eu"
        in listed
    )

    run(
        capsys, *account, "eu"
    )  # the provider's storage changes; the pool already added keeps its own
    assert (
        "l4  makes up to 1 NVIDIA L4 on runpod; safeguards: machine timer, launcher cleanup; records in west"
        in run(capsys, "pools")
    )
    assert main(["pool", "image", "local", image]) == 1
    assert "makes no machines on demand" in capsys.readouterr().err
    assert main(["pool", "image", "l4", "r/e@sha256:" + "b" * 64]) == 0


@pytest.mark.parametrize("cohort", [False, True])
def test_launch_makes_machines_that_start_themselves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cohort: bool,
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    path = tmp_path / "job.py"
    path.write_text(
        'from etude.launch.records import Job, Run\ndef runs(): return Job(command="train", max_hours=1, max_usd=1)'
        + (".runs(1, 2)" if cohort else "")
    )
    lines = run(
        capsys,
        "launch",
        str(path),
        "--pool",
        "l4",
        *(["--cohort", "pair"] if cohort else []),
    )
    ids = [line.split()[0] for line in lines]
    assert len(ids) == (2 if cohort else 1)
    assert all(b.scheduler.record(id).autorun is True for id in ids)
    assert all(held.packet.autorun is True for held in b.service.sessions.values())


def test_explicit_rent_uses_the_same_startup_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    run(capsys, "rent", "--pool", "l4", "--hours", "1")
    assert all(held.packet.autorun is True for held in b.service.sessions.values())
    assert all(not held.packet.work for held in b.service.sessions.values())


def test_session_does_not_create_a_launcher_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "launcher"))
    directory = tmp_path / "session"
    directory.mkdir()
    packet = Packet(
        session="r",
        deadline=datetime.now(UTC) + timedelta(seconds=1),
        work=(),
        kept=False,
        autorun=False,
        cap_usd=Decimal(0),
    )
    (directory / "packet.json").write_text(packet.model_dump_json())
    assert main(["worker", str(directory)]) == 0
    assert (directory / "ended").exists()
    assert not (tmp_path / "launcher").exists()


def test_bootstrap_session_carries_records_without_a_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "launcher"))
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    uploads: list[bytes] = []
    bootstrap: bytes = b""

    class BucketHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(bootstrap if self.path == "/bootstrap" else b'{"stop":[],"work":[]}')

        def do_POST(self) -> None:
            uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BucketHandler)
    url = f"http://127.0.0.1:{server.server_port}"
    packet = Packet(
        session="r", deadline=datetime.now(UTC) + timedelta(seconds=10), work=(), kept=False
    )
    bootstrap = json.dumps(
        {
            "packet": packet.model_dump_json(),
            "fetch": {},
            "inbox_url": url + "/inbox",
            "form": {"url": url + "/upload", "fields": {}, "prefix": "out/"},
        }
    ).encode()
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "etude",
                "worker",
                "--bootstrap",
                url + "/bootstrap",
                "--root",
                str(tmp_path / "sessions"),
            ],
            capture_output=True,
            timeout=15,
        )
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "sessions/r/ended").exists()
        assert any(b"out/receipt" in upload for upload in uploads)
        assert any(b"out/ended" in upload for upload in uploads)
        assert not (tmp_path / "launcher").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_job_file_takes_parameters_and_records_them_on_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    path = tmp_path / "job.py"
    path.write_text(
        "from __future__ import annotations\nfrom etude.launch.records import Job, Run\ndef runs(hidden: int = 256, seeds: int = 1, lr: float = 0.1, fast: bool = False,\n         tag='x', shape: list[int] | None = None) -> list[Run]:\n    assert type(hidden) is int and type(lr) is float and type(fast) is bool\n    job = Job(command=f'echo {hidden} {lr} {fast} {tag}', max_hours=0.01, max_usd=1, name=f'h{hidden}')\n    return job.runs(*range(seeds))\n"
    )

    sent = run(
        capsys, "queue", str(path), "--hidden", "512", "--seeds", "2", "--fast", "--cohort", "big"
    )
    assert [line.split()[1] for line in sent] == ["h512-seed0", "h512-seed1"]
    records = [default().record(line.split()[0]) for line in sent]
    expected = {"hidden": 512, "seeds": 2, "lr": 0.1, "fast": True, "tag": "x"}
    assert [record.params for record in records] == [expected, expected]
    # Identity comes from what runs, not from the arguments that led there.
    again = run(capsys, "queue", str(path), "--hidden", "512", "--fast", "--tag", "x")[0].split()[0]
    assert default().record(again).job == records[0].job
    assert default().record(again).params["seeds"] == 1

    for wrong in (("--width", "3"), ("hidden=512",), ("--hidden", "big")):
        try:
            result = main(["queue", str(path), *wrong])
        except SystemExit as error:
            result = error.code if isinstance(error.code, int) else 1
        assert result != 0
        assert capsys.readouterr().err


def test_job_help_required_flags_mixed_order_and_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    path = tmp_path / "job.py"
    path.write_text(
        'from etude.launch.records import Job\ndef runs(width: int, fast: bool = False):\n    """Train a chosen width."""\n    return Job(command="true", max_hours=1)\n'
    )
    assert main(["launch", str(path), "--help"]) == 0
    help = capsys.readouterr().out
    assert (
        "--pool" in help
        and "--width" in help
        and "<int>" in help
        and "Train a chosen width" in help
    )
    assert not (tmp_path / "home").exists()
    assert main(["queue", str(path), "--width", "32", "--pool", "local", "--fast"]) == 0
    id = capsys.readouterr().out.split()[0]
    assert default().record(id).params == {"width": 32, "fast": True}
    assert main(["queue", str(path), "--", "--width", "64"]) == 0
    path.write_text("def runs(pool: str): return None\n")
    assert main(["queue", str(path), "--help"]) == 1
    assert "clashes" in capsys.readouterr().err
