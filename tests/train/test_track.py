"""The tracking client end to end: files, a directory standing in for a bucket, and S3."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import boto3  # type: ignore[import-untyped]
import numpy as np
import pytest
from moto import mock_aws

from etude.cli import main
from etude.runs.delivery import sync
from etude.runs.errors import EtudeError
from etude.runs.listing import runs
from etude.runs.storage import DirectoryStorage, S3Storage, default_root
from etude.train.block import rows
from etude.train.read import buckets, read
from etude.train.records import Step, Time
from etude.train.report import page_data, pages
from etude.train.run import EtudeWarning, Run, track
from etude.train.serve import Server

CLOCKS = [
    Step("progress/updates", "optimizer updates, absolute across continuation"),
    Time("progress/training_seconds", "active training time, pauses excluded"),
]


def manabot_row(update: int) -> dict[str, object]:
    """The shape of a manabot projection row: coordinates and metrics in one mapping."""
    return {
        "progress/updates": update,
        "progress/training_seconds": update * 0.25,
        "rl/loss": 1.0 / (1 + update),
        "rl/entropy": math.nan if update == 3 else 1.5,
        "stage/id": "self-play",
        "availability/rl/loss": True,
        "ppo/losses": {"policy": 0.1 * update},
    }


def test_rows_round_trip_through_files_and_a_bucket_directory(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = track(
        experiment="capacity-pilot",
        run="training-3f9a",
        clocks=CLOCKS,
        job="capacity-64",
        seed=1,
        kind="training-monitor",
        config='{"lr": 0.0003,   "seed": 7}',
        directory=tmp_path / "local",
        storages=[bucket],
        segment_seconds=0.05,
    )
    for update in range(200):
        run.log(manabot_row(update))
    # One clock of two: charted against the clock it gave, and counted.
    with pytest.warns(EtudeWarning, match="no value for clock") as said:
        run.log({"rl/loss": 0.5}, **{CLOCKS[0].name: 999})
        run.log({"rl/loss": 0.5}, **{CLOCKS[0].name: 999})
    # Said once, at the caller's line; the second such row is only counted.
    assert len(said) == 1 and said[0].filename == __file__
    run.finish()
    assert run.dropped == 0 and run.incomplete == 2 and run.problems == {}

    prefix = "training-3f9a/"
    keys = bucket.keys(prefix)
    assert prefix + "run.json" in keys and prefix + "state.json" in keys
    # A finished run this small is one block, and its rows read back as logged.
    assert len([key for key in keys if key.endswith(".blk")]) == 1
    stored = [
        row
        for table in read(bucket, "capacity-pilot", "training-3f9a").tables
        for row in rows(table)
    ]
    assert len(stored) == 202
    partial, _ = [row for row in stored if row["clocks"] == {CLOCKS[0].name: 999}]
    assert partial["values"] == {"rl/loss": 0.5}
    third = stored[3]
    assert third["clocks"] == {"progress/updates": 3, "progress/training_seconds": 0.75}
    values = third["values"]
    assert isinstance(values, dict)
    # The files keep everything as written, including what a chart cannot draw.
    assert math.isnan(values["rl/entropy"]) and values["stage/id"] == "self-play"
    assert values["availability/rl/loss"] is True and values["ppo/losses/policy"] == pytest.approx(
        0.3
    )
    declared = json.loads(bucket.get(prefix + "run.json"))["declaration"]
    assert declared["config_json"] == '{"lr": 0.0003,   "seed": 7}'  # Byte for byte.

    # Read from the bucket's copy alone.
    data = read(bucket, "capacity-pilot", "training-3f9a")
    assert data.state is not None and data.state.state == "finished"
    assert data.job == "capacity-64" and data.state.counters.incomplete == 2
    assert set(data.metrics()) == {"rl/loss", "rl/entropy", "ppo/losses/policy"}
    entropy = data.points("rl/entropy", "progress/training_seconds")
    assert len(entropy) == 200 and int(np.isnan(entropy.y).sum()) == 1
    assert entropy.x[-1] == 199 * 0.25
    # The partial rows appear on the clock they gave and on no other.
    for clock, points, last in (("progress/updates", 201, 999), (CLOCKS[1].name, 200, 49.75)):
        loss = data.points("rl/loss", clock)
        assert len(loss) == points and loss.x[-1] == last


def test_resume_replays_harmlessly_and_sync_delivers_what_an_outage_left(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    # First process: no destination is reachable, so only local files exist.
    first = track(
        experiment="pilot", run="seed-1", clocks=CLOCKS, directory=local, segment_seconds=0.05
    )
    for update in range(50):
        first.log(manabot_row(update))
    first.finish()
    # A second process resumes the same name behind the last row and replays ten.
    second = track(
        experiment="pilot", run="seed-1", clocks=CLOCKS, directory=local, segment_seconds=0.05
    )
    for update in range(40, 80):
        second.log(manabot_row(update), **{"rl/loss": 9.0})
    second.finish()
    with pytest.raises(EtudeError, match="run_clock_conflict"):
        track(experiment="pilot", run="seed-1", clocks=CLOCKS[:1], directory=local)

    bucket = DirectoryStorage(tmp_path / "bucket")
    assert sync(local, storages=[bucket]) == {}
    assert sync(local, storages=[bucket]) == {}  # Idempotent.
    data = read(bucket, "pilot", "seed-1")
    assert data.rows == 90  # Every row is kept...
    loss = data.points("rl/loss", "progress/updates")
    # ...and a reader sees one point per clock value, the later write.
    assert len(loss) == 80 and loss.y[45] == 9.0 and loss.y[10] != 9.0


def test_log_never_blocks_or_raises_and_counts_what_it_drops(tmp_path: Path) -> None:
    run = track(
        experiment="pilot",
        run="flood",
        clocks=CLOCKS,
        directory=tmp_path,
        queue_size=100,
        segment_seconds=0.05,
    )
    started = time.perf_counter()
    with pytest.warns(EtudeWarning) as said:
        for update in range(50_000):
            run.log(manabot_row(update))
        run.log(None)
        run.log({"progress/updates": object()})
    assert sorted("queue is full" in str(w.message) for w in said) == [False, True]
    elapsed = time.perf_counter() - started
    run.finish(timeout=5)
    written = read(tmp_path, "pilot", "flood").rows
    assert run.dropped > 0 and written + run.dropped == 50_002
    assert elapsed < 5

    strict = track(experiment="pilot", run="strict", clocks=CLOCKS, directory=tmp_path, strict=True)
    with pytest.raises(EtudeError, match="row_missing_clock"):
        strict.log({"rl/loss": 1.0})
    strict.log(manabot_row(10))
    strict.log(manabot_row(10))  # The same step twice is ordinary.
    with pytest.raises(EtudeError, match="clock_went_backwards"):
        strict.log(manabot_row(9))
    strict.finish()


def test_files_alone_read_back_and_report_with_a_seed_sample(tmp_path: Path) -> None:

    for seed, length in enumerate((120, 120, 60)):
        run = track(
            experiment="pilot",
            run=f"seed-{seed}",
            clocks=CLOCKS,
            job="arm",
            seed=seed,
            directory=tmp_path,
            segment_seconds=0.05,
        )
        for update in range(length):
            run.log(manabot_row(update), **{"rl/loss": float(seed)})
        with pytest.warns(EtudeWarning, match="went backwards"):
            # A later write at an old value: warned about, and still kept.
            run.log(manabot_row(5), **{"rl/loss": 100.0 + seed})
        run.finish()

    assert runs(tmp_path) == [("pilot", f"seed-{seed}") for seed in range(3)]
    data = read(tmp_path, "pilot", "seed-1")
    assert data.rows == 121 and data.job == "arm" and data.state is not None
    points = data.points("rl/loss", "progress/updates")
    assert len(points) == 120 and (points.x[5], points.y[5]) == (5.0, 101.0)
    assert (points.x[6], points.y[6]) == (6.0, 1.0)
    reduced = buckets(points, 10)
    assert len(reduced) == 10 and int(reduced.count.sum()) == 120
    assert reduced.max[0] == 101.0 and reduced.min[0] == 1.0  # The extreme survives bucketing.

    page = page_data("pilot", [read(tmp_path, "pilot", f"seed-{seed}") for seed in range(3)])
    jobs = page["jobs"]
    assert isinstance(jobs, dict)
    (band,) = jobs["progress/updates"]["rl/entropy"]
    assert band["job"] == "arm" and band["n"][0] == 3 and band["n"][-1] == 2
    assert main(["reports", "write", "--source", str(tmp_path)]) == 0
    target = tmp_path / "reports/pilot.html"
    html = target.read_text()
    assert "__DATA__" not in html and "__UPLOT_JS__" not in html
    assert "active training time, pauses excluded" in html and '"refresh": null' in html
    # Generating again replaces the report, and reports are not mistaken for runs.
    assert main(["reports", "write", "--source", str(tmp_path), "--experiment", "pilot"]) == 0
    assert list(target.parent.iterdir()) == [target] and len(runs(tmp_path)) == 3
    assert main(["reports", "write", "--source", str(tmp_path), "--experiment", "absent"]) == 1


def test_checkpoint_references_read_back_and_an_incomplete_coordinate_is_not_recorded(
    tmp_path: Path,
) -> None:
    training = track(
        experiment="pilot",
        run="training",
        clocks=CLOCKS,
        directory=tmp_path / "trainer",
        segment_seconds=0.05,
    )
    for update in (100, 200):
        training.log(manabot_row(update))
        training.checkpoint(
            f"s3://models/pilot/{update}.pt",
            {"progress/updates": update, "progress/training_seconds": update * 0.25},
        )
    with pytest.warns(EtudeWarning, match="checkpoint_clock_mismatch"):
        training.checkpoint("s3://models/pilot/bad.pt", {"progress/updates": 300})
    training.finish()
    assert training.incomplete == 1 and training.problems == {}

    trained = read(tmp_path / "trainer", "pilot", "training")
    assert [(c.ordinal, c.artifact.uri) for c in trained.checkpoints] == [
        (1, "s3://models/pilot/100.pt"),
        (2, "s3://models/pilot/200.pt"),
    ]
    assert trained.checkpoints[1].at == {
        "progress/updates": 200.0,
        "progress/training_seconds": 50.0,
    }


AT = {"progress/updates": 50, "progress/training_seconds": 12.5}


def test_metrics_and_checkpoints_reach_an_s3_bucket_and_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="runs")
        bucket = S3Storage("runs", "team/a", client=client)
        run = track(
            experiment="pilot",
            run="s3",
            clocks=CLOCKS,
            directory=tmp_path / "local",
            storages=[bucket],
            segment_seconds=0.05,
        )
        for update in range(50):
            run.log(manabot_row(update))
        model = tmp_path / "latest.pt"
        weights = os.urandom(9 * 1024 * 1024)  # Large enough to upload in parts.
        model.write_bytes(weights)
        first = run.checkpoint(model, AT, tags=["periodic"])
        model.write_bytes(b"the trainer saved over the same path")
        second = run.checkpoint(model, {**AT, "progress/updates": 60})
        run.tag(first, "best")
        run.untag(first, "periodic")
        run.finish(timeout=30)
        assert run.problems == {} and run.incomplete == 0

        prefix = "team/a/s3/"
        assert first.uri == f"s3://runs/{prefix}checkpoints/progress_updates-50.pt"
        assert first.sha256 == sha256(weights).hexdigest() and first.size_bytes == len(weights)
        stored = client.get_object(Bucket="runs", Key=prefix + "checkpoints/progress_updates-50.pt")
        assert stored["Body"].read() == weights  # Not what the trainer wrote afterwards.
        assert second.sha256 != first.sha256

        # Everything reads back from the bucket alone.
        data = read(bucket, "pilot", "s3")
        assert data.rows == 50 and data.state is not None
        assert [c.tags for c in data.checkpoints] == [("best",), ()]
        assert data.checkpoints[0].artifact == first and data.checkpoints[0].at == AT
        assert pages(bucket)["pilot"]["runs"] == pages(tmp_path / "local")["pilot"]["runs"]
        assert sync(tmp_path / "local", storages=[bucket]) == {}
        assert main(["checkpoints", "--source", "s3://runs/team/a"]) == 0
        listed = capsys.readouterr().out.splitlines()
        assert len(listed) == 2 and "\tbest\t" in listed[0] and first.uri in listed[0]


def test_checkpoints_are_stored_locally_and_tagged_after_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNPOD_POD_ID", raising=False)
    monkeypatch.delenv("ETUDE_JOB_ID", raising=False)

    model = tmp_path / "step.pt"
    model.write_bytes(b"weights")

    def opened() -> Run:
        return track(
            experiment="pilot",
            run="local",
            clocks=CLOCKS,
            directory=tmp_path / "runs",
            storages=[DirectoryStorage(tmp_path / "copy")],
            segment_seconds=0.05,
        )

    later = {**AT, "progress/updates": 60}
    run = opened()
    stored = run.checkpoint(model, AT)
    referenced = run.checkpoint("s3://elsewhere/model.pt", later)
    with pytest.warns(EtudeWarning, match="checkpoint was not recorded"):
        run.checkpoint(tmp_path / "absent.pt", AT)
    run.finish()
    assert run.incomplete == 1
    copied = tmp_path / "copy/local/checkpoints/progress_updates-50.pt"
    assert copied.read_bytes() == b"weights" and stored.uri == copied.resolve().as_uri()
    assert referenced.sha256 is None and referenced.size_bytes is None

    monkeypatch.setenv("RUNPOD_POD_ID", "pod-b")
    monkeypatch.setenv("ETUDE_ATTEMPT", "attempt-7")
    resumed = opened()
    resumed.log(manabot_row(1))
    resumed.tag(1, "best")
    # Named by where the first clock stood: a third checkpoint, at step 70.
    assert resumed.checkpoint(model, {**AT, "progress/updates": 70}).uri.endswith(
        "checkpoints/progress_updates-70.pt"
    )
    resumed.finish()
    for source in (tmp_path / "runs", tmp_path / "copy"):
        data = read(source, "pilot", "local")
        assert [(c.ordinal, c.tags) for c in data.checkpoints] == [(1, ("best",)), (2, ()), (3, ())]
        # The relaunch is the same run and a second attempt, on other hardware.
        first, second = data.attempts
        assert (first.pod, first.attempt) == (None, None) and first.writer != second.writer
        assert (second.pod, second.attempt) == ("pod-b", "attempt-7")
    # Only the relaunch logged rows, and its block carries its name.
    assert [table.writer for table in data.tables] == [second.writer]


def test_logging_from_training_code_imports_no_command_line_library() -> None:
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, etude.train.run, etude.train.read, etude.train.report; "
            "print(sorted(name for name in ('typer', 'rich', 'boto3') if name in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "[]"


def test_the_server_lists_experiments_and_serves_each_report_from_the_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    run = track(experiment="pilot", run="a", clocks=CLOCKS)  # no directory named
    run.log(manabot_row(1))
    run.finish()
    server = Server(default_root(), ("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/") as response:
            assert '<a href="/pilot">pilot</a></td><td>1</td>' in response.read().decode()
        with urlopen(base + "/pilot") as response:
            page = response.read().decode()
        assert "rl/entropy" in page and '"refresh": 5.0' in page
        with pytest.raises(HTTPError) as missing:
            urlopen(base + "/absent")
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_tracking_commands_use_local_files_with_a_selected_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "controller").write_text("unreachable.invalid")
    monkeypatch.setenv("ETUDE_HOME", str(home))
    monkeypatch.delenv("ETUDE_CONTROLLER_LOCAL", raising=False)
    local = tmp_path / "local"
    destination = tmp_path / "copy"
    with track(experiment="pilot", run="one", clocks=CLOCKS, directory=local) as run:
        run.log(manabot_row(10))
        run.checkpoint(
            "s3://models/one.pt", {"progress/updates": 10, "progress/training_seconds": 2.5}
        )
    assert main(["sync", "--directory", str(local), "--to", str(destination), "--seal"]) == 0
    assert read(destination, "pilot", "one").points("rl/loss", "progress/updates").x.tolist() == [
        10
    ]
    assert main(["checkpoints", "--source", str(destination), "--experiment", "pilot"]) == 0
    assert "s3://models/one.pt" in capsys.readouterr().out
    target = tmp_path / "pages"
    assert main(["reports", "write", "--source", str(destination), "--out", str(target)]) == 0
    assert (target / "pilot.html").is_file()

    def serve_once(server: Server) -> None:
        # Replace the blocking server loop; inspect the pages it would serve.
        assert "pilot" in server.index()
        assert server.report("pilot") is not None
        raise KeyboardInterrupt

    monkeypatch.setattr(Server, "serve_forever", serve_once)
    assert main(["reports", "serve", "--source", str(destination), "--port", "0"]) == 0
    assert "http://127.0.0.1:" in capsys.readouterr().out
    assert not (home / "launcher.db").exists()
