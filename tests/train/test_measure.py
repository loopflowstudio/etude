"""Measurements for docs/design/metrics-store.md. Run with ETUDE_MEASURE=1 and -s; skipped otherwise.

These print numbers; their only assertions are loose ceilings that catch a
pathological regression, not a performance contract.
"""

from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import numpy as np
import pytest
from moto import mock_aws

from etude.page import build
from etude.run.block_file import block_key
from etude.run.records import RunDeclaration, RunFile, SourceKey
from etude.run.storage import DirectoryStorage, S3Storage, Storage, run_prefix
from etude.train.block import Series, Table, encode
from etude.train.manifest import write_manifest
from etude.train.read import buckets, read
from etude.train.records import Step, Time
from etude.train.run import track
from tests.experiments import experiment_file
from tests.run.storage import MeasuredStorage

pytestmark = pytest.mark.skipif(
    os.environ.get("ETUDE_MEASURE") != "1", reason="measurement, not a check"
)

CLOCKS = (Step("updates", "optimizer updates"), Time("training_seconds", "active training"))
METRICS = 20


def test_log_call_latency(tmp_path: Path) -> None:
    run = track(
        experiment="measure", run="latency", clocks=CLOCKS, directory=tmp_path, queue_size=50_000
    )
    row = {f"rl/metric_{i}": float(i) for i in range(METRICS)}
    samples: list[int] = []
    for update in range(100_000):
        row["updates"] = update
        row["training_seconds"] = update * 0.1
        started = time.perf_counter_ns()
        run.log(row)
        samples.append(time.perf_counter_ns() - started)
        if update % 100 == 99:
            time.sleep(0.0005)  # A training loop does other work between calls.
    run.finish()
    samples.sort()
    print(
        f"\nlog() with {METRICS} metrics, 100,000 calls: "
        f"median {statistics.median(samples) / 1000:.1f} us, "
        f"p99 {samples[len(samples) * 99 // 100] / 1000:.1f} us, "
        f"max {samples[-1] / 1000:.0f} us, dropped {run.dropped}"
    )
    assert statistics.median(samples) < 1_000_000


def test_chart_read_time(tmp_path: Path) -> None:
    rows = 500_000
    run = track(
        experiment="measure", run="read", clocks=CLOCKS, directory=tmp_path, queue_size=50_000
    )
    row = {f"rl/metric_{i}": float(i) for i in range(METRICS)}
    for update in range(rows):
        row["updates"] = update
        row["training_seconds"] = update * 0.1
        run.log(row)
        if update % 1_000 == 999:
            time.sleep(0.02)  # Let the writer keep up, so nothing is dropped.
    run.finish()
    size = sum(path.stat().st_size for path in tmp_path.rglob("*.blk"))

    started = time.perf_counter()
    data = read(tmp_path, "measure", "read")
    loaded = time.perf_counter() - started
    started = time.perf_counter()
    whole = buckets(data.points("rl/metric_3", "updates"))
    charted = time.perf_counter() - started
    started = time.perf_counter()
    build(tmp_path, experiment_file(tmp_path / "experiments", "measure"))
    reported = time.perf_counter() - started
    print(
        f"\n{data.rows:,} rows of {METRICS} metrics ({size / 1e6:.0f} MB), "
        f"dropped {run.dropped}, {len(list(tmp_path.rglob('*.blk')))} block files: "
        f"load {loaded * 1000:.0f} ms, "
        f"one metric into {len(whole)} buckets {charted * 1000:.1f} ms, "
        f"whole report page {reported * 1000:.0f} ms"
    )
    assert len(whole) == 1000


@pytest.mark.parametrize("rows", [500_000, 5_000_000])
@pytest.mark.parametrize("backend", ["directory", "s3"])
def test_store_measurement(tmp_path: Path, rows: int, backend: str) -> None:
    """A reproducible 20-metric run; moto measures transport volume, not S3 latency."""
    with mock_aws():
        storage: Storage
        if backend == "s3":
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="etude-measure")
            storage = S3Storage("etude-measure", client=client)
        else:
            storage = DirectoryStorage(tmp_path)
        prefix = run_prefix("read")
        writer = "abcdef123456"
        size = 0
        started = time.perf_counter()
        files = 0
        for sequence, start in enumerate(range(0, rows, 32768)):
            x = np.arange(start, min(start + 32768, rows), dtype=np.float64)
            table = Table(
                writer=writer,
                first=sequence,
                last=sequence,
                wall_ms=(x * 100).astype("<i8"),
                clocks={"updates": x, "training_seconds": x * 0.1},
                series={
                    (f"rl/metric_{i}", "metric"): Series(rows=None, data=np.sin(x / (i + 1)))
                    for i in range(METRICS)
                },
            )
            body = encode(table)
            size += len(body)
            key = prefix + block_key(2, sequence, sequence, writer)
            storage.put(key, body)
            if backend == "s3":
                DirectoryStorage(tmp_path).put(key, body)
            files += 1
        write_manifest(tmp_path / prefix, writer)
        if backend == "s3":
            for manifest in sorted((tmp_path / prefix / "metrics").glob("*.json")):
                storage.put(prefix + f"metrics/{manifest.name}", manifest.read_bytes())
        declaration = RunFile(
            experiment="measure",
            namespace="local",
            declaration=RunDeclaration(
                source_key=SourceKey(namespace="local", kind="run", source_id="read"), clocks=CLOCKS
            ),
        )
        storage.put(prefix + "run.json", declaration.model_dump_json().encode())
        written = time.perf_counter() - started
        measured = MeasuredStorage(storage)
        started = time.perf_counter()
        data = read(measured, "measure", "read")
        loaded = time.perf_counter() - started
        metadata_bytes = measured.bytes
        metadata_requests = measured.requests
        started = time.perf_counter()
        reduced = data.chart("rl/metric_3", "updates")
        charted = time.perf_counter() - started
        chart_bytes = measured.bytes - metadata_bytes
        chart_requests = measured.requests - metadata_requests
        before_bytes, before_requests = measured.bytes, measured.requests
        started = time.perf_counter()
        zoom_span = (rows * 0.4, rows * 0.5)
        zoom = data.chart("rl/metric_3", "updates", span=zoom_span)
        zoomed = time.perf_counter() - started
        zoom_bytes, zoom_requests = (
            measured.bytes - before_bytes,
            measured.requests - before_requests,
        )
        before_bytes, before_requests = measured.bytes, measured.requests
        started = time.perf_counter()
        report = build(measured, experiment_file(tmp_path / "experiments", "measure"))
        reported = time.perf_counter() - started
        report_bytes, report_requests = (
            measured.bytes - before_bytes,
            measured.requests - before_requests,
        )
        print(
            f"\nstore {backend} {rows:,}: {size / 1e6:.2f} MB, {files} files; "
            f"write {written:.3f} s, load {loaded:.3f} s, chart {charted * 1000:.2f} ms; "
            f"metadata {metadata_bytes} bytes/{metadata_requests} GETs, "
            f"chart {chart_bytes} bytes/{chart_requests} range GETs; "
            f"zoom {zoomed * 1000:.2f} ms/{zoom_bytes} bytes/{zoom_requests} GETs; "
            f"report {reported * 1000:.2f} ms/{report_bytes} bytes/{report_requests} GETs"
        )
        assert data.rows == rows
        assert int(reduced.count.sum()) == rows
        assert len(reduced) == 1000
        if rows >= 4096 * 1000:
            assert chart_bytes == 0 and chart_requests == 0
            assert report_requests == metadata_requests
        else:
            assert chart_bytes == 2_250_000
            assert chart_requests == 2 * files
            assert report_requests == metadata_requests + 2 * files * METRICS * (len(CLOCKS) + 1)
        assert int(zoom.count.sum()) == rows // 10 + 1
        assert zoom.x_first[0] == zoom_span[0] and zoom.x_last[-1] == zoom_span[1]
        assert zoom_bytes < rows * 16 / 4
        assert report.training.runs[0].rows == rows
