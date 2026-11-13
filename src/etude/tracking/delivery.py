"""Delivery: copying a run's finished files, whole, to the author's directories and buckets.

`RunFiles` is one run's local folder and the markers of what each destination
holds; `ship` is one delivery pass and `sync` delivers every run in a runs
directory. Live writers and `sync` share the same files and markers, so a pass
may be repeated or interrupted at any point: a replay is harmless.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from etude.carriage import (
    EVAL_BLOCK_PACK,
    RecordedFile,
    checkpoint_file,
    immutable,
    older_saves,
    prune_state,
    publication,
    recorded_files,
    retired,
    save_name,
    save_number,
    save_pointer,
)
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.records import RunFile
from etude.storage import DirectoryStorage, Storage, run_prefix
from etude.tracking.block_file import parse


class RunFiles:
    """One run's local directory, and the markers of what each destination holds."""

    def __init__(self, root: Path, tail_age: float = 60.0) -> None:
        self.root = root
        self.tail_age = tail_age
        self.lock = threading.RLock()

    def run_file(self) -> RunFile | None:
        path = self.root / "run.json"
        return RunFile.model_validate_json(path.read_bytes()) if path.is_file() else None

    def _marker(self, destination: str, key: str) -> Path:
        return self.root / ".shipped" / destination / (key.replace("/", "__") + ".sent")

    def shipped(self, destination: str, key: str) -> str | None:
        marker = self._marker(destination, key)
        return marker.read_text() if marker.is_file() else None

    def mark(self, destination: str, key: str, fingerprint: str) -> None:
        marker = self._marker(destination, key)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(fingerprint)


@dataclass(frozen=True)
class Delivery:
    key: str
    fingerprint: str
    body: bytes | Path


def _fingerprint(key: str, path: Path, recorded: Mapping[str, RecordedFile]) -> str:
    """Use recorded out digests; hash mutable files and send other immutable files once."""
    if key.startswith("out/") and key in recorded:
        return recorded[key].sha256
    if immutable(key):
        return "immutable"
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


class StorageDestination:
    """The author's directory or bucket: the same files, copied whole."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.name = storage.name

    def ship(self, files: RunFiles, run: str) -> None:
        prefix = run_prefix(run)
        # Freeze mutable objects together with the permanent-file listing. No
        # network I/O holds the writer's lock, and large immutable files stream.
        with TemporaryDirectory(prefix=".out-", dir=files.root) as staging:
            deliveries: list[Delivery] = []
            with files.lock:
                removed = retired(files.root)
                snapshot = publication(files.root, tail_seconds=files.tail_age)
                acknowledged = {
                    key: prior.encode()
                    for key in snapshot.pointers
                    if (prior := files.shipped(self.name, key)) is not None
                }
                keys = [key for key in snapshot.keys if key != MEMBERSHIP]
                recorded = recorded_files(files.root)
                out: list[OutFile] = []
                for index, key in enumerate(keys):
                    path = files.root / key
                    if key in snapshot.pointers:
                        pointer_body = snapshot.pointers[key]
                        fingerprint = pointer_body.decode()
                        out.append(
                            OutFile(
                                key=key,
                                sha256=sha256(pointer_body).hexdigest(),
                                size_bytes=len(pointer_body),
                            )
                        )
                        if files.shipped(self.name, key) != fingerprint:
                            deliveries.append(Delivery(key, fingerprint, pointer_body))
                        continue
                    if key.startswith("out/"):
                        # Recipes save by atomic replacement. A link keeps the
                        # fingerprint and upload on that same inode, without a copy.
                        saved = Path(staging) / str(index)
                        saved.hardlink_to(path)
                        path = saved
                    fingerprint = _fingerprint(key, path, recorded)
                    if key.startswith("out/"):
                        out.append(
                            OutFile(
                                key=key,
                                sha256=fingerprint,
                                size_bytes=path.stat().st_size,
                            )
                        )
                    if files.shipped(self.name, key) == fingerprint:
                        continue
                    block = parse(key)
                    body = (
                        path
                        if checkpoint_file(key)
                        or key.startswith("out/")
                        or key.endswith("/" + EVAL_BLOCK_PACK)
                        or block is not None
                        and block.level >= 2
                        else (files.root / key).read_bytes()
                    )
                    deliveries.append(Delivery(key, fingerprint, body))
                if (files.root / "out").is_dir() or files.shipped(
                    self.name, MEMBERSHIP
                ) is not None:
                    membership = OutFiles(files=tuple(out)).model_dump_json()
                    if files.shipped(self.name, MEMBERSHIP) != membership:
                        # The same queue publishes membership only after all its files succeed.
                        deliveries.append(Delivery(MEMBERSHIP, membership, membership.encode()))
            for delivery in deliveries:
                # A missing file aborts the pass too: a concurrent sync/compaction may
                # have replaced it with a block that was not in this snapshot.
                if isinstance(delivery.body, Path):
                    self.storage.put_file(prefix + delivery.key, delivery.body)
                else:
                    self.storage.put(prefix + delivery.key, delivery.body)
                files.mark(self.name, delivery.key, delivery.fingerprint)
        # Acknowledgement of the pointer makes only older saves disposable.
        for key in (*_older_saves(files, acknowledged), *removed):
            if files.shipped(self.name, key) is not None and self.storage.delete(prefix + key):
                files._marker(self.name, key).unlink(missing_ok=True)


def _older_saves(files: RunFiles, acknowledged: Mapping[str, bytes]) -> list[str]:
    """Local save files numbered below the pointers a destination acknowledged."""
    return older_saves(
        (path.relative_to(files.root).as_posix() for path in files.root.glob("out/state/**/*")),
        acknowledged,
    )


def ship(files: RunFiles, run: str, destination: StorageDestination) -> str | None:
    """One delivery pass. Returns None when current, else a short reason."""
    try:
        destination.ship(files, run)
    except Exception as error:  # noqa: BLE001 - delivery must never raise into a run
        return f"unavailable: {type(error).__name__}"
    return None


def prune(files: RunFiles, destinations: Sequence[StorageDestination]) -> None:
    """Local saves wait until every destination has acknowledged their replacement."""
    with files.lock:
        snapshot = publication(files.root, tail_seconds=files.tail_age)
        acknowledged: dict[str, bytes] = {}
        for key, body in snapshot.pointers.items():
            confirmations = [files.shipped(destination.name, key) for destination in destinations]
            if not destinations:
                acknowledged[key] = body
            elif all(value is not None for value in confirmations):
                acknowledged[key] = min(
                    (value.encode() for value in confirmations if value is not None),
                    key=lambda value: save_number(save_name(value)) or 0,
                )
        obsolete = _older_saves(files, acknowledged)
        # Keep names needed to retry remote retirement. Delivery owns network I/O.
        waiting = {
            save_pointer(key)
            for key in obsolete
            if any(files.shipped(destination.name, key) is not None for destination in destinations)
        }
        acknowledged = {key: body for key, body in acknowledged.items() if key not in waiting}
        prune_state(files.root, acknowledged)


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
        targets = destinations(base, storages)
        prune(files, targets)
        for destination in targets:
            reason = ship(files, run, destination)
            if reason is not None:
                undelivered.setdefault(run, {})[destination.name] = reason
    return undelivered
