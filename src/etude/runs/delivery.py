"""Delivery: copying a run's finished files, whole, to the author's directories and buckets.

`RunFiles` is one run's local folder and the markers of what each destination
holds; `ship` is one delivery pass and `sync` delivers every run in a runs
directory. Live writers and `sync` share the same files and markers, so a pass
may be repeated or interrupted at any point: a replay is harmless.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from stat import S_ISREG

from etude.runs.block_file import parse
from etude.runs.listing import RUN_FILES, RUN_FOLDERS
from etude.runs.records import RunFile
from etude.runs.storage import DirectoryStorage, Storage, run_prefix


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

        Blocks precede the indexes that describe them. A crash log or a
        half-written file is never sent; a tail only once it is old enough that
        its writer has died.
        """

        def folder(name: str) -> list[str]:
            names: list[str] = []
            for path in (self.root / name).glob("*"):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # folded away since the listing
                if not S_ISREG(stat.st_mode) or path.suffix in (".partial", ".open"):
                    continue
                if path.suffix == ".tail" and time.time() - stat.st_mtime < self.tail_age:
                    continue
                names.append(f"{name}/{path.name}")
            return sorted(names, key=lambda key: (not key.endswith((".blk", ".tail")), key))

        return [name for name in RUN_FILES if (self.root / name).is_file()] + [
            key for name in RUN_FOLDERS for key in folder(name)
        ]

    def fingerprint(self, key: str) -> str:
        """Blocks never change; a stored checkpoint is large; other files are hashed."""
        if key.endswith((".blk", ".tail")):
            return "sealed"
        if key.startswith("checkpoints/") and not key.endswith(".json"):
            stat = (self.root / key).stat()
            # A resume may replace a checkpoint at the same coordinate, even
            # with the same byte count. Avoid hashing large files on every pass.
            return f"file-{stat.st_size}-{stat.st_mtime_ns}"
        return sha256((self.root / key).read_bytes()).hexdigest()

    def removed(self) -> list[str]:
        """Every path, relative to the run, that a writer has retired."""
        lines: list[str] = []
        for note in sorted((self.root / "metrics").glob("*.removed")):
            lines += note.read_text().split("\n")[:-1]
        return lines

    def _marker(self, destination: str, key: str) -> Path:
        return self.root / ".shipped" / destination / (key.replace("/", "__") + ".sent")

    def shipped(self, destination: str, key: str) -> str | None:
        marker = self._marker(destination, key)
        return marker.read_text() if marker.is_file() else None

    def mark(self, destination: str, key: str, fingerprint: str) -> None:
        marker = self._marker(destination, key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint)

    def pending(self, destination: str) -> list[tuple[str, str]]:
        pending: list[tuple[str, str]] = []
        for key in self.deliverable():
            mark = self.fingerprint(key)
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
            for key, fingerprint in files.pending(self.name):
                block = parse(key)
                large = key.startswith("checkpoints/") and not key.endswith(".json")
                body = (
                    files.root / key
                    if large or block is not None and block.level >= 2
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
