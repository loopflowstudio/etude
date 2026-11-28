"""A live evaluation run: match validation, staging, rollups and outcome publication.

EvalRun owns match execution, game processes and its local DuckDB connection
until finish. Result is the playing code's returned data; EvalResult counts
what play completed. Tournament scheduling lives in eval.tournament.
"""

from __future__ import annotations

import inspect
import math
import multiprocessing
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from multiprocessing.connection import Connection, wait
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Literal, NoReturn

from etude.call import Function
from etude.errors import EtudeError, RetryRequested, explain, sanitize
from etude.eval.agents import AgentProcess, ForwardError, running_agents
from etude.eval.block import (
    Block,
    Staging,
    partial_totals,
    quote,
    run_blocks,
    seal_abandoned,
    stage,
)
from etude.eval.game import (
    CheckpointFailure,
    Done,
    Fatal,
    GameReport,
    Grant,
    Outcome,
    Play,
    PlayMany,
    Stream,
    StreamEnd,
    Take,
    _match_type,
    run_game_process,
)
from etude.eval.loader import AgentLoader
from etude.eval.records import (
    ADDED,
    COLUMN_TYPES,
    IDENTIFIER,
    NO_ROWS,
    ColumnType,
    Environment,
    Match,
    MatchRecord,
    Rollup,
    as_config,
    outcomes,
    player_checkpoints,
)
from etude.eval.resources import Resources, TournamentQueue, place, resources, seconds_left
from etude.eval.result import EvalResult as EvalResult
from etude.eval.result import Result as Result
from etude.eval.result import Row
from etude.eval.result import Tables as Tables
from etude.records import RunCounters, RunWriter, Tally
from etude.run import Declared, Run, StorageDestination, report_progress
from etude.stopping import Stopped
from etude.storage import DirectoryStorage, Storage, run_prefix

if TYPE_CHECKING:
    import duckdb

    from etude.jobs import Agents

# Etude's own tables, which `etude.eval.query.statsdb` keeps beside the author's.
ETUDE_TABLES = (
    "runs",
    "players",
    "matches",
    "seats",
    "groups",
    "tournaments",
    "schedule",
    "training_checkpoints",
    "blobs",
)
# The seat's name before etude-sdk 0.2.7. Refused in rows, so a stored table with
# a `player` column is an older one (`etude.eval.query` reads it as such).
OLD_SEAT = "player"


class EvalRun(Run):
    """One process's matches of an eval run: staged, sealed into blocks and delivered."""

    def __init__(
        self,
        root: Path,
        name: str,
        targets: Sequence[StorageDestination],
        rollups: tuple[Rollup, ...],
        *,
        experiment: str,
        environment: Environment,
        matches: Sequence[Match],
        progress: _Report | None = None,
    ) -> None:
        # Imported here: DuckDB is the optional `eval` extra.
        import duckdb  # noqa: PLC0415

        super().__init__(experiment=experiment, name=name, root=root, targets=targets)
        self._delivery = progress.run if progress else self
        self.environment = environment
        self.matches = tuple(matches)
        self.rollups = rollups
        self.id = secrets.token_hex(6)
        self.writer = _writer(self.address, self.id)
        self._failed: set[str] = set()
        self.db: duckdb.DuckDBPyConnection = duckdb.connect()
        try:
            seal_abandoned(root, self.db)
            stored = self._stored()
        except BaseException:
            self.db.close()
            raise
        self.staging = Staging(root, self.id, rollups)
        self.done = {key for key, record in stored.items() if record.state == "done"}
        self.columns: dict[str, dict[str, ColumnType]] = {}
        for record in stored.values():
            for table, columns in record.columns.items():
                self.columns.setdefault(table, {}).update(columns)

        self._report = progress or _Report(
            total=len(self.matches),
            done=sum(match.key in self.done for match in self.matches),
            writer=self.writer,
            run=self,
        )
        self._report.write()

    def play(
        self,
        play: Play,
        *,
        agents: Agents | None = None,
    ) -> EvalResult:
        """Play selected matches with constructed seats, skipping durable results on resume."""
        return self._play(play, agents, streaming=False)

    def play_many(self, play_many: PlayMany, *, agents: Agents | None = None) -> EvalResult:
        """Play selected matches with a streaming generator that owns its slots."""
        return self._play(play_many, agents, streaming=True)

    def _play(self, play: Play | PlayMany, agents: Agents | None, *, streaming: bool) -> EvalResult:
        _match_type(play, streaming=streaming)
        authored = inspect.unwrap(play)
        function = Function(module=authored.__module__, name=authored.__qualname__)
        definition = agents.plain(None) if agents is not None else AgentLoader()
        tournament = Playing(
            run=self,
            play=function,
            tournament=self.name,
            agent_loader=definition,
            streaming=streaming,
        )
        (result,) = play_together((tournament,), artifacts=self.files.root / "artifacts" / "agents")
        if isinstance(result, (EtudeError, RetryRequested)):
            raise result
        return result

    def _store_played(self, scheduled: Match, result: Tables | Result, seconds: float) -> None:
        self.store(scheduled, result, seconds=seconds)
        self.done.add(scheduled.key)
        self._report.played()
        self._seal()

    def _stored(self) -> dict[str, MatchRecord]:
        """Each match's standing record (`outcomes`) of the blocks here and at every destination.

        One listing per place and one read of each block not already read here.
        """
        places: list[tuple[Storage, str]] = [(DirectoryStorage(self.files.root), "")]
        path = (
            self.address if self._delivery is self else f"{self._delivery.address}/{self.address}"
        )
        places += [(target.storage, run_prefix(path)) for target in self._delivery._destinations]
        found: list[MatchRecord] = []
        known: dict[str, Block] = {}
        for storage, prefix in places:
            names = (key[len(prefix) :] for key in storage.keys(prefix))
            for block in run_blocks(storage, prefix, self.name, names, known):
                if block.tables and block.rollups != self.rollups:
                    raise EtudeError("eval_run_rollups_conflict", 409) from ValueError(
                        f"block {block.name} keeps rollups"
                        f" {[rollup.name for rollup in block.rollups]}"
                    )
                found += block.matches
        return outcomes(found)

    def fail_pending(self, error: RetryRequested, matches: Iterable[Match] | None = None) -> bool:
        """Record retryable failures for unrecorded matches in the given set or the whole run."""
        candidates = self.matches if matches is None else matches
        affected = False
        for match in candidates:
            if match.key not in self.done and match.key not in self._failed:
                self._stage_failure(match, str(error), retry=True)
                affected = True
        if affected:
            self._seal()
        return affected

    @property
    def failed(self) -> int:
        return len(self._failed)

    def _stage_failure(self, match: Match, detail: str, *, retry: bool = False) -> None:
        self._failed.add(match.key)
        self.staging.stage(
            MatchRecord(
                match=match,
                state="failed",
                error=sanitize(detail)[:2000],
                retry=retry,
                wall_ms=_now_ms(),
            )
        )

    def fail(self, scheduled: Match, error: BaseException) -> NoReturn:
        raise self._record_failure((scheduled,), error)

    def _record_failure(
        self, scheduled: Sequence[Match], error: BaseException
    ) -> EtudeError | RetryRequested:
        if isinstance(error, ForwardError) and error.loading:
            detail = str(error)
        elif isinstance(error, subprocess.SubprocessError):
            detail = explain(error)
        else:
            detail = f"{type(error).__name__}: {error}"
        retry = isinstance(error, RetryRequested) or isinstance(error, ForwardError) and error.retry
        for match in scheduled:
            self._stage_failure(match, detail, retry=retry)
        if scheduled:
            self._seal()
        failure: EtudeError | RetryRequested = (
            RetryRequested(sanitize(detail))
            if retry
            else EtudeError("match_failed", 500, detail=sanitize(detail))
        )
        # In-flight outcomes may be retained before this failure is raised.
        failure.__cause__ = error
        return failure

    def store(
        self, scheduled: Match, outcome: Tables | Result, *, seconds: float | None = None
    ) -> None:
        """Check every table, then stage them, the blobs and the match's record; nothing on refusal."""
        result = outcome if isinstance(outcome, Result) else Result(tables=outcome)
        try:
            execution = as_config(result.execution)
            checked = {
                table: _checked(scheduled, table, rows, self.rollups)
                for table, rows in result.tables.items()
            }
            types = {
                table: _types(table, rows, self.columns.get(table, {}))
                for table, rows in checked.items()
                if rows
            }
            if not types:
                raise EtudeError(NO_ROWS, 422)
            if not all(isinstance(blob, bytes) for blob in result.blobs):
                raise EtudeError("blob_must_be_bytes", 422)
        except EtudeError as refused:
            self.fail(scheduled, refused)
        staged = {table: [self.staging.table(table, scheduled.key)] for table in types}
        for table, columns in types.items():
            self._write(staged[table][0], columns, checked[table], scheduled.key)
        # A match's partial totals are taken now, so a rollup that cannot sum it refuses it.
        try:
            for rollup in self.rollups:
                target = self.staging.partial(rollup.name, scheduled.key)
                partial_totals(self.db, rollup, staged, [scheduled], target)
        except EtudeError as refused:
            self.fail(scheduled, refused)
        for table, columns in types.items():
            self.columns.setdefault(table, {}).update(columns)
        digests = tuple(dict.fromkeys(sha256(blob).hexdigest() for blob in result.blobs))
        record = MatchRecord(
            match=scheduled,
            state="done",
            columns=types,
            wall_ms=_now_ms(),
            blobs=digests,
            execution=execution,
            seconds=seconds,
        )
        self.staging.stage(record, result.blobs)
        if self.staging.due():
            self._seal()

    def _write(
        self, target: Path, columns: dict[str, ColumnType], rows: Sequence[Row], match: str
    ) -> None:
        """Write one match's rows of a table to `target` as Parquet; readers never see half a file."""
        stage(
            self.db,
            "_match_rows",
            {**columns, "run": "VARCHAR", "match": "VARCHAR"},
            [[row.get(name) for name in columns] + [self.name, match] for row in rows],
        )
        partial = target.with_name(target.name + ".partial")
        self.db.execute(f"copy _match_rows to {quote(partial)} (format parquet)")
        self.db.execute("drop table _match_rows")
        partial.replace(target)

    def _seal(self) -> None:
        """Seal what is staged into a block and offer it to every destination."""
        with self.files.lock:
            self.staging.seal(self.db)
        if not self._delivery.ship():
            raise RetryRequested("evaluation delivery failed; results remain in the local run")

    def finish(self, state: Literal["finished", "failed", "stopped"]) -> None:
        with self.files.lock:
            self.staging.seal(self.db)
        self.staging.close()
        self._write_outcome(state, counters=RunCounters(), writer=self.id)
        self.db.close()
        if not self._delivery.ship():
            self._write_outcome("failed", counters=RunCounters(), writer=self.id)
            print(
                f"etude: not delivered: {self._delivery.problems}. Files are in {self._delivery.files.root}; "
                "deliver them with `etude sync`.",
                file=sys.stderr,
            )
            raise RetryRequested("evaluation delivery failed; results remain in the local run")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _checked(
    scheduled: Match, table: str, rows: Sequence[Row], rollups: Sequence[Rollup]
) -> list[Row]:
    """The rows, refused unless each names exactly one seat or group of this match."""
    if (
        not IDENTIFIER.match(table)
        or table in ETUDE_TABLES
        or table in (rollup.name for rollup in rollups)
    ):
        raise EtudeError("table_name_refused", 422) from ValueError(table)
    for row in rows:
        seat, group = row.get("seat"), row.get("group")
        where = ValueError(f"table {table!r}: {dict(row)!r}")
        if (seat is None) == (group is None):
            raise EtudeError("row_needs_seat_or_group", 422) from where
        if seat is not None and (type(seat) is not int or not 0 <= seat < len(scheduled.players)):
            raise EtudeError("row_seat_not_seated", 422) from where
        if group is not None and group not in scheduled.groups:
            raise EtudeError("row_group_not_in_match", 422) from where
        for column in row:
            if column in ADDED or column == OLD_SEAT or not IDENTIFIER.match(column):
                raise EtudeError("column_name_refused", 422) from where
    return list(rows)


def _types(
    table: str, rows: Sequence[Row], known: Mapping[str, ColumnType]
) -> dict[str, ColumnType]:
    """Each column's type in this write. Whole numbers may join a column of fractions."""
    found: dict[str, ColumnType] = {"seat": "BIGINT", "group": "VARCHAR"}
    for row in rows:
        for column, value in row.items():
            if value is None:
                continue
            kind = COLUMN_TYPES.get(type(value))
            if kind is None:
                raise EtudeError("column_value_refused", 422) from TypeError(
                    f"table {table!r}, column {column!r}: {type(value).__name__}"
                )
            found[column] = _widened(found.get(column, kind), kind, table, column)
    for column, kind in list(found.items()):
        if column in known:
            found[column] = _widened(known[column], kind, table, column, onto=True)
    # A column given only as nulls here keeps the type an earlier write gave it.
    nulls = {column for row in rows for column in row} - found.keys()
    found.update({column: known[column] for column in nulls if column in known})
    return found


def _widened(
    first: ColumnType, then: ColumnType, table: str, column: str, *, onto: bool = False
) -> ColumnType:
    if first == then:
        return first
    if {first, then} == {"BIGINT", "DOUBLE"} and not (onto and first == "BIGINT"):
        return "DOUBLE"
    raise EtudeError("column_type_conflict", 422) from TypeError(
        f"table {table!r}, column {column!r}: {first} then {then}"
    )


# A running eval run says how far it has got at most this often: each report
# rewrites the allocation's progress file.
_REPORT_SECONDS = 15.0
# Matches a recent pace is taken over.
_PACE_MATCHES = 50


def _writer(address: str, writer: str) -> RunWriter:
    return RunWriter(
        writer=writer,
        run=address,
        started_ms=_now_ms(),
        host=socket.gethostname(),
        attempt=os.environ.get("ETUDE_ATTEMPT"),
        commit=os.environ.get("ETUDE_COMMIT"),
    )


@contextmanager
def _group_progress(
    declaration: Declared, targets: Sequence[StorageDestination], *, total: int
) -> Iterator[_Report]:
    run = Run(
        experiment=declaration.record.experiment,
        name=declaration.name,
        root=declaration.root,
        targets=targets,
    )
    writer = _writer(run.address, secrets.token_hex(6))
    DirectoryStorage(declaration.root).put(
        f"metrics/{writer.writer}.json", writer.model_dump_json().encode()
    )
    progress = _Report(total=total, done=0, writer=writer, run=run)
    state: Literal["finished", "failed", "stopped"] = "failed"
    try:
        progress.write()
        yield progress
        state = "finished"
    except (Stopped, Terminated):
        state = "stopped"
        raise
    finally:
        run._write_outcome(state, counters=RunCounters(), writer=writer.writer)
        if not run.ship():
            run._write_outcome("failed", counters=RunCounters(), writer=writer.writer)
            raise RetryRequested("evaluation delivery failed; results remain in the local run")


class _Report:
    """Under `etude launch`, how far the eval run has got, for the launcher's status.

    `etude-result.json` in `ETUDE_OUTPUT_DIR` holds a tally, the shape of
    `etude.records.Tally`; evaluation does not import the launcher.
    It is progress only: results are the run's blocks.
    """

    def __init__(self, *, total: int, done: int, writer: RunWriter, run: Run) -> None:
        self.run = run
        self.writer = writer
        self.total, self.done = total, done
        self.failed = 0
        self.outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        self.recent: deque[float] = deque(maxlen=_PACE_MATCHES)
        self.written = time.monotonic()

    def played(self) -> None:
        """Count one stored match, and report when the last report is old enough."""
        self.done += 1
        now = time.monotonic()
        self.recent.append(now)
        if now - self.written >= _REPORT_SECONDS:
            self.write()

    def write(self) -> None:
        if self.outputs is None:
            return
        elapsed = self.recent[-1] - self.recent[0] if self.recent else 0.0
        tally = Tally(
            unit="matches",
            done=self.done,
            total=self.total,
            failed=self.failed,
            per_hour=(len(self.recent) - 1) / elapsed * 3600 if elapsed > 0 else None,
        )
        report_progress(writer=self.writer, clocks={}, tally=tally)
        self.written = time.monotonic()


class Terminated(RetryRequested):
    """An eval run stopped on request, SIGTERM, or an approaching deadline."""


@contextmanager
def _terminable() -> Iterator[None]:
    """Turn SIGTERM into `Terminated` while matches are played, on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminated(signum: int, frame: FrameType | None) -> NoReturn:
        raise Terminated("evaluation received SIGTERM")

    before = signal.signal(signal.SIGTERM, terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, before)


@dataclass
class Playing:
    """A live tournament, its remaining grants and its retained outcomes."""

    run: EvalRun
    play: Function
    tournament: str
    agent_loader: AgentLoader
    streaming: bool = False
    pending: deque[Match] = field(init=False)
    skipped: int = field(init=False)
    played: int = 0
    failure: EtudeError | RetryRequested | None = None

    def __post_init__(self) -> None:
        self.pending = deque(m for m in self.run.matches if m.key not in self.run.done)
        self.skipped = len(self.run.matches) - len(self.pending)

    def grant(self, count: int, *, processes: int = 1) -> tuple[Match, ...]:
        """Reserve up to one process's share of the remaining matches."""
        count = min(count, math.ceil(len(self.pending) / processes))
        granted: list[Match] = []
        while self.pending and len(granted) < count:
            match = self.pending.popleft()
            if match.key not in self.run._failed and match.key not in self.run.done:
                granted.append(match)
        return tuple(granted)

    def _remember_failure(self, error: EtudeError | RetryRequested) -> None:
        if self.failure is None or (
            isinstance(error, RetryRequested) and not isinstance(self.failure, RetryRequested)
        ):
            self.failure = error

    def fail_checkpoint(self, failure: CheckpointFailure) -> None:
        affected = [
            match
            for match in self.run.matches
            if match.key not in self.run.done
            and match.key not in self.run._failed
            and any(
                checkpoint.key == failure.checkpoint
                for player in match.players
                for checkpoint in player_checkpoints(player)
            )
        ]
        if not affected:
            return
        self._remember_failure(
            RetryRequested(failure.error)
            if failure.retry
            else EtudeError("match_failed", 500, detail=failure.error)
        )
        for match in affected:
            self.run._stage_failure(match, failure.error, retry=failure.retry)
        self.run._seal()
        self.pending = deque(match for match in self.pending if match.key not in self.run._failed)

    def fail_matches(self, matches: Sequence[Match], error: BaseException) -> None:
        if not self.streaming:
            # A retry raised by per-match play interrupts the whole pass.
            if isinstance(error, RetryRequested):
                raise error
            if isinstance(error, ForwardError) and error.retry:
                raise RetryRequested(str(error)) from error
        self._remember_failure(self.run._record_failure(matches, error))
        if not self.streaming:
            # Stop grants while already-running matches report their outcomes.
            self.pending.clear()

    def accept(self, match: Match, outcome: Outcome, started: float) -> None:
        if match.key in self.run._failed:
            return
        if outcome.error is not None:
            self.fail_matches((match,), outcome.error)
            return
        try:
            if outcome.result is None:
                self.run.fail(
                    match, RuntimeError("game process returned neither a result nor an error")
                )
            self.run._store_played(match, outcome.result, outcome.seconds)
        except EtudeError as error:
            if error.code != "match_failed":
                raise
            self._remember_failure(error)
            self.pending.clear()
            return
        self.played += 1
        print(
            f"etude: {self.run.name}: {self.played}/{len(self.run.matches) - self.skipped} matches played, "
            f"{time.monotonic() - started:.0f}s elapsed",
            file=sys.stderr,
            flush=True,
        )


@dataclass
class _GameStream:
    playing: Playing
    held: dict[str, Match]
    reserved: deque[Match]
    ended: bool = False

    def take(self, count: int, processes: int) -> tuple[Match, ...]:
        each = self.playing
        available = len(each.pending) + len(self.reserved)
        # The startup reservation belongs to this stream until used or released.
        share = max(len(self.reserved), math.ceil(available / processes))
        count = min(count, share)
        granted: list[Match] = []
        while self.reserved and len(granted) < count:
            match = self.reserved.popleft()
            if match.key not in each.run._failed:
                granted.append(match)
        return (*granted, *each.grant(count - len(granted)))

    def release_reservation(self) -> None:
        self.playing.pending.extendleft(
            match for match in reversed(self.reserved) if match.key not in self.playing.run._failed
        )
        self.reserved.clear()

    def restore(self) -> None:
        self.playing.pending.extendleft(reversed(self.held.values()))
        self.release_reservation()


@dataclass
class _GameProcess:
    process: multiprocessing.context.SpawnProcess
    pipe: Connection
    streams: tuple[_GameStream, ...]
    done: bool = False

    @property
    def failure_streams(self) -> tuple[_GameStream, ...]:
        """Attribute a failure to unfinished streams, or to all after teardown."""
        return tuple(stream for stream in self.streams if not stream.ended) or self.streams


def play_together(
    plays: Sequence[Playing],
    *,
    artifacts: Path,
    checkout: str | None = None,
) -> tuple[EvalResult | EtudeError | RetryRequested, ...]:
    """Run all pending tournament queues under one placement."""
    pending = tuple(each for each in plays if each.pending)
    failed_owner: tuple[Playing, ...] = ()
    agents = pending[0].agent_loader if pending else AgentLoader()
    if any(each.agent_loader != agents for each in pending):
        raise EtudeError("incompatible_eval_agents", 422)
    started = time.monotonic()
    failed_checkpoints: set[str] = set()

    def fail_checkpoints(failures: Sequence[CheckpointFailure] = ()) -> None:
        for failure in failures:
            if failure.checkpoint in failed_checkpoints:
                continue
            failed_checkpoints.add(failure.checkpoint)
            for each in plays:
                each.fail_checkpoint(failure)

    def run_pending(owned: Resources) -> None:
        nonlocal failed_owner
        if any(each.run.should_stop for each in pending):
            raise Terminated("evaluation stopped before its execution deadline")
        stopping = False
        warned_imports = False
        needs_loading = False
        match_bytes = 0
        if agents.load is not None:
            for each in pending:
                for match in each.pending:
                    sizes: dict[str, int] = {}
                    for player in match.players:
                        for checkpoint in player_checkpoints(player):
                            key = checkpoint.key
                            size = checkpoint.artifact.size_bytes or 0
                            sizes[key] = max(sizes.get(key, 0), size)
                    if sizes:
                        needs_loading = True
                        match_bytes = max(match_bytes, sum(sizes.values()))
        placement = place(
            [TournamentQueue(len(each.pending), streaming=each.streaming) for each in pending],
            loader=agents,
            checkpoints=needs_loading,
            match_bytes=match_bytes,
        )
        load = agents.load if placement.bus == "local" else None
        games_per_process = placement.games_per_process

        with ExitStack() as agent_lifetime:
            agent_process: AgentProcess | None = None
            if placement.bus == "socket":
                agent_process = agent_lifetime.enter_context(
                    running_agents(agents, checkout, owned, placement)
                )
            agent_address = agent_process.address if agent_process is not None else None

            def record_report(report: GameReport | None) -> None:
                nonlocal warned_imports
                if report is None:
                    return
                name = f"game-{report.pid}.json"
                try:
                    DirectoryStorage(owned.artifacts).put(name, report.model_dump_json().encode())
                except OSError as error:
                    raise RetryRequested(f"game report {name} could not be retained") from error
                if placement.bus is not None and not warned_imports:
                    warning = report.imports.warning(count_limit=placement.bus == "socket")
                    if warning:
                        print(warning, file=sys.stderr, flush=True)
                        warned_imports = True

            context = multiprocessing.get_context("spawn")
            live: list[_GameProcess] = []
            serving: Counter[int] = Counter()
            stream_groups = [pending] if placement.share_streams else [(each,) for each in pending]

            def spawn_one(targets: tuple[Playing, ...], shares: dict[int, int]) -> _GameProcess:
                parent, child_pipe = context.Pipe()
                # Starting async games own their initial matches before any outcome is handled.
                # Streaming engines request work when they have free slots.
                specs = []
                streams = []
                for each in targets:
                    concurrency = None if each.streaming else games_per_process
                    starters = shares[id(each)] - serving[id(each)]
                    initial: tuple[Match, ...] = ()
                    reserved: tuple[Match, ...] = ()
                    if concurrency is not None:
                        initial = each.grant(concurrency, processes=starters)
                    else:
                        # Direct engines start with enough work to use their batch.
                        # Other streams reserve one match and share the rest on demand.
                        batch = agents.max_batch if placement.share_streams else 1
                        reserved = each.grant(batch, processes=starters)
                    streams.append(
                        _GameStream(
                            playing=each,
                            held={match.key: match for match in initial},
                            reserved=deque(reserved),
                        )
                    )
                    specs.append(
                        Stream(
                            tournament=each.tournament,
                            function=each.play,
                            concurrency=concurrency,
                            initial=initial,
                        )
                    )
                process = context.Process(
                    target=run_game_process,
                    args=(child_pipe, owned, tuple(specs)),
                    kwargs={
                        "threads": placement.game_threads,
                        "agents": agent_address,
                        "checkout": checkout,
                        "build": agents.build,
                        "load": load,
                        "checkpoint_bytes": placement.checkpoint_bytes,
                        "max_batch": agents.max_batch,
                    },
                )
                try:
                    process.start()
                except BaseException:
                    for stream in streams:
                        stream.restore()
                    parent.close()
                    child_pipe.close()
                    raise
                child_pipe.close()
                return _GameProcess(process=process, pipe=parent, streams=tuple(streams))

            def spawn_ready() -> None:
                nonlocal stopping
                if len(live) >= placement.processes or stopping:
                    return
                assigned: Counter[int] = Counter()
                for child in live:
                    for stream in child.streams:
                        assigned[id(stream.playing)] += len(stream.held) + len(stream.reserved)
                shares = dict(
                    zip(
                        map(id, pending),
                        placement.process_shares(
                            [
                                TournamentQueue(
                                    len(each.pending) + assigned[id(each)], streaming=each.streaming
                                )
                                for each in pending
                            ],
                            agents.max_batch,
                        ),
                        strict=True,
                    )
                )
                while len(live) < placement.processes:
                    spawned = False
                    for group in stream_groups:
                        if len(live) >= placement.processes:
                            break
                        targets = tuple(
                            each
                            for each in group
                            if each.pending and serving[id(each)] < shares[id(each)]
                        )
                        if not targets:
                            continue
                        if any(each.run.should_stop for each in targets):
                            stopping = True
                            return
                        live.append(spawn_one(targets, shares))
                        serving.update(map(id, targets))
                        spawned = True
                    if not spawned:
                        break

            def fail_stream(stream: _GameStream, error: BaseException) -> None:
                each = stream.playing
                matches = list(stream.held.values())
                if not matches and not each.streaming and each.pending:
                    matches = [each.pending[0]]
                matches = [
                    match
                    for match in matches
                    if match.key not in each.run._failed and match.key not in each.run.done
                ]
                if matches or each.streaming:
                    each.fail_matches(matches, error)
                if each.streaming:
                    each.pending.clear()
                    # A broken stream also revokes reservations its peers have yet to claim.
                    for peer in live:
                        for opened in peer.streams:
                            if opened.playing is each:
                                opened.reserved.clear()

            def end_stream(stream: _GameStream) -> None:
                stream.held.clear()
                stream.ended = True
                serving[id(stream.playing)] -= 1

            def handle_message(child: _GameProcess, message: object) -> None:
                nonlocal stopping, failed_owner
                if isinstance(message, Take):
                    stream = child.streams[message.stream]
                    each = stream.playing
                    if not stopping and (each.pending or stream.reserved) and each.run.should_stop:
                        stopping = True
                    granted: tuple[Match, ...] = ()
                    if not stopping and message.count > 0:
                        processes = serving[id(each)] if each.streaming else 1
                        granted = stream.take(message.count, processes)
                        stream.held.update((match.key, match) for match in granted)
                    child.pipe.send(Grant(granted))
                elif isinstance(message, Outcome):
                    stream = child.streams[message.stream]
                    held = stream.held
                    # A retry during acceptance still belongs to this grant.
                    fail_checkpoints(message.loading_failures)
                    stream.playing.accept(held[message.match_key], message, started)
                    del held[message.match_key]
                elif isinstance(message, StreamEnd):
                    fail_checkpoints(message.loading_failures)
                    stream = child.streams[message.stream]
                    each = stream.playing
                    stream.release_reservation()
                    if message.error is not None:
                        fail_stream(stream, message.error)
                    elif stream.held:
                        fail_stream(
                            stream,
                            RuntimeError("play_many returned without yielding its held matches"),
                        )
                    elif each.streaming and not stopping and each.pending and not message.matches:
                        fail_stream(
                            stream,
                            RuntimeError("play_many returned without taking pending matches"),
                        )
                    end_stream(stream)
                elif isinstance(message, (Done, Fatal)):
                    fail_checkpoints(message.loading_failures)
                    owners = child.failure_streams
                    try:
                        record_report(message.report)
                        if isinstance(message, Fatal):
                            if isinstance(message.error, RetryRequested):
                                raise message.error
                            for stream in owners:
                                each = stream.playing
                                each._remember_failure(each.run._record_failure((), message.error))
                                if not stream.ended:
                                    fail_stream(stream, message.error)
                                    end_stream(stream)
                    except RetryRequested:
                        failed_owner = tuple(stream.playing for stream in owners)
                        raise
                    child.done = True
                    child.pipe.close()
                else:
                    raise TypeError(f"unexpected game message {type(message).__name__}")

            def receive(child: _GameProcess) -> None:
                try:
                    message = child.pipe.recv()
                except (EOFError, OSError):
                    child.pipe.close()
                else:
                    handle_message(child, message)

            try:
                spawn_ready()
                description = placement.describe(
                    [each.tournament for each in pending],
                    streaming=any(each.streaming for each in pending),
                )
                if description is not None:
                    print(description, file=sys.stderr, flush=True)
                while live:
                    left = seconds_left()
                    allowance = (
                        None
                        if left is None
                        else max(0.0, left - max(each.run._slowest for each in pending))
                    )
                    handles: list[Connection | int] = []
                    for child in live:
                        if not child.pipe.closed:
                            handles.append(child.pipe)
                        handles.append(child.process.sentinel)
                    if agent_process is not None:
                        handles.append(agent_process.process.sentinel)
                    ready = set(wait(handles, timeout=allowance))
                    if agent_process is not None:
                        agent_process.check()
                        if agent_process.process.sentinel in ready:
                            raise RetryRequested("agent process exited before games finished")
                    if not ready:
                        raise RetryRequested("evaluation deadline reached before a match completed")
                    for child in live:
                        if not child.pipe.closed and child.pipe in ready:
                            receive(child)
                    exited = [child for child in live if child.process.sentinel in ready]
                    # Retain buffered outcomes before reporting a game process crash.
                    for child in exited:
                        while not child.pipe.closed and child.pipe.poll():
                            receive(child)
                    for child in exited:
                        child.process.join()
                        if not child.done or child.process.exitcode != 0:
                            failed_owner = tuple(stream.playing for stream in child.failure_streams)
                            raise RetryRequested("game process exited before returning its match")
                        live.remove(child)
                        child.process.close()
                        child.pipe.close()
                    spawn_ready()
            except RetryRequested:
                # Keep unreturned grants eligible for retry after the children are reaped.
                for child in live:
                    for stream in child.streams:
                        if not stream.ended:
                            stream.restore()
                raise
            finally:
                for child in live:
                    if child.process.is_alive():
                        child.process.terminate()
                for child in live:
                    child.process.join()
                    child.process.close()
                    child.pipe.close()
            if agent_process is not None:
                agent_process.check()
            if stopping:
                raise Terminated("evaluation stopped before its execution deadline")

    try:
        if pending:
            with _terminable(), resources(artifacts) as owned:
                run_pending(owned)
    except RetryRequested as error:
        affected = False
        for each in plays:
            if each.run.fail_pending(error, each.pending):
                each.failure = error
                affected = True
            each.pending.clear()
        owners = failed_owner or (() if affected else pending)
        if not affected and not owners:
            raise
        for each in owners:
            each.failure = error
    finally:
        for each in plays:
            each.run._report.failed += each.run.failed
        for each in plays:
            each.run._report.write()
    return tuple(
        each.failure
        if each.failure is not None
        else EvalResult(
            experiment=each.run.experiment,
            name=each.run.name,
            environment=each.run.environment,
            played=each.played,
            skipped=each.skipped,
        )
        for each in plays
    )
