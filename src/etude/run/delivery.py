"""Delivery: copying a run's finished files, whole, to the author's directories and buckets.

`RunFiles` is one run's local folder and the markers of what each destination
holds; `ship` is one delivery pass and `sync` delivers every run in a runs
directory. Live writers and `sync` share the same files and markers, so a pass
may be repeated or interrupted at any point: a replay is harmless.
"""

from __future__ import annotations

import threading
from collections.abc import Container, Sequence
from dataclasses import dataclass
from hashlib import file_digest
from pathlib import Path

from etude.carriage import (
    EVAL_BLOCK_PACK,
    checkpoint_file,
    immutable,
    listing,
    recorded_files,
    retired,
)
from etude.run.block_file import parse
from etude.run.records import RunFile
from etude.run.storage import DirectoryStorage, Storage, run_prefix
from etude.working import MEMBERSHIP, WorkingFiles


class RunFiles:
    """One run's local directory, and the markers of what each destination holds."""

    def __init__(self, root: Path, tail_age: float = 60.0) -> None:
        self.root = root
        self.tail_age = tail_age
        self.lock = threading.RLock()

    def run_file(self) -> RunFile | None:
        path = self.root / "run.json"
        return RunFile.model_validate_json(path.read_bytes()) if path.is_file() else None

    def deliverable(self) -> list[str]:
        """Every file a destination should hold, as keys under the run, in sending order.

        The rules are the courier's too (`etude.carriage.listing`). A tail is
        sent only once it is old enough that its writer has died.
        """
        return [key for key in listing(self.root, tail_seconds=self.tail_age) if key != MEMBERSHIP]

    def fingerprint(self, key: str, recorded: Container[str] | None = None) -> str:
        """Immutable files are sent once; others are hashed, so a change is sent again."""
        if immutable(key, recorded_files(self.root) if recorded is None else recorded):
            return "immutable"
        with (self.root / key).open("rb") as stream:
            return file_digest(stream, "sha256").hexdigest()

    def removed(self) -> list[str]:
        """Every path, relative to the run, that a writer has retired."""
        return retired(self.root)

    def _marker(self, destination: str, key: str) -> Path:
        return self.root / ".shipped" / destination / (key.replace("/", "__") + ".sent")

    def shipped(self, destination: str, key: str) -> str | None:
        marker = self._marker(destination, key)
        return marker.read_text() if marker.is_file() else None

    def mark(self, destination: str, key: str, fingerprint: str) -> None:
        marker = self._marker(destination, key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint)

    def pending(self, destination: str, keys: Sequence[str] | None = None) -> list[tuple[str, str]]:
        pending: list[tuple[str, str]] = []
        recorded = recorded_files(self.root)
        for key in self.deliverable() if keys is None else keys:
            mark = self.fingerprint(key, recorded)
            if self.shipped(destination, key) != mark:
                pending.append((key, mark))
        return pending


@dataclass(frozen=True)
class Delivery:
    key: str
    fingerprint: str
    body: bytes | Path


class StorageDestination:
    """The author's directory or bucket: the same files, copied whole."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.name = storage.name

    def ship(self, files: RunFiles, run: str) -> None:
        prefix = run_prefix(run)
        # Freeze mutable objects together with the permanent-file listing. No
        # network I/O holds the writer's lock, and large immutable files stream.
        deliveries: list[Delivery] = []
        with files.lock:
            retired = files.removed()
            keys = files.deliverable()
            working = WorkingFiles(
                files=tuple(sorted(key for key in keys if key.startswith("out/")))
            )
            for key, fingerprint in files.pending(self.name, keys):
                block = parse(key)
                body = (
                    files.root / key
                    if checkpoint_file(key)
                    or key.startswith("out/")
                    or key.endswith("/" + EVAL_BLOCK_PACK)
                    or block is not None
                    and block.level >= 2
                    else (files.root / key).read_bytes()
                )
                deliveries.append(Delivery(key, fingerprint, body))
        for delivery in deliveries:
            # A missing file aborts the pass too: a concurrent sync/compaction may
            # have replaced it with a block that was not in this snapshot.
            if isinstance(delivery.body, Path):
                self.storage.put_file(prefix + delivery.key, delivery.body)
            else:
                self.storage.put(prefix + delivery.key, delivery.body)
            files.mark(self.name, delivery.key, delivery.fingerprint)
        if (files.root / "out").is_dir() or files.shipped(self.name, MEMBERSHIP) is not None:
            membership = working.model_dump_json()
            if files.shipped(self.name, MEMBERSHIP) != membership:
                self.storage.put(prefix + MEMBERSHIP, membership.encode())
                files.mark(self.name, MEMBERSHIP, membership)
        for key in retired:
            if files.shipped(self.name, key) is not None and self.storage.delete(prefix + key):
                files._marker(self.name, key).unlink(missing_ok=True)


def ship(files: RunFiles, run: str, destination: StorageDestination) -> str | None:
    """One delivery pass. Returns None when current, else a short reason."""
    try:
        destination.ship(files, run)
    except Exception as error:  # noqa: BLE001 - delivery must never raise into a run
        return f"unavailable: {type(error).__name__}"
    return None


def destinations(root: Path, storages: Sequence[Storage]) -> list[StorageDestination]:
    """Each storage but the runs directory `root` itself, which already holds the files."""
    local = DirectoryStorage(root).name
    return [StorageDestination(storage) for storage in storages if storage.name != local]


def sync(
    directory: Path | str,
    *,
    storages: Sequence[Storage] = (),
    stopped: bool = False,
) -> dict[str, dict[str, str]]:
    """Deliver every run under the runs `directory`. Idempotent; safe beside a live run.

    Only sealed blocks are sent. A tail waits until it is old enough that its
    writer has died, unless `stopped` says every writer has. Returns, per run,
    the destinations that are still not current and why.
    """
    base = Path(directory)
    undelivered: dict[str, dict[str, str]] = {}
    for root in sorted(path.parent for path in base.glob("*/run.json")):
        run = root.name
        files = RunFiles(root, tail_age=0.0 if stopped else 60.0)
        for destination in destinations(base, storages):
            reason = ship(files, run, destination)
            if reason is not None:
                undelivered.setdefault(run, {})[destination.name] = reason
    return undelivered
