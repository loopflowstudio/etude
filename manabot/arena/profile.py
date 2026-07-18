"""Arena cost summaries with explicit host and memory identities."""

from __future__ import annotations

import platform
import resource
from typing import Any

import psutil


def summarize_profiles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples: dict[str, list[float]] = {}
    decisions: dict[str, int] = {}
    for row in rows:
        for player_id, block in row.get("latency", {}).items():
            if block["p50"] is not None:
                samples.setdefault(player_id, []).append(float(block["p50"]))
                decisions[player_id] = decisions.get(player_id, 0) + int(block["count"])
    process = psutil.Process()
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return {
        "schema_version": 1,
        "method": "native-gameplay-summary-v1",
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": psutil.cpu_count(logical=True),
        },
        "rss": {
            "current_bytes": int(process.memory_info().rss),
            "ru_maxrss_native_units": peak,
            "sampler_interval_ms": 5,
            "limitations": "shared pages may be counted; short spikes may be missed",
        },
        "players": {
            player_id: {
                "decisions": decisions[player_id],
                "cell_p50_samples": values,
                "p50_seconds": sorted(values)[len(values) // 2],
                "decisions_per_second": decisions[player_id] / sum(values)
                if sum(values)
                else None,
            }
            for player_id, values in samples.items()
        },
    }
