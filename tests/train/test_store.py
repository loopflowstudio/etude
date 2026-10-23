"""Block lifecycle and chart reads through the public tracking interfaces."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import numpy as np
import pytest
from moto import mock_aws

from etude.run.block_file import block_key, tail_key
from etude.run.delivery import RunFiles, StorageDestination, sync
from etude.run.errors import EtudeError
from etude.run.listing import runs
from etude.run.records import Attempt, RunDeclaration, RunFile, SourceKey
from etude.run.storage import DirectoryStorage, S3Storage, Storage, run_prefix
from etude.train.block import Builder
from etude.train.manifest import Manifest, write_manifest
from etude.train.read import buckets, read, read_all
from etude.train.records import Step
from etude.train.report import pages
from etude.train.run import log_line, seal_runs, track, write_block
from tests.run.storage import CountedClient, MeasuredStorage


def test_reports_fetch_each_runs_metadata_once(tmp_path: Path) -> None:
    for name in ("one", "two"):
        with track(experiment="e", run=name, clocks=[], directory=tmp_path):
            pass
    measured = MeasuredStorage(DirectoryStorage(tmp_path))
    report = pages(measured)
    records = report["e"]["runs"]
    assert isinstance(records, list) and len(records) == 2
    # One declaration, one attempt and one state per run; discover once, then
    # list each run. An empty run needs no metric reads.
    assert measured.requests == 6
    assert measured.listings == 3


@pytest.mark.parametrize("attributed", [False, True])
def test_resuming_does_not_inherit_a_previous_attempts_finished_state(
    tmp_path: Path, attributed: bool
) -> None:
    local = tmp_path / "local"
    bucket = DirectoryStorage(tmp_path / "bucket")
    with track(experiment="e", run="r", clocks=[], directory=local):
        pass
    if not attributed:
        # Earlier evidence has no writer on its completion; retain those bytes.
        path = local / "r/state.json"
        state = json.loads(path.read_text())
        state.pop("writer")
        path.write_text(json.dumps(state))
    assert sync(local, storages=[bucket]) == {}
    finished = read(bucket, "e", "r").state
    assert finished is not None and finished.state == "finished"

    resumed = track(experiment="e", run="r", clocks=[], directory=local)
    try:
        assert sync(local, storages=[bucket]) == {}
        assert read(local, "e", "r").state is None
        assert read(bucket, "e", "r").state is None
    finally:
        resumed.finish("failed")
    assert sync(local, storages=[bucket]) == {}
    failed = read(bucket, "e", "r").state
    assert failed is not None and failed.state == "failed"


@pytest.mark.parametrize("backend", ["directory", "s3"])
@pytest.mark.parametrize("suffix", [".pt", ".json"])
def test_a_new_model_at_a_recorded_step_is_a_new_version(
    tmp_path: Path, backend: str, suffix: str
) -> None:
    local = tmp_path / "local"
    model = tmp_path / ("weights" + suffix)
    with mock_aws():
        destination: Storage = DirectoryStorage(tmp_path / "bucket")
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="etude-checkpoints")
            destination = S3Storage("etude-checkpoints", client=client)
        measured = MeasuredStorage(destination)
        extension = suffix + ".data" if suffix == ".json" else suffix
        # Each process: an attempt record and the state, then any new checkpoint and its record.
        kept = {"": b"first", ".ordinal-2": b"other"}
        for body, uploads, recorded in ((b"first", 5, 1), (b"other", 4, 2), (b"other", 2, 2)):
            model.write_bytes(body)
            with track(
                experiment="e", run="r", clocks=[Step("step", "steps")], directory=local
            ) as run:
                run.checkpoint(model, step=10)
                model.write_bytes(b"overwritten by training")
            before = measured.uploads
            assert sync(local, storages=[measured]) == {}
            assert measured.uploads - before == uploads
            checkpoints = read(destination, "e", "r").checkpoints
            assert [c.ordinal for c in checkpoints] == list(range(1, recorded + 1))
            # Nothing delivered is replaced: a new model at the same step is a new version.
            for version, held in list(kept.items())[:recorded]:
                assert destination.get(f"r/checkpoints/step-10{version}{extension}") == held
            assert checkpoints[-1].artifact.size_bytes == len(body)
            before = measured.uploads
            assert sync(local, storages=[measured]) == {}
            assert measured.uploads == before


def test_recent_blocks_compact_and_delete_covered_uploads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.train.run.RECENT_BLOCKS", 2)
    bucket = DirectoryStorage(tmp_path / "bucket")
    run = track(
        experiment="e",
        run="r",
        clocks=[Step("step", "steps")],
        directory=tmp_path / "local",
        segment_seconds=0.01,
        storages=[bucket],
    )
    for step in range(3):
        run.log({"value": step}, step=step)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if read(bucket, "e", "r").rows == step + 1:
                break
            time.sleep(0.01)
        else:
            pytest.fail("recent rows were not delivered")
    run.finish()
    keys = bucket.keys(run_prefix("r"))
    assert not any(key.endswith(".tail") for key in keys)
    assert len([key for key in keys if "/metrics/2-" in key]) == 2
    assert not any("/metrics/1-" in key for key in keys)
    (note,) = [key for key in keys if key.endswith(".removed")]
    removed = bucket.get(note).decode().splitlines()
    # Only what could have been delivered is listed: a live writer's tails never were.
    assert removed and all(key.startswith("metrics/1-") for key in removed)
    assert all(not (run.files.root / key).exists() for key in removed)
    assert read(bucket, "e", "r").points("value", "step").y.tolist() == [0, 1, 2]
    assert run.problems == {}


def test_stale_tail_and_explicit_sealing(tmp_path: Path) -> None:
    run = track(experiment="e", run="r", clocks=[Step("step", "steps")], directory=tmp_path)
    run.finish()
    writer = "abcdef123456"
    builder = Builder(["step"])
    builder.add(1000, {"step": 1}, {"value": 7})
    key = tail_key(0, writer)
    write_block(run.files.root, key, builder.table(writer, 0))
    assert key not in RunFiles(run.files.root).deliverable()
    os.utime(run.files.root / key, (time.time() - 61, time.time() - 61))
    assert key in RunFiles(run.files.root).deliverable()
    os.utime(run.files.root / key, None)
    # A dead process's unsealed log becomes the next block; stopped writers' tails go at once.
    (run.files.root / f"metrics/00000001-{writer}.open").write_text(
        log_line(2000, {"step": 2.0}, {"value": 8})
    )
    seal_runs(tmp_path)
    bucket = DirectoryStorage(tmp_path / "bucket")
    assert sync(tmp_path, storages=[bucket], stopped=True) == {}
    assert read(bucket, "e", "r").points("value", "step").y.tolist() == [7, 8]


@pytest.mark.parametrize("backend", ["directory", "s3"])
@pytest.mark.parametrize("failure", [OSError, FileNotFoundError])
def test_removals_wait_for_successful_replacement_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, failure: type[OSError]
) -> None:
    root = tmp_path / "local"
    (root / "metrics").mkdir(parents=True)
    files = RunFiles(root)
    first = block_key(1, 0, 0, "abcdef123456")
    replacement = block_key(2, 0, 0, "abcdef123456")
    (root / first).write_bytes(b"first")
    prefix = run_prefix("r")
    with mock_aws():
        storage: Storage = DirectoryStorage(tmp_path / "destination")
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="etude-removals")
            storage = S3Storage("etude-removals", client=client)
        destination = StorageDestination(storage)
        destination.ship(files, "r")
        (root / replacement).write_bytes(b"replacement")
        (root / "metrics/abcdef123456.removed").write_text(first + "\n")
        (root / first).unlink()
        put_file = storage.put_file

        def unavailable(key: str, path: Path) -> None:
            raise failure("unavailable")

        monkeypatch.setattr(storage, "put_file", unavailable)
        with pytest.raises(failure, match="unavailable"):
            destination.ship(files, "r")
        assert storage.get(prefix + first) == b"first"
        monkeypatch.setattr(storage, "put_file", put_file)
        destination.ship(files, "r")
        assert storage.get(prefix + replacement) == b"replacement"
        assert prefix + first not in storage.keys(prefix)
        assert files.shipped(destination.name, first) is None


def test_permanent_and_recent_duplicate_is_read_once(tmp_path: Path) -> None:
    prefix = run_prefix("r")
    writer = "abcdef123456"
    builder = Builder(["step"])
    builder.add(1000, {"step": 1}, {"value": 7})
    table = builder.table(writer, 0)
    write_block(tmp_path / prefix, block_key(1, 0, 0, writer), table)
    write_block(tmp_path / prefix, block_key(2, 0, 0, writer), table)
    data = read(tmp_path, "e", "r")
    assert data.rows == 1
    np.testing.assert_array_equal(data.points("value", "step").y, [7])


def test_rollups_preserve_spikes_counts_and_nonfinite_values(tmp_path: Path) -> None:
    builder = Builder(["step", "seconds"])
    for row in range(8192):
        value = float(row % 17)
        if row == 123:
            value = 10000.0
        if row == 124:
            value = float("nan")
        builder.add(row * 1000, {"step": float(row), "seconds": row * 0.1}, {"value": value})
    writer = "abcdef123456"
    write_block(tmp_path / run_prefix("r"), block_key(1, 0, 0, writer), builder.table(writer, 0))
    data = read(tmp_path, "e", "r")
    assert set(data.tables[0].reductions) == {16, 256, 4096}
    for clock in ("step", "seconds", "wall_seconds"):
        reduced = data.chart("value", clock, 32)
        if not len(reduced):
            continue
        assert len(reduced) <= 32
        assert int(reduced.count.sum()) == 8192
        assert int(reduced.nonfinite.sum()) == 1
        assert float(reduced.max.max()) == 10000
        assert reduced.first[0] == 0 and reduced.last[-1] == 8191 % 17
        raw = data.points("value", clock).y
        assert np.nansum(reduced.mean * (reduced.count - reduced.nonfinite)) == pytest.approx(
            np.nansum(raw)
        )
    zoom = data.chart("value", "step", 20, span=(100, 200))
    assert int(zoom.count.sum()) == 101
    assert zoom.max.max() == 10000


def test_repeated_clock_and_attempt_overlap_use_raw_rows(tmp_path: Path) -> None:
    builder = Builder(["step"])
    for row in range(256):
        builder.add(row * 1000, {"step": float(row // 2)}, {"value": row})
    write_block(
        tmp_path / run_prefix("r"),
        block_key(1, 0, 0, "abcdef123456"),
        builder.table("abcdef123456", 0),
    )
    data = read(tmp_path, "e", "r")
    reduced = data.chart("value", "step", 10)
    assert int(reduced.count.sum()) == 128
    assert reduced.first[0] == 1 and reduced.last[-1] == 255


@pytest.mark.parametrize("backend", ["directory", "s3"])
def test_manifest_chart_fetches_only_reduced_columns(tmp_path: Path, backend: str) -> None:
    writer = "abcdef123456"
    prefix = run_prefix("r")
    builder = Builder(["step"])
    for row in range(32768):
        builder.add(
            row * 1000, {"step": float(row)}, {"value": float(row), "other": float(row * 2)}
        )
    root = tmp_path / prefix
    key = block_key(2, 0, 0, writer)
    write_block(root, key, builder.table(writer, 0))
    write_manifest(root, writer)
    with mock_aws():
        storage: Storage = DirectoryStorage(tmp_path)
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="etude-chart")
            storage = S3Storage("etude-chart", client=client)
            for path in root.rglob("*"):
                if path.is_file():
                    storage.put(prefix + path.relative_to(root).as_posix(), path.read_bytes())
        measured = MeasuredStorage(storage)
        data = read(measured, "e", "r")
        assert measured.ranges == 0
        before = measured.bytes
        chart = data.chart("value", "step", 8)
        assert len(chart) == 8
        assert int(chart.count.sum()) == 32768
        assert chart.max.max() == 32767
        assert measured.bytes == before
        chart = data.chart("value", "step", 32)
        assert len(chart) == 32
        assert int(chart.count.sum()) == 32768
        assert measured.ranges == 2
        zoom = data.chart("value", "step", 32, span=(100, 200))
        assert zoom.count.sum() == 101
        assert measured.ranges > 0
        assert "tables" not in data.__dict__
        assert data.metrics() == ["value", "other"]
        assert data.rows == 32768


@pytest.mark.parametrize("rows", [4000, 60000])
def test_overview_preserves_requested_chart_resolution(tmp_path: Path, rows: int) -> None:
    writer = "abcdef123456"
    root = tmp_path / run_prefix("r")
    builder = Builder(["step"])
    for row in range(rows):
        builder.add(row * 1000, {"step": float(row)}, {"value": float(row)})
    write_block(root, block_key(2, 0, 0, writer), builder.table(writer, 0))
    write_manifest(root, writer)
    chart = read(tmp_path, "e", "r").chart("value", "step", limit=1000)
    assert len(chart) == 1000
    assert chart.count.sum() == rows
    assert chart.first[0] == 0 and chart.last[-1] == rows - 1


def test_a_read_is_a_snapshot_that_survives_compaction(tmp_path: Path) -> None:
    writer = "abcdef123456"
    root = tmp_path / run_prefix("r")
    builder = Builder(["step"])
    for row in range(256):
        builder.add(row * 1000, {"step": float(row)}, {"value": row})
    write_block(root, block_key(1, 0, 0, writer), builder.table(writer, 0))
    write_manifest(root, writer, permanent=False)
    snapshot = read(tmp_path, "e", "r")
    for row in range(256, 512):
        builder.add(row * 1000, {"step": float(row)}, {"value": row})
    write_block(root, block_key(2, 0, 0, writer), builder.table(writer, 0))
    (root / block_key(1, 0, 0, writer)).unlink()
    write_manifest(root, writer)
    # The recent block was taken whole when the run was read, so the earlier
    # read still answers from it; a new read sees the permanent block.
    assert int(snapshot.chart("value", "step", 10).count.sum()) == 256
    chart = read(tmp_path, "e", "r").chart("value", "step", 10, span=(0, 511))
    assert int(chart.count.sum()) == 512
    assert chart.last[-1] == 511


def test_reader_refreshes_vanished_tails_and_sequence_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = "abcdef123456"
    prefix = run_prefix("r")
    root = tmp_path / prefix
    builder = Builder(["step"])
    builder.add(1000, {"step": 1}, {"value": 1})
    write_block(root, tail_key(0, writer), builder.table(writer, 0))
    snapshot = read(tmp_path, "e", "r")
    write_block(root, block_key(1, 0, 0, writer), builder.table(writer, 0))
    (root / tail_key(0, writer)).unlink()
    assert snapshot.chart("value", "step").last.tolist() == [1]
    write_block(root, block_key(1, 0, 0, writer), builder.table(writer, 0))
    builder = Builder(["step"])
    builder.add(2000, {"step": 2}, {"value": 2})
    write_block(root, block_key(1, 1, 1, writer), builder.table(writer, 1))
    storage = DirectoryStorage(tmp_path)
    keys = storage.keys
    stale = True

    def listing(prefix: str) -> list[str]:
        nonlocal stale
        result = keys(prefix)
        if stale:
            stale = False
            return [key for key in result if "/metrics/1-00000000-" not in key]
        return result

    monkeypatch.setattr(storage, "keys", listing)
    assert read(storage, "e", "r").chart("value", "step").last.tolist() == [1, 2]


def test_range_read_refuses_a_damaged_permanent_array(tmp_path: Path) -> None:
    writer = "abcdef123456"
    prefix = run_prefix("r")
    builder = Builder(["step"])
    for row in range(256):
        builder.add(row * 1000, {"step": float(row)}, {"value": row})
    key = block_key(2, 0, 0, writer)
    write_block(tmp_path / prefix, key, builder.table(writer, 0))
    data = read(tmp_path, "e", "r")
    block = data.blocks[0]
    span = block.header.rollups[0].series[0].data
    path = tmp_path / prefix / key
    body = bytearray(path.read_bytes())
    body[block.description.start + span.offset] ^= 1
    path.write_bytes(body)
    with pytest.raises(EtudeError, match="block_array_damaged"):
        data.chart("value", "step", 4)


class AppendOnlyDirectory(DirectoryStorage):
    def delete(self, key: str) -> bool:
        return False


def test_s3_reads_use_one_request_and_normalize_missing_objects() -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="etude-reads")
        storage = S3Storage("etude-reads", client=client)
        storage.put("block", b"abcdef")
        requests = 0

        def count_get(**fields: object) -> None:
            nonlocal requests
            requests += 1

        client.meta.events.register("after-call.s3.GetObject", count_get)
        assert storage.get("block") == b"abcdef"
        assert requests == 1
        assert storage.get_range("block", 2, 3) == b"cde"
        assert requests == 2
        assert storage.get_range("block", 0, 0) == b""
        assert requests == 2
        with pytest.raises(FileNotFoundError):
            storage.get("missing")
        with pytest.raises(FileNotFoundError):
            storage.get_range("missing", 0, 1)
        assert requests == 4


def test_destination_without_delete_keeps_covered_blocks_readable(tmp_path: Path) -> None:
    bucket = AppendOnlyDirectory(tmp_path / "bucket")
    run = track(
        experiment="e",
        run="r",
        clocks=[Step("step", "steps")],
        directory=tmp_path / "local",
        storages=[bucket],
        segment_seconds=0.01,
    )
    run.log({"value": 1}, step=1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and read(bucket, "e", "r").rows != 1:
        time.sleep(0.01)
    assert read(bucket, "e", "r").rows == 1
    run.finish()
    keys = bucket.keys(run_prefix("r"))
    assert any("/metrics/1-" in key for key in keys)
    assert any("/metrics/2-" in key for key in keys)
    assert read(bucket, "e", "r").chart("value", "step").count.sum() == 1
    assert run.problems == {}


def test_size_threshold_promotes_before_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.train.run.PERMANENT_BYTES", 1)
    run = track(
        experiment="e",
        run="r",
        clocks=[Step("step", "steps")],
        directory=tmp_path,
        segment_seconds=0.01,
    )
    run.log({"value": 1}, step=1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not list((run.files.root / "metrics").glob("2-*.blk")):
        time.sleep(0.01)
    assert list((run.files.root / "metrics").glob("2-*.blk"))
    run.finish()
    assert read(tmp_path, "e", "r").rows == 1


def test_attempt_resume_cuts_metrics_even_when_the_new_attempt_omits_them(tmp_path: Path) -> None:
    prefix = run_prefix("r")
    builder = Builder(["step"])
    for row in range(20):
        builder.add(row * 1000, {"step": float(row)}, {"value": row})
    write_block(
        tmp_path / prefix, block_key(2, 0, 0, "abcdef123456"), builder.table("abcdef123456", 0)
    )
    builder = Builder(["step"])
    builder.add(30000, {"step": 10}, {"other": 1})
    builder.add(31000, {"step": 11}, {"value": 100})
    write_block(
        tmp_path / prefix, block_key(2, 0, 0, "abcdef123457"), builder.table("abcdef123457", 0)
    )
    data = read(tmp_path, "e", "r")
    # The first attempt counts up to and including where the second began.
    assert data.points("value", "step").x.tolist() == [*range(11), 11]
    assert data.chart("value", "step").last[-1] == 100


@pytest.mark.parametrize("backend", ["directory", "s3"])
@pytest.mark.parametrize("logging", ["metric", "other", "none"])
def test_checkpoint_resume_supersedes_before_first_logged_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, logging: str
) -> None:
    first = track(experiment="e", run="r", clocks=[Step("step", "steps")], directory=tmp_path)
    for step in range(20):
        first.log({"value": step}, step=step)
        if step == 10:
            (tmp_path / "model.pt").write_bytes(b"weights at 10")
            first.checkpoint(tmp_path / "model.pt", step=10)
    first.finish()
    evidence = {
        path: path.read_bytes()
        for folder in ("attempts", "blocks")
        for path in (first.files.root / folder).glob("*")
    }

    # The next attempt resumes from the run's latest checkpoint, which it finds recorded.
    monkeypatch.setenv("ETUDE_ATTEMPT", "attempt-resumed")
    second = track(experiment="e", run="r", clocks=[Step("step", "steps")], directory=tmp_path)
    if logging != "none":
        second.log({"other": 1}, step=12)
    if logging == "metric":
        second.log({"value": 100}, step=13)
    second.finish()
    assert all(path.read_bytes() == body for path, body in evidence.items())

    with mock_aws():
        storage: Storage = DirectoryStorage(tmp_path)
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="etude-resume")
            storage = S3Storage("etude-resume", client=client)
            assert sync(tmp_path, storages=[storage]) == {}
        data = read(storage, "e", "r")
        assert data.attempts[-1].resumed_at == {"step": 10}
        assert data.attempts[-1].attempt == "attempt-resumed"
        # Step 10 is the checkpoint the second attempt resumed from: still the first's.
        expected_x = [*range(11), 13] if logging == "metric" else list(range(11))
        expected_y = [*range(11), 100] if logging == "metric" else list(range(11))
        assert data.points("value", "step").x.tolist() == expected_x
        assert data.points("value", "step").y.tolist() == expected_y
        chart = data.chart("value", "step")
        assert chart.x_first.tolist() == expected_x
        assert chart.last.tolist() == expected_y
        zoom = data.chart("value", "step", span=(10, 19))
        assert zoom.count.sum() == (2 if logging == "metric" else 1)
        assert sum(table.rows for table in data.tables) == 20 + (
            2 if logging == "metric" else 1 if logging == "other" else 0
        )


@pytest.mark.parametrize("resume", [5, 15])
def test_multiple_attempts_apply_the_earliest_later_checkpoint(tmp_path: Path, resume: int) -> None:
    root = tmp_path / run_prefix("r")
    storage = DirectoryStorage(root)
    for index, (start, steps) in enumerate(
        ((None, range(20)), (10, range(12, 20)), (resume, range(17, 18)))
    ):
        writer = f"{index:012x}"
        attempt = Attempt(
            writer=writer,
            started_ms=index * 1000,
            host="fixture",
            resumed_at={} if start is None else {"step": float(start)},
        )
        storage.put(f"metrics/{writer}.json", attempt.model_dump_json().encode())
        builder = Builder(["step"])
        for step in steps:
            builder.add(index * 100000 + step, {"step": float(step)}, {"value": index * 100 + step})
        write_block(root, block_key(2, 0, 0, writer), builder.table(writer, 0))

    data = read(tmp_path, "e", "r")
    expected_x = [*range(6), 17] if resume == 5 else [*range(11), 12, 13, 14, 15, 17]
    expected_y = [*range(6), 217] if resume == 5 else [*range(11), 112, 113, 114, 115, 217]
    assert data.points("value", "step").x.tolist() == expected_x
    assert data.points("value", "step").y.tolist() == expected_y
    assert data.chart("value", "step").last.tolist() == expected_y
    assert data.rows == 29  # Superseded rows remain available for inspection.


def test_manifest_latest_and_overview_retain_nonfinite_numbers(tmp_path: Path) -> None:
    prefix = run_prefix("r")
    writer = "abcdef123456"
    builder = Builder(["step"])
    builder.add(1000, {"step": 1}, {"value": float("inf")})
    write_block(tmp_path / prefix, block_key(2, 0, 0, writer), builder.table(writer, 0))
    write_manifest(tmp_path / prefix, writer)
    manifest = Manifest.model_validate_json(
        (tmp_path / prefix / f"metrics/{writer}.index.json").read_bytes()
    )
    assert manifest.latest["value"] == "Infinity"
    chart = read(tmp_path, "e", "r").chart("value", "step")
    assert chart.first[0] == float("inf")
    assert chart.nonfinite.sum() == 1


def test_sparse_metrics_have_row_order_levels_and_exact_chart_reads(tmp_path: Path) -> None:
    builder = Builder(["step"])
    for row in range(512):
        values = {"sparse": float(row)} if row % 7 == 0 else {"dense": float(row)}
        builder.add(row * 1000, {"step": float(row)}, values)
    writer = "abcdef123456"
    write_block(tmp_path / run_prefix("r"), block_key(2, 0, 0, writer), builder.table(writer, 0))
    data = read(tmp_path, "e", "r")
    reduced = data.tables[0].reductions[16].series["sparse"]
    assert reduced[:, 6].sum() == len(range(0, 512, 7))
    assert data.chart("sparse", "step").last.tolist() == list(range(0, 512, 7))


@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize("rows", [1, 8192])
def test_chart_uses_shared_span_coordinates(tmp_path: Path, manifest: bool, rows: int) -> None:
    """A shorter seed occupies its part of a cohort's shared chart, even with roll-ups."""
    writer = "abcdef123456"
    root = tmp_path / run_prefix("r")
    builder = Builder(["step"])
    for row in range(rows):
        builder.add(row * 1000, {"step": float(row)}, {"value": float(row)})
    write_block(root, block_key(2, 0, 0, writer), builder.table(writer, 0))
    if manifest:
        write_manifest(root, writer)
    data = read(tmp_path, "e", "r")
    span = (0.0, float(2 * rows - 1))
    chart = data.chart("value", "step", 16, span=span)
    assert chart.index[0] == 0
    assert chart.index[-1] == (7 if rows > 1 else 0)
    assert chart.count.sum() == rows
    assert chart.first[0] == 0 and chart.last[-1] == rows - 1
    # A zero-width zoom of a singleton is still one bucket with its original value.
    singleton = data.chart("value", "step", 16, span=(0, 0))
    assert singleton.index.tolist() == [0]
    assert singleton.count.tolist() == [1]
    assert singleton.last.tolist() == [0]


def test_a_resumed_run_reads_roll_ups_on_both_sides_of_the_resume_point(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    storage = DirectoryStorage(root)
    # The first attempt reached step 40,000; the second resumed from the
    # checkpoint at 30,000, so its first row is step 30,001.
    for index, (resumed, steps) in enumerate(((None, range(40000)), (30000, range(30001, 70001)))):
        writer = f"{index:012x}"
        attempt = Attempt(
            writer=writer,
            started_ms=index * 10**9,
            host="fixture",
            resumed_at={} if resumed is None else {"step": float(resumed)},
        )
        storage.put(f"metrics/{writer}.json", attempt.model_dump_json().encode())
        for sequence, start in enumerate(range(steps.start, steps.stop, 10000)):
            builder = Builder(["step"])
            for step in range(start, start + 10000):
                builder.add(
                    index * 10**9 + step,
                    {"step": float(step)},
                    {"value": float(index * 10**6 + step)},
                )
            write_block(
                root, block_key(2, sequence, sequence, writer), builder.table(writer, sequence)
            )
        write_manifest(root, writer)

    measured = MeasuredStorage(DirectoryStorage(tmp_path))
    data = read(measured, "e", "r")
    before = measured.bytes
    chart = data.chart("value", "step", limit=100)
    fetched = measured.bytes - before
    exact = buckets(data.points("value", "step"), 100)
    # One point per step: the first attempt up to the resume point, then the second.
    assert int(chart.count.sum()) == int(exact.count.sum()) == 70001
    assert chart.first[0] == 0 and chart.last[-1] == 10**6 + 70000
    assert chart.max.max() == exact.max.max() and chart.min.min() == exact.min.min()
    # Reading two raw columns for every row would be 80,000 rows of 16 bytes;
    # the 16-row level is a quarter of that.
    assert fetched < 80000 * 16 / 2


def test_the_manifest_rewritten_at_each_fold_does_not_grow_with_the_run(tmp_path: Path) -> None:
    writer = "abcdef123456"
    root = tmp_path / run_prefix("r")

    def block(level: int, sequence: int, rows: int) -> None:
        builder = Builder(["step"])
        for row in range(rows):
            builder.add(row, {"step": float(sequence * 10**6 + row)}, {"value": float(row)})
        write_block(
            root, block_key(level, sequence, sequence, writer), builder.table(writer, sequence)
        )

    sizes: list[int] = []
    for sequence in range(4):
        block(2, sequence * 2, 20000)
        write_manifest(root, writer, permanent=True)
        block(1, sequence * 2 + 1, 200)
        write_manifest(root, writer, permanent=False)
        sizes.append((root / f"metrics/{writer}.index.json").stat().st_size)
        (root / block_key(1, sequence * 2 + 1, sequence * 2 + 1, writer)).unlink()
        block(2, sequence * 2 + 1, 200)
    # Four times the permanent blocks, the same recent one: the same small file.
    assert max(sizes) - min(sizes) < 200
    write_manifest(root, writer, permanent=True)
    data = read(tmp_path, "e", "r")
    assert data.rows == 4 * 20200 and int(data.chart("value", "step").count.sum()) == 4 * 20200


def test_a_run_split_across_machine_folders_reads_as_one(tmp_path: Path) -> None:
    """The launcher keeps each machine's files under its own folder in the bucket."""
    for machine, writer, steps, start in (
        ("s1", "aaaaaaaaaaaa", range(15), 0),
        ("s2", "bbbbbbbbbbbb", range(11, 20), 1),
    ):
        root = tmp_path / f"pool/{machine}/out/track" / run_prefix("r")
        storage = DirectoryStorage(root)
        resumed = {} if machine == "s1" else {"step": 10.0}
        attempt = Attempt(writer=writer, started_ms=start, host=machine, resumed_at=resumed)
        storage.put(f"metrics/{writer}.json", attempt.model_dump_json().encode())
        declared = RunFile(
            experiment="e",
            namespace="local",
            declaration=RunDeclaration(
                source_key=SourceKey(namespace="local", kind="run", source_id="r"),
                clocks=(Step("step", "steps"),),
            ),
        )
        storage.put("run.json", declared.model_dump_json().encode())
        builder = Builder(["step"])
        for step in steps:
            builder.add(
                start * 10**6 + step, {"step": float(step)}, {"value": float(step + start * 100)}
            )
        write_block(root, block_key(2, 0, 0, writer), builder.table(writer, 0))
        write_manifest(root, writer)
    assert runs(tmp_path) == [("e", "r")]
    (data,) = read_all(tmp_path)
    assert [attempt.host for attempt in data.attempts] == ["s1", "s2"]
    points = data.points("value", "step")
    # Up to the resume point from the first machine, after it from the second.
    assert points.x.tolist() == list(range(20))
    assert points.y.tolist() == [*range(11), *(step + 100 for step in range(11, 20))]
    assert read(tmp_path, "e", "r").rows == 24


def test_watched_reports_read_each_bucket_file_once_per_computer(tmp_path: Path) -> None:
    local = tmp_path / "local"
    with track(experiment="e", run="r", clocks=[Step("step", "steps")], directory=local) as run:
        for step in range(5000):
            run.log({"loss": 1 / (step + 1), "reward": float(step)}, step=step)
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="etude-watched")
        assert sync(local, storages=[S3Storage("etude-watched", client=client)]) == {}
        written = client.stored("etude-watched")
        # `etude reports write --watch` and `etude reports serve` regenerate every few seconds.
        first = pages(S3Storage("etude-watched", client=client))
        assert 0 < client.bytes <= written
        read_once = client.bytes
        for _ in range(5):
            assert pages(S3Storage("etude-watched", client=client)) == first
        assert client.bytes == read_once
