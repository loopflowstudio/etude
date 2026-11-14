"""Run identity, local files, delivery and discovery, shared by training and evaluation.

Run owns a live run's identity and destinations. Declaration claims run.json
before a writer starts; delivery publishes complete files and discovery joins
historical copies by declared experiment and run.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from bisect import bisect_left
from collections.abc import Container, Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Literal

from pydantic import ValidationError

from etude.block_file import parse
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
from etude.errors import EtudeError
from etude.identity import resume_conflicts, run_address
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.records import (
    Clock,
    Continuation,
    Record,
    RunCounters,
    RunRecord,
    RunState,
)
from etude.storage import DirectoryStorage, Storage, run_directory, run_prefix, storage_from_uri


class Run:
    """A live run's identity, local files and delivery destinations."""

    def __init__(
        self,
        *,
        experiment: str,
        name: str,
        root: Path,
        targets: Sequence[StorageDestination],
    ) -> None:
        self.experiment = experiment
        self.name = name
        self.files = RunFiles(root)
        self._destinations = list(targets)
        self.problems: dict[str, str] = {}

    def ship(self) -> bool:
        """Deliver to every destination once; retain each destination's failure reason."""
        for destination in self._destinations:
            reason = ship(self.files, self.name, destination)
            if reason is None:
                self.problems.pop(destination.name, None)
            else:
                self.problems[destination.name] = reason
        return not self.problems

    def _write_outcome(
        self,
        state: Literal["finished", "failed", "stopped"],
        *,
        counters: RunCounters,
        writer: str,
    ) -> None:
        DirectoryStorage(self.files.root).put(
            "outcome.json",
            RunState(state=state, counters=counters, writer=writer).model_dump_json().encode(),
        )


class RunFiles:
    """One run's local directory, and the markers of what each destination holds."""

    def __init__(self, root: Path, tail_age: float = 60.0) -> None:
        self.root = root
        self.tail_age = tail_age
        self.lock = threading.RLock()

    def run_file(self) -> RunRecord | None:
        path = self.root / "run.json"
        return RunRecord.model_validate_json(path.read_bytes()) if path.is_file() else None

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
        record = files.run_file()
        prefix = run_prefix(run_address(run, record.experiment if record else None))
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


def run_directories(directory: Path | str) -> Iterator[Path]:
    """Run roots at any depth; a run's own files are data, never nested runs."""
    for parent, folders, files in os.walk(directory):
        if "run.json" in files:
            folders.clear()
            yield Path(parent)
        else:
            folders[:] = sorted(name for name in folders if not name.startswith("."))


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
    for root in sorted(run_directories(base)):
        files = RunFiles(root, tail_age=0.0 if stopped else 60.0)
        record = files.run_file()
        run = run_address(root.name, record.experiment if record else None)
        targets = destinations(base, storages)
        prune(files, targets)
        for destination in targets:
            reason = ship(files, run, destination)
            if reason is not None:
                undelivered.setdefault(run, {})[destination.name] = reason
    return undelivered


@dataclass(frozen=True)
class Declared:
    """A declared run: its runs directory, its own folder and its declaration."""

    base: Path
    root: Path
    record: RunRecord

    @property
    def name(self) -> str:
        return self.root.name


def declare(
    *,
    experiment: str,
    clocks: Sequence[Clock],
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    cohort: str | None = None,
    kind: str | None = None,
    config: Mapping[str, object] | str | None = None,
    continues: Continuation | None = None,
    directory: Path | str | None = None,
    namespace: str = "local",
    resume: bool = False,
) -> Declared:
    """Claim a new run's `run.json`, or validate an explicitly resumed one.

    A resumed run's declaration is the stored one. Under `etude launch`, `run`,
    `job`, `seed` and `cohort` default to what the launcher handed the process
    (`ETUDE_RUN`, `ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument given
    here wins. Elsewhere `run` defaults to a new random name. Re-using a name
    is refused unless `resume`; its identity must match the first declaration.
    `config` given as text is kept byte for byte; a mapping is written as JSON.
    """
    handed = os.environ
    name = (run or handed.get("ETUDE_RUN") or secrets.token_hex(8)).removeprefix(experiment + "/")
    if seed is None and "ETUDE_SEED" in handed:
        seed = int(handed["ETUDE_SEED"])
    base, root = run_directory(directory, experiment, name)
    text = (
        config if isinstance(config, str) or config is None else json.dumps(config, sort_keys=True)
    )
    declaration = RunRecord(
        id=run_address(name, experiment),
        experiment=experiment,
        namespace=namespace,
        clocks=tuple(clocks),
        job=job or handed.get("ETUDE_JOB"),
        job_name=None if job else handed.get("ETUDE_JOB_NAME"),
        seed=seed,
        cohort=cohort or handed.get("ETUDE_COHORT"),
        kind=kind,
        config=text,
        continues=continues,
        start=handed.get("ETUDE_START"),
    )
    prior = RunFiles(root).run_file()
    if prior is not None:
        if prior.experiment != experiment:
            # Runs are kept by name alone, so a name belongs to one experiment.
            raise EtudeError("run_name_belongs_to_another_experiment", 409)
        if prior.clocks != declaration.clocks:
            raise EtudeError("run_clock_conflict", 409)
        if not resume:
            raise EtudeError("run_name_taken", detail=f"{name}; use --resume {name}")
        validate_resume(prior, config=text, seed=seed)
        return Declared(base, root, prior)
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", prefix=".run-", dir=root) as stream:
        stream.write(declaration.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())
        try:
            os.link(stream.name, root / "run.json")
        except FileExistsError:
            raise EtudeError("run_name_taken", detail=name) from None
    return Declared(base, root, declaration)


def validate_resume(prior: RunRecord, *, config: str | None, seed: int | None) -> None:
    """Refuse changed config fields or seed before an attempt can write anything."""
    differences = resume_conflicts(
        config=config,
        seed=seed,
        prior_config=prior.config_json,
        prior_seed=prior.seed,
    )
    if differences:
        raise EtudeError("run_identity_conflict", detail="; ".join(differences))


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
    roots = {folder for folders in found.values() for folder in folders}
    result: dict[str, list[str]] = {}
    for run, folders in found.items():
        kept = [
            folder
            for folder in folders
            if not any(parent.as_posix() + "/" in roots for parent in PurePosixPath(folder).parents)
        ]
        if kept:
            result[run] = kept
    return result


def under(keys: Sequence[str], prefix: str) -> Iterator[str]:
    """The keys under `prefix` of a sorted listing."""
    for key in keys[bisect_left(keys, prefix) :]:
        if not key.startswith(prefix):
            return
        yield key


def declared(storage: Storage, folder: str, listed: Container[str]) -> RunRecord | None:
    """The valid declaration a run folder holds, else None.

    The folder is asked only when its `run.json` is in `listed`, the source's listing.
    """
    if folder + "run.json" not in listed:
        return None
    try:
        return RunRecord.model_validate_json(storage.get(folder + "run.json"))
    except (FileNotFoundError, ValidationError):
        return None


def runs(source: Storage | Path | str) -> list[tuple[str, str]]:
    """Every (experiment, run) under this directory or bucket, at any depth.

    A run is listed once its declaration, `run.json`, has arrived.
    """
    storage = as_storage(source)
    keys = storage.keys("")
    return [(run.file.experiment, run.name) for run in located_runs(storage, keys)]


class LocatedRun(Record):
    """A declared identity and all copies of its files under one source."""

    name: str
    file: RunRecord
    folders: tuple[str, ...]


def located_runs(
    storage: Storage, keys: Sequence[str], *, run: str | None = None
) -> list[LocatedRun]:
    """Group copies by declared experiment and run, never by the folder name alone.

    Historical flat and machine-nested folders join the same identity, which
    keeps the first copy's declaration. Each listed declaration is read once;
    undeclared folders cannot claim an identity. Sorted by experiment and run;
    `run` keeps only folders of that name.
    """
    listed = set(keys)
    by_name = run_folders(keys)
    if run is not None:
        by_name = {run: by_name.get(run, [])}
    found: dict[tuple[str, str], tuple[RunRecord, list[str]]] = {}
    for name, folders in by_name.items():
        for folder in folders:
            file = declared(storage, folder, listed)
            if file is not None:
                found.setdefault((file.experiment, name), (file, []))[1].append(folder)
    return [
        LocatedRun(name=name, file=file, folders=tuple(folders))
        for (_, name), (file, folders) in sorted(found.items(), key=lambda item: item[0])
    ]
