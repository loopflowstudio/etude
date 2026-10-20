"""An experiment's report: one self-contained page, regenerated from its files.

A report is a file with a fixed home, `DIR/reports/EXPERIMENT.html`. Generating
it again replaces it; the page keeps the reader's clock, mode and hidden runs
in its URL, so a regenerated or reloaded page shows the same view. It is the
quickest way to see that what was logged is what was meant: every
metric against any declared clock with its min-max envelope, runs overlaid,
and jobs as mean and 95% interval over their seeds. It needs no server and no
network; the page embeds its data and uPlot, which is vendored under `vendor/`
with its MIT licence.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from etude.runs.records import WALL_CLOCK
from etude.runs.storage import Storage
from etude.train.read import Floats, RunData, read_all
from etude.train.records import interval95

BUCKETS = 240
CASE_BUCKETS = 120
WALL_MEANING = "wall time since the run's first row, seconds"


def _round(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(f"{value:.6g}")


def _rounded(values: Floats) -> list[float | None]:
    """Six significant figures for the page; NaN and infinities become gaps."""
    return [_round(value) for value in values.tolist()]


def _seed_sample(
    members: Sequence[RunData],
    metric: str,
    clock: str,
) -> dict[str, list[float | None] | list[int]] | None:
    """Mean and 95% interval of the runs' bucket means, with the run count."""
    present = [(run, run.chart(metric, clock, CASE_BUCKETS)) for run in members]
    present = [(run, chart) for run, chart in present if len(chart)]
    if len(present) < 2:
        return None
    origins = [float(chart.x_first[0]) if clock == WALL_CLOCK else 0.0 for _, chart in present]
    span = (
        min(float(chart.x_first[0]) - origin for (_, chart), origin in zip(present, origins)),
        max(float(chart.x_last[-1]) - origin for (_, chart), origin in zip(present, origins)),
    )
    width = (span[1] - span[0]) / CASE_BUCKETS
    means = np.full((len(present), CASE_BUCKETS), np.nan)
    for row, ((run, _), origin) in enumerate(zip(present, origins)):
        reduced = run.chart(metric, clock, CASE_BUCKETS, span=(span[0] + origin, span[1] + origin))
        means[row, reduced.index] = reduced.mean
    count = (~np.isnan(means)).sum(axis=0)
    filled = np.flatnonzero(count)
    xs: list[float | None] = []
    mean: list[float | None] = []
    low: list[float | None] = []
    high: list[float | None] = []
    for index in filled.tolist():
        values = means[:, index][~np.isnan(means[:, index])].tolist()
        centre = statistics.fmean(values)
        bounds = interval95(
            centre, statistics.stdev(values) if len(values) > 1 else 0.0, len(values)
        )
        xs.append(_round(span[0] + (index + 0.5) * width))
        mean.append(_round(centre))
        low.append(_round(bounds[0]) if bounds else None)
        high.append(_round(bounds[1]) if bounds else None)
    return {"x": xs, "mean": mean, "min": low, "max": high, "n": count[filled].tolist()}


def _labels(data: Sequence[RunData]) -> dict[str, str]:
    """What to call each job: its author's name when that is unambiguous, else its key."""
    names = {
        run.job: run.file.declaration.job_name or run.job
        for run in data
        if run.job is not None and run.file is not None
    }
    shared = {name for name in names.values() if list(names.values()).count(name) > 1}
    return {job: f"{name} {job[:6]}" if name in shared else name for job, name in names.items()}


def page_data(experiment: str, data: Sequence[RunData]) -> dict[str, object]:
    """Everything the page draws, already bucketed. Public so tests can inspect it."""
    clocks: dict[str, dict[str, str]] = {}
    for run in data:
        for declared in run.clocks:
            clocks.setdefault(declared.name, declared.model_dump())
    clocks[WALL_CLOCK] = {"name": WALL_CLOCK, "kind": "time", "meaning": WALL_MEANING}
    labels = _labels(data)
    metrics: dict[str, None] = {}
    for run in data:
        metrics.update(dict.fromkeys(run.metrics()))
    series: dict[str, dict[str, list[dict[str, object]]]] = {}
    jobs: dict[str, dict[str, list[dict[str, object]]]] = {}
    for clock in clocks:
        for metric in metrics:
            lines: list[dict[str, object]] = []
            members: dict[str, list[RunData]] = {}
            for index, run in enumerate(data):
                if clock != WALL_CLOCK and clock not in {c.name for c in run.clocks}:
                    continue
                reduced = run.chart(metric, clock, BUCKETS)
                if not len(reduced):
                    continue
                origin = float(reduced.x_first[0]) if clock == WALL_CLOCK else 0.0
                lines.append(
                    {
                        "run": index,
                        "x": _rounded((reduced.x_first + reduced.x_last) / 2 - origin),
                        "mean": _rounded(reduced.mean),
                        "min": _rounded(reduced.min),
                        "max": _rounded(reduced.max),
                        "nonfinite": int(reduced.nonfinite.sum()),
                    }
                )
                if run.job is not None:
                    members.setdefault(run.job, []).append(run)
            if lines:
                series.setdefault(clock, {})[metric] = lines
            for job, group in members.items():
                band = _seed_sample(group, metric, clock)
                if band is not None:
                    jobs.setdefault(clock, {}).setdefault(metric, []).append(
                        {"job": labels[job], **band}
                    )
    return {
        "experiment": experiment,
        "clocks": list(clocks.values()),
        "runs": [
            {
                "name": run.name,
                "job": labels.get(run.job) if run.job is not None else None,
                "cohort": run.file.declaration.cohort if run.file else None,
                "seed": run.file.declaration.seed if run.file else None,
                "kind": run.file.declaration.kind if run.file else None,
                "state": run.state.state if run.state else "running or unfinished",
                "rows": run.rows,
                "checkpoints": len(run.checkpoints),
                "attempts": len(run.attempts),
                "incomplete": run.incomplete,
                "dropped": run.state.counters.dropped if run.state else None,
            }
            for run in data
        ],
        "series": series,
        "jobs": jobs,
    }


def pages(
    source: Storage | Path | str, experiment: str | None = None
) -> dict[str, dict[str, object]]:
    """What each experiment's report draws, for one experiment or every one found."""
    data = read_all(source, experiment)
    names = sorted({run.experiment for run in data})
    return {
        name: page_data(name, [run for run in data if run.experiment == name]) for name in names
    }


def render(page: dict[str, object], *, refresh: float | None = None) -> str:
    """The page with its data and the vendored uPlot inlined: one file, no network.

    `refresh` is the seconds between the page reloading itself, for a report
    that is being regenerated while its runs are still writing.
    """
    here = Path(__file__).parent
    stamped = page | {"generated_ms": time.time_ns() // 1_000_000, "refresh": refresh}
    payload = json.dumps(stamped, allow_nan=False).replace("</", "<\\/")
    return (
        (here / "report.html")
        .read_text()
        .replace("__TITLE__", str(page["experiment"]).replace("<", "").replace("&", ""))
        .replace("__UPLOT_CSS__", (here / "vendor/uPlot.min.css").read_text())
        .replace("__UPLOT_JS__", (here / "vendor/uPlot.iife.min.js").read_text())
        .replace("__DATA__", payload)
    )


def write(page: dict[str, object], directory: Path, *, refresh: float | None = None) -> Path:
    """Replace the experiment's report under `directory` in one step; returns its path."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{page['experiment']}.html"
    partial = target.with_name(target.name + ".partial")
    partial.write_text(render(page, refresh=refresh))
    partial.replace(target)  # A reloading page never reads half a file.
    return target
