"""The training client: `track`, scalar and table logging, checkpoints, and shipping.

Ownership and invariants:

- `track` is strict and local. It validates the declaration, writes `run.json`
  and starts two daemon threads. It never touches the network.
- `log` declares first-use keyword clocks before queueing their rows. With
  an established clock set it only queues: overflow drops and counts the row.
  `strict` raises for malformed coordinates; otherwise the writer warns once.
  Rows still queued when the process dies are not durable.
- `log_table` serializes JSON rows to a local immutable artifact, then queues
  its digest through `log`. Its local I/O and validation errors are visible.
- The writer thread appends each row to a local log at once and keeps it by
  column in memory. Every few seconds it seals those rows as one small
  columnar block (`etude.train.block`) and removes the log. Small blocks are
  folded into recent blocks, then permanent ones by size or cadence.
  Blocks are immutable and are the unit of delivery.
- The shipper thread copies blocks, whole, to each destination, the
  author's directory or bucket (`etude.run`). A marker per
  destination records what arrived, so `sync` can finish the job later from
  the same files and replays are harmless.
- The local run's folder uses the same layout as a destination, so with no
  destination configured the files are already the record.

Units: wall time is integer milliseconds since the Unix epoch. Clock values
are whatever the author's clocks count; times are seconds. Etude adds its two
clocks in hours (`etude.records.BUILT_IN_CLOCKS`) to every row and
checkpoint: `running_hours` continues from the saved state's value (or an
older run's checkpoint when it has no saved state) by
this process's monotonic time, `hours_since_start` is wall time since the
line's first attempt began.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import warnings
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest, sha256
from itertools import chain, count
from pathlib import Path
from types import TracebackType
from typing import IO, Literal, Self, overload
from urllib.parse import unquote

from pydantic import BaseModel, ValidationError

from etude.block_file import block_key, log_key, sealed_as, tail_key
from etude.cache import Content
from etude.carriage import OUT_PRESENT, checkpoint_key, note_retired, taken
from etude.checkpoints import CheckpointRecord
from etude.deadline import limit_time
from etude.errors import EtudeError
from etude.records import (
    BUILT_IN_NAMES,
    HOURS_SINCE_START,
    RUNNING_HOURS,
    ArtifactRef,
    Clock,
    Continuation,
    Record,
    RunCounters,
    RunWriter,
)
from etude.run import (
    Run,
    RunFiles,
    StorageDestination,
    declare,
    declare_clock,
    destinations,
    prune,
    report_progress,
    run_directories,
)
from etude.stopping import Stopped
from etude.storage import DirectoryStorage, Storage, run_prefix
from etude.train.block import Builder, Columns, Json, LoggedRow, concat
from etude.train.block import encode as encode_block
from etude.train.manifest import Index, metric_names
from etude.train.records import ParentRun, RowTable, checkpoint_name
from etude.train.state import State, saved_states, state_folder, write_objects

# Compaction is bounded by bytes or elapsed cadence, independent of training steps.
PERMANENT_BYTES, RECENT_BLOCKS = 8_000_000, 64

type Logged = Mapping[str, object]


def flatten(values: Logged, prefix: str = "") -> Iterator[tuple[str, object]]:
    for name, value in values.items():
        if isinstance(value, Mapping):
            yield from flatten(value, f"{prefix}{name}/")
        else:
            yield prefix + str(name), value


def log_line(row: LoggedRow) -> str:
    """One row of the local log: a checksum, then the row, so a torn line is detected."""
    body = row.model_dump_json()
    return f"{zlib.crc32(body.encode()):08x} {body}\n"


def write_block(root: Path, key: str, columns: Columns) -> int:
    """Write one block whole and durably; a reader never sees half of it."""
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    with partial.open("wb") as stream:
        stream.write(encode_block(columns))
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
        tail = sealed_as(log.relative_to(root).as_posix())
        if tail is None:
            continue  # not a log a writer opened
        builder = Builder(clocks)
        for line in log.read_text().splitlines():
            checksum, _, body = line.partition(" ")
            if checksum != f"{zlib.crc32(body.encode()):08x}":
                break
            row = LoggedRow.model_validate_json(body)
            builder.add(row.wall_ms, row.clocks, row.values)
        if len(builder):
            write_block(root, tail.key, builder.columns(tail.writer, tail.first))
        log.unlink()


def _same_content(a: ArtifactRef, b: ArtifactRef) -> bool:
    """Whether two artifacts hold the same content: by digest, else by address."""
    return (a.sha256 or a.uri) == (b.sha256 or b.uri)


def _suffix(source: Path) -> str:
    """A stored checkpoint's suffix: its source's, except that JSON never takes a record's."""
    return ".json.data" if source.suffix == ".json" else source.suffix


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _folder_artifact(folder: Path) -> ArtifactRef:
    members: dict[str, Content] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("a checkpoint must be self-contained, without symbolic links")
        if path.is_file():
            key = path.relative_to(folder)
            if any(part.startswith(".") for part in key.parts) or path.suffix in (
                ".open",
                ".partial",
            ):
                raise ValueError(
                    "checkpoint files must use portable published names, without private or unfinished files"
                )
            with path.open("rb") as stream:
                content = Content(
                    sha256=file_digest(stream, "sha256").hexdigest(),
                    size_bytes=os.fstat(stream.fileno()).st_size,
                )
                os.fsync(stream.fileno())
            members[key.as_posix()] = content
    if not members:
        raise ValueError("a checkpoint folder must hold files")
    manifest = json.dumps(
        {name: content.model_dump() for name, content in members.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ArtifactRef(
        uri=folder.as_uri(),
        sha256=sha256(manifest.encode()).hexdigest(),
        size_bytes=sum(c.size_bytes for c in members.values()),
        members=members,
    )


class EtudeWarning(UserWarning):
    """Logging needs attention: lost rows, invalid clocks or excessive metric names."""


@contextmanager
def local_launch(*, out: Path | str, experiment: str) -> Iterator[None]:
    """Call a recipe locally with one persistent directory and experiment.

    Environment settings are restored on exit, including after an exception.
    Like process environment itself, this context is not thread-local.
    """
    warnings.warn(
        "local_launch is replaced by etude train; removed in 0.5", DeprecationWarning, stacklevel=2
    )
    folder = Path(out).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    launch = {
        "ETUDE_OUT": str(folder),
        "ETUDE_EXPERIMENT": experiment,
        "ETUDE_RUN": re.sub(r"[^A-Za-z0-9_.-]", "-", folder.name),
        "ETUDE_TRACK_DIR": str(folder / "etude"),
        "ETUDE_RESUME": "1",
    }
    previous = {key: os.environ.get(key) for key in launch}
    os.environ.update(launch)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_STOP = object()


@dataclass(frozen=True)
class _Row:
    """A logged row for the writer: when it was logged, and everything it gave."""

    wall_ms: int
    values: Logged


@dataclass(frozen=True)
class _Clocks:
    names: tuple[str, ...]


@dataclass(frozen=True)
class _Kept:
    """A checkpoint record and its stored name, also queued to the writer."""

    name: str
    record: CheckpointRecord


class TrainingRun(Run):
    """A live run, created by `track`. Safe to use from one thread."""

    def __init__(
        self,
        *,
        experiment: str,
        name: str,
        root: Path,
        clocks: tuple[Clock, ...],
        destinations: Sequence[StorageDestination],
        queue_size: int,
        segment_seconds: float,
        segment_rows: int,
        strict: bool,
        closed_clocks: bool,
        metric_name_limit: int | None = 1000,
        inherited: CheckpointRecord | None = None,
        seed: int = 0,
    ) -> None:
        self.seed = seed
        super().__init__(experiment=experiment, name=name, root=root, targets=destinations)
        self.clocks = clocks
        self.strict = strict
        self._closed_clocks = closed_clocks
        self._until_clock = os.environ.get("ETUDE_UNTIL_CLOCK")
        value = os.environ.get("ETUDE_UNTIL_VALUE")
        self._until_value = None if value is None else float(value)
        if (self._until_clock is None) != (self._until_value is None) or (
            self._until_value is not None
            and (not math.isfinite(self._until_value) or self._until_value < 0)
        ):
            raise ValueError("a launch target needs a clock and a finite nonnegative value")
        self._out = (
            Path(given).resolve()
            if (given := os.environ.get("ETUDE_OUT"))
            else (root / "out").resolve()
        )
        self._out.mkdir(parents=True, exist_ok=True)
        if self._out == (root / "out").resolve():
            (root / OUT_PRESENT).touch()
        # Rows refused by the full queue, and rows kept but not chartable.
        self.dropped = 0
        self.incomplete = 0
        self._warned: set[str] = set()
        # The highest value this process has logged for each clock.
        self._high: dict[str, float] = {}
        self._states: dict[str | None, State] = {}
        self._step_clock: str | None = None
        self._step_target: float | None = None
        self._save_every: int | None = None
        self._save_every_seconds: float | None = 300
        self._clock_names = tuple(clock.name for clock in clocks)
        # The clock columns every row and checkpoint carries: the author's, then Etude's.
        self._columns = (*self._clock_names, *BUILT_IN_NAMES)
        # Every checkpoint recorded in this folder, by any attempt on any machine,
        # and the name its record and file are kept under.
        self._checkpoints: dict[int, _Kept] = {}
        for path in sorted((root / "checkpoints").glob("*.json")):
            known = CheckpointRecord.model_validate_json(path.read_bytes())
            known = known.model_copy(
                update={"run": name, "experiment": experiment, "line": known.line or name}
            )
            self._checkpoints[known.ordinal] = _Kept(path.stem, known)
        self._inherited = inherited
        self._segment_seconds = segment_seconds
        self._finish_deadline: float | None = None
        self._segment_rows = segment_rows
        self._writer_id = secrets.token_hex(6)
        # Etude's clocks continue from the saved state an attempt restores;
        # without one, from the latest checkpoint, this run's or the parent's
        # a continuation was handed.
        latest = self._latest_record()
        started_ms = time.time_ns() // 1_000_000
        saved = saved_states(self.out)
        if saved:
            for item in saved:
                for clock, position in item.at.items():
                    self._high[clock] = max(self._high.get(clock, 0.0), position)
            self._ran = self._high.get(RUNNING_HOURS, 0.0)
            self._origin_ms = min(
                item.wall_ms - round(item.at[HOURS_SINCE_START] * 3_600_000) for item in saved
            )
        else:
            self._ran = 0.0 if latest is None else latest.at.get(RUNNING_HOURS, 0.0)
            self._origin_ms = _origin(latest, root, started_ms)
        self._began = time.monotonic()
        # Its first durable row says where this attempt's rows supersede
        # earlier attempts' (`first_row`); one that logs nothing replaces nothing.
        self._writer_record = RunWriter(
            writer=self._writer_id,
            run=self.address,
            started_ms=started_ms,
            host=socket.gethostname(),
            attempt=os.environ.get("ETUDE_ATTEMPT"),
            pod=os.environ.get("RUNPOD_POD_ID"),
            commit=os.environ.get("ETUDE_COMMIT"),
            until=None if self._until_clock is None else f"{self._until_clock}={self._until_value}",
        )
        self._save("metrics", self._writer_id, self._writer_record)
        seal_logs(self.files.root, self._columns)  # what an earlier process left unsealed
        self._index = Index(root, self._writer_id)
        self._metric_name_limit = metric_name_limit
        self._metric_names = metric_names(root) if metric_name_limit is not None else set()
        self._sequence = 0
        # Only the current compaction window stays in memory.
        self._tails: list[Columns] = []
        self._tail_bytes = 0
        self._recent: list[Columns] = []
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
        self._report_progress()
        self._writer.start()
        self._shipper.start()
        atexit.register(self.finish)

    # --- the training-side calls -------------------------------------------

    def at(self, clock: str) -> float:
        """The current absolute position, including time since this attempt started."""
        if clock in BUILT_IN_NAMES:
            return self._now()[1][clock]
        if clock not in self._clock_names:
            raise ValueError(f"undeclared clock: {clock!r}")
        return self._high.get(clock, 0.0)

    def steps(
        self,
        clock: str,
        *,
        to: int,
        save_every: int | None = None,
        save_every_seconds: float | None = None,
    ) -> Iterator[int]:
        """Declare a step clock, count each step, and save before a planned stop."""
        if save_every is not None and save_every < 1:
            raise ValueError("save_every must be at least one step")
        if save_every_seconds is not None and (
            not math.isfinite(save_every_seconds) or save_every_seconds <= 0
        ):
            raise ValueError("save_every_seconds must be finite and positive")
        if clock not in self._clock_names:
            if self._closed_clocks:
                raise EtudeError(
                    "clock_undeclared",
                    422,
                    detail=f"{clock}; include it in track(clocks=) or omit clocks=",
                )
            self._declare_clock(Clock(name=clock, kind="step", meaning=clock))
        elif next(c for c in self.clocks if c.name == clock).kind != "step":
            raise ValueError("steps requires a step clock")
        target = self.until(clock, default=to)
        position = self.at(clock)
        if target < 0 or not float(target).is_integer() or not float(position).is_integer():
            raise ValueError("steps requires whole positions and a whole target")
        self._step_clock, self._save_every = clock, save_every
        self._save_every_seconds = (
            save_every_seconds
            if save_every_seconds is not None
            else (300 if save_every is None else None)
        )
        self._step_target = target
        for state in self._states.values():
            state.saved_step = position
        for value in range(int(position) + 1, int(target) + 1):
            if self.should_stop:
                for state in self._states.values():
                    if state.last_at is None or any(
                        state.last_at.get(name) != self.at(name) for name in self._clock_names
                    ):
                        self._save_state(state)
                raise Stopped()
            self._high[clock] = float(value)
            yield value

    def _declare_clock(self, clock: Clock) -> None:
        """Publish the clock before rows; the writer seals before changing columns."""
        updated = declare_clock(self.files, clock)
        self.clocks = updated.clocks
        self._clock_names = tuple(item.name for item in self.clocks)
        self._columns = (*self._clock_names, *BUILT_IN_NAMES)
        self._queue.put(_Clocks(self._columns))

    @overload
    def resume[T: BaseModel](
        self, *, extra: T, name: str | None = None, **objects: object
    ) -> T: ...

    @overload
    def resume(
        self, *, extra: None = None, name: str | None = None, **objects: object
    ) -> Path | None: ...

    def resume(
        self, *, extra: BaseModel | None = None, name: str | None = None, **objects: object
    ) -> BaseModel | Path | None:
        """Register objects and restore only those objects from the last whole save."""
        state = State(state_folder(self.out, name), objects, extra)
        found = state.restore(seed=self.seed)
        self._states[name] = state
        return extra if extra is not None else found

    def save(
        self, export: Callable[[Path], None] | None = None, *, name: str | None = None
    ) -> None:
        """Write registered state when due, at the target, or before a planned stop."""
        if name not in self._states:
            raise ValueError("register state with run.resume() before saving")
        state = self._states[name]
        if export is not None:
            state.export = export
        clock = self._step_clock
        due = (
            self._save_every_seconds is not None
            and time.monotonic() - state.saved_at >= self._save_every_seconds
        ) or (
            self._save_every is not None
            and clock is not None
            and self.at(clock) - state.saved_step >= self._save_every
        )
        final = (
            clock is not None
            and self._step_target is not None
            and self.at(clock) >= self._step_target
        )
        if due or final or self.should_stop:
            self._save_state(state)

    def _save_state(self, state: State) -> None:
        wall_ms, hours = self._now()
        at = {**{clock: self.at(clock) for clock in self._clock_names}, **hours}
        with self.files.lock:
            state.write(
                at=at, wall_ms=wall_ms, seed=self.seed, commit=os.environ.get("ETUDE_COMMIT")
            )
        if self._step_clock is not None:
            state.saved_step = self.at(self._step_clock)
        self._wake.set()

    def input(self, name: str) -> Path:
        """A given file or folder here; missing inputs raise before the caller opens them."""
        folder = os.environ.get("ETUDE_INPUTS_DIR")
        if folder is None or not name or Path(name).name != name:
            raise ValueError(f"input not given: {name!r}")
        path = Path(folder) / name
        if not path.exists():
            raise ValueError(f"input not given: {name!r}")
        return path

    def until(self, clock: str, *, default: float) -> float:
        """The launch's absolute target for this clock, otherwise the recipe's default.

        Clock values are numeric, including fractional time clocks. An undeclared
        clock raises, so a misspelled name cannot silently ignore a launch target.
        A target for a clock the run has not declared raises too: a loop over a
        clock first declared later must name it in `track(clocks=)`.
        """
        if clock not in self._clock_names:
            raise ValueError(f"undeclared clock: {clock!r}")
        if self._until_clock is not None and self._until_clock not in self._clock_names:
            raise EtudeError(
                "until_clock_undeclared",
                422,
                detail=(
                    f"the target names {self._until_clock}; this run counts "
                    f"{', '.join(self._clock_names)}"
                ),
            )
        if clock == self._until_clock and self._until_value is not None:
            return self._until_value
        return default

    @property
    def out(self) -> Path:
        """The run's persistent output folder, shared by its attempts."""
        return self._out

    def _use_clocks(self, clocks: Mapping[str, float]) -> bool:
        unknown = [name for name in clocks if name not in (*self._clock_names, *BUILT_IN_NAMES)]
        if unknown and self._closed_clocks:
            message = (
                "undeclared clocks: "
                + ", ".join(unknown)
                + "; include them in track(clocks=) or omit clocks="
            )
            if self.strict:
                raise EtudeError("clock_undeclared", 422, detail=message)
            self._warn("undeclared_clock", message)
            self._count_dropped()
            return False
        for name in unknown:
            self._declare_clock(Clock(name=name, kind="step", meaning=name))
        if not self._clock_names:
            raise EtudeError(
                "row_has_no_clock",
                422,
                detail='declare a clock with run.log(values, updates=N) or log inside run.steps("updates", to=N)',
            )
        return True

    def log(self, values: Logged | None = None, /, **clocks: float) -> None:
        """Record metrics; keywords declare count clocks, present on every later row."""
        if not self._use_clocks(clocks):
            return
        try:
            row = {**(values or {}), **clocks}
            if self._step_clock is not None:
                row.setdefault(self._step_clock, self.at(self._step_clock))
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
            wall_ms, hours = self._now()
            self._queue.put_nowait(_Row(wall_ms, {**row, **hours}))
            self._high.update(hours)
        except queue.Full:
            self._count_dropped()
        except Exception:
            if self.strict:
                raise
            self._count_dropped()

    def log_table(self, name: str, rows: Sequence[Mapping[str, object]], **clocks: float) -> None:
        """Store JSON rows locally, then queue their digest at the given clocks.

        Local serialization and I/O finish before return; invalid rows, names,
        coordinates or damaged existing artifacts raise. Queue overflow follows
        `log`: the reference is dropped and counted, leaving a harmless orphan.
        Do not reuse the table's name for scalar or ordinary text values.
        """
        if not self._use_clocks(clocks):
            return
        if self._step_clock is not None:
            clocks = {self._step_clock: self.at(self._step_clock), **clocks}
        if not name or name in self._clock_names:
            raise ValueError("table name must be nonempty and distinct from clocks")
        if set(clocks) != set(self._clock_names) or any(
            isinstance(value, bool) or not math.isfinite(value) for value in clocks.values()
        ):
            raise ValueError("table requires every declared clock with a finite value")
        table = RowTable.model_validate({"rows": rows})
        body = table.model_dump_json().encode()
        digest = sha256(body).hexdigest()
        target = self.files.root / "artifacts" / f"{digest}.json"
        if target.exists():
            if sha256(target.read_bytes()).hexdigest() != digest:
                raise EtudeError("table_artifact_damaged", 422) from ValueError(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Different attempts may log identical content concurrently.
            partial = target.with_name(f"{digest}.{secrets.token_hex(6)}.partial")
            try:
                with partial.open("xb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
        self.log({name: digest}, **clocks)

    def _warn(self, kind: str, message: str, stacklevel: int = 3) -> None:
        """Warn once per writer; warning filters must not interrupt training."""
        if kind in self._warned:
            return
        self._warned.add(kind)
        try:
            warnings.warn(
                f"etude: run {self.name}: {message}.",
                EtudeWarning,
                stacklevel=stacklevel,
            )
        except Exception:  # noqa: BLE001, S110 - a warnings filter must not break training
            pass

    def _count_dropped(self) -> None:
        self.dropped += 1
        self._warn("dropped", "the log queue is full and a row was dropped", stacklevel=4)

    def checkpoint(
        self,
        artifact: ArtifactRef | Path | str | Callable[[Path], None] | None = None,
        at: Mapping[str, float] | None = None,
        /,
        *,
        tags: Sequence[str] = (),
        **objects: object,
    ) -> ArtifactRef:
        """Store a saved model and record where every declared clock stood.

        A file inside `out` is recorded by reference: keep it unchanged. Any
        other local file is copied before return, so the caller may overwrite
        its source. Recorded files are carried before their records. Given an
        address such as `s3://...` or an `ArtifactRef`, only the reference is
        recorded.

        Recording is idempotent and nothing recorded is replaced. The same
        model at the same `at` returns the existing record's reference.
        A different one at a recorded `at` is kept as a new version,
        `CLOCK-VALUE.ordinal-N`, beside the first.

        Returns the model's reference: pass it to whatever evaluates the model.
        Missing a clock, or an unreadable file, the checkpoint is not recorded.
        """
        staged: Path | None = None
        try:
            clocks = {
                name: objects.pop(name) for name in tuple(objects) if name in self._clock_names
            }
            if artifact is not None and objects:
                raise ValueError("give a checkpoint path, an export function, or named objects")
            given = (
                {**(at or {}), **clocks}
                if at is not None or clocks
                else {name: self.at(name) for name in self._clock_names}
            )
            if set(given) != set(self._clock_names):
                raise ValueError("checkpoint_clock_mismatch")
            declared: dict[str, float] = {}
            for clock in self._clock_names:
                value = given[clock]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError("checkpoint clocks must be numbers")
                declared[clock] = float(value)
            wall_ms, hours = self._now()
            at = {**declared, **hours}
            folder_checkpoint = (
                artifact is None
                or callable(artifact)
                or isinstance(artifact, (str, Path))
                and "://" not in str(artifact)
                and Path(artifact).is_dir()
            )
            name = checkpoint_name(at, padded=folder_checkpoint)
            if artifact is None or callable(artifact):
                if artifact is None and not objects:
                    raise ValueError("a checkpoint needs objects, a path, or an export function")
                reference, staged = self._export(artifact, objects, declared)
            else:
                reference, staged = self._stage(artifact, name)
            file = self._file_key(reference)
            for kept in self._checkpoints.values():
                known = kept.record
                if (
                    reference.uri == known.artifact.uri or file is not None and file == known.file
                ) and reference.sha256 != known.artifact.sha256:
                    raise ValueError("checkpoint_file_changed")
                # Etude's clocks always move on, so the same `at` is the author's.
                same = all(known.at.get(n) == v for n, v in declared.items())
                if same and _same_content(known.artifact, reference):
                    if staged is not None:
                        _remove(staged)
                    return known.artifact
            ordinal = max(self._checkpoints, default=0) + 1
            suffix = (
                _suffix(Path(artifact))
                if staged is not None and not staged.is_dir() and isinstance(artifact, (Path, str))
                else ""
            )
            name = self._unused(name, suffix, ordinal)
            record = CheckpointRecord(
                run=self.name,
                experiment=self.experiment,
                line=self._inherited.line
                if self._inherited is not None and self._inherited.line
                else self.name,
                ordinal=ordinal,
                artifact=reference,
                at=at,
                wall_ms=wall_ms,
                tags=tuple(dict.fromkeys(tags)),
            )
            if staged is not None:
                if staged.is_dir():
                    target = staged.parent / name
                    staged.rename(target)
                    reference = reference.model_copy(update={"uri": target.resolve().as_uri()})
                else:
                    reference = self._rename_staged(staged, name + suffix, reference)
            record = record.model_copy(
                update={"artifact": reference, "file": self._file_key(reference)}
            )
        except (ValidationError, ValueError, TypeError, OSError) as error:
            if staged is not None:
                _remove(staged)
            if self.strict:
                raise
            self.incomplete += 1
            self._warn("checkpoint", f"a checkpoint was not recorded ({error})")
            return artifact if isinstance(artifact, ArtifactRef) else ArtifactRef(uri=str(artifact))
        kept = _Kept(name, record)
        self._checkpoints[ordinal] = kept
        self._offer(kept)
        return record.artifact

    def checkpoint_due(self, every: float, clock: str = RUNNING_HOURS) -> bool:
        """Whether `clock` has crossed a multiple of `every` since the latest checkpoint.

        `if run.checkpoint_due(1): run.checkpoint(...)` saves every running hour,
        counted from zero and continuing across resumes and continuations.
        """
        if every <= 0 or not math.isfinite(every):
            raise ValueError("every must be positive")
        here, latest = self.at(clock), self._latest_record()
        there = 0.0 if latest is None else latest.at.get(clock, 0.0)
        return here // every > there // every

    def _now(self) -> tuple[int, dict[str, float]]:
        """The wall time in milliseconds, and where Etude's clocks stand."""
        wall_ms = time.time_ns() // 1_000_000
        return wall_ms, {
            RUNNING_HOURS: self._ran + (time.monotonic() - self._began) / 3600,
            HOURS_SINCE_START: (wall_ms - self._origin_ms) / 3_600_000,
        }

    def _latest_record(self) -> CheckpointRecord | None:
        """The latest record this run kept, else the parent's it continues from."""
        return (
            self._checkpoints[max(self._checkpoints)].record
            if self._checkpoints
            else self._inherited
        )

    def latest_checkpoint(self) -> CheckpointRecord | None:
        """The latest checkpoint record, or an inherited parent checkpoint.

        Relocate its address when the file is present here. A removed out file
        is not restored just because its record survives; its recorded address
        may be unavailable. None when there is no record.
        """
        warnings.warn(
            "Run.latest_checkpoint() is now checkpoints(); removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
        record = self._latest_record()
        return None if record is None else self._checkpoint_here(record)

    def checkpoints(self) -> tuple[CheckpointRecord, ...]:
        """Published checkpoints in ordinal order, relocated when their bytes are here."""
        return tuple(
            self._checkpoint_here(self._checkpoints[n].record) for n in sorted(self._checkpoints)
        )

    def _checkpoint_here(self, record: CheckpointRecord) -> CheckpointRecord:
        key = record.file or checkpoint_key(record.artifact.uri)
        local = (
            self.out / key.removeprefix("out/") if key.startswith("out/") else self.files.root / key
        )
        if record.artifact.sha256 is None or not local.exists():
            return record
        artifact = record.artifact.model_copy(update={"uri": local.resolve().as_uri()})
        return record.model_copy(update={"artifact": artifact})

    def _export(
        self,
        export: Callable[[Path], None] | None,
        objects: Mapping[str, object],
        at: dict[str, float],
    ) -> tuple[ArtifactRef, Path]:
        staged = self._staging()
        staged.mkdir()
        try:
            if export is not None:
                export(staged)
            files = {
                path.relative_to(staged).as_posix(): path.relative_to(staged).as_posix()
                for path in staged.rglob("*")
                if path.is_file() and path.name != "checkpoint.json"
            }
            files.update(write_objects(staged, objects))
            (staged / "checkpoint.json").write_text(
                json.dumps({"at": at, "files": files}, sort_keys=True)
            )
            return _folder_artifact(staged), staged
        except BaseException:
            _remove(staged)
            raise

    def _staging(self) -> Path:
        """An unused private name beside folder checkpoints, which no carrier sends."""
        folder = self.out / "checkpoints"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (".checkpoint-" + secrets.token_hex(6))

    def _stage(
        self, artifact: ArtifactRef | Path | str, name: str
    ) -> tuple[ArtifactRef, Path | None]:
        """Digest a file in out in place; stage any other local file as a copy.

        A staged copy has a name no carrier sends until it is kept.
        """
        if isinstance(artifact, ArtifactRef):
            return artifact, None
        if isinstance(artifact, str) and "://" in artifact:
            return ArtifactRef(uri=artifact), None
        source = Path(artifact).resolve()
        if source.is_dir():
            if source.is_relative_to(self._out):
                return _folder_artifact(source), None
            staged = self._staging()
            try:
                # Validate links before copying, so a directory cannot publish outside bytes.
                _folder_artifact(source)
                shutil.copytree(source, staged)
                return _folder_artifact(staged), staged
            except BaseException:
                _remove(staged)
                raise
        if source.is_relative_to(self._out):
            with source.open("rb") as stream:
                digest = file_digest(stream, "sha256")
                size = os.fstat(stream.fileno()).st_size
            return ArtifactRef(
                uri=source.as_uri(), sha256=digest.hexdigest(), size_bytes=size
            ), None
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

    def _file_key(self, artifact: ArtifactRef) -> str | None:
        """A local stored file's portable address within the tracked folder."""
        prefix = self.files.root.resolve().as_uri() + "/"
        return (
            unquote(artifact.uri.removeprefix(prefix)) if artifact.uri.startswith(prefix) else None
        )

    def _unused(self, name: str, suffix: str, ordinal: int) -> str:
        """The first of `name`, `name.ordinal-N`, `name.ordinal-N-2`, ... no record or file holds.

        A file counts even without a record, here or where storage or another
        host keeps it (`etude.carriage.taken`), so nothing is ever replaced.
        """
        files = taken(self.files.root)
        records = {kept.name for kept in self._checkpoints.values()}
        records.update(path.name for path in (self.out / "checkpoints").glob("*"))
        versions = (f"{name}.ordinal-{ordinal}-{n}" for n in count(2))
        candidates = chain((name, f"{name}.ordinal-{ordinal}"), versions)
        return next(c for c in candidates if c not in records and c + suffix not in files)

    def _rename_staged(self, staged: Path, filename: str, reference: ArtifactRef) -> ArtifactRef:
        """Give a staged copy its recorded name, which nothing has used before."""
        kept = staged.with_name(filename)
        staged.replace(kept)
        key = kept.relative_to(self.files.root).as_posix()
        remote = next(iter(self._destinations), None)
        uri = (
            remote.storage.uri(run_prefix(self.address) + key)
            if remote
            else kept.resolve().as_uri()
        )
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
            kept = (
                self._checkpoints.get(checkpoint)
                if isinstance(checkpoint, int)
                else next(
                    (
                        kept
                        for kept in self._checkpoints.values()
                        if _same_content(kept.record.artifact, checkpoint)
                    ),
                    None,
                )
            )
            if kept is None:
                raise EtudeError("checkpoint_not_recorded", 404)
            record = kept.record
            tags = tuple(t for t in dict.fromkeys((*record.tags, *add)) if t not in remove)
            changed = CheckpointRecord.model_validate(record.model_dump() | {"tags": tags})
        except (ValidationError, EtudeError) as error:
            if self.strict:
                raise
            self.incomplete += 1
            self._warn("tag", f"a checkpoint was not tagged ({error})")
            return
        updated = _Kept(kept.name, changed)
        self._checkpoints[changed.ordinal] = updated
        self._offer(updated)

    def _offer(self, item: object) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._count_dropped()

    # --- the writer thread ---------------------------------------------------

    def _write(self) -> None:
        stream: IO[str] | None = None
        opened_at = 0.0
        clock_names = self._columns
        builder = Builder(clock_names)

        def seal(final: bool = False) -> None:
            nonlocal stream, builder
            if stream is not None:
                columns = builder.columns(self._writer_id, self._sequence)
                self._tail_bytes += write_block(
                    self.files.root, tail_key(self._sequence, self._writer_id), columns
                )
                stream.close()
                Path(stream.name).unlink()  # The block now holds these rows.
                self._tails.append(columns)
                stream, builder = None, Builder(clock_names)
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
                if isinstance(item, _Clocks):
                    seal()
                    clock_names = item.names
                    builder = Builder(clock_names)
                elif isinstance(item, _Kept):
                    self._save("checkpoints", item.name, item.record)
                    self._dirty = True
                elif isinstance(item, _Row):
                    if stream is None:
                        log = self.files.root / log_key(self._sequence, self._writer_id)
                        log.parent.mkdir(parents=True, exist_ok=True)
                        stream = log.open("a")
                        opened_at = time.monotonic()
                    row = self._stored(item, clock_names)
                    stream.write(log_line(row))
                    stream.flush()
                    if not self._writer_record.resumed_at:
                        self._writer_record = self._writer_record.model_copy(
                            update={"resumed_at": dict(row.clocks), "first_row": True}
                        )
                        self._save("metrics", self._writer_id, self._writer_record)
                    builder.add(row.wall_ms, row.clocks, row.values)
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
                columns = concat(self._tails)
                recent.append(columns)
                if not final:
                    key = block_key(1, columns.first, columns.last, columns.writer)
                    size += write_block(self.files.root, key, columns)
                    self._index.add(key)
            compact = final or size >= PERMANENT_BYTES or len(recent) >= RECENT_BLOCKS
            if compact:
                columns = concat(recent)
                key = block_key(2, columns.first, columns.last, columns.writer)
                write_block(self.files.root, key, columns)
                self._index.add(key)
            self._index.write()
            retired = [tail_key(tail.first, tail.writer) for tail in self._tails]
            if compact:
                retired.extend(
                    block_key(1, columns.first, columns.last, columns.writer) for columns in recent
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
            note_retired(self.files.root, self._writer_id, delivered)
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
        report_progress(writer=self._writer_record, clocks=dict(self._high))

    def _stored(self, row: _Row, columns: tuple[str, ...]) -> LoggedRow:
        """A logged row as its clock values and everything else, ready to store."""
        values: dict[str, Json] = {}
        for name, value in flatten(row.values):
            if value is None or isinstance(value, (bool, str, int, float)):
                values[name] = value
            else:
                try:
                    values[name] = float(value)  # type: ignore[arg-type]  # numpy and tensor scalars
                except (TypeError, ValueError):
                    values[name] = type(value).__name__
        at: dict[str, float] = {}
        for name in columns:
            value = values.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(value):
                    at[name] = float(values.pop(name))  # type: ignore[arg-type]
                else:
                    # Not a finite number: stays among the values as written.
                    self._warn(
                        "incomplete", f"a logged row's clock {name!r} is not a finite number"
                    )
        if len(at) != len(columns):
            # Charted against the clocks it does give, and wall time; counted.
            self.incomplete += 1
        if self._metric_name_limit is not None and "metric_names" not in self._warned:
            self._metric_names.update(
                name
                for name, value in values.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            if len(self._metric_names) > self._metric_name_limit:
                self._warn(
                    "metric_names",
                    f"{len(self._metric_names)} distinct metric names exceed "
                    f"metric_name_limit={self._metric_name_limit}; use log_table for "
                    "per-update collections instead of encoding dimensions in metric names",
                )
                # The one-time warning needs no growing name registry afterward.
                self._metric_names.clear()
        return LoggedRow(wall_ms=row.wall_ms, clocks=at, values=values)

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
            if self._stopping.is_set():
                break
            # Back off while a destination is down; sealed files keep accumulating.
            delay = self._segment_seconds if self.ship() else min(delay * 2, 60.0)
        assert self._finish_deadline is not None
        while (left := self._finish_deadline - time.monotonic()) > 0:
            with limit_time(left):
                if self.ship():
                    return
            self._wake.wait(
                timeout=min(
                    self._segment_seconds, max(0.0, self._finish_deadline - time.monotonic())
                )
            )
            self._wake.clear()

    def ship(self) -> bool:
        """One delivery pass over every destination. True when all are current."""
        # A launched job has no bucket credentials; its courier owns acknowledgement.
        if not os.environ.get("ETUDE_ATTEMPT"):
            prune(self.files, self._destinations)
        return super().ship()

    # --- ending --------------------------------------------------------------

    def finish(
        self, state: Literal["finished", "failed", "stopped"] = "finished", *, timeout: float = 10.0
    ) -> None:
        """Seal, record the final state and deliver, within `timeout` seconds."""
        if self._finished:
            return
        self._finished = True
        deadline = self._finish_deadline = time.monotonic() + timeout
        try:
            try:
                self._queue.put(_STOP, timeout=timeout)
            except queue.Full:
                self.dropped += 1
            self._writer.join(max(0.0, deadline - time.monotonic()))
            counters = RunCounters(dropped=self.dropped, incomplete=self.incomplete)
            self._write_outcome(state, counters=counters, writer=self._writer_id)
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
        if isinstance(error, Stopped):
            self.finish("stopped")
        else:
            self.finish("finished" if error is None else "failed")


def track(
    *,
    experiment: str | None = None,
    clocks: Sequence[str | Clock] | None = None,
    run: str | None = None,
    job: str | None = None,
    seed: int | None = None,
    cohort: str | None = None,
    kind: str | None = None,
    config: BaseModel | Mapping[str, object] | str | None = None,
    continues: Continuation | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    namespace: str = "local",
    queue_size: int = 10_000,
    segment_seconds: float = 2.0,
    segment_rows: int = 5_000,
    strict: bool = False,
    metric_name_limit: int | None = 1000,
    resume: bool = False,
) -> TrainingRun:
    """Open a new run, or explicitly resume one. A bad declaration raises here.

    The experiment is the one `etude launch` started the run for
    (`ETUDE_EXPERIMENT`, by default the job file's stem) unless named here. Naming
    another raises `EtudeError("experiment_differs_from_launch")`; naming none
    outside such a launch raises `EtudeError("experiment_not_named")`. The
    run's identity, and whether it resumes, is settled by
    `etude.run.declare`. Under `etude launch` the files are
    written to `ETUDE_TRACK_DIR`, which the launcher carries to storage as they
    are, and a later attempt finds the run's checkpoints there
    (`Run.checkpoints`). A continuation (`etude.jobs.continue_from`) is
    handed its parent's position and optional checkpoint in `ETUDE_CONTINUES`:
    unless `continues` is given, the run declares that lineage.

    A taken name is refused unless `resume=True` or the launcher requests a retry.
    A resumed run's first logged row sets where it supersedes earlier rows.
    An attempt that logs nothing replaces nothing. `etude train` (`etude.local.train`)
    supplies the experiment, identity and persistent `out` for a local call.
    """
    if metric_name_limit is not None and metric_name_limit < 0:
        raise ValueError("metric_name_limit must be nonnegative or None")
    if seed is not None:
        warnings.warn(
            "track(seed=) is replaced by launch seeds or etude train --seed; removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
    launched = os.environ.get("ETUDE_EXPERIMENT") or None
    if experiment is not None and launched not in (None, experiment):
        raise EtudeError("experiment_differs_from_launch", 409) from ValueError(
            f"track(experiment={experiment!r}) in a run etude launch started for {launched!r};"
            " omit experiment= here, or name it on the launched Job"
        )
    experiment = experiment or launched
    if experiment is None:
        raise EtudeError(
            "experiment_not_named",
            422,
            detail=(
                "use etude train FILE or etude launch FILE; for a process Etude did not start, "
                "name the run with track(experiment=..., run=...)"
            ),
        )
    handed = _inherited()
    if handed is not None and continues is None:
        at = handed.at
        until = os.environ.get("ETUDE_UNTIL_CLOCK")
        clock = until if until in at else next(iter(at))
        continues = Continuation(parent=handed.parent, clock=clock, value=at[clock])
    declared = declare(
        experiment=experiment,
        clocks=None
        if clocks is None
        else tuple(
            Clock(name=clock, kind="step", meaning=clock) if isinstance(clock, str) else clock
            for clock in clocks
        ),
        run=run,
        job=job,
        seed=seed,
        cohort=cohort,
        kind=kind,
        config=config.model_dump(mode="json") if isinstance(config, BaseModel) else config,
        continues=continues,
        directory=directory,
        namespace=namespace,
        resume=resume
        or os.environ.get("ETUDE_RESUME") == "1"
        or bool(os.environ.get("ETUDE_ATTEMPT")),
    )
    return TrainingRun(
        experiment=experiment,
        name=declared.name,
        root=declared.root,
        clocks=declared.record.clocks,
        destinations=destinations(declared.base, storages),
        queue_size=queue_size,
        segment_seconds=segment_seconds,
        segment_rows=segment_rows,
        strict=strict,
        closed_clocks=clocks is not None,
        metric_name_limit=metric_name_limit,
        inherited=None if handed is None else handed.checkpoint,
        seed=declared.record.seed or 0,
    )


def _origin(latest: CheckpointRecord | None, root: Path, started_ms: int) -> int:
    """When the line's first attempt began, in milliseconds since the epoch.

    The checkpoint resumed says how long after that it was saved, also across a
    continuation; without one, the earliest attempt recorded in this folder.
    """
    if latest is not None and latest.wall_ms is not None and HOURS_SINCE_START in latest.at:
        return latest.wall_ms - round(latest.at[HOURS_SINCE_START] * 3_600_000)
    starts = [started_ms]
    for path in (root / "metrics").glob("*.json"):
        if re.fullmatch(r"[0-9a-f]{12}", path.stem):
            try:
                starts.append(RunWriter.model_validate_json(path.read_bytes()).started_ms)
            except ValidationError:
                continue
    return min(starts)


def _inherited() -> ParentRun | None:
    """The handed lineage, relocating its checkpoint input onto this machine."""
    given = os.environ.get("ETUDE_CONTINUES")
    if not given:
        return None
    parent = ParentRun.model_validate_json(given)
    record = parent.checkpoint
    if parent.input is not None and record is not None:
        folder = os.environ.get("ETUDE_INPUTS_DIR")
        if not folder:
            raise EtudeError("continuation_input_missing", 422)
        local = (Path(folder) / parent.input).resolve().as_uri()
        record = record.model_copy(
            update={"artifact": record.artifact.model_copy(update={"uri": local})}
        )
        return parent.model_copy(update={"checkpoint": record})
    return parent


def seal_runs(directory: Path | str) -> None:
    """Turn the local logs dead processes left in every run under `directory` into blocks."""
    for root in sorted(run_directories(directory)):
        declared = RunFiles(root).run_file()
        if declared is not None:
            names = [clock.name for clock in declared.clocks]
            seal_logs(root, [*names, *BUILT_IN_NAMES])


__all__ = [
    "EtudeWarning",
    "TrainingRun",
    "local_launch",
    "seal_runs",
    "track",
]
