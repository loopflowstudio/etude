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
from etude.launch.database import launcher_file
from etude.records import Clock
from etude.run import sync
from etude.storage import DirectoryStorage
from tests.train.tracking import open_run

# What reads, schedules or plays evaluations, beside the records a page carries.
EVAL_READERS = tuple(
    f"etude.eval.{name}"
    for name in ("block", "convert", "match", "query", "schedule", "tournament")
)


@pytest.mark.parametrize(
    ("command", "count", "forbidden"),
    [
        (["view", "--help"], 8, ("numpy", "pydantic")),
        (["report", "--help"], 8, ("numpy", "pydantic")),
        # No eval run and no eval_jobs(state), so nothing that reads or plays evaluations
        # loads: only `etude.eval.records`, whose `Gap` a page's points carry. The report
        # recognises experiment files as `etude launch` does (`etude.experiment_file`).
        (
            ["report", "{directory}/pilot.py", "--source", "{directory}"],
            31,  # shared run identity, delivery and job specification: the checkpoint records runs and evals share
            ("boto3", "duckdb", *EVAL_READERS, "etude.train.tracking"),
        ),
        (
            ["sync", "--directory", "{directory}"],
            19,  # shared run and job specification: out membership
            ("boto3", "numpy", "etude.page", "etude.train.report"),
        ),
        (
            ["sync", "--directory", "{directory}", "--seal"],
            # etude.out, etude.stopping for Stopped, etude.train.state for saves and
            # etude.checkpoints for the records a run publishes, and
            # etude.identity for config and seed validation shared with the launcher.
            26,
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
    # The launcher contributes its command table; report errors use etude.errors.
    assert {name for name in loaded if name.startswith("etude.launch")} <= {
        "etude.launch",
        "etude.launch.cli",
        "etude.launch.commands",
    }
    assert not launcher_file(tmp_path / "home").exists()


def test_checkpoints_are_listed_at_their_storage_address_while_the_run_goes_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    local, bucket = tmp_path / "local", tmp_path / "bucket"
    run = open_run(
        experiment="e",
        run="r",
        clocks=[Clock(name="step", kind="step", meaning="steps")],
        directory=local,
    )
    model = tmp_path / "model.pt"
    for step in (1, 2, 3):
        model.write_bytes(f"weights at {step}".encode())
        run.checkpoint(model, step=step)
    deadline = time.monotonic() + 10
    while len(list((local / "e/r/checkpoints").glob("*.json"))) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert sync(local, storages=[DirectoryStorage(bucket)]) == {}
    assert not (bucket / "e/r/outcome.json").exists()  # the run has not finished

    assert main(["checkpoints", "--source", str(bucket)]) == 0
    listed = [line.split("\t") for line in capsys.readouterr().out.splitlines()]
    # Where each file is in the source read, not where the machine that wrote it kept it.
    assert [(fields[1], fields[-1]) for fields in listed] == [
        (str(step), (bucket / f"e/r/checkpoints/step-{step}.pt").resolve().as_uri())
        for step in (1, 2, 3)
    ]
    pytest.importorskip("duckdb")
    recorded = statsdb("e", bucket).execute("select ordinal from training_checkpoints").fetchall()
    assert sorted(recorded) == [(1,), (2,), (3,)]
    run.finish()


def test_checkpoints_default_to_this_computers_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home with spaces"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    model = tmp_path / "model.pt"
    model.write_bytes(b"weights")
    with open_run(
        experiment="e", run="r", clocks=[Clock(name="step", kind="step", meaning="steps")]
    ) as run:
        run.checkpoint(model, step=3)
    assert main(["checkpoints"]) == 0
    (listed,) = capsys.readouterr().out.splitlines()
    fields = listed.split("\t")
    assert fields[:2] == ["e/r", "1"]
    assert "step=3" in fields[2].split()
    assert fields[-1] == (home / "track/e/r/checkpoints/step-3.pt").as_uri()


def test_checkpoints_explains_an_empty_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home with spaces"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    assert main(["checkpoints"]) == 1
    assert f"no runs in {home / 'track'}" in capsys.readouterr().err
    with open_run(
        experiment="e", run="r", clocks=[Clock(name="step", kind="step", meaning="steps")]
    ):
        pass
    assert main(["checkpoints", "--experiment", "missing"]) == 1
    assert f"no runs for experiment 'missing' in {home / 'track'}" in capsys.readouterr().err
