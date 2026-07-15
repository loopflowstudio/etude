#!/usr/bin/env python3
"""Run and verify the search-branching contract-v1 benchmark.

Invoke only through uv, for example:

    uv run scripts/bench_branching.py run
    uv run scripts/bench_branching.py verify
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "docs/benchmarks/search-branching-contract-v1.md"
DEFAULT_RAW = ROOT / "experiments/data/w2-182-search-branching-v1.json"
DEFAULT_REPORT = ROOT / "experiments/w2-182-search-branching-v1.md"
BINARY = ROOT / "managym/target/release/branching_bench"
SCHEMA = "manabot.search-branching.result.v1"
WHOLE_CELLS = {
    "flat-single-64-v1",
    "flat-saturated-64-v1",
    "retained-single-8-v1",
    "retained-saturated-16-v1",
}
ALL_CELLS = {"step-v1", "clone-v1", *WHOLE_CELLS}
POLL_SECONDS = 0.005
READY_TIMEOUT_SECONDS = 120.0
WORKER_TIMEOUT_SECONDS = 300.0
TERMINATE_GRACE_SECONDS = 2.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
DRIVER_COUNTER_FIELDS = {
    "allocation_count",
    "allocation_bytes",
    "journal_bytes",
    "cow_bytes",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hash(payload: dict[str, Any]) -> str:
    unhashed = dict(payload)
    unhashed.pop("artifact_sha256", None)
    return hashlib.sha256(canonical_json(unhashed)).hexdigest()


def deterministic_repeat_checksum(
    cell_id: str, root_seed: int, workers: list[dict[str, Any]]
) -> str:
    """Hash only logical workload identity and ordered native checksums."""
    logical = {
        "cell_id": cell_id,
        "root_seed": root_seed,
        "workers": [
            {
                "worker_index": worker["seed_path"]["worker_index"],
                "worker_seed": worker["seed_path"]["worker_seed"],
                "result_checksum": worker["metrics"]["result_checksum"],
            }
            for worker in sorted(
                workers, key=lambda value: value["seed_path"]["worker_index"]
            )
        ],
    }
    return hashlib.sha256(canonical_json(logical)).hexdigest()


def expected_worker_simulations(dimensions: dict[str, Any], action_count: int) -> int:
    """Contract-v1 W * A * L * R count for one worker and root seed."""
    return (
        dimensions["actors_per_worker"]
        * action_count
        * dimensions["worlds"]
        * dimensions["rollouts_per_world"]
    )


def verify_worker_contract_shape(
    cell: dict[str, Any], group: dict[str, Any], driver: str
) -> None:
    """Reject results that improved a driver by changing its logical work."""
    dimensions = cell["dimensions"]
    workers = group["workers"]
    expected_indices = set(range(dimensions["workers"]))
    actual_indices = {worker["seed_path"]["worker_index"] for worker in workers}
    if actual_indices != expected_indices:
        raise RuntimeError(f"worker topology mismatch for {cell['id']}")

    for worker in workers:
        if worker["driver"] != driver:
            raise RuntimeError(f"driver mismatch for {cell['id']}")
        if worker["workload_id"] != cell["id"]:
            raise RuntimeError(f"workload identity mismatch for {cell['id']}")
        if worker["shape"] != dimensions["shape"]:
            raise RuntimeError(f"workload shape mismatch for {cell['id']}")
        if worker["root_seed"] != group["root_seed"]:
            raise RuntimeError(f"root seed mismatch for {cell['id']}")
        if worker["fixture"]["id"] != dimensions["fixture"]:
            raise RuntimeError(f"fixture mismatch for {cell['id']}")

        metrics = worker["metrics"]
        missing_counters = DRIVER_COUNTER_FIELDS - metrics.keys()
        if missing_counters:
            raise RuntimeError(
                f"missing driver counters for {cell['id']}: {sorted(missing_counters)}"
            )
        if (
            all(metrics[field] is None for field in DRIVER_COUNTER_FIELDS)
            and not metrics["unsupported_counters_reason"]
        ):
            raise RuntimeError(f"unsupported counters need a reason for {cell['id']}")

        if cell["id"] in WHOLE_CELLS:
            expected = expected_worker_simulations(
                dimensions, worker["fixture"]["action_count"]
            )
            if metrics["simulations"] != expected:
                raise RuntimeError(
                    f"W*A*L*R simulation mismatch for {cell['id']}: "
                    f"{metrics['simulations']} != {expected}"
                )
        elif dimensions["shape"] == "step":
            if metrics["transitions"] != dimensions["measured_count"]:
                raise RuntimeError(f"step count mismatch for {cell['id']}")
        elif dimensions["shape"] == "clone_latency":
            if len(metrics["clone_latency_ns"]) != dimensions["measured_count"]:
                raise RuntimeError(f"clone sample count mismatch for {cell['id']}")


def percentile(values: list[float | int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def command_output(command: list[str], *, required: bool = True) -> str | None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if required:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}"
        )
    return None


def source_sha256() -> str:
    excluded_parts = {
        ".git",
        ".lf",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "htmlcov",
        "target",
        "node_modules",
        "scratch",
    }
    excluded_files = {DEFAULT_RAW.resolve(), DEFAULT_REPORT.resolve()}
    digest = hashlib.sha256()
    files: list[Path] = []
    for directory, names, filenames in os.walk(ROOT):
        names[:] = sorted(name for name in names if name not in excluded_parts)
        base = Path(directory)
        files.extend(
            base / filename
            for filename in sorted(filenames)
            if filename != ".DS_Store"
            and Path(filename).suffix not in {".dylib", ".pyc", ".so"}
        )
    for path in sorted(files):
        if path.resolve() not in excluded_files:
            relative = path.relative_to(ROOT).as_posix()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def build_binary() -> dict[str, Any]:
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            "managym/Cargo.toml",
            "--bin",
            "branching_bench",
        ],
        cwd=ROOT,
        check=True,
    )
    return {
        "profile": "release",
        "allocator": "system",
        "threads_per_worker": 1,
        "rustc": command_output(["rustc", "--version"]),
        "cargo": command_output(["cargo", "--version"]),
        "uv": command_output(["uv", "--version"]),
        "python": command_output(["uv", "run", "python", "--version"]),
        "binary": str(BINARY.relative_to(ROOT)),
    }


def native_json(arguments: list[str]) -> dict[str, Any]:
    output = command_output([str(BINARY), *arguments])
    assert output is not None
    return json.loads(output)


def hardware_metadata(oversubscribed: bool) -> dict[str, Any]:
    cpu_model = command_output(
        ["sysctl", "-n", "machdep.cpu.brand_string"], required=False
    )
    if not cpu_model:
        cpu_model = platform.processor() or None
    power = command_output(["pmset", "-g", "batt"], required=False)
    thermal = command_output(["pmset", "-g", "therm"], required=False)
    return {
        "os": platform.platform(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": cpu_model,
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "total_memory_bytes": psutil.virtual_memory().total,
        "power_mode": power,
        "power_mode_unavailable_reason": None if power else "pmset unavailable",
        "thermal_state": thermal,
        "thermal_state_unavailable_reason": None if thermal else "pmset unavailable",
        "oversubscribed": oversubscribed,
        "rss_method": "sum(psutil.Process(worker_pid).memory_info().rss)",
        "rss_poll_interval_seconds": POLL_SECONDS,
        "rss_shared_page_note": "Summed process RSS may double-count shared pages; 5 ms sampling may miss shorter spikes.",
    }


def smoke_spec(spec: dict[str, Any]) -> dict[str, Any]:
    value = dict(spec)
    if value["shape"] == "step":
        value.update(warmup_count=20, measured_count=100)
    elif value["shape"] == "clone_latency":
        value.update(warmup_count=10, measured_count=100)
    elif value["shape"] == "sequential":
        value.update(workers=min(value["workers"], 2), worlds=1, rollouts_per_world=1)
    else:
        value.update(actors_per_worker=min(value["actors_per_worker"], 2), worlds=1)
    return value


def read_worker_result(process: subprocess.Popen[str]) -> tuple[str, str]:
    assert process.stdout is not None
    assert process.stderr is not None
    result_line = process.stdout.readline()
    remainder = process.stdout.read()
    error = process.stderr.read()
    return result_line + remainder, error


def process_rss(processes: list[subprocess.Popen[str]]) -> int:
    total = 0
    for process in processes:
        try:
            total += psutil.Process(process.pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


def run_group(spec: dict[str, Any], root_seed: int, max_steps: int) -> dict[str, Any]:
    processes: list[subprocess.Popen[str]] = []
    commands: list[list[str]] = []
    for worker in range(spec["workers"]):
        request = {
            "schema_version": 1,
            "driver": "full_clone/current_game_v1",
            "workload": spec,
            "root_seed": root_seed,
            "worker": worker,
            "max_steps": max_steps,
        }
        command = [
            str(BINARY),
            "--request-json",
            json.dumps(request, separators=(",", ":")),
        ]
        commands.append(command)
        processes.append(
            subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        )

    ready: list[dict[str, Any]] = []
    for process in processes:
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            error = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"worker exited before ready: {error}")
        record = json.loads(line)
        if record.get("type") != "ready" or record.get("pid") != process.pid:
            raise RuntimeError(f"malformed ready record: {record}")
        ready.append(record)

    rss_baseline = process_rss(processes)
    parent_rss = psutil.Process().memory_info().rss
    rss_peak = rss_baseline
    rss_samples = [{"offset_seconds": 0.0, "rss_bytes": rss_baseline}]
    with ThreadPoolExecutor(max_workers=len(processes)) as executor:
        readers = [
            executor.submit(read_worker_result, process) for process in processes
        ]
        barrier_started = time.perf_counter()
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("x")
            process.stdin.flush()
            process.stdin.close()
        while any(process.poll() is None for process in processes):
            sampled_rss = process_rss(processes)
            rss_peak = max(rss_peak, sampled_rss)
            rss_samples.append(
                {
                    "offset_seconds": time.perf_counter() - barrier_started,
                    "rss_bytes": sampled_rss,
                }
            )
            time.sleep(POLL_SECONDS)
        sampled_rss = process_rss(processes)
        rss_peak = max(rss_peak, sampled_rss)
        rss_samples.append(
            {
                "offset_seconds": time.perf_counter() - barrier_started,
                "rss_bytes": sampled_rss,
            }
        )
        barrier_wall = time.perf_counter() - barrier_started
        outputs = [reader.result() for reader in readers]

    workers: list[dict[str, Any]] = []
    for process, (stdout, stderr) in zip(processes, outputs, strict=True):
        if process.returncode != 0:
            raise RuntimeError(
                f"worker {process.pid} failed ({process.returncode}): {stderr}"
            )
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise RuntimeError(
                f"worker {process.pid} returned {len(lines)} result lines"
            )
        workers.append(json.loads(lines[0]))

    workers.sort(key=lambda value: value["seed_path"]["worker_index"])
    return {
        "root_seed": root_seed,
        "ready": ready,
        "commands": commands,
        "workers": workers,
        "barrier_wall_seconds": barrier_wall,
        "rss_baseline_bytes": rss_baseline,
        "rss_peak_bytes": rss_peak,
        "rss_peak_delta_bytes": max(0, rss_peak - rss_baseline),
        "rss_samples": rss_samples,
        "parent_rss_bytes": parent_rss,
        "result_checksum": deterministic_repeat_checksum(
            spec["id"], root_seed, workers
        ),
    }


def summarize_cell(cell: dict[str, Any]) -> dict[str, Any]:
    repeats = cell["repeats"]
    metrics = [worker["metrics"] for repeat in repeats for worker in repeat["workers"]]
    simulations = sum(metric["simulations"] for metric in metrics)
    transitions = sum(metric["transitions"] for metric in metrics)
    barrier_seconds = sum(repeat["barrier_wall_seconds"] for repeat in repeats)
    root_latencies = [
        max(worker["metrics"]["elapsed_seconds"] for worker in repeat["workers"])
        for repeat in repeats
    ]
    caps = sum(metric["cap_hits"] for metric in metrics)
    return {
        "simulations": simulations,
        "transitions": transitions,
        "barrier_wall_seconds": barrier_seconds,
        "simulations_per_second": simulations / barrier_seconds
        if barrier_seconds
        else 0.0,
        "transitions_per_second": transitions / barrier_seconds
        if barrier_seconds
        else 0.0,
        "root_latency_seconds_p50": percentile(root_latencies, 0.50),
        "root_latency_seconds_p95": percentile(root_latencies, 0.95),
        "root_latency_seconds_p99": percentile(root_latencies, 0.99),
        "rss_peak_bytes": max(repeat["rss_peak_bytes"] for repeat in repeats),
        "rss_peak_delta_bytes": max(
            repeat["rss_peak_delta_bytes"] for repeat in repeats
        ),
        "max_live_states": max(metric["max_live_states"] for metric in metrics),
        "cap_rate": caps / simulations if simulations else 0.0,
        "repeat_checksums": [repeat["result_checksum"] for repeat in repeats],
    }


def summarize_diagnostic(cell: dict[str, Any]) -> dict[str, Any]:
    metrics = cell["repeats"][0]["workers"][0]["metrics"]
    latencies = (
        metrics["step_latency_ns"]
        if cell["id"] == "step-v1"
        else metrics["clone_latency_ns"]
    )
    count = len(latencies)
    elapsed = metrics["elapsed_seconds"]
    return {
        "samples": count,
        "samples_per_second": count / elapsed if elapsed else 0.0,
        "latency_ns_p50": percentile(latencies, 0.50),
        "latency_ns_p95": percentile(latencies, 0.95),
        "latency_ns_p99": percentile(latencies, 0.99),
        "resets": metrics["resets"],
        "reset_seconds": metrics["reset_seconds"],
        "allocation_count": metrics["allocation_count"],
        "allocation_bytes": metrics["allocation_bytes"],
        "result_checksum": metrics["result_checksum"],
    }


def run_equivalence(manifest: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for fixture in manifest["fixtures"]:
        for seed in manifest["equivalence_seeds"]:
            arguments = [
                "--equivalence",
                fixture,
                "--seed",
                str(seed),
                "--max-steps",
                str(manifest["max_steps"]),
            ]
            first = native_json(arguments)
            second = native_json(arguments)
            first["repeat_match"] = (
                first["replay_checksum"] == second["replay_checksum"]
                and first["replay_final_hash"] == second["replay_final_hash"]
            )
            checks.append(first)
    return {
        "seeds": manifest["equivalence_seeds"],
        "checks": checks,
        "passed": all(check["passed"] and check["repeat_match"] for check in checks),
    }


def run_benchmark(profile: str, oversubscribed: bool) -> dict[str, Any]:
    started_at = datetime.now().astimezone().isoformat()
    build = build_binary()
    manifest = native_json(["--manifest"])
    if manifest["contract_id"] != "manabot.search-branching.v1":
        raise RuntimeError("native manifest does not implement contract v1")
    physical_cores = psutil.cpu_count(logical=False)
    if profile == "full" and (physical_cores or 0) < 8 and not oversubscribed:
        raise RuntimeError(
            "canonical saturated cell requires >=8 physical cores; pass --oversubscribed for a labeled noncanonical run"
        )

    fixtures = [native_json(["--fixture", fixture]) for fixture in manifest["fixtures"]]
    equivalence = run_equivalence(manifest)
    if not equivalence["passed"]:
        raise RuntimeError("deterministic equivalence gate failed")

    measured_seeds = (
        manifest["measured_seeds"]
        if profile == "full"
        else [manifest["measured_seeds"][0]]
    )
    cells: list[dict[str, Any]] = []
    for original_spec in manifest["workloads"]:
        spec = original_spec if profile == "full" else smoke_spec(original_spec)
        seeds = measured_seeds if spec["id"] in WHOLE_CELLS else [measured_seeds[0]]
        cell = {
            "id": spec["id"],
            "dimensions": spec,
            "warmup": {
                "count": spec["warmup_count"],
                "seed": manifest["warmup_seed"] if spec["id"] in WHOLE_CELLS else None,
                "included_in_measurement": False,
            },
            "repeats": [run_group(spec, seed, manifest["max_steps"]) for seed in seeds],
        }
        if cell["id"] in WHOLE_CELLS:
            replay = run_group(spec, seeds[0], manifest["max_steps"])
            replay["matches_first_repeat"] = (
                replay["result_checksum"] == cell["repeats"][0]["result_checksum"]
            )
            if not replay["matches_first_repeat"]:
                raise RuntimeError(
                    f"deterministic checksum replay failed for {cell['id']}"
                )
            cell["determinism_replay"] = replay
        cell["summary"] = (
            summarize_cell(cell)
            if cell["id"] in WHOLE_CELLS
            else summarize_diagnostic(cell)
        )
        cells.append(cell)

    task_status = command_output(
        ["lf", "task", "status", "W2-182", "--json"], required=False
    )
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "contract": {
            "id": manifest["contract_id"],
            "sha256": sha256_file(CONTRACT_PATH),
        },
        "run": {
            "started_at": started_at,
            "timezone": str(datetime.now().astimezone().tzinfo),
            "argv": [
                "uv",
                "run",
                "scripts/bench_branching.py",
                "run",
                "--profile",
                profile,
            ],
            "source_sha256": source_sha256(),
            "driver": manifest["driver"],
            "profile": profile,
            "canonical": profile == "full" and not oversubscribed,
            "status": "complete",
            "loopflow": {
                "task": "W2-182",
                "task_session": "ts_b39e9de2861c44bc8c4234a32c2f6bcf",
                "worktree": str(ROOT),
                "branch": "jack-heart/build-whole-rollout-branching-benchmark",
                "status_snapshot": json.loads(task_status) if task_status else None,
                "status_unavailable_reason": None
                if task_status
                else "local Loopflow registry unavailable during evidence run",
            },
        },
        "hardware": hardware_metadata(oversubscribed),
        "build": build,
        "manifest": manifest,
        "fixtures": fixtures,
        "equivalence": equivalence,
        "cells": cells,
    }
    payload["artifact_sha256"] = artifact_hash(payload)
    return payload


def atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def bytes_mib(value: int | float) -> float:
    return float(value) / (1024 * 1024)


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# W2-182: whole-rollout branching baseline",
        "",
        f"Contract: `{payload['contract']['id']}` (`{payload['contract']['sha256']}`)",
        f"Driver: `{payload['run']['driver']}`",
        f"Run: `{payload['run']['started_at']}`; canonical: `{str(payload['run']['canonical']).lower()}`",
        "",
        "## Primary whole-rollout evidence",
        "",
        "| Cell | simulations/s | transitions/s | root p50 / p95 / p99 | peak RSS | peak delta | max live | cap rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cell in payload["cells"]:
        if cell["id"] not in WHOLE_CELLS:
            continue
        summary = cell["summary"]
        lines.append(
            f"| `{cell['id']}` | {summary['simulations_per_second']:.1f} | "
            f"{summary['transitions_per_second']:.1f} | "
            f"{summary['root_latency_seconds_p50']:.3f}s / {summary['root_latency_seconds_p95']:.3f}s / {summary['root_latency_seconds_p99']:.3f}s | "
            f"{bytes_mib(summary['rss_peak_bytes']):.1f} MiB | "
            f"{bytes_mib(summary['rss_peak_delta_bytes']):.1f} MiB | "
            f"{summary['max_live_states']} | {summary['cap_rate']:.3%} |"
        )
    lines.extend(
        [
            "",
            "Peak RSS is the 5 ms sampled sum across worker processes and can double-count shared pages. Clone latency is diagnostic, not a storage decision.",
            "",
            "## Step and clone diagnostics",
            "",
            "| Cell | samples/s | latency p50 / p95 / p99 | resets | reset time |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for cell in payload["cells"]:
        if cell["id"] in WHOLE_CELLS:
            continue
        summary = cell["summary"]
        lines.append(
            f"| `{cell['id']}` | {summary['samples_per_second']:.1f} | "
            f"{summary['latency_ns_p50'] / 1000:.1f}µs / {summary['latency_ns_p95'] / 1000:.1f}µs / {summary['latency_ns_p99'] / 1000:.1f}µs | "
            f"{summary['resets']} | {summary['reset_seconds']:.3f}s |"
        )
    lines.extend(
        [
            "",
            "## Reproduction and evidence",
            "",
            "```bash",
            "uv run scripts/bench_branching.py run",
            "uv run scripts/bench_branching.py verify",
            "```",
            "",
            f"Equivalence: `{str(payload['equivalence']['passed']).lower()}` across {len(payload['equivalence']['checks'])} fixture/seed checks, each replayed twice.",
            "Each primary cell also repeated its first measured root in a fresh worker group and matched the ordered deterministic result checksum.",
            f"Artifact SHA-256: `{payload['artifact_sha256']}`.",
            f"Source SHA-256: `{payload['run']['source_sha256']}`.",
            "",
            "The raw artifact contains hardware, versions, exact commands, fixture tapes and hashes, all worker records, all seeds, timings, outcomes, deterministic checksums, and RSS samples/summaries. No undo or page-COW implementation was selected or measured.",
            "",
        ]
    )
    return "\n".join(lines)


def verify(payload: dict[str, Any]) -> None:
    if payload.get("schema") != SCHEMA:
        raise RuntimeError(f"unexpected schema {payload.get('schema')}")
    if payload["contract"]["id"] != "manabot.search-branching.v1":
        raise RuntimeError("unexpected contract ID")
    if payload["contract"]["sha256"] != sha256_file(CONTRACT_PATH):
        raise RuntimeError("contract digest mismatch")
    if payload["artifact_sha256"] != artifact_hash(payload):
        raise RuntimeError("artifact digest mismatch")
    if {cell["id"] for cell in payload["cells"]} != ALL_CELLS:
        raise RuntimeError("required cell set mismatch")
    if not payload["equivalence"]["passed"]:
        raise RuntimeError("equivalence evidence failed")
    if (
        payload["run"]["canonical"]
        and payload["run"]["source_sha256"] != source_sha256()
    ):
        raise RuntimeError("source digest mismatch")
    for cell in payload["cells"]:
        expected = (
            summarize_cell(cell)
            if cell["id"] in WHOLE_CELLS
            else summarize_diagnostic(cell)
        )
        if expected != cell["summary"]:
            raise RuntimeError(f"summary mismatch for {cell['id']}")
        for repeat in cell["repeats"]:
            verify_worker_contract_shape(cell, repeat, payload["run"]["driver"])
            expected_checksum = deterministic_repeat_checksum(
                cell["id"], repeat["root_seed"], repeat["workers"]
            )
            if repeat["result_checksum"] != expected_checksum:
                raise RuntimeError(f"repeat checksum mismatch for {cell['id']}")
        if cell["id"] in WHOLE_CELLS:
            replay = cell.get("determinism_replay")
            if not replay or not replay.get("matches_first_repeat"):
                raise RuntimeError(f"missing deterministic replay for {cell['id']}")
            verify_worker_contract_shape(cell, replay, payload["run"]["driver"])
            expected_checksum = deterministic_repeat_checksum(
                cell["id"], replay["root_seed"], replay["workers"]
            )
            if replay["result_checksum"] != expected_checksum:
                raise RuntimeError(f"replay checksum mismatch for {cell['id']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", choices=("full", "smoke"), default="full")
    run_parser.add_argument("--oversubscribed", action="store_true")
    run_parser.add_argument("--output", type=Path, default=DEFAULT_RAW)
    run_parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--input", type=Path, default=DEFAULT_RAW)
    verify_parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    if args.command == "run":
        payload = run_benchmark(args.profile, args.oversubscribed)
        verify(payload)
        atomic_write(args.output, canonical_json(payload) + b"\n")
        atomic_write(args.report, render_report(payload).encode())
        print(
            json.dumps(
                {
                    "status": "complete",
                    "profile": args.profile,
                    "artifact": str(args.output),
                    "report": str(args.report),
                    "artifact_sha256": payload["artifact_sha256"],
                }
            )
        )
    else:
        payload = json.loads(args.input.read_text())
        verify(payload)
        expected_report = render_report(payload)
        if args.report.read_text() != expected_report:
            raise RuntimeError("generated report is stale")
        print(
            json.dumps(
                {"status": "verified", "artifact_sha256": payload["artifact_sha256"]}
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
