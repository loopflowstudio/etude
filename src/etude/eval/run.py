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
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
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
    GAME_IMPORT_FIX,
    Done,
    Game,
    GameReport,
    Outcome,
    Play,
    PlayMany,
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
from etude.eval.resources import ExecutionPlan, Resources, plan_execution, resources, seconds_left
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
        artifacts = self.files.root / "artifacts" / "agents"
        tournament = Playing(
            run=self,
            play=function,
            tournament=self.name,
            agent_loader=definition,
            streaming=streaming,
        )
        (result,) = play_together((tournament,), artifacts=artifacts)
        if isinstance(result, (EtudeError, RetryRequested)):
            raise result
        return result

    def _store_played(self, match: Match, result: Tables | Result, seconds: float) -> None:
        self.store(match, result, seconds=seconds)
        self.done.add(match.key)
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

    def fail_pending(self, error: RetryRequested) -> None:
        """Mark unfinished matches failed with retry=True before the run fails."""
        for match in self.matches:
            if match.key not in self.done and match.key not in self._failed:
                self._stage_failure(match, str(error), retry=True)
        self._seal()

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
        # The scheduler may hold this failure until in-flight outcomes are retained.
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
    """SIGTERM reached an eval run: what is staged is sealed before it ends."""


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

    def fail_checkpoint(self, error: ForwardError) -> None:
        affected = [
            match
            for match in self.run.matches
            if match.key not in self.run.done
            and match.key not in self.run._failed
            and any(
                c.key == error.checkpoint
                for player in match.players
                for c in player_checkpoints(player)
            )
        ]
        if affected:
            self.failure = self.run._record_failure(affected, error)
            self.pending = deque(
                match for match in self.pending if match.key not in self.run._failed
            )

    def fail_matches(self, matches: Sequence[Match], error: BaseException) -> None:
        if not self.streaming:
            # A retry raised by per-match play interrupts the whole pass.
            if isinstance(error, RetryRequested):
                raise error
            if isinstance(error, ForwardError) and error.retry:
                raise RetryRequested(str(error)) from error
            self.pending.clear()
        self._remember_failure(self.run._record_failure(matches, error))

    def accept(self, match: Match, outcome: Outcome, started: float) -> None:
        if match.key in self.run._failed:
            return
        if isinstance(outcome.result, BaseException):
            self.fail_matches((match,), outcome.result)
            return
        try:
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


def play_together(
    plays: Sequence[Playing],
    *,
    artifacts: Path,
    checkout: str | None = None,
) -> tuple[EvalResult | EtudeError | RetryRequested, ...]:
    """Play tournaments sharing one progress report and retain unfinished work on interruption."""
    if not plays:
        return ()
    report = plays[0].run._report
    try:
        with _terminable(), resources(artifacts) as owned:
            pending = [each for each in plays if each.pending]
            if pending:
                _play_pending(pending, owned, checkout)
    except RetryRequested as error:
        delivery_failure = None
        for each in plays:
            try:
                each.run.fail_pending(error)
            except RetryRequested as undelivered:
                # The block is already sealed locally. Keep recording the other runs.
                delivery_failure = undelivered
        if delivery_failure is not None:
            raise delivery_failure from error
        raise
    finally:
        report.failed = sum(each.run.failed for each in plays)
        report.write()
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


def _play_pending(plays: Sequence[Playing], resources: Resources, checkout: str | None) -> None:
    definition = plays[0].agent_loader
    if any(each.agent_loader != definition for each in plays):
        raise EtudeError("incompatible_eval_agents", 422)
    has_checkpoints = definition.load is not None and any(
        player_checkpoints(player)
        for each in plays
        for match in each.pending
        for player in match.players
    )
    matches = sum(len(each.pending) for each in plays)
    plan = plan_execution(matches, definition, checkpoints=has_checkpoints)
    with (
        running_agents(definition, checkout, resources, plan) if plan.shared else nullcontext()
    ) as process:
        _play_matches(
            plays,
            resources=resources,
            plan=plan,
            checkout=checkout,
            agent_process=process,
            load=definition.load if has_checkpoints and not plan.shared else None,
        )
        if process is not None:
            process.check()


@dataclass
class _GameProcess:
    playing: Playing
    process: multiprocessing.context.SpawnProcess
    pipe: Connection
    held: dict[str, Match] = field(default_factory=dict)
    done: bool = False

    def finish(self, message: Done, *, stopping: bool) -> None:
        self.done = True
        self.pipe.close()
        each = self.playing
        error = message.error
        if error is None:
            if self.held:
                error = RuntimeError("play_many returned without yielding its held matches")
            elif each.streaming and not stopping and each.pending and not message.report.matches:
                error = RuntimeError("play_many returned without taking pending matches")
            else:
                return
        matches = list(self.held.values())
        if not matches and not each.streaming and each.pending:
            matches.append(each.pending.popleft())
        matches = [
            match
            for match in matches
            if match.key not in each.run._failed and match.key not in each.run.done
        ]
        if matches or each.streaming:
            # A broken stream stops grants even after releasing its last match.
            each.pending.clear()
            each.fail_matches(matches, error)


def _play_matches(
    plays: Sequence[Playing],
    *,
    resources: Resources,
    plan: ExecutionPlan,
    checkout: str | None,
    agent_process: AgentProcess | None,
    load: Function | None,
) -> None:
    """Play tournament streams in spawned processes and retain outcomes on the invoking thread."""
    started = time.monotonic()
    warned_imports = False
    failed_checkpoints: set[str] = set()

    def fail_checkpoints(failures: Sequence[ForwardError]) -> None:
        for failure in failures:
            if failure.checkpoint in failed_checkpoints:
                continue
            failed_checkpoints.add(failure.checkpoint)
            for each in plays:
                each.fail_checkpoint(failure)

    def record_report(report: GameReport) -> None:
        nonlocal warned_imports
        name = f"game-{report.pid}.json"
        try:
            DirectoryStorage(resources.artifacts).put(name, report.model_dump_json().encode())
        except OSError as error:
            raise RetryRequested(f"game report {name} could not be retained") from error
        if agent_process is not None and not warned_imports:
            if (warning := report.imports.warning) is not None:
                print(f"etude: {warning}; {GAME_IMPORT_FIX}", file=sys.stderr, flush=True)
                warned_imports = True

    stopping = any(each.run.should_stop for each in plays)
    context = multiprocessing.get_context("spawn")
    live: list[_GameProcess] = []
    address = agent_process.address if agent_process is not None else None

    def spawn_one(each: Playing) -> _GameProcess:
        parent, child_pipe = context.Pipe()
        # Starting play processes own their first matches even if another match fails
        # before they finish importing the game. Streaming engines request their own slots.
        initial = () if each.streaming else each.grant(plan.games)
        game = Game(
            play=each.play,
            tournament=each.tournament,
            concurrency=None if each.streaming else plan.games,
            initial=initial,
            agent_address=address,
            checkout=checkout,
            build=each.agent_loader.build,
            load=load,
            memory=plan.checkpoint_bytes,
            max_batch=each.agent_loader.max_batch,
        )
        process = context.Process(target=run_game_process, args=(child_pipe, resources, game))
        process.start()
        child_pipe.close()
        return _GameProcess(
            playing=each, process=process, pipe=parent, held={m.key: m for m in initial}
        )

    def spawn_ready() -> None:
        nonlocal stopping
        while len(live) < plan.processes and not stopping:
            spawned = False
            for each in plays:
                if len(live) >= plan.processes:
                    break
                if not each.pending:
                    continue
                if each.streaming and sum(child.playing is each for child in live) >= len(
                    each.pending
                ):
                    continue
                if each.run.should_stop:
                    stopping = True
                    return
                live.append(spawn_one(each))
                spawned = True
            if not spawned:
                break

    def handle_message(child: _GameProcess, message: object) -> None:
        nonlocal stopping
        each = child.playing
        if isinstance(message, Take):
            stopping = stopping or each.run.should_stop
            processes = (
                sum(other.playing is each and not other.done for other in live)
                if each.streaming
                else 1
            )
            granted = () if stopping else each.grant(message.count, processes=processes)
            child.held.update((match.key, match) for match in granted)
            child.pipe.send(granted)
        elif isinstance(message, Outcome):
            match = child.held.pop(message.match_key)
            if isinstance(message.result, ForwardError) and message.result.loading:
                fail_checkpoints((message.result,))
            each.accept(match, message, started)
        elif isinstance(message, Done):
            fail_checkpoints(message.loading_failures)
            record_report(message.report)
            child.finish(message, stopping=stopping)
        else:
            raise TypeError(f"unexpected game message {type(message).__name__}")

    try:
        spawn_ready()
        if live:
            _log_execution(plays, plan, len(live))
        while live:
            left = seconds_left()
            allowance = (
                None if left is None else max(0.0, left - max(each.run._slowest for each in plays))
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
                exited = child.process.sentinel in ready
                if child.pipe in ready or exited:
                    # Drain buffered outcomes before interpreting the child's exit status.
                    while not child.pipe.closed and child.pipe.poll():
                        try:
                            message = child.pipe.recv()
                        except (EOFError, OSError):
                            child.pipe.close()
                            break
                        handle_message(child, message)
                        if not exited:
                            break
            # Retain buffered outcomes from every child before raising on a crashed process.
            for child in live.copy():
                if child.process.sentinel not in ready:
                    continue
                child.process.join()
                exitcode = child.process.exitcode
                child.process.close()
                child.pipe.close()
                live.remove(child)
                if not child.done or exitcode != 0:
                    raise RetryRequested("game process exited before returning its match")
            spawn_ready()
    finally:
        for child in live:
            if child.process.is_alive():
                child.process.terminate()
        for child in live:
            child.process.join()
            child.process.close()
            child.pipe.close()
    if stopping:
        raise Terminated("evaluation stopped before its execution deadline")


def _log_execution(plays: Sequence[Playing], plan: ExecutionPlan, processes: int) -> None:
    streaming = any(each.streaming for each in plays)
    process_count = f"{processes} game {'process' if processes == 1 else 'processes'}"
    game_count = f"{plan.games} {'game' if plan.games == 1 else 'games'} each"
    capacity = f"up to {game_count}"
    if streaming:
        capacity = "slots requested by play_many"
        if any(not each.streaming for each in plays):
            capacity += f"; play holds up to {game_count}"
    if plan.inference in ("no loader", "no checkpoints"):
        if streaming:
            print(f"etude: {process_count}, {capacity}", file=sys.stderr, flush=True)
        return

    tournaments = ", ".join(each.tournament for each in plays)
    mode = "shared" if plan.shared else "direct"
    threads = f"{plan.threads} inference threads" if plan.threads != 1 else "1 inference thread"
    if not plan.shared:
        threads += " each"
    print(
        f"etude: agents: {mode} inference for {tournaments} ({plan.inference}); "
        f"{process_count}, {capacity}; {threads}",
        file=sys.stderr,
        flush=True,
    )
