from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "bench_branching", ROOT / "scripts/bench_branching.py"
)
assert SPEC is not None and SPEC.loader is not None
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def worker(index: int, checksum: str) -> dict:
    return {
        "seed_path": {"worker_index": index, "worker_seed": 100 + index},
        "metrics": {
            "result_checksum": checksum,
            "elapsed_seconds": 999.0,
        },
        "rss_peak_bytes": 123,
    }


def test_repeat_checksum_is_ordered_but_excludes_timing_and_rss() -> None:
    first = [worker(1, "b"), worker(0, "a")]
    second = [worker(0, "a"), worker(1, "b")]
    second[0]["metrics"]["elapsed_seconds"] = 0.001
    second[0]["rss_peak_bytes"] = 999_999
    assert bench.deterministic_repeat_checksum("cell", 7, first) == (
        bench.deterministic_repeat_checksum("cell", 7, second)
    )


def test_repeat_checksum_changes_for_logical_outcome() -> None:
    first = [worker(0, "a")]
    second = [worker(0, "different")]
    assert bench.deterministic_repeat_checksum("cell", 7, first) != (
        bench.deterministic_repeat_checksum("cell", 7, second)
    )


def test_artifact_hash_omits_only_its_own_field() -> None:
    payload = {"schema": "x", "value": 1, "artifact_sha256": "old"}
    original = bench.artifact_hash(payload)
    payload["artifact_sha256"] = "new"
    assert bench.artifact_hash(payload) == original
    payload["value"] = 2
    assert bench.artifact_hash(payload) != original


def test_percentile_uses_nearest_rank() -> None:
    assert bench.percentile([4, 1, 3, 2], 0.50) == 2
    assert bench.percentile([4, 1, 3, 2], 0.99) == 4


def test_expected_worker_simulations_is_a_times_l_times_r() -> None:
    dimensions = {
        "actors_per_worker": 8,
        "worlds": 16,
        "rollouts_per_world": 2,
    }
    assert bench.expected_worker_simulations(dimensions, action_count=6) == 1_536
