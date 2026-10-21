"""Finding runs in a directory or bucket by their folders, at any depth, with no server."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from etude.run.records import RunFile
from etude.run.storage import DirectoryStorage, Storage


def as_storage(source: Storage | Path | str) -> Storage:
    """A path is a local directory; anything else is already a storage."""
    return DirectoryStorage(source) if isinstance(source, (Path, str)) else source


# A folder is a run's when it holds one of these directly: its name is the run's.
# `environment.json`, `tournament.json`, `tables` and `matches` are an eval run's
# (`etude.eval`).
# Delivery sends folders in this order, so a match's tables precede its record.
RUN_FOLDERS = ("metrics", "checkpoints", "tables", "matches")
RUN_FILES = ("run.json", "environment.json", "tournament.json", "state.json")


def places(storage: Storage) -> dict[str, list[str]]:
    """Each run's folders, as prefixes, by run name, in listing order.

    A run's folder is named for the run and may sit at any depth, so one
    source can hold several copies of a run, one per machine that wrote to it,
    and a bucket can hold a launcher's other files beside them.
    """
    found: dict[str, dict[str, None]] = {}
    for key in storage.keys(""):
        parts = key.split("/")
        for index in range(1, len(parts)):
            last = index == len(parts) - 1
            if (parts[index] in RUN_FOLDERS and not last) or (last and parts[index] in RUN_FILES):
                found.setdefault(parts[index - 1], {})["/".join(parts[:index]) + "/"] = None
                break
    return {run: list(folders) for run, folders in found.items()}


def declared(storage: Storage, folders: Sequence[str]) -> RunFile | None:
    """The run's declaration from the first of its folders that holds a valid one."""
    for place in folders:
        try:
            return RunFile.model_validate_json(storage.get(place + "run.json"))
        except (FileNotFoundError, ValidationError):
            continue
    return None


def runs(source: Storage | Path | str) -> list[tuple[str, str]]:
    """Every (experiment, run) under this directory or bucket, at any depth.

    A run is listed once its declaration, `run.json`, has arrived.
    """
    storage = as_storage(source)
    found = []
    for run, folders in places(storage).items():
        file = declared(storage, folders)
        if file is not None:
            found.append((file.experiment, run))
    return sorted(found)
