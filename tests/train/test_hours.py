"""Etude's clocks: `running_hours` and `hours_since_start`, through a resume and a continuation.

Time is injected: the tracker's `time` module is replaced, so hours pass without waiting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etude.run.records import HOURS_SINCE_START, RUNNING_HOURS, SourceKey
from etude.train.read import read
from etude.train.records import ParentRun, Step
from etude.train.report import training
from etude.train.run import Run, track

HOUR_MS = 3_600_000
START_MS = 1_800_000_000_000


class Clockwork:
    """Stands in for the `time` module the tracker reads."""

    def __init__(self) -> None:
        self.wall_ms = START_MS
        self.mono = 0.0

    def run(self, hours: float) -> None:
        """A process runs for `hours`: wall and monotonic time both pass."""
        self.wall_ms += round(hours * HOUR_MS)
        self.mono += hours * 3600

    def wait(self, hours: float) -> None:
        """Nothing runs, as between a crash and the next attempt."""
        self.wall_ms += round(hours * HOUR_MS)

    def time_ns(self) -> int:
        return self.wall_ms * 1_000_000

    def time(self) -> float:
        return self.wall_ms / 1000

    def monotonic(self) -> float:
        return self.mono


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clockwork:
    fake = Clockwork()
    monkeypatch.setattr("etude.train.run.time", fake)
    return fake


def _start(directory: Path, name: str) -> Run:
    return track(experiment="e", run=name, clocks=[Step("step", "updates")], directory=directory)


def _hours(at: dict[str, float]) -> tuple[float, float]:
    return round(at[RUNNING_HOURS], 6), round(at[HOURS_SINCE_START], 6)


def test_hours_continue_across_a_resume_and_a_continuation(
    tmp_path: Path, clock: Clockwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _start(tmp_path, "r")
    clock.run(0.5)
    first.log({"loss": 1.0}, step=5)
    clock.run(0.5)
    assert first.checkpoint_due(1)
    first.checkpoint("s3://models/10.pt", step=10)
    assert not first.checkpoint_due(1)
    clock.run(0.5)
    first.log({"loss": 9.0}, step=15)  # past the checkpoint: a resume discards it
    first.finish("failed")

    clock.wait(2)  # the machine is lost; the next attempt starts two hours later
    second = _start(tmp_path, "r")
    assert second.at(RUNNING_HOURS) == pytest.approx(1.0)
    assert second.at(HOURS_SINCE_START) == pytest.approx(3.5)
    clock.run(0.25)
    second.log({"loss": 0.5}, step=12)
    parent = second.checkpoint("s3://models/20.pt", step=20)
    second.finish()

    data = read(tmp_path, "e", "r")
    assert [_hours(c.at) for c in data.checkpoints] == [(1.0, 1.0), (1.25, 3.75)]
    points = data.points("loss", RUNNING_HOURS)
    assert points.x.round(6).tolist() == [0.5, 1.25] and points.y.tolist() == [1.0, 0.5]
    # An attempt's first row sets its cut per clock. Hours since start kept
    # moving while the machine was gone, so the discarded row at 1.5 stays on it.
    assert data.points("loss", HOURS_SINCE_START).x.round(6).tolist() == [0.5, 1.5, 3.75]
    assert "loss" in training([data]).series[RUNNING_HOURS]

    clock.wait(1)
    record = data.checkpoints[-1]
    assert record.artifact == parent
    handed = ParentRun(
        parent=SourceKey(namespace="local", kind="run", source_id="r"),
        at=record.at,
        checkpoint=record,
    )
    monkeypatch.setenv("ETUDE_CONTINUES", handed.model_dump_json())
    monkeypatch.setenv("ETUDE_UNTIL_CLOCK", RUNNING_HOURS)
    monkeypatch.setenv("ETUDE_UNTIL_VALUE", "3")
    continued = _start(tmp_path, "c")
    clock.run(0.5)
    assert not continued.checkpoint_due(1)  # 1.75 hours: the hour after 1.25 is not over
    continued.checkpoint("s3://models/30.pt", step=30)
    clock.run(0.3)
    assert continued.checkpoint_due(1)
    continued.finish()

    child = read(tmp_path, "e", "c")
    assert child.file is not None and child.file.declaration.continues is not None
    assert child.file.declaration.continues.clock == RUNNING_HOURS
    assert [_hours(c.at) for c in child.checkpoints] == [(1.75, 5.25)]


def test_checkpoints_saved_before_etudes_clocks_derive_them_from_wall_time(
    tmp_path: Path, clock: Clockwork
) -> None:
    first = _start(tmp_path, "r")
    clock.run(1)
    first.checkpoint("s3://models/10.pt", step=10)
    first.finish("failed")
    clock.wait(2)
    second = _start(tmp_path, "r")
    clock.run(0.25)
    second.checkpoint("s3://models/20.pt", step=20)
    second.finish()
    # As an older client wrote them: no Etude clock in a checkpoint or where an attempt resumed.
    for path in [
        *(tmp_path / "r/checkpoints").glob("*.json"),
        *(tmp_path / "r/metrics").glob("*.json"),
    ]:
        record = json.loads(path.read_text())
        for field in ("at", "resumed_at"):
            for name in (RUNNING_HOURS, HOURS_SINCE_START):
                record.get(field, {}).pop(name, None)
        if record.pop("first_row", False) and record["started_ms"] > START_MS:
            record["resumed_at"] = {"step": 10}  # it named the checkpoint it resumed from
        path.write_text(json.dumps(record))

    data = read(tmp_path, "e", "r")
    assert [_hours(c.at) for c in data.checkpoints] == [(1.0, 1.0), (1.25, 3.25)]
