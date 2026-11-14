"""Streaming a run's metric history batch by batch (`read_batches`)."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import numpy as np
import pytest

from etude.block_file import block_key
from etude.errors import EtudeError
from etude.storage import DirectoryStorage, run_prefix
from etude.train.block import Builder, Columns, Series, concat, decode
from etude.train.manifest import Index, describe
from etude.train.read import Points, read, read_batches
from etude.train.tracking import write_block
from tests.run.storage import MeasuredStorage

FIRST, LATER = "abcdef123456", "abcdef123457"


def _messy_run(root: Path) -> None:
    """A clock that went backwards, sparse rows, rows without the clock, and a later writer."""
    builder = Builder(["step"])
    for step in range(10):
        values: dict[str, object] = {"value": float(step)}
        if step % 3 == 0:
            values["rare"] = -float(step)
        builder.add(1000 + step, {"step": float(step)}, values)
    write_block(root, block_key(1, 0, 0, FIRST), builder.columns(FIRST, 0))
    builder = Builder(["step"])
    for step in range(8, 15):  # went back to 8: these replace the earlier 8 and 9
        builder.add(2000 + step, {"step": float(step)}, {"value": 100.0 + step})
    write_block(root, block_key(1, 1, 1, FIRST), builder.columns(FIRST, 1))
    builder = Builder(["step"])
    builder.add(3000, {}, {"value": 999.0})  # gives no step
    for step in range(20, 25):
        builder.add(3001 + step, {"step": float(step)}, {"value": float(step), "rare": 1.0})
    write_block(root, block_key(1, 2, 2, FIRST), builder.columns(FIRST, 2))
    # A later process began at 22, so the first writer's rows count up to 22.
    builder = Builder(["step"])
    for step in range(22, 27):
        builder.add(5000 + step, {"step": float(step)}, {"value": 200.0 + step})
    write_block(root, block_key(1, 0, 0, LATER), builder.columns(LATER, 0))


def _joined(parts: list[Points]) -> Points:
    return Points(x=np.concatenate([p.x for p in parts]), y=np.concatenate([p.y for p in parts]))


def test_batches_join_into_exactly_what_points_returns(tmp_path: Path) -> None:
    clock = "step"
    _messy_run(tmp_path / run_prefix("r"))
    metrics = ["value", "rare"]
    batches = list(read_batches(tmp_path, "e", "r", clock=clock, metrics=metrics))
    data = read(tmp_path, "e", "r")
    for metric in metrics:
        joined = _joined([batch.points[metric] for batch in batches])
        expected = data.points(metric, clock)
        np.testing.assert_array_equal(joined.x, expected.x)
        np.testing.assert_array_equal(joined.y, expected.y)
    xs = [np.concatenate([b.points[m].x for m in metrics]) for b in batches]
    xs = [x for x in xs if len(x)]
    assert all(before.max() < after.min() for before, after in zip(xs, xs[1:]))


def test_overlapping_blocks_share_a_batch_and_the_latest_write_wins(tmp_path: Path) -> None:
    _messy_run(tmp_path / run_prefix("r"))
    batches = list(read_batches(tmp_path, "e", "r", clock="step", metrics=["value", "rare"]))
    assert len(batches) == 2
    value = _joined([batch.points["value"] for batch in batches])
    assert value.x.tolist() == [*range(15), *range(20, 27)]
    assert value.y.tolist() == [
        *range(8),
        *(100 + s for s in range(8, 15)),
        20,
        21,
        *(200 + s for s in range(22, 27)),
    ]
    rare = _joined([batch.points["rare"] for batch in batches])
    assert rare.x.tolist() == [0, 3, 6, 9, 20, 21, 22]  # no later row logged rare at 9


def _long_run(root: Path, blocks: int, rows: int = 8192) -> None:
    """Permanent blocks of five metrics, with the writer's index beside them."""
    for sequence in range(blocks):
        x = np.arange(sequence * rows, (sequence + 1) * rows, dtype=np.float64)
        table = Columns(
            writer=FIRST,
            first=sequence,
            last=sequence,
            wall_ms=(x * 100).astype("<i8"),
            clocks={"updates": x},
            series={
                (f"rl/metric_{i}", "metric"): Series(rows=None, data=np.sin(x / (i + 1)))
                for i in range(5)
            },
        )
        write_block(root, block_key(2, sequence, sequence, FIRST), table)
    Index(root, FIRST).write()


def _peak(blocks: int, root: Path, streaming: bool) -> int:
    _long_run(root / run_prefix("r"), blocks)
    tracemalloc.start()
    try:
        if streaming:
            total = sum(
                len(batch.points["rl/metric_0"])
                for batch in read_batches(root, "e", "r", clock="updates", metrics=["rl/metric_0"])
            )
        else:
            total = len(read(root, "e", "r").points("rl/metric_0", "updates"))
        assert total == blocks * 8192
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_peak_memory_stays_flat_as_the_run_grows(tmp_path: Path) -> None:
    small, large = (
        {s: _peak(blocks, tmp_path / f"{blocks}-{s}", s) for s in (True, False)}
        for blocks in (4, 32)
    )
    # Eight times the blocks: the whole-run reader grows with them, batches do not.
    assert large[False] > 4 * small[False]
    assert large[True] < 1.5 * small[True]
    assert large[True] < large[False] / 4


def test_reads_each_block_twice_and_only_its_selected_arrays(tmp_path: Path) -> None:
    _long_run(tmp_path / run_prefix("r"), 6)
    measured = MeasuredStorage(DirectoryStorage(tmp_path))
    metrics = ["rl/metric_0", "rl/metric_1"]
    batches = list(read_batches(measured, "e", "r", clock="updates", metrics=metrics))
    assert len(batches) == 6
    # Per block: the whole block twice, planning then reading (a bucket's copy
    # comes from the cache the second time); then its clock and the two metrics.
    assert measured.requests == 6 * (2 + 1 + len(metrics))
    assert measured.listings == 2


def test_an_unreadable_index_is_never_needed(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    _long_run(root, 3, rows=16)
    (root / f"metrics/{FIRST}.permanent.json").write_text("not an index")
    batches = read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0"])
    assert sum(len(batch.points["rl/metric_0"]) for batch in batches) == 3 * 16


def test_a_damaged_array_raises_after_the_good_batches(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    _long_run(root, 3, rows=16)
    key = block_key(2, 2, 2, FIRST)
    described = describe(DirectoryStorage(root), "", key)
    column = next(c for c in described.header.series if c.name == "rl/metric_0")
    path = root / key
    body = bytearray(path.read_bytes())
    body[described.start + column.data.offset] ^= 0xFF
    path.write_bytes(bytes(body))
    batches = read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0"])
    assert len(next(batches).points["rl/metric_0"]) == 16
    assert len(next(batches).points["rl/metric_0"]) == 16
    with pytest.raises(EtudeError, match="block_array_damaged"):
        next(batches)


def test_a_sequence_gap_raises(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    _long_run(root, 3, rows=16)
    (root / block_key(2, 1, 1, FIRST)).unlink()
    with pytest.raises(FileNotFoundError, match="block sequence gap"):
        list(read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0"]))


def test_a_compaction_while_reading_resumes_after_the_last_batch(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    _long_run(root, 3, rows=16)
    batches = read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0"])
    parts = [next(batches).points["rl/metric_0"]]
    originals = [root / block_key(2, sequence, sequence, FIRST) for sequence in range(3)]
    merged = concat([decode(path.read_bytes()) for path in originals])
    write_block(root, block_key(3, 0, 2, FIRST), merged)
    for path in originals:
        path.unlink()
    parts += [batch.points["rl/metric_0"] for batch in batches]
    assert _joined(parts).x.tolist() == list(range(3 * 16))


def test_a_slow_reader_outlasts_every_compaction_of_a_live_run(tmp_path: Path) -> None:
    root = tmp_path / run_prefix("r")
    _long_run(root, 6, rows=16)
    batches = read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0"])
    parts = [next(batches).points["rl/metric_0"]]
    for sequence in range(1, 6):
        # Before each batch the writer has replaced the next block once more.
        path = root / block_key(2, sequence, sequence, FIRST)
        path.rename(root / block_key(3, sequence, sequence, FIRST))
        parts.append(next(batches).points["rl/metric_0"])
    assert _joined(parts).x.tolist() == list(range(6 * 16))


def test_a_metric_no_block_logged_raises_before_any_batch(tmp_path: Path) -> None:
    _long_run(tmp_path / run_prefix("r"), 2, rows=16)
    batches = read_batches(tmp_path, "e", "r", clock="updates", metrics=["rl/metric_0", "rl/lsos"])
    with pytest.raises(EtudeError, match="metric_not_logged"):
        next(batches)


def test_one_name_given_as_a_string_is_refused(tmp_path: Path) -> None:
    _long_run(tmp_path / run_prefix("r"), 1, rows=16)
    with pytest.raises(TypeError, match=r"\['rl/metric_0'\]"):
        next(read_batches(tmp_path, "e", "r", clock="updates", metrics="rl/metric_0"))
