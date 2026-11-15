"""The `etude` command over an experiment file, running real local processes."""

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
from etude.declarations import read_training_jobs
from etude.errors import LauncherError
from etude.experiment import observe
from etude.launch.client import default
from etude.launch.database import launcher_file, upgrade
from etude.launch.schemas import Packet
from etude.local import train
from etude.spec import Runtime
from etude.storage import default_root
from tests.launch.fake import bench


@pytest.fixture(autouse=True)
def _command_environment(command_python: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_TEST_PYTHON", command_python)
    monkeypatch.setattr("tests.launch.tasks.RUNTIME", Runtime(python=command_python))


JOB_FILE = r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command

import sys
from etude.launch.records import Job, Run
from decimal import Decimal
from pathlib import Path
command = f"{sys.executable} {Path(__file__).parent / 'train.py'}"
here = Path(__file__).parent


def training_jobs(steps: int = 26000, seeds: int = 2) -> list[Run]:
    return [run for arm in ("small", "large") for run in
            TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=command, text=(here / (arm + ".toml")).read_text()), max_hours=0.1, max_usd=Decimal(5), until=f"steps={steps}", name=arm, experiment="capacity", repo=None).runs(*range(1, seeds + 1))]


if __name__ == "__main__":
    raise SystemExit("the command must not run this block")
"""


# A tracked job: it resumes from the checkpoint its run, or its parent, recorded.
TRAIN = r"""
import os
from pathlib import Path
from urllib.parse import urlparse
from etude.train.records import Step
from etude.train.tracking import track
run = track(experiment="capacity", clocks=[Step("steps", "training steps")])
latest = run.latest_checkpoint()
start = 0 if latest is None else int(latest.at["steps"])
target = int(os.environ["ETUDE_UNTIL_VALUE"])
config = Path(os.environ["ETUDE_CONFIG"]).read_text()
print(f"{config} seed {os.environ['ETUDE_SEED']} from {start} to {target}")
weights = Path(os.environ["ETUDE_OUTPUT_DIR"]) / "weights.pt"
weights.write_text("weights\n")
run.log({"loss": 0.5}, steps=target)
run.checkpoint(weights, steps=target)
run.finish()
"""


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    assert main(argv) == 0
    return [line for line in capsys.readouterr().out.splitlines() if ": state from " not in line]


@pytest.mark.parametrize(
    ("reference", "identity"),
    [
        (("coin-seed-0",), "coin-seed-0"),
        (("coin/coin-seed-0",), "coin/coin-seed-0"),
        (("coin-seed-0", "--experiment", "coin"), "coin/coin-seed-0"),
    ],
)
def test_continue_explains_how_to_read_or_change_a_local_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reference: tuple[str, ...],
    identity: str,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    assert main(["continue", "--run", *reference, "--until", "updates=30"]) == 1
    error = capsys.readouterr().err
    assert f"the launcher has no run {identity!r}" in error
    assert "etude report FILE" in error
    assert "new --experiment" in error


def test_continue_help_explains_experiment_without_a_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["continue", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "experiment label qualifying a bare run id" in help_text
    assert "file stem" not in help_text


def test_a_job_file_launches_runs_and_each_verb_reads_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    for arm in ("small", "large"):
        (tmp_path / f"{arm}.toml").write_text(f"arm = {arm}")
    (tmp_path / "job.py").write_text(JOB_FILE)
    (tmp_path / "train.py").write_text(TRAIN)

    launched = run(
        capsys, "launch", str(tmp_path / "job.py"), "--experiment", "capacity", "--max-usd", "20"
    )
    ids = [line.split()[0] for line in launched]
    assert len(ids) == 4 and "new, queued" in launched[1]

    # Reproduce the race: a quick first run can finish before the next command.

    default().wait(ids[0], every=0.01, timeout=10)
    assert [line.split()[0] for line in run(capsys, "runs")] == ids
    done = run(capsys, "wait", *ids)
    assert all("done: succeeded, steps 26000" in line for line in done)
    assert "arm = large seed 2 from 0 to 26000" in run(
        capsys, "logs", "--run", ids[3], "--job-only", "--follow"
    )

    assert run(capsys, "status") == ["last loop: never"]
    assert len(run(capsys, "runs")) == 4
    assert [
        line.split()[0] for line in run(capsys, "status", "--experiment", "capacity")[:-1]
    ] == ids
    (continued,) = run(capsys, "continue", "--run", ids[3], "--until", "steps=63000")
    next_id = continued.split()[0]
    assert "steps 63000" in run(capsys, "wait", next_id)[0]
    assert "arm = large seed 2 from 26000 to 63000" in run(
        capsys, "logs", "--run", next_id, "--job-only", "--follow"
    )
    (copy,) = run(capsys, "fetch", next_id, "weights.pt", str(tmp_path / "out"))
    assert Path(copy).read_text() == "weights\n"


def test_an_experiment_file_launches_runs_of_its_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    (tmp_path / "train.py").write_text(
        "from etude.train.records import Step\n"
        "from etude.train.tracking import track\n"
        'run = track(clocks=[Step("steps", "training steps")])\n'
        'run.log({"loss": 0.5}, steps=1)\n'
        "run.finish()\n"
    )
    (tmp_path / "ppo.py").write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
import sys
from pathlib import Path
from etude.launch.records import Job
def training_jobs() -> Job:
    train = Path(__file__).parent / 'train.py'
    return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=f'{sys.executable} {train}'), max_hours=0.1, name="base", repo=None)
def eval_jobs(state):
    return []
"""
    )
    (launched,) = run(capsys, "launch", str(tmp_path / "ppo.py"))
    identity = launched.split()[0]
    assert "done: succeeded" in run(capsys, "wait", identity)[0]
    # What `etude eval ppo.py` and `etude report ppo.py` read: the runs of experiment ppo.
    assert [each.name for each in observe(default_root(), "ppo").runs] == [
        identity.rsplit("/", 1)[-1]
    ]
    # An explicit launch label overrides the file and its job declaration.
    other = tmp_path / "league.py"
    named = (tmp_path / "ppo.py").read_text().replace(")\n", ", experiment='ppo')\n", 1)
    other.write_text(named)
    assert main(["launch", str(other), "--experiment", "league"]) == 0
    assert default().record("league/base@0").experiment == "league"


@pytest.mark.parametrize(
    "definition",
    [
        "def training_jobs(): return 42",
        "training_jobs = 42",
        "job = None",
    ],
)
def test_invalid_python_file_does_not_submit_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    definition: str,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    path = tmp_path / "job.py"
    path.write_text(definition)
    assert main(["launch", str(path)]) == 1
    assert "training_jobs()" in capsys.readouterr().err
    assert not default().runs(done=True)


def test_machines_join_pools_and_this_computer_can_be_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    assert run(capsys, "pools") == ["local  this computer (default)"]
    assert run(capsys, "machine", "add", "bench", "bench")[-1] == "bench  bench"
    listed = run(capsys, "machine", "add", "second", "second", "--pool", "bench")
    assert listed[-1] == "bench  bench, second"
    assert main(["ondemand", "add", "l4", "--provider", "lambda", "--gpu", "NVIDIA L4"]) == 1
    assert "no provider named 'lambda'" in capsys.readouterr().err
    assert main(["machine", "add", "local", "localhost"]) == 1
    assert "already the local pool" in capsys.readouterr().err

    assert run(capsys, "pool", "off", "local")[0] == "local  this computer (default) (off)"
    (tmp_path / "job.py").write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, RunRequest
def training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="true"), max_hours=1, max_usd=1, name="base", repo=None)
"""
    )
    assert main(["launch", str(tmp_path / "job.py")]) == 1
    assert "pool 'local' is turned off" in capsys.readouterr().err
    assert (
        run(capsys, "pools")[0] == "local  this computer (default) (off)"
    )  # still off on the next call
    assert run(capsys, "machines") == [
        "local  local  unavailable, owned (this computer), $0.00 committed, never expires",
        "bench  bench  available, owned, $0.00 committed, never expires",
        "second  bench  available, owned, $0.00 committed, never expires",
    ]


def test_default_target_and_owned_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    assert run(capsys, "default", "--pool", "local") == ["default: pool local, rent none"]
    assert main(["default"]) == 1
    assert "needs a pool, an on-demand or both" in capsys.readouterr().err
    path = tmp_path / "job.py"
    path.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, RunRequest
def training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="echo hi"), max_hours=0.01, max_usd=1, name="base", repo=None)
"""
    )
    (line,) = run(capsys, "launch", str(path))
    assert "new, running" in line
    assert "succeeded" in run(capsys, "wait", line.split()[0])[0]
    assert run(capsys, "machines") == [  # no session
        "local  local  available, owned (this computer), $0.00 committed, never expires"
    ]
    path.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, RunRequest
def training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="echo free"), name="free", max_hours=0.001, repo=None)
"""
    )
    assert (
        "new, running" in run(capsys, "launch", str(path), "--experiment", "free")[0]
    )  # no dollars needed here
    for old in ("--on", "--reserve"):
        with pytest.raises(SystemExit):
            main(["launch", str(path), old])
        capsys.readouterr()


def test_a_second_bucket_or_place_is_a_second_pool_not_a_second_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
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
    west = ["storage", "add", "west", "--bucket", "b-west", "--prefix", "etude"]
    assert main(west) == 1  # the provider is always stated
    assert "--provider" in capsys.readouterr().err
    west += ["--provider", "s3", "--region"]
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
        "--provider",
        "s3",
        "--region",
        "eu-west-1",
    )
    account = ["provider", "set", "runpod", "--kind", "runpod", "--registry", "r1", "--storage"]
    assert run(capsys, *account, "west") == ["runpod  runpod  storage west"]

    l4 = ["--provider", "runpod", "--gpu", "NVIDIA L4", "--image", image]
    run(capsys, "ondemand", "add", "l4", *l4, "--max-usd-per-hour", "0.80")
    listed = run(
        capsys, "ondemand", "add", "l4-eu", *l4, "--data-center", "EU-RO-1", "--storage", "eu"
    )
    assert listed == [
        "l4  NVIDIA L4 on runpod; up to 1 at once under $0.80/hour;"
        " safeguards: machine timer, launcher cleanup; records in west",
        "l4-eu  NVIDIA L4 on runpod in EU-RO-1; up to 1 at once;"
        " safeguards: machine timer, launcher cleanup; records in eu",
    ]

    run(capsys, *account, "eu")  # the provider's storage changes; an added on-demand keeps its own
    assert run(capsys, "ondemands")[0].endswith("records in west")
    assert main(["ondemand", "image", "local", image]) == 1
    assert "no on-demand named 'local'" in capsys.readouterr().err
    assert main(["ondemand", "image", "l4", "r/e@sha256:" + "b" * 64]) == 0


@pytest.mark.parametrize("cohort", [False, True])
def test_launch_makes_machines_that_start_themselves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cohort: bool,
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    monkeypatch.setattr(import_module("etude.launch.client"), "default", lambda: b.scheduler)
    monkeypatch.setattr(
        "etude.controller.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: b.scheduler.standing(experiment)
    )
    monkeypatch.setattr(import_module("etude.launch.client"), "_client", lambda: b.scheduler)
    path = tmp_path / "job.py"
    path.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, RunRequest
def training_jobs(): return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="train"), max_hours=1, max_usd=1, name="base", repo=None)"""
        + (".runs(1, 2)" if cohort else "")
    )
    lines = run(
        capsys,
        "launch",
        str(path),
        "--rent",
        "l4",
    )
    ids = [line.split()[0] for line in lines]
    assert len(ids) == (2 if cohort else 1)
    assert all(b.scheduler.record(id).launch.autorun is True for id in ids)
    assert all(held.packet.autorun is True for held in b.service.sessions.values())


def test_explicit_rent_uses_the_same_startup_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    b = bench(tmp_path)
    monkeypatch.setattr(import_module("etude.launch.cli"), "default", lambda: b.scheduler)
    monkeypatch.setattr(import_module("etude.launch.client"), "default", lambda: b.scheduler)
    monkeypatch.setattr(
        "etude.controller.source_for",
        lambda pool, rent, source=None: source or str(tmp_path / "track"),
    )
    monkeypatch.setattr(
        "etude.launch.client.standing", lambda experiment: b.scheduler.standing(experiment)
    )
    monkeypatch.setattr(import_module("etude.launch.client"), "_client", lambda: b.scheduler)
    run(capsys, "rent", "l4", "--hours", "1")
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
            "packet": packet.model_dump(mode="json"),
            "fetch": {},
            "inbox_url": url + "/inbox",
            "out": {"prefix": "out/", "form": {"url": url + "/upload", "fields": {}}},
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
        # The worker keeps its cache under ETUDE_HOME, but no launcher records.
        assert not launcher_file(tmp_path / "launcher").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_job_file_takes_parameters_and_records_them_on_each_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    path = tmp_path / "job.py"
    path.write_text(
        r"""from __future__ import annotations
from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, RunRequest
def training_jobs(hidden: int = 256, seeds: int = 1, lr: float = 0.1, fast: bool = False,
         tag='x', shape: list[int] | None = None) -> list[RunRequest]:
    assert type(hidden) is int and type(lr) is float and type(fast) is bool
    job = TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=f'echo {hidden} {lr} {fast} {tag}'), max_hours=0.01, max_usd=1, name=f'h{hidden}', repo=None)
    return job.runs(*range(seeds))
"""
    )

    sent = run(capsys, "queue", str(path), "--hidden", "512", "--seeds", "2", "--fast")
    records = [default().record(line.split()[0]) for line in sent]
    expected = {"hidden": 512, "seeds": 2, "lr": 0.1, "fast": True, "tag": "x"}
    assert [record.launch.params for record in records] == [expected, expected]
    # Identity comes from what runs, not from the arguments that led there.
    again = run(capsys, "queue", str(path), "--hidden", "512", "--fast", "--tag", "x")[0].split()[0]
    assert default().record(again).job == records[0].job
    assert default().record(again).launch.params["seeds"] == 2  # an existing address is left alone

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
    upgrade(tmp_path / "home", development=True)
    path = tmp_path / "job.py"
    path.write_text(
        r'''from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job
def training_jobs(width: int, fast: bool = False):
    """Train a chosen width."""
    return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="true"), name=f"width-{width}", max_hours=1, repo=None)
'''
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
    id = [line for line in capsys.readouterr().out.splitlines() if ": state from " not in line][
        0
    ].split()[0]
    assert default().record(id).launch.params == {"width": 32, "fast": True}
    assert main(["queue", str(path), "--width", "64", "--experiment", "wider"]) == 0
    path.write_text(r"""def training_jobs(pool: str): return None
""")
    assert main(["queue", str(path), "--help"]) == 1
    assert "clashes" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "logs", "cancel"])
def test_missing_launcher_runs_offer_a_reading_remedy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    upgrade(tmp_path / "home", development=True)
    arguments = {
        "status": ["coin-seed-0"],
        "logs": ["--run", "coin-seed-0"],
        "cancel": ["coin-seed-0"],
    }
    assert main([command, *arguments[command]]) == 1
    error = capsys.readouterr().err
    assert "the launcher has no run 'coin-seed-0'" in error
    assert "etude report FILE" in error
    assert "new --experiment" not in error


@pytest.mark.parametrize("command", ["launch", "queue"])
def test_several_seed_launch_needs_no_extra_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    file = tmp_path / "job.py"
    file.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.spec import Job
def training_jobs() -> Job:
    return TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command="true"), seeds=(0, 1, 2), max_hours=1, name="base", repo=None)
"""
    )
    assert main([command, str(file)]) == 0
    assert len([line for line in capsys.readouterr().out.splitlines() if "new," in line]) == 3


@pytest.mark.parametrize("command", ["launch", "queue", "train", "smoke"])
@pytest.mark.parametrize("recipe", [False, True])
def test_legacy_declarations_refuse_before_training_or_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    recipe: bool,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    path = tmp_path / "legacy.py"
    path.write_text(
        "from pathlib import Path\n"
        "def runs() -> object:\n"
        "    Path(__file__).with_suffix('.called').touch()\n"
        "    raise AssertionError('legacy declaration ran')\n"
        + (
            "from pydantic import BaseModel\n"
            "def train(config: BaseModel) -> None:\n"
            "    Path(__file__).with_suffix('.called').touch()\n"
            if recipe
            else ""
        )
    )
    assert main([command, str(path)]) == 1
    assert "rename runs() to training_jobs()" in capsys.readouterr().err
    assert not path.with_suffix(".called").exists()
    assert not launcher_file(home).exists()
    assert not (home / "track").exists()
    with pytest.raises(LauncherError, match=r"rename runs\(\) to training_jobs\(\)"):
        read_training_jobs(path)
    with pytest.raises(LauncherError, match=r"rename runs\(\) to training_jobs\(\)"):
        train(path)


def test_new_declaration_takes_precedence_and_help_does_not_invoke_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    path = tmp_path / "sweep.py"
    path.write_text(
        r"""from etude.jobs import TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from pathlib import Path
from etude.spec import Job
def runs() -> Job:
    raise AssertionError('legacy declaration ran')
def training_jobs(width: int = 32) -> list[Job]:
    Path(__file__).with_suffix('.called').touch()
    return [TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=f'echo {width}'), name=name, seeds=(0, 7), max_hours=1, repo=None) for name in ('small', 'wide')]
"""
    )
    assert main(["launch", str(path), "--help"]) == 1
    assert "rename runs()" in capsys.readouterr().err
    path.write_text(
        path.read_text().replace(
            "def runs() -> Job:\n    raise AssertionError('legacy declaration ran')\n", ""
        )
    )
    assert main(["launch", str(path), "--help"]) == 0
    assert "--width" in capsys.readouterr().out
    assert not path.with_suffix(".called").exists()
    assert not launcher_file(home).exists()
    requests = read_training_jobs(path, ["--width", "64"])
    assert [request.id for request in requests] == [
        "sweep/small@0",
        "sweep/small@7",
        "sweep/wide@0",
        "sweep/wide@7",
    ]
    assert all(request.params == {"width": 64} for request in requests)
    assert main(["queue", str(path), "--width", "64", "--job", "wide", "--seed", "7"]) == 0
    assert "sweep/wide@7  new, queued" in capsys.readouterr().out
    assert default().record("sweep/wide@7").launch.params == {"width": 64}
