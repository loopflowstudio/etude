"""The training client: `track`, `Run.log`, checkpoints, and shipping.

Ownership and invariants:

- `track` is strict and local. It validates the declaration, writes `run.json`
  and starts two daemon threads. It never touches the network.
- `log` is a put on a bounded in-memory queue. It never blocks, never does
  I/O and never raises into training code. A full queue drops the row and
  counts it; rows still in the queue when the process dies are not durable.
- The writer thread appends each row to a local log at once and keeps it by
  column in memory. Every few seconds it seals those rows as one small
  columnar block (`etude.train.block`) and removes the log. Small blocks are
  folded into recent blocks, then permanent ones by size or cadence.
  Blocks are immutable and are the unit of delivery.
- The shipper thread copies blocks, whole, to each destination, the
  author's directory or bucket (`etude.run.delivery`). A marker per
  destination records what arrived, so `sync` can finish the job later from
  the same files and replays are harmless.
- The local run directory uses the same layout as a destination, so with no
  destination configured the files are already the record.

Units: wall time is integer milliseconds since the Unix epoch. Clock values
are whatever the author's clocks count; times are seconds.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import secrets
import socket
import sys
import threading
import time
import warnings
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import chain, count
from pathlib import Path
from types import TracebackType
from typing import IO, Literal, Self

from pydantic import ValidationError

from etude.carriage import taken
from etude.run.block_file import block_key, tail_key
from etude.run.declaration import declare
from etude.run.delivery import RunFiles, StorageDestination, destinations, ship
from etude.run.errors import EtudeError
from etude.run.records import (
    ArtifactRef,
    Attempt,
    Clock,
    Continuation,
    Record,
    RunCounters,
    RunState,
    SourceKey,
)
from etude.run.storage import DirectoryStorage, Storage, run_prefix
from etude.train.block import Builder, Table, concat
from etude.train.block import encode as encode_block
from etude.train.manifest import write_manifest
from etude.train.records import CheckpointRecord, ParentCheckpoint, checkpoint_name

# Compaction is bounded by bytes or elapsed cadence, independent of training steps.
PERMANENT_BYTES, RECENT_BLOCKS = 8_000_000, 64

type Logged = Mapping[str, object]


def flatten(values: Logged, prefix: str = "") -> Iterator[tuple[str, object]]:
    for name, value in values.items():
        if isinstance(value, Mapping):
            yield from flatten(value, f"{prefix}{name}/")
        else:
            yield prefix + str(name), value


def log_line(wall_ms: int, clocks: Mapping[str, float], values: Mapping[str, object]) -> str:
    """One row of the local log: a checksum, then the row, so a torn line is detected."""
    body = json.dumps({"wall_ms": wall_ms, "clocks": clocks, "values": values})
    return f"{zlib.crc32(body.encode()):08x} {body}\n"


def write_block(root: Path, key: str, table: Table) -> int:
    """Write one block whole and durably; a reader never sees half of it."""
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    with partial.open("wb") as stream:
        stream.write(encode_block(table))
        stream.flush()
        os.fsync(stream.fileno())
    partial.replace(target)
    return target.stat().st_size


def seal_logs(root: Path, clocks: Sequence[str]) -> None:
    """Turn local logs a dead process left behind into blocks.

    Each log holds the rows of one sequence that never became a block. Lines
    are read until the first that fails its checksum, which is where a crash
    tore it. Sealing a log whose process is in fact alive is harmless: that
    process later writes the same sequence again, with more rows.
    """
    for log in sorted((root / "metrics").glob("*.open")):
        sequence, _, writer = log.stem.partition("-")
        builder = Builder(clocks)
        for line in log.read_text().splitlines():
            checksum, _, body = line.partition(" ")
            if checksum != f"{zlib.crc32(body.encode()):08x}":
                break
            row = json.loads(body)
            builder.add(row["wall_ms"], row["clocks"], row["values"])
        if len(builder):
            write_block(root, tail_key(int(sequence), writer), builder.table(writer, int(sequence)))
        log.unlink()


def _suffix(source: Path) -> str:
    """A stored checkpoint's suffix: its source's, except that JSON never takes a record's."""
    return ".json.data" if source.suffix == ".json" else source.suffix


class EtudeWarning(UserWarning):
    """A row was set aside or lost. Turn it into an error with `strict=True`."""


_STOP = object()


@dataclass(frozen=True)
class _Kept:
    """A checkpoint record for the writer, and the name it is kept under."""

    name: str
    record: CheckpointRecord


class Run:
    """A live run, created by `track`. Safe to use from one thread."""

    def __init__(
        self,
        *,
        experiment: str,
        name: str,
        namespace: str,
        root: Path,
        clocks: tuple[Clock, ...],
        destinations: Sequence[StorageDestination],
        queue_size: int,
        segment_seconds: float,
        segment_rows: int,
        strict: bool,
        inherited: CheckpointRecord | None = None,
        resumed_at: Mapping[str, float] | None = None,
    ) -> None:
        self.experiment = experiment
        self.name = name
        self.source_key = SourceKey(namespace=namespace, kind="run", source_id=name)
        self.files = RunFiles(root)
        self.clocks = clocks
        self.strict = strict
        # Rows refused by the full queue, and rows kept but not chartable.
        self.dropped = 0
        self.incomplete = 0
        # Destination name to the reason its last delivery pass did not complete.
        self.problems: dict[str, str] = {}
        self._warned: set[str] = set()
        # The highest value this process has logged for each clock.
        self._high: dict[str, float] = {}
        self._clock_names = tuple(clock.name for clock in clocks)
        # Every checkpoint recorded in this folder, by any attempt on any machine,
        # and the name its record and file are kept under.
        self._recorded: dict[int, CheckpointRecord] = {}
        self._names: dict[int, str] = {}
        for path in sorted((root / "checkpoints").glob("*.json")):
            known = CheckpointRecord.model_validate_json(path.read_bytes())
            self._recorded[known.ordinal] = known
            self._names[known.ordinal] = path.stem
        self._checkpoints = max(self._recorded, default=0)
        self._inherited = inherited
        self._destinations = list(destinations)
        self._segment_seconds = segment_seconds
        self._segment_rows = segment_rows
        self._writer_id = secrets.token_hex(6)
        # A later attempt or a continuation resumes from here, and its rows
        # supersede earlier attempts' from these coordinates on.
        if resumed_at is None:
            latest = self.latest_checkpoint()
            resumed_at = {} if latest is None else latest.at
        self._save(
            "metrics",
            self._writer_id,
            Attempt(
                writer=self._writer_id,
                started_ms=time.time_ns() // 1_000_000,
                host=socket.gethostname(),
                attempt=os.environ.get("ETUDE_ATTEMPT"),
                pod=os.environ.get("RUNPOD_POD_ID"),
                resumed_at=dict(resumed_at),
            ),
        )
        seal_logs(self.files.root, self._clock_names)  # what an earlier process left unsealed
        self._sequence = 0
        # Only the current compaction window stays in memory.
        self._tails: list[Table] = []
        self._tail_bytes = 0
        self._recent: list[Table] = []
        self._recent_bytes = 0
        self._folded_at = time.monotonic()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_size)
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._finished = False
        # Checkpoint changes not yet sealed; only the writer thread touches it.
        self._dirty = False
        # The clocks last written to the launcher's progress file.
        self._progress: dict[str, float] = {}
        self._writer = threading.Thread(target=self._write, name="etude-writer", daemon=True)
        self._shipper = threading.Thread(target=self._deliver, name="etude-shipper", daemon=True)
        self._writer.start()
        self._shipper.start()
        atexit.register(self.finish)

    # --- the training-side calls: no I/O, no blocking, no exceptions ---------

    def log(self, values: Logged | None = None, /, **clocks: float) -> None:
        """Record one row. Every declared clock must be present, as a key or keyword."""
        try:
            row = {**(values or {}), **clocks}
            for name in self._clock_names:
                value = row.get(name)
                if value is None:
                    if self.strict:
                        raise EtudeError("row_missing_clock", 422)
                    self._warn("incomplete", f"a logged row has no value for clock {name!r}")
                elif isinstance(value, (int, float)):
                    high = self._high.get(name)
                    if high is not None and value < high:
                        # Within one process a clock only moves forward; a resume
                        # from an earlier checkpoint is a new process and is fine.
                        if self.strict:
                            raise EtudeError("clock_went_backwards", 422)
                        self._warn(
                            "backwards",
                            f"a logged row's clock {name!r} went backwards, from {high} to {value}",
                        )
                    else:
                        self._high[name] = value
            self._queue.put_nowait((time.time_ns() // 1_000_000, row))
        except queue.Full:
            self._lost()
        except Exception:
            if self.strict:
                raise
            self._lost()

    def _warn(self, kind: str, message: str, stacklevel: int = 3) -> None:
        """Say once per run, at the caller's line, that rows are being set aside."""
        if kind in self._warned:
            return
        self._warned.add(kind)
        try:
            warnings.warn(
                f"etude run {self.name}: {message}.",
                EtudeWarning,
                stacklevel=stacklevel,
            )
        except Exception:  # noqa: BLE001, S110 - a warnings filter must not break training
            pass

    def _lost(self) -> None:
        self.dropped += 1
        self._warn("dropped", "the log queue is full and a row was dropped", stacklevel=4)

    def checkpoint(
        self,
        artifact: ArtifactRef | Path | str,
        at: Mapping[str, float] | None = None,
        /,
        *,
        tags: Sequence[str] = (),
        **clocks: float,
    ) -> ArtifactRef:
        """Store a saved model and record where every declared clock stood.

        Given a local file, Etude copies it into the run's directory, records
        its digest and size, and delivers it to the author's bucket in the
        background like every other run file. The copy is made before this
        returns, so the caller may overwrite or delete its own file at once;
        the call takes about as long as writing the file did. Given an address
        such as `s3://...` or an `ArtifactRef`, only the reference is recorded.

        Recording is idempotent and nothing recorded is replaced. The same
        model at the same coordinate returns the existing record's reference.
        A different one at a recorded coordinate is kept as a new version,
        `CLOCK-VALUE.ordinal-N`, beside the first.

        Returns the model's reference: pass it to whatever evaluates the model.
        Missing a clock, or an unreadable file, the checkpoint is not recorded.
        """
        staged: Path | None = None
        try:
            coordinate = {**(at or {}), **clocks}
            if set(coordinate) != set(self._clock_names):
                raise ValueError("checkpoint_clock_mismatch")
            at = {name: float(coordinate[name]) for name in self._clock_names}
            name = checkpoint_name(at)
            reference, staged = self._stage(artifact, name)
            for known in self._recorded.values():
                same = (known.artifact.sha256 or known.artifact.uri) == (
                    reference.sha256 or reference.uri
                )
                if known.at == at and same:
                    if staged is not None:
                        staged.unlink()
                    return known.artifact
            ordinal = self._checkpoints + 1
            stored = staged is not None and not isinstance(artifact, ArtifactRef)
            suffix = (
                "" if isinstance(artifact, ArtifactRef) or not stored else _suffix(Path(artifact))
            )
            name = self._unused(name, suffix, ordinal)
            if staged is not None and stored:
                reference = self._keep(staged, name + suffix, reference)
            record = CheckpointRecord(
                ordinal=ordinal,
                artifact=reference,
                at=at,
                wall_ms=time.time_ns() // 1_000_000,
                tags=tuple(dict.fromkeys(tags)),
            )
        except (ValidationError, ValueError, TypeError, OSError) as error:
            if staged is not None:
                staged.unlink(missing_ok=True)
            if self.strict:
                raise
            self.incomplete += 1
            self._warn("checkpoint", f"a checkpoint was not recorded ({error})")
            return artifact if isinstance(artifact, ArtifactRef) else ArtifactRef(uri=str(artifact))
        self._checkpoints = ordinal
        self._recorded[ordinal] = record
        self._names[ordinal] = name
        self._offer(_Kept(name, record))
        return record.artifact

    def latest_checkpoint(self) -> CheckpointRecord | None:
        """The checkpoint to resume from: the latest this run recorded, on any attempt.

        A later attempt sees every earlier attempt's records, restored to a
        fresh machine before the job starts, and its file at the record's
        `artifact.uri`, which is on this machine. A continuation gets its
        parent's checkpoint until it records its own. None when there is none.
        """
        if not self._recorded:
            return self._inherited
        record = self._recorded[max(self._recorded)]
        # The file keeps its name wherever the record was written.
        local = self.files.root / "checkpoints" / record.artifact.uri.rpartition("/")[2]
        if record.artifact.sha256 is None or not local.is_file():
            return record
        artifact = record.artifact.model_copy(update={"uri": local.resolve().as_uri()})
        return record.model_copy(update={"artifact": artifact})

    def _stage(
        self, artifact: ArtifactRef | Path | str, name: str
    ) -> tuple[ArtifactRef, Path | None]:
        """The reference to record; a local file is first copied in and digested.

        The copy is staged under a name no carrier sends until it is kept.
        """
        if isinstance(artifact, ArtifactRef):
            return artifact, None
        if isinstance(artifact, str) and "://" in artifact:
            return ArtifactRef(uri=artifact), None
        source = Path(artifact)
        folder = self.files.root / "checkpoints"
        folder.mkdir(parents=True, exist_ok=True)
        staged = folder / f"{name}.{secrets.token_hex(4)}.partial"
        digest = sha256()
        # A copy, not a link: a trainer that saves over the same path would
        # otherwise change the recorded model after the fact.
        try:
            with source.open("rb") as reader, staged.open("wb") as writer:
                while block := reader.read(8 * 1024 * 1024):
                    digest.update(block)
                    writer.write(block)
        except OSError:
            staged.unlink(missing_ok=True)
            raise
        return (
            ArtifactRef(uri="", sha256=digest.hexdigest(), size_bytes=staged.stat().st_size),
            staged,
        )

    def _unused(self, name: str, suffix: str, ordinal: int) -> str:
        """The first of `name`, `name.ordinal-N`, `name.ordinal-N-2`, ... no record or file holds.

        A file counts even without a record, here or where storage or another
        host keeps it (`etude.carriage.taken`), so nothing is ever replaced.
        """
        files, records = taken(self.files.root), set(self._names.values())
        versions = (f"{name}.ordinal-{ordinal}-{n}" for n in count(2))
        candidates = chain((name, f"{name}.ordinal-{ordinal}"), versions)
        return next(c for c in candidates if c not in records and c + suffix not in files)

    def _keep(self, staged: Path, filename: str, reference: ArtifactRef) -> ArtifactRef:
        """Give a staged copy its recorded name, which nothing has used before."""
        kept = staged.with_name(filename)
        staged.replace(kept)
        key = kept.relative_to(self.files.root).as_posix()
        remote = next(iter(self._destinations), None)
        uri = remote.storage.uri(run_prefix(self.name) + key) if remote else kept.resolve().as_uri()
        return reference.model_copy(update={"uri": uri})

    def tag(self, checkpoint: ArtifactRef | int, *tags: str) -> None:
        """Add labels to a checkpoint this run recorded, by reference or ordinal."""
        self._retag(checkpoint, tags, ())

    def untag(self, checkpoint: ArtifactRef | int, *tags: str) -> None:
        self._retag(checkpoint, (), tags)

    def _retag(
        self, checkpoint: ArtifactRef | int, add: Sequence[str], remove: Sequence[str]
    ) -> None:
        try:
            record = (
                self._recorded.get(checkpoint)
                if isinstance(checkpoint, int)
                else next(
                    (
                        known
                        for known in self._recorded.values()
                        # The same model: the digest, else the URI.
                        if (known.artifact.sha256 or known.artifact.uri)
                        == (checkpoint.sha256 or checkpoint.uri)
                    ),
                    None,
                )
            )
            if record is None:
                raise EtudeError("checkpoint_not_recorded", 404)
            kept = tuple(t for t in dict.fromkeys((*record.tags, *add)) if t not in remove)
            changed = CheckpointRecord.model_validate(record.model_dump() | {"tags": kept})
        except (ValidationError, EtudeError) as error:
            if self.strict:
                raise
            self.incomplete += 1
            self._warn("tag", f"a checkpoint was not tagged ({error})")
            return
        self._recorded[changed.ordinal] = changed
        self._offer(_Kept(self._names[changed.ordinal], changed))

    def _offer(self, item: object) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._lost()

    # --- the writer thread ---------------------------------------------------

    def _write(self) -> None:
        stream: IO[str] | None = None
        opened_at = 0.0
        builder = Builder(self._clock_names)

        def seal(final: bool = False) -> None:
            nonlocal stream, builder
            if stream is not None:
                table = builder.table(self._writer_id, self._sequence)
                self._tail_bytes += write_block(
                    self.files.root, tail_key(self._sequence, self._writer_id), table
                )
                stream.close()
                Path(stream.name).unlink()  # The block now holds these rows.
                self._tails.append(table)
                stream, builder = None, Builder(self._clock_names)
                self._sequence += 1
            self._fold(final)
            self._report_progress()
            self._dirty = False
            self._wake.set()

        while True:
            try:
                item = self._queue.get(timeout=self._segment_seconds / 4)
            except queue.Empty:
                item = None
            try:
                if item is _STOP:
                    seal(final=True)
                    return
                if isinstance(item, _Kept):
                    self._save("checkpoints", item.name, item.record)
                    self._dirty = True
                elif isinstance(item, tuple):
                    if stream is None:
                        directory = self.files.root / "metrics"
                        directory.mkdir(parents=True, exist_ok=True)
                        name = f"{self._sequence:08d}-{self._writer_id}.open"
                        stream = (directory / name).open("a")
                        opened_at = time.monotonic()
                    coordinate, values = self._row(item[1])
                    stream.write(log_line(item[0], coordinate, values))
                    stream.flush()
                    builder.add(item[0], coordinate, values)
                due = stream is not None and (
                    len(builder) >= self._segment_rows
                    or time.monotonic() - opened_at >= self._segment_seconds
                )
                if due or (item is None and self._dirty):
                    seal()
                elif item is None:
                    self._fold()
            except Exception as error:  # noqa: BLE001 - the writer must outlive bad input
                self.problems["writer"] = type(error).__name__
                self.incomplete += 1

    def _fold(self, final: bool = False) -> None:
        """Seal recent blocks, then compact them before removing covered files."""
        if not self._tails and not (final and self._recent):
            return
        if (
            not final
            and self._tail_bytes < PERMANENT_BYTES
            and time.monotonic() - self._folded_at < 8 * self._segment_seconds
        ):
            return
        with self.files.lock:
            recent = list(self._recent)
            size = self._recent_bytes
            if self._tails:
                table = concat(self._tails)
                recent.append(table)
                if not final:
                    key = block_key(1, table.first, table.last, table.writer)
                    size += write_block(self.files.root, key, table)
            compact = final or size >= PERMANENT_BYTES or len(recent) >= RECENT_BLOCKS
            if compact:
                table = concat(recent)
                write_block(
                    self.files.root, block_key(2, table.first, table.last, table.writer), table
                )
            write_manifest(self.files.root, self._writer_id, permanent=compact)
            retired = [tail_key(tail.first, tail.writer) for tail in self._tails]
            if compact:
                retired.extend(
                    block_key(1, table.first, table.last, table.writer) for table in recent
                )
            retired = [key for key in retired if (self.files.root / key).is_file()]
            # The replacement and manifest exist before the append. Couriers need
            # only these complete lines, never knowledge of block names or bytes.
            # A tail is listed only if it was old enough to have been delivered:
            # a live writer folds its tails long before that, and listing every
            # one would grow this file by a line every few seconds for ever.
            delivered = [
                key
                for key in retired
                if not key.endswith(".tail")
                or time.time() - (self.files.root / key).stat().st_mtime >= self.files.tail_age
            ]
            if delivered:
                note = self.files.root / "metrics" / f"{self._writer_id}.removed"
                with note.open("a") as removed:
                    removed.write("".join(key + "\n" for key in delivered))
                    removed.flush()
            for key in retired:
                (self.files.root / key).unlink(missing_ok=True)
            self._recent = [] if compact else recent
            self._recent_bytes = 0 if compact else size
            self._tails.clear()
            self._tail_bytes = 0
            self._folded_at = time.monotonic()

    def _report_progress(self) -> None:
        """Under `etude launch`, say how far the run has got, for the launcher's status.

        `etude-result.json` in `ETUDE_OUTPUT_DIR` holds the highest value
        logged on each clock. It is progress only: checkpoints are this
        run's records, never the launcher's.
        """
        outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        high = dict(self._high)
        if outputs is None or high == self._progress:
            return
        DirectoryStorage(Path(outputs)).put(
            "etude-result.json", json.dumps({"clocks": high}).encode()
        )
        self._progress = high

    def _row(self, row: Logged) -> tuple[dict[str, float], dict[str, object]]:
        """A logged row as its clock values and everything else, ready to store."""
        values: dict[str, object] = {}
        for name, value in flatten(row):
            if value is None or isinstance(value, (bool, str, int, float)):
                values[name] = value
            else:
                try:
                    values[name] = float(value)  # type: ignore[arg-type]  # numpy and tensor scalars
                except (TypeError, ValueError):
                    values[name] = type(value).__name__
        coordinate: dict[str, float] = {}
        for name in self._clock_names:
            value = values.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(value):
                    coordinate[name] = float(values.pop(name))  # type: ignore[arg-type]
                else:
                    # Not a finite number: stays among the values as written.
                    self._warn(
                        "incomplete", f"a logged row's clock {name!r} is not a finite number"
                    )
        if len(coordinate) != len(self._clock_names):
            # Charted against the clocks it does give, and wall time; counted.
            self.incomplete += 1
        return coordinate, values

    def _save(self, folder: str, name: str, record: Record) -> None:
        """Write one small record whole, so a reader never sees half of it."""
        DirectoryStorage(self.files.root).put(
            f"{folder}/{name}.json", record.model_dump_json().encode()
        )

    # --- the shipper thread --------------------------------------------------

    def _deliver(self) -> None:
        delay = self._segment_seconds
        while not self._stopping.is_set():
            self._wake.wait(timeout=delay)
            self._wake.clear()
            # Back off while a destination is down; sealed files keep accumulating.
            delay = self._segment_seconds if self.ship() else min(delay * 2, 60.0)
        self.ship()

    def ship(self) -> bool:
        """One delivery pass over every destination. True when all are current."""
        for destination in self._destinations:
            reason = ship(self.files, self.name, destination)
            if reason is None:
                self.problems.pop(destination.name, None)
            else:
                self.problems[destination.name] = reason
        return not any(d.name in self.problems for d in self._destinations)

    # --- ending --------------------------------------------------------------

    def finish(
        self, state: Literal["finished", "failed"] = "finished", *, timeout: float = 10.0
    ) -> None:
        """Seal, record the final state and deliver, within `timeout` seconds."""
        if self._finished:
            return
        self._finished = True
        deadline = time.monotonic() + timeout
        try:
            try:
                self._queue.put(_STOP, timeout=timeout)
            except queue.Full:
                self.dropped += 1
            self._writer.join(max(0.0, deadline - time.monotonic()))
            counters = RunCounters(dropped=self.dropped, incomplete=self.incomplete)
            DirectoryStorage(self.files.root).put(
                "state.json",
                RunState(state=state, counters=counters, writer=self._writer_id)
                .model_dump_json()
                .encode(),
            )
            self._stopping.set()
            self._wake.set()
            self._shipper.join(max(0.0, deadline - time.monotonic()))
            undelivered = self._shipper.is_alive() or bool(self.problems)
            if undelivered or self.dropped or self.incomplete:
                print(
                    f"etude: {self.dropped} rows dropped, {self.incomplete} incomplete."
                    + (
                        f" Not delivered: {self.problems or 'still sending'}. Files are in "
                        f"{self.files.root}; deliver them with `etude sync`."
                        if undelivered
                        else ""
                    ),
                    file=sys.stderr,
                )
        except Exception as error:  # noqa: BLE001 - finishing must never fail training
            print(f"etude: finish did not complete ({type(error).__name__})", file=sys.stderr)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.finish("failed" if error is not None else "finished")


def track(
    *,
    experiment: str | None = None,
    clocks: Sequence[Clock],
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    cohort: str | None = None,
    kind: str | None = None,
    config: Mapping[str, object] | str | None = None,
    continues: Continuation | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    namespace: str = "local",
    queue_size: int = 10_000,
    segment_seconds: float = 2.0,
    segment_rows: int = 5_000,
    strict: bool = False,
    resumed_at: Mapping[str, float] | None = None,
) -> Run:
    """Declare or resume a run. Strict: a bad declaration raises here, not later.

    The experiment is the one `etude launch` started the run for
    (`ETUDE_EXPERIMENT`, the experiment file's stem) unless named here. Naming
    another raises `EtudeError("experiment_differs_from_launch")`; naming none
    outside such a launch raises `EtudeError("experiment_not_named")`. The
    run's identity, and whether it resumes, is settled by
    `etude.run.declaration.declare`. Under `etude launch` the files are
    written to `ETUDE_TRACK_DIR`, which the launcher carries to storage as they
    are, and a later attempt finds the run's checkpoints there
    (`Run.latest_checkpoint`). A continuation (`etude.jobs.continue_from`) is
    handed its parent's checkpoint in `ETUDE_CONTINUES`: unless `continues` is
    given, the run declares that it continues its parent from there.

    A resumed run's rows replace earlier attempts' from its latest checkpoint
    on. A process that restored its state from somewhere else, such as a
    snapshot of its own taken after that checkpoint, passes where it stands
    as `resumed_at`, keyed by declared clocks.
    """
    launched = os.environ.get("ETUDE_EXPERIMENT") or None
    if experiment is not None and launched not in (None, experiment):
        raise EtudeError("experiment_differs_from_launch", 409) from ValueError(
            f"track(experiment={experiment!r}) in a run etude launch started for {launched!r}"
        )
    experiment = experiment or launched
    if experiment is None:
        raise EtudeError("experiment_not_named", 422)
    if resumed_at is not None and any(
        clock not in {c.name for c in clocks} or not math.isfinite(value)
        for clock, value in resumed_at.items()
    ):
        raise EtudeError("resumed_at_unknown_clock", 422)
    inherited = _inherited()
    if inherited is not None and continues is None:
        parent, record = inherited
        until = os.environ.get("ETUDE_UNTIL_CLOCK")
        clock = until if until in record.at else next(iter(record.at))
        continues = Continuation(parent=parent, clock=clock, value=record.at[clock])
    declared = declare(
        experiment=experiment,
        clocks=clocks,
        run=run,
        job=job,
        seed=seed,
        cohort=cohort,
        kind=kind,
        config=config,
        continues=continues,
        directory=directory,
        namespace=namespace,
    )
    return Run(
        experiment=experiment,
        name=declared.name,
        namespace=namespace,
        root=declared.root,
        clocks=declared.declaration.clocks,
        destinations=destinations(declared.base, storages),
        queue_size=queue_size,
        segment_seconds=segment_seconds,
        segment_rows=segment_rows,
        strict=strict,
        inherited=None if inherited is None else inherited[1],
        resumed_at=resumed_at,
    )


def _inherited() -> tuple[SourceKey, CheckpointRecord] | None:
    """The parent's checkpoint a continuation was handed, with its file on this machine."""
    given = os.environ.get("ETUDE_CONTINUES")
    if not given:
        return None
    parent = ParentCheckpoint.model_validate_json(given)
    record = parent.checkpoint
    if parent.input is not None:
        folder = os.environ.get("ETUDE_INPUTS_DIR")
        if not folder:
            raise EtudeError("continuation_input_missing", 422)
        local = (Path(folder) / parent.input).resolve().as_uri()
        record = record.model_copy(
            update={"artifact": record.artifact.model_copy(update={"uri": local})}
        )
    return parent.parent, record


def seal_runs(directory: Path | str) -> None:
    """Turn the local logs dead processes left in every run under `directory` into blocks."""
    for root in sorted(path.parent for path in Path(directory).glob("*/run.json")):
        declared = RunFiles(root).run_file()
        if declared is not None:
            seal_logs(root, [clock.name for clock in declared.declaration.clocks])


__all__ = [
    "EtudeWarning",
    "Run",
    "seal_runs",
    "track",
]
