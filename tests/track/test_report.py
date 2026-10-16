"""Reports summarize tracker-written timing observations on the selected clock."""

import json
from pathlib import Path

import numpy as np
import pytest

from etude.track.read import read, read_all
from etude.track.records import WALL_CLOCK, Step
from etude.track.report import BUCKETS, TimingSummary, page_data, pages, render
from etude.track.run import EtudeWarning, init
from etude.track.serve import Server
from etude.track.storage import DirectoryStorage
from tests.track.storage import MeasuredStorage


def _summaries(page: dict[str, object], clock: str) -> dict[tuple[str, str], TimingSummary]:
    timings = page["timings"]
    assert isinstance(timings, dict)
    summaries = [TimingSummary.model_validate(value) for value in timings[clock]]
    return {(summary.run, summary.metric): summary for summary in summaries}


def test_timing_mean_weights_observations_before_rounding(tmp_path: Path) -> None:
    with init(
        experiment="e", run="r", clocks=[Step("updates", "updates")], directory=tmp_path
    ) as run:
        for update in range(1200):
            run.log(
                {"time/learn": 100.0 if update == 1199 else float(update % 5)},
                updates=100_000 if update == 1199 else update,
            )
    data = read(tmp_path, "e", "r")
    reduced = data.chart("time/learn", "updates", BUCKETS)
    assert len(set(reduced.count.tolist())) > 1
    summary = _summaries(page_data("e", [data]), "updates")["r", "time/learn"]
    values = data.points("time/learn", "updates").y
    assert summary.finite == 1200 and summary.nonfinite == 0
    assert summary.mean == pytest.approx(float(values.mean()), rel=1e-5)
    assert summary.mean != pytest.approx(float(reduced.mean.mean()))
    assert summary.min == 0 and summary.max == 100


def test_timing_coverage_follows_clock_and_latest_write(tmp_path: Path) -> None:
    clocks = [Step("updates", "updates"), Step("samples", "samples")]
    with init(experiment="e", run="r", clocks=clocks, directory=tmp_path) as run:
        run.log({"time/learn": 0.0, "time/note": "text"}, updates=0, samples=0)
        run.log({"time/learn": 4.0, "time/learn/backward": 2.0}, updates=1, samples=10)
        run.log({"time/learn": float("nan"), "time/bad": float("nan")}, updates=2, samples=20)
        run.log({"time/learn": float("inf"), "time/bad": -float("inf")}, updates=3, samples=30)
        run.log({"loss": 1.0}, updates=4, samples=40)
        with pytest.warns(EtudeWarning, match="no value for clock"):
            run.log({"time/learn": 10.0}, updates=5)
        run.log({"time/learn": 8.0}, updates=5, samples=50)
        run.log({"time/learn": 6.0}, updates=6, samples=50)
        run.checkpoint("s3://models/model.pt", updates=6, samples=50)
    with init(experiment="e", run="untimed", clocks=[], directory=tmp_path) as run:
        run.log({"loss": 3.0})
    page = page_data("e", read_all(tmp_path))
    updates = _summaries(page, "updates")
    assert updates["r", "time/learn"] == TimingSummary(
        run="r", metric="time/learn", finite=4, nonfinite=2, mean=4.5, min=0, max=8
    )
    samples = _summaries(page, "samples")["r", "time/learn"]
    assert samples.finite == 3 and samples.nonfinite == 2
    assert samples.mean == pytest.approx(10 / 3, rel=1e-5)
    assert samples.max == 6
    assert updates["r", "time/learn/backward"].mean == 2
    bad = updates["r", "time/bad"]
    assert bad.finite == 0 and bad.nonfinite == 2
    assert bad.mean is bad.min is bad.max is None
    absent = updates["untimed", "time/learn"]
    assert absent.finite == absent.nonfinite == 0 and absent.mean is None
    assert ("r", "time/note") not in updates
    assert all(s.finite == s.nonfinite == 0 for s in _summaries(page, "checkpoint").values())
    # Wall-time observations also follow the existing logical reader, including
    # replacement at equal millisecond coordinates.
    points = read(tmp_path, "e", "r").points("time/learn").y
    wall = _summaries(page, WALL_CLOCK)["r", "time/learn"]
    assert wall.finite == int(np.isfinite(points).sum())
    assert wall.nonfinite == int((~np.isfinite(points)).sum())
    html = render(page)
    assert '"mean": null' in html and "__DATA__" not in html
    with Server(tmp_path, ("127.0.0.1", 0)) as server:
        served = server.report("e")
    assert served is not None
    # Both entry points embed the same summary payload, through the same renderer.
    for content in (html, served):
        payload = content.split('<script type="application/json" id="data">')[1].split("</script>")[
            0
        ]
        assert json.loads(payload)["timings"] == page["timings"]


@pytest.mark.parametrize("metric", ["loss", "time/learn"])
def test_timing_summary_needs_no_extra_storage_requests(tmp_path: Path, metric: str) -> None:
    with init(
        experiment="e", run="r", clocks=[Step("updates", "updates")], directory=tmp_path
    ) as run:
        run.log({metric: 2.0}, updates=0)
    measured = MeasuredStorage(DirectoryStorage(tmp_path))
    page = pages(measured)["e"]
    # The ordinary chart read budget also covers timing summaries.
    assert measured.requests == 9
    assert measured.listings == 2
    if metric.startswith("time/"):
        assert _summaries(page, "updates")["r", metric].mean == 2
    else:
        assert page["timings"] == {}
        series = page["series"]
        assert isinstance(series, dict) and series["updates"]["loss"][0]["mean"] == [2]
