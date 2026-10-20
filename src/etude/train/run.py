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
import shutil
import socket
import sys
import threading
import time
import warnings
import zlib
from collections.abc import Iterator, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import IO, Literal, Self

from pydantic import ValidationError

from etude.run.block_file import block_key, tail_key
from etude.run.delivery import RunFiles, StorageDestination, destinations, ship
from etude.run.errors import EtudeError
from etude.run.records import (
    ArtifactRef,
    Attempt,
    Clock,
    Continuation,
    Record,
    RunCounters,
    RunDeclaration,
    RunFile,
    RunState,
    SourceKey,
)
from etude.run.storage import DirectoryStorage, Storage, run_directory, run_prefix
from etude.train.block import Builder, Table, concat
from etude.train.block import encode as encode_block
from etude.train.manifest import write_manifest
from etude.train.records import CheckpointRecord, checkpoint_name

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


class EtudeWarning(UserWarning):
    """A row was set aside or lost. Turn it into an error with `strict=True`."""


_STOP = object()


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
        self._recorded: dict[int, CheckpointRecord] = {}
        for path in sorted((root / "checkpoints").glob("*.json")):
            known = CheckpointRecord.model_validate_json(path.read_bytes())
            self._recorded[known.ordinal] = known
        self._checkpoints = max(self._recorded, default=0)
        # The file this process last put in the launcher's output directory.
        self._reported: Path | None = None
        self._destinations = list(destinations)
        self._segment_seconds = segment_seconds
        self._segment_rows = segment_rows
        self._writer_id = secrets.token_hex(6)
        resumed_at: dict[str, float] = {}
        if (start := os.environ.get("ETUDE_FROM_VALUE")) is not None:
            clock = os.environ.get("ETUDE_UNTIL_CLOCK")
            if clock is None:
                raise ValueError("ETUDE_FROM_VALUE requires ETUDE_UNTIL_CLOCK")
            resumed_at[clock] = float(start)
        self._save(
            "metrics",
            self._writer_id,
            Attempt(
                writer=self._writer_id,
                started_ms=time.time_ns() // 1_000_000,
                host=socket.gethostname(),
                attempt=os.environ.get("ETUDE_ATTEMPT"),
                pod=os.environ.get("RUNPOD_POD_ID"),
                resumed_at=resumed_at,
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

        Returns the model's reference: pass it to whatever evaluates the model.
        Missing a clock, or an unreadable file, the checkpoint is not recorded.
        """
        try:
            coordinate = {**(at or {}), **clocks}
            if set(coordinate) != set(self._clock_names):
                raise ValueError("checkpoint_clock_mismatch")
            ordinal = self._checkpoints + 1
            at = {name: float(coordinate[name]) for name in self._clock_names}
            record = CheckpointRecord(
                ordinal=ordinal,
                artifact=self._stored(artifact, checkpoint_name(at)),
                at=at,
                wall_ms=time.time_ns() // 1_000_000,
                tags=tuple(dict.fromkeys(tags)),
            )
        except (ValidationError, ValueError, TypeError, OSError) as error:
            if self.strict:
                raise
            self.incomplete += 1
            self._warn("checkpoint", f"a checkpoint was not recorded ({error})")
            return artifact if isinstance(artifact, ArtifactRef) else ArtifactRef(uri=str(artifact))
        self._checkpoints = ordinal
        self._recorded[ordinal] = record
        self._offer(record)
        self._tell_launcher(artifact, record.at)
        return record.artifact

    def _tell_launcher(self, artifact: ArtifactRef | Path | str, at: Mapping[str, float]) -> None:
        """Under `etude launch`, report this checkpoint the way the launcher reads it.

        The launcher resumes a run from the checkpoint named in
        `etude-result.json` in the job's output directory, at the clocks beside
        it. Writing that here means a script reports a checkpoint once. A file
        is linked from the tracker's snapshot into the output directory, since
        the launcher only carries files from there. The trainer may overwrite
        its own file without changing this snapshot. Only checkpoints are reported: the file holds
        one set of clocks, so later progress written with an earlier
        checkpoint's name would misstate where that checkpoint stood.
        """
        outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        if outputs is None or isinstance(artifact, ArtifactRef) or "://" in str(artifact):
            return
        try:
            directory = Path(outputs).resolve()
            source = self._checkpoint_path(artifact, checkpoint_name(dict(at)))
            name = source.name
            target = directory / name
            if target != source:
                partial = directory / f"{name}.partial"
                partial.unlink(missing_ok=True)
                try:
                    os.link(source, partial)
                except OSError:  # another file system, or one without links
                    shutil.copyfile(source, partial)
                partial.replace(target)
                # Only the latest checkpoint is reported; the name made for the
                # one before is ours to remove, so outputs do not pile up.
                if self._reported is not None and self._reported != target:
                    self._reported.unlink(missing_ok=True)
                self._reported = target
            result = directory / "etude-result.json"
            partial = directory / "etude-result.json.partial"
            partial.write_text(json.dumps({"clocks": dict(at), "checkpoint": name}))
            partial.replace(result)
        except OSError as error:
            if self.strict:
                raise
            self._warn("checkpoint", f"the launcher was not told of a checkpoint ({error})")

    def _stored(self, artifact: ArtifactRef | Path | str, name: str) -> ArtifactRef:
        """The reference to record; a local file is first copied in and digested."""
        if isinstance(artifact, ArtifactRef):
            return artifact
        if isinstance(artifact, str) and "://" in artifact:
            return ArtifactRef(uri=artifact)
        source = Path(artifact)
        staged = self._checkpoint_path(source, name)
        key = staged.relative_to(self.files.root).as_posix()
        staged.parent.mkdir(parents=True, exist_ok=True)
        partial = staged.with_name(staged.name + ".partial")
        digest = sha256()
        # A copy, not a link: a trainer that saves over the same path would
        # otherwise change the recorded model after the fact.
        with source.open("rb") as reader, partial.open("wb") as writer:
            while block := reader.read(8 * 1024 * 1024):
                digest.update(block)
                writer.write(block)
        partial.replace(staged)
        remote = next(iter(self._destinations), None)
        return ArtifactRef(
            uri=remote.storage.uri(run_prefix(self.name) + key)
            if remote
            else staged.resolve().as_uri(),
            sha256=digest.hexdigest(),
            size_bytes=staged.stat().st_size,
        )

    def _checkpoint_path(self, source: Path | str, name: str) -> Path:
        suffix = Path(source).suffix
        # JSON checkpoints must not overwrite the coordinate's JSON record.
        if suffix == ".json":
            suffix += ".data"
        filename = name + suffix
        outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        if outputs is not None and Path(outputs).resolve() / filename == Path(source).resolve():
            # The launcher's link must not replace the trainer's own path:
            # its next in-place save would then mutate our snapshot too.
            filename += ".snapshot"
        return self.files.root / "checkpoints" / filename

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
        self._offer(changed)

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
                if isinstance(item, CheckpointRecord):
                    self._save("checkpoints", checkpoint_name(item.at), item)
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
    storages: Sequence[Storage] = (),
    namespace: str = "local",
    queue_size: int = 10_000,
    segment_seconds: float = 2.0,
    segment_rows: int = 5_000,
    strict: bool = False,
) -> Run:
    """Declare or resume a run. Strict: a bad declaration raises here, not later.

    The experiment is always named here; the launcher does not know it. Under
    `etude launch` the launcher has already said which run this is, so `run`,
    `job`, `seed` and `cohort` default to what it handed the process
    (`ETUDE_RUN`, `ETUDE_JOB`, `ETUDE_SEED`, `ETUDE_COHORT`); an argument given
    here wins. There, `Run.checkpoint` also tells the launcher, and the files
    are written to `ETUDE_TRACK_DIR`, which the launcher keeps with the job's log. Elsewhere `run` defaults to a new random name.
    Re-using a name resumes it; its clocks must match the first declaration.
    `ETUDE_FROM_VALUE` on `ETUDE_UNTIL_CLOCK` records the checkpoint resume
    coordinate, from which earlier attempts' points are superseded on read.
    `config` given as text is kept byte for byte; a mapping is written as JSON.
    """
    handed = os.environ
    name = run or handed.get("ETUDE_RUN") or secrets.token_hex(8)
    if seed is None and "ETUDE_SEED" in handed:
        seed = int(handed["ETUDE_SEED"])
    base, root = run_directory(directory, experiment, name)
    text = (
        config if isinstance(config, str) or config is None else json.dumps(config, sort_keys=True)
    )
    declaration = RunDeclaration(
        source_key=SourceKey(namespace=namespace, kind="run", source_id=name),
        clocks=tuple(clocks),
        job=job or handed.get("ETUDE_JOB"),
        job_name=None if job else handed.get("ETUDE_JOB_NAME"),
        seed=seed,
        cohort=cohort or handed.get("ETUDE_COHORT"),
        kind=kind,
        config_json=text,
        config_sha256=sha256(text.encode()).hexdigest() if text is not None else None,
        continues=continues,
    )
    files = RunFiles(root)
    prior = files.run_file()
    if prior is not None:
        if prior.experiment != experiment:
            # Runs are kept by name alone, so a name belongs to one experiment.
            raise EtudeError("run_name_belongs_to_another_experiment", 409)
        if prior.declaration.clocks != declaration.clocks:
            raise EtudeError("run_clock_conflict", 409)
    else:
        root.mkdir(parents=True, exist_ok=True)
        (root / "run.json").write_text(
            RunFile(
                experiment=experiment, namespace=namespace, declaration=declaration
            ).model_dump_json()
        )
    return Run(
        experiment=experiment,
        name=name,
        namespace=namespace,
        root=root,
        clocks=declaration.clocks,
        destinations=destinations(base, storages),
        queue_size=queue_size,
        segment_seconds=segment_seconds,
        segment_rows=segment_rows,
        strict=strict,
    )


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
