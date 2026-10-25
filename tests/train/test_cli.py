"""Tracking commands keep startup independent of compute and unused dependencies."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from etude.cli import main
from etude.eval.query import statsdb
from etude.run.delivery import sync
from etude.run.storage import DirectoryStorage
from etude.train.records import Step
from etude.train.run import track
from tests.experiments import experiment_file


@pytest.mark.parametrize(
    ("command", "count", "forbidden"),
    [
        (["report", "--help"], 10, ("numpy", "pydantic")),
        # No eval run and no eval(state), so nothing of evaluation loads.
        (
            ["report", "{directory}/pilot.py", "--source", "{directory}"],
            22,
            ("boto3", "duckdb", "etude.eval", "etude.train.run"),
        ),
        (
            ["sync", "--directory", "{directory}"],
            15,
            ("boto3", "numpy", "etude.page", "etude.train.report"),
        ),
        (
            ["sync", "--directory", "{directory}", "--seal"],
            20,
            ("boto3", "etude.page", "etude.train.report"),
        ),
    ],
)
def test_tracking_command_import_counts(
    tmp_path: Path, command: list[str], count: int, forbidden: tuple[str, ...]
) -> None:
    directory = tmp_path / "files"
    directory.mkdir()
    (directory / "pilot.py").write_text("")
    args = [value.format(directory=directory) for value in command]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json; from etude.cli import main; "
            f"result=main({args!r}); print(json.dumps(sorted(sys.modules))); sys.exit(result)",
        ],
        env={**os.environ, "ETUDE_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    # The directory holds no runs, so the report says so and fails.
    assert done.returncode == (1 if "--source" in command else 0), done.stderr
    modules = json.loads(done.stdout.splitlines()[-1])
    loaded = [name for name in modules if name.startswith("etude")]
    # Pin help and execution separately: help builds no records, sync loads no
    # report, and only sealing training logs loads the block format.
    assert len(loaded) == count, loaded
    assert not [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    # One `etude` command: the launcher contributes only its command table.
    assert {name for name in loaded if name.startswith("etude.launch")} <= {
        "etude.launch",
        "etude.launch.cli",
        "etude.launch.commands",
        "etude.launch.errors",
    }
    assert not (tmp_path / "home" / "launcher.db").exists()


def test_checkpoints_are_listed_at_their_storage_address_while_the_run_goes_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local, bucket = tmp_path / "local", tmp_path / "bucket"
    run = track(experiment="e", run="r", clocks=[Step("step", "steps")], directory=local)
    model = tmp_path / "model.pt"
    for step in (1, 2, 3):
        model.write_bytes(f"weights at {step}".encode())
        run.checkpoint(model, step=step)
    deadline = time.monotonic() + 10
    while len(list((local / "r/checkpoints").glob("*.json"))) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert sync(local, storages=[DirectoryStorage(bucket)]) == {}
    assert not (bucket / "r/state.json").exists()  # the run has not finished

    assert main(["checkpoints", "--source", str(bucket)]) == 0
    listed = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    # Where each file is in the source read, not where the machine that wrote it kept it.
    assert [(fields[1], fields[-1]) for fields in listed] == [
        (str(step), (bucket / f"r/checkpoints/step-{step}.pt").resolve().as_uri())
        for step in (1, 2, 3)
    ]
    pytest.importorskip("duckdb")
    recorded = statsdb("e", bucket).execute("select ordinal from training_checkpoints").fetchall()
    assert sorted(recorded) == [(1,), (2,), (3,)]
    run.finish()


SERVE_INDEX = """
import json, sys, threading
from pathlib import Path
from urllib.request import urlopen
from etude.cli import main  # what `etude` loads before it runs a command
from etude.page import Server
server = Server(Path(sys.argv[1]), Path(sys.argv[2]), ("127.0.0.1", 0))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
index, page = (urlopen(base + route).read().decode() for route in ("/", "/pilot"))
assert '"href": "/sweep"' in index and "pilot.py:report" in page
print(json.dumps(sorted(sys.modules)))
"""


def test_serving_an_index_loads_no_evaluation_launcher_or_bucket_client(tmp_path: Path) -> None:
    runs, experiments = tmp_path / "runs", tmp_path / "experiments"
    for name in ("pilot", "sweep"):
        with track(
            experiment=name, run=f"{name}-1", clocks=[Step("step", "steps")], directory=runs
        ) as run:
            run.log({"loss": 1.0}, step=1)
        experiment_file(
            experiments,
            name,
            "from etude.report import Report\n\n\ndef report() -> Report:\n    return Report()\n",
        )
    done = subprocess.run(
        [sys.executable, "-c", SERVE_INDEX, str(runs), str(experiments)],
        env={**os.environ, "ETUDE_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    modules = json.loads(done.stdout.splitlines()[-1])
    loaded = [name for name in modules if name.startswith("etude")]
    # Neither experiment has eval runs or eval(): the index reads training files alone.
    assert len(loaded) == 22, loaded
    forbidden = (
        "boto3",
        "duckdb",
        "etude.eval",
        "etude.experiment",
        "etude.train.run",
        "etude.launch.client",
    )
    assert not [n for n in modules if any(n == f or n.startswith(f + ".") for f in forbidden)]
