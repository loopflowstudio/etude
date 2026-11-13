"""Per-update JSON tables follow selected metric history and its storage locations."""

from __future__ import annotations

import shutil
import warnings
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from etude.carriage import immutable, listing
from etude.run.delivery import sync
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage
from etude.train.read import read
from etude.train.records import RowTable, Step, TablePoint, Time
from etude.train.run import EtudeWarning, track
from tests.run.storage import MeasuredStorage

CLOCKS = [Step("updates", "optimizer updates"), Time("seconds", "active training")]


def test_row_tables_roundtrip_ship_once_and_relocate(tmp_path: Path) -> None:
    local = tmp_path / "local"
    rows: list[dict[str, object]] = [
        {"deck": "红", "seat": 0, "wins": 12, "rate": 0.6, "ok": True, "note": None},
        {"deck": "blue", "nested": {"results": [1, False, None, "win"]}},
    ]
    with track(experiment="e", run="r", clocks=CLOCKS, directory=local) as run:
        run.log_table("decks", rows, updates=400, seconds=20)
        (artifact,) = (local / "r/artifacts").iterdir()
        before = artifact.stat().st_mtime_ns
        run.log_table("decks", rows, updates=401, seconds=21)
        assert artifact.stat().st_mtime_ns == before
        assert artifact.stem == sha256(artifact.read_bytes()).hexdigest()
        assert not list((local / "r/artifacts").glob("*.partial"))
        # Callers can change their input as soon as logging returns.
        rows[0]["wins"] = 99
        run.log_table("empty", [], updates=401, seconds=21)
    assert run.problems == {} and run.dropped == 0
    data = read(local, "e", "r")
    expected = RowTable.model_validate({"rows": [{**rows[0], "wins": 12}, rows[1]]}).rows
    assert data.read_table("decks", "updates") == (
        TablePoint(x=400, rows=expected),
        TablePoint(x=401, rows=expected),
    )
    assert [p.x for p in data.read_table("decks", "seconds")] == [20, 21]
    assert data.read_table("empty", "updates") == (TablePoint(x=401, rows=()),)
    assert data.read_table("absent", "updates") == ()
    assert data.metrics() == []
    # Only a digest is retained in the update's text column.
    assert data.tables[0].row(0)["values"] == {"decks": artifact.stem}
    key = artifact.relative_to(local / "r").as_posix()
    assert immutable(key) and key in listing(local / "r", tail_seconds=0)

    bucket = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    assert sync(local, storages=[bucket]) == {}
    # One attempt, two indexes, one block, run/state, and two unique table artifacts.
    assert bucket.uploads == 8
    assert sync(local, storages=[bucket]) == {} and bucket.uploads == 8
    shutil.rmtree(local)
    relocated = tmp_path / "moved"
    (tmp_path / "bucket").rename(relocated)
    selected = read(relocated, "e", "r").read_table("decks", "updates")
    assert selected == (TablePoint(x=400, rows=expected), TablePoint(x=401, rows=expected))


@pytest.mark.parametrize("empty_resume", [False, True])
def test_row_tables_resolve_only_surviving_updates_across_machine_folders(
    tmp_path: Path, empty_resume: bool
) -> None:
    first = tmp_path / "pool/first/out/track"
    with track(experiment="e", run="r", clocks=CLOCKS, directory=first) as run:
        run.log_table("decks", [{"result": "keep"}], updates=1, seconds=1)
        run.log_table("decks", [{"result": "checkpoint"}], updates=2, seconds=2)
        run.log_table("decks", [{"result": "superseded"}], updates=3, seconds=3)
    # Damage superseded evidence: selecting history must happen before fetching artifacts.
    for path in (first / "r/artifacts").glob("*.json"):
        if b"superseded" in path.read_bytes():
            path.unlink()
    second = tmp_path / "pool/second/out/track"
    with track(
        experiment="e",
        run="r",
        clocks=CLOCKS,
        directory=second,
        resumed_at={"updates": 2, "seconds": 2},
    ) as resumed:
        if not empty_resume:
            resumed.log_table("decks", [{"result": "replaced"}], updates=2, seconds=2)
            resumed.log_table("decks", [{"result": "latest"}], updates=2, seconds=2)
            # Scalar updates after resume must not bring old tables back.
            resumed.log({"loss": 1}, updates=4, seconds=4)
    if not empty_resume:
        for path in (second / "r/artifacts").glob("*.json"):
            if b"replaced" in path.read_bytes():
                path.write_bytes(b"damaged but superseded")
    # No reference to this corrupt orphan: its bytes must never be read.
    (first / "r/artifacts/orphan.json").write_bytes(b"not JSON")
    storage = MeasuredStorage(DirectoryStorage(tmp_path))
    data = read(storage, "e", "r")
    assert data.blocks  # blocks are read when first used
    before = storage.requests
    assert data.read_table("decks", "updates") == (
        TablePoint(x=1, rows=({"result": "keep"},)),
        TablePoint(x=2, rows=({"result": "checkpoint" if empty_resume else "latest"},)),
    )
    # Two arrays per contributing block plus exactly two selected artifacts; planning reads
    # the resumed block's clock once more, since its repeated values give no range in the header.
    assert storage.requests - before == (4 if empty_resume else 8)
    assert data.read_table("orphan", "updates") == ()


@pytest.mark.parametrize("damage", ["missing", "digest", "schema", "reference"])
def test_selected_row_table_damage_is_visible(tmp_path: Path, damage: str) -> None:
    with track(experiment="e", run="r", clocks=CLOCKS, directory=tmp_path) as run:
        if damage == "schema":
            body = b'{"rows":[1]}'
            digest = sha256(body).hexdigest()
            DirectoryStorage(tmp_path / "r").put(f"artifacts/{digest}.json", body)
            run.log({"decks": digest}, updates=1, seconds=1)
        elif damage == "reference":
            run.log({"decks": "../../elsewhere"}, updates=1, seconds=1)
        else:
            run.log_table("decks", [{"value": 1}], updates=1, seconds=1)
    if damage in ("missing", "digest"):
        (artifact,) = (tmp_path / "r/artifacts").iterdir()
        if damage == "missing":
            artifact.unlink()
        else:
            artifact.write_bytes(b'{"rows":[]}')
    error = (
        FileNotFoundError
        if damage == "missing"
        else ValidationError
        if damage == "schema"
        else EtudeError
    )
    with pytest.raises(error):
        read(tmp_path, "e", "r").read_table("decks", "updates")


def test_row_table_rejects_invalid_rows_clocks_and_existing_damage(tmp_path: Path) -> None:
    with track(experiment="e", run="r", clocks=CLOCKS, directory=tmp_path) as run:
        for value in (object(), float("nan"), {"nested": float("inf")}):
            with pytest.raises(ValidationError):
                run.log_table("t", [{"value": value}], updates=1, seconds=1)
        with pytest.raises(ValueError, match="every declared clock"):
            run.log_table("t", [], updates=1)
        with pytest.raises(ValueError, match="distinct from clocks"):
            run.log_table("updates", [], updates=1, seconds=1)
        run.log_table("t", [], updates=1, seconds=1)
        (artifact,) = (tmp_path / "r/artifacts").iterdir()
        artifact.write_bytes(b"broken")
        with pytest.raises(EtudeError, match="table_artifact_damaged"):
            run.log_table("t", [], updates=2, seconds=2)
    assert read(tmp_path, "e", "r").rows == 1


def test_metric_name_warning_counts_numeric_names_across_attempts(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as said:
        warnings.simplefilter("always", EtudeWarning)
        with track(
            experiment="e",
            run="r",
            clocks=CLOCKS,
            directory=tmp_path,
            metric_name_limit=2,
        ) as run:
            run.log(
                {"loss": 1, "nested": {"reward": np.float32(2)}, "flag": True, "text": "hi"},
                updates=1,
                seconds=1,
            )
            run.log_table("decks", [], updates=1, seconds=1)
        assert said == []  # Clocks, text, flags and table references do not add numeric names.
        with track(
            experiment="e",
            run="r",
            clocks=CLOCKS,
            directory=tmp_path,
            metric_name_limit=2,
        ) as resumed:
            resumed.log({"loss": 2}, updates=2, seconds=2)
            resumed.log({"third": 3}, updates=3, seconds=3)
            resumed.log({"fourth": 4}, updates=4, seconds=4)
    assert len(said) == 1
    assert "3 distinct metric names" in str(said[0].message)
    assert "metric_name_limit=2" in str(said[0].message) and "log_table" in str(said[0].message)
    assert read(tmp_path, "e", "r").points("fourth", "updates").y.tolist() == [4]
    with warnings.catch_warnings(record=True) as disabled:
        warnings.simplefilter("always", EtudeWarning)
        with track(
            experiment="e",
            run="r",
            clocks=CLOCKS,
            directory=tmp_path,
            metric_name_limit=None,
        ) as run:
            run.log({"fifth": 5}, updates=5, seconds=5)
    assert disabled == []
