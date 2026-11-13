"""Finding runs in a directory or bucket by their folders, at any depth, with no server."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Container, Iterator, Sequence
from pathlib import Path

from pydantic import ValidationError

from etude.tracking.records import RunFile
from etude.tracking.storage import Storage, storage_from_uri


def as_storage(source: Storage | Path | str) -> Storage:
    """A path or `s3://`/`r2://` URI is read as `storage_from_uri` reads it."""
    return storage_from_uri(source) if isinstance(source, (Path, str)) else source


# A folder is a run's when it holds one of these directly: its name is the run's.
# `environment.json`, `tournament.json` and `blocks` are an eval run's (`etude.eval`);
# `tables` and `matches` an eval run's of the per-file layout, kept until converted.
RUN_FOLDERS = ("metrics", "checkpoints", "blocks", "tables", "matches")
RUN_FILES = ("run.json", "environment.json", "tournament.json", "state.json", "outcome.json")


def run_folders(keys: Sequence[str]) -> dict[str, list[str]]:
    """Each run's folders, as prefixes, by run name, in the order `keys` lists them.

    A run's folder is named for the run and may sit at any depth, so one
    source can hold several copies of a run, one per machine that wrote to it,
    and a bucket can hold a launcher's other files beside them. Taking a
    listing already made lets a reader list a source once.
    """
    found: dict[str, dict[str, None]] = {}
    for key in keys:
        parts = key.split("/")
        for index in range(1, len(parts)):
            last = index == len(parts) - 1
            if (parts[index] in RUN_FOLDERS and not last) or (last and parts[index] in RUN_FILES):
                found.setdefault(parts[index - 1], {})["/".join(parts[:index]) + "/"] = None
                break
    return {run: list(folders) for run, folders in found.items()}


def under(keys: Sequence[str], prefix: str) -> Iterator[str]:
    """The keys under `prefix` of a sorted listing."""
    for key in keys[bisect_left(keys, prefix) :]:
        if not key.startswith(prefix):
            return
        yield key


def declared(storage: Storage, folders: Sequence[str], listed: Container[str]) -> RunFile | None:
    """The run's declaration from the first of its folders that holds a valid one.

    Only folders whose `run.json` is in `listed`, the source's listing, are asked.
    """
    for folder in folders:
        if folder + "run.json" not in listed:
            continue
        try:
            return RunFile.model_validate_json(storage.get(folder + "run.json"))
        except (FileNotFoundError, ValidationError):
            continue
    return None


def runs(source: Storage | Path | str) -> list[tuple[str, str]]:
    """Every (experiment, run) under this directory or bucket, at any depth.

    A run is listed once its declaration, `run.json`, has arrived.
    """
    storage = as_storage(source)
    keys = storage.keys("")
    listed = set(keys)
    found = []
    for run, folders in run_folders(keys).items():
        file = declared(storage, folders, listed)
        if file is not None:
            found.append((file.experiment, run))
    return sorted(found)
