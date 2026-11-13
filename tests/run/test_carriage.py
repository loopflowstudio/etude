"""The carriage rules read what the tracker writes, without importing it."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from etude.carriage import (
    RecordMismatch,
    immutable,
    put_back,
    recorded_file,
    restore_set,
    resume_file,
    taken,
)
from etude.tracking.delivery import sync
from etude.tracking.storage import DirectoryStorage
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
    with pytest.raises(RecordMismatch, match="does not match"):
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
    with pytest.raises(RecordMismatch, match="does not match"):
        put_back(tmp_path / "three/r", files)


def test_an_author_s_artifacts_are_sent_once_and_never_read_again(tmp_path: Path) -> None:
    assert immutable("artifacts/evidence/ab.json.gz")
    assert immutable("r/artifacts/x")
    assert not immutable("evidence/ab.json.gz")
    run = track(
        experiment="e", clocks=[Step("steps", "steps")], run="r", directory=tmp_path / "src"
    )
    run.finish()
    folder = tmp_path / "src/r"
    (folder / "artifacts/evidence").mkdir(parents=True)
    (folder / "artifacts/evidence/ab.json.gz").write_bytes(b"first")
    (folder / "notes.txt").write_text("first")
    storage = DirectoryStorage(tmp_path / "dst")
    assert sync(tmp_path / "src", storages=[storage]) == {}
    # Changed bytes under artifacts/ are never read or sent again; other files are.
    (folder / "artifacts/evidence/ab.json.gz").write_bytes(b"second")
    (folder / "notes.txt").write_text("second")
    assert sync(tmp_path / "src", storages=[storage]) == {}
    assert storage.get("r/artifacts/evidence/ab.json.gz") == b"first"
    assert storage.get("r/notes.txt") == b"second"


def test_full_relay_reconciles_file_directory_changes_and_keeps_private_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "out/nested").mkdir(parents=True)
    (source / "out/nested/state").write_bytes(b"state")
    (source / "out/flat").write_bytes(b"flat")
    target = tmp_path / "target"
    (target / "out/flat").mkdir(parents=True)
    (target / "out/flat/old").write_bytes(b"old")
    (target / "out/nested").write_bytes(b"old file")
    (target / "out/.private").mkdir()
    (target / "out/save.partial").write_bytes(b"incomplete")
    put_back(target, restore_set(source))
    assert (target / "out/nested/state").read_bytes() == b"state"
    assert (target / "out/flat").read_bytes() == b"flat"
    assert (target / "out/.private").is_dir()
    assert (target / "out/save.partial").read_bytes() == b"incomplete"
