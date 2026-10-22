"""The carriage rules read what the tracker writes, without importing it."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from etude.carriage import put_back, recorded_file, restore_set, resume_file, taken
from etude.train.records import Step
from etude.train.run import track


def _recorded(tmp_path: Path) -> Path:
    run = track(
        experiment="pin", clocks=[Step("steps", "training steps")], run="r", directory=tmp_path
    )
    weights = tmp_path / "weights.pt"
    for step in (1, 2):
        weights.write_bytes(f"weights at {step}".encode())
        run.checkpoint(weights, steps=step)
    run.finish()
    deadline = time.monotonic() + 10
    while len(list((tmp_path / "r/checkpoints").glob("*.json"))) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return tmp_path / "r"


def test_resume_file_names_the_file_of_the_tracker_s_latest_record(tmp_path: Path) -> None:
    folder = _recorded(tmp_path)
    wanted = resume_file(folder)
    assert wanted is not None and wanted.key == "checkpoints/steps-2.pt"
    body = (folder / wanted.key).read_bytes()
    assert (wanted.sha256, wanted.size_bytes) == (hashlib.sha256(body).hexdigest(), len(body))


def test_a_relayed_folder_keeps_only_what_resume_needs_and_every_taken_name(
    tmp_path: Path,
) -> None:
    folder = _recorded(tmp_path / "one")
    (folder / "checkpoints/steps-3.pt").write_bytes(b"kept without a record")
    there = tmp_path / "two/r"
    put_back(there, restore_set(folder))
    assert not (there / "checkpoints/steps-1.pt").exists()
    assert (there / "checkpoints/steps-2.pt").read_bytes() == b"weights at 2"
    assert taken(there) == {"steps-1.pt", "steps-2.pt", "steps-3.pt"}

    files = restore_set(folder)
    files["checkpoints/steps-2.pt"] = b"something else"
    with pytest.raises(ValueError, match="does not match"):
        put_back(tmp_path / "three/r", files)
    assert not (tmp_path / "three/r/checkpoints/steps-2.pt").exists()


def test_one_recorded_file_travels_with_only_its_record(tmp_path: Path) -> None:
    folder = _recorded(tmp_path / "one")
    there = tmp_path / "two/r"
    put_back(there, recorded_file(folder, "checkpoints/steps-1.pt"))
    assert sorted(p.name for p in (there / "checkpoints").iterdir()) == [
        "steps-1.json",
        "steps-1.pt",
    ]
    assert (there / "checkpoints/steps-1.pt").read_bytes() == b"weights at 1"
    with pytest.raises(FileNotFoundError):
        recorded_file(folder, "checkpoints/steps-3.pt")

    files = recorded_file(folder, "checkpoints/steps-1.pt")
    files["checkpoints/steps-1.pt"] = b"something else"
    with pytest.raises(ValueError, match="does not match"):
        put_back(tmp_path / "three/r", files)
