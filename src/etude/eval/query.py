"""Querying an experiment's eval runs: `statsdb` opens its database; `compare` asks one question.

`statsdb` gives one table per author table, holding the rows of every eval
run of the experiment (a match's rows from the latest file holding them),
beside Etude's own tables:

| table | columns |
|---|---|
| `runs` | run, experiment, environment (identity), environment_name, environment_config, tournament, state (`finished` or `failed` from its `outcome.json`; null while it has not ended, so its results are partial) |
| `players` | player (its key, encoded), name (the latest its seats give), names (every one, the latest first), kind (`model`, `composition` or `code`), checkpoint (its key), artifact (digest), uri, config, seated_at, lines, and `config.KEY` per scalar config key |
| `matches` | run, match, config, games, seed, state, retry, error, wall_ms, execution (result provenance), newest (of the match's done records in its environment, 1 for the latest recorded, the one that counts; null for a failure) |
| `seats` | run, match, seat, player, name (what the record calls it), the version that played: commit, source (digest), at (where its checkpoint stood, or null) and line (the learner it belongs to, or null) |
| `groups` | run, match, group, seat |
| `tournaments` | tournament, format (`benchmark` or `round_robin`), environment, conflict |
| `schedule` | tournament, match, seating, checkpoint (its key, the one held) and the seating's tags of a benchmark's match; no checkpoint for a seating of fixed players |
| `training_checkpoints` | run, experiment, ordinal, checkpoint (its key, `EXPERIMENT/RUN/checkpoints/NAME`), artifact (digest), wall_ms, at (where each of its run's clocks stood) |
| `blobs` | run, match (none for a converted run's other files), sha256, size_bytes |
| ROLLUP (one per `etude.eval.records.Rollup`) | run, the rollup's `by` columns, rows, and its `sums` |

A tournament is assembled from the experiment's eval runs naming it in their
`tournament.json`. `schedule` holds every match it calls for, held or not.
When its eval runs disagree, `conflict` names how (`tournament_conflict`,
`seating_conflict`) and it has no schedule.
`training_checkpoints` holds every checkpoint the experiment's training runs
recorded, read from their stored JSON, so a model joins to where its
run's clocks stood by key (`players.checkpoint = training_checkpoints.checkpoint`),
never by its bytes. A seat may say where its checkpoint stood itself
(`etude.eval.records.CheckpointRef.at`): `players.seated_at` lists every distinct
`at` match records give it, the latest record's first, and `players.lines` every
learner they say it belongs to (`CheckpointRef.line`), and `players.names` every
name, the same way. A match counts once per environment whichever version
played it: the latest done record holding it, `matches.newest = 1`
(`seats.commit` tells which version that was).

The database is a DuckDB file under `ETUDE_HOME/statsdb`, one per experiment
and store, derived from the store and never the authority. Opening it lists
the store once and reads only the files it has not read before: an eval
run's blocks (`etude.eval.block`), tournament and checkpoint files are
written once, so nothing already held is read again. A block costs one read
of its index and one of each table and rollup; its blobs are read only by
`read_blobs`. A rollup's totals are added to as blocks arrive (`_rollup`),
never summed again from earlier blocks or raw rows.
A failed match's record is replaced by the done record that follows it
(`etude.eval.records.outcomes`). An
eval run still kept a file per match raises `eval_run_not_converted`. The few tables that summarize the rest
(`runs`, `players`, `tournaments`, `schedule`) are rebuilt from what the
database holds whenever something arrives. Etude's bookkeeping lives in the
`kept` schema, out of `show tables`.

`compare` sets a subject against each opponent and against all of them. The
subject of a `seat` row is its seat's player; of a `group` row, the group's
players. Opponents are the other seats' players, or the match's other groups
except `all`.

`estimate` gives many ratios of totals over the same resampling units, such as
matches, each with an interval from one shared bootstrap; `compare` and the
tournament's `scores` use it.

Table files published etude-sdk wrote name the seat `player`; such a file has
no `seat` column, since every file written since has one, and its `player`
reads as `seat`. Results from different environments are never pooled.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import tempfile
import threading
import time
import warnings
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
import numpy.typing as npt
from pydantic import JsonValue, TypeAdapter, ValidationError

from etude.cache import lock_file
from etude.carriage import EVAL_BLOCK_INDEX, EVAL_BLOCK_PACK, EVAL_BLOCKS
from etude.checkpoints import CheckpointRecord
from etude.errors import EtudeError
from etude.eval.block import (
    BLOCK_FILE,
    Block,
    file_key,
    partial_totals,
    per_file,
    quote,
    refuse_unconverted,
    stage,
)
from etude.eval.match import KIND
from etude.eval.records import (
    COLUMN_TYPES,
    IDENTIFIER,
    ROLLUP_ROWS,
    TOURNAMENT_FILE,
    Benchmark,
    ColumnType,
    Environment,
    Player,
    Rollup,
    Scheduled,
    TournamentRun,
    as_config,
    assemble,
    outcomes,
    player_checkpoint,
)
from etude.eval.run import ETUDE_TABLES
from etude.home import home
from etude.identity import SAFE_NAME
from etude.records import Digest, Record, RunOutcome
from etude.run import RunListing, as_storage
from etude.storage import DirectoryStorage, Storage, default_root, store_identity

__all__ = ("statsdb", "read_blobs", "estimate", "compare", "Estimate", "Comparison")


if TYPE_CHECKING:
    import duckdb
    from _typeshed import SupportsRichComparison

REPLICATES = 2000
# Blobs this close together in a pack are read in one range: a member's header lies between.
ADJACENT_BYTES = 1024
SEED = 0
# Floats of bootstrap weights `_weights` keeps across calls: 256 MiB.
WEIGHTS_KEPT = 2**25
# Column types `compare` can take as a value.
NUMERIC = ("BOOLEAN", "BIGINT", "DOUBLE")
# Keys and coordinates of an author table's rows: never a value.
KEYS = ("seat", "group", "game", "run", "match")
# A kept database of another schema is deleted and read again from its store.
SCHEMA = 16
# New blocks ingested per transaction and checkpoint during catch-up.
_CATCH_UP_CHUNK = 64
# Bounded object fetch pool, inside botocore's default 10-connection pool.
_FETCH_WORKERS = 8

# How a match's records order, newest first: by when each was recorded, then by run name.
# `matches.newest` ranks done records so; `etude.eval.tournament.played` orders runs alike.
_NEWEST_FIRST = "m.wall_ms desc, m.run desc"

_ENVIRONMENT_FILE = "environment.json"
_OUTCOME_FILES = ("outcome.json", "state.json")
# mypy reports this alias as an arg-type error or not depending on its cache; pydantic accepts it.
_PLAYER: TypeAdapter[Player] = TypeAdapter(Player)  # type: ignore[arg-type, unused-ignore]


def statsdb(
    experiment: str,
    store: Storage | Path | str | None = None,
    *,
    rebuild: bool = False,
    rollups: Sequence[Rollup] = (),
) -> duckdb.DuckDBPyConnection:
    """The experiment's database of its eval runs, caught up with `store` as it opens.

    `store` is a directory or bucket searched at any depth; by default, where
    runs are written on this computer. Readers share a read-only snapshot.
    Catch-up publishes a new snapshot when readers hold the current one.
    `rebuild` reads everything again before replacing the kept file.

    `rollups` are held from now on beside those blocks bring: a block that
    lacks one has its partial totals summed once from its tables and kept
    here, never written to the store. A name held with another definition
    raises `rollup_conflict`.
    """
    return _open_statsdb(experiment, store, rebuild=rebuild, rollups=rollups)


def _open_statsdb(
    experiment: str,
    store: Storage | Path | str | RunListing | None,
    *,
    rebuild: bool = False,
    rollups: Sequence[Rollup] = (),
    extra_experiments: Sequence[str] = (),
    cache_suffix: str = "",
) -> duckdb.DuckDBPyConnection:
    """Open a read-only snapshot, ingesting new files from this listing when needed."""
    if not SAFE_NAME.match(experiment):
        raise EtudeError("unsafe_run_or_experiment_name", 422)
    allowed = frozenset((experiment, *extra_experiments))
    listing = (
        store
        if isinstance(store, RunListing)
        else RunListing.read(as_storage(default_root() if store is None else store))
    )
    path = _kept_path(experiment, listing.storage, suffix=cache_suffix)
    # DuckDB is the optional eval dependency.
    import duckdb  # noqa: PLC0415

    def current() -> duckdb.DuckDBPyConnection | None:
        if rebuild or not path.exists():
            return None
        db = duckdb.connect(path.resolve(), read_only=True)
        try:
            if not _needs_catch_up(db, allowed, listing, rollups):
                return db
        except BaseException:
            db.close()
            raise
        db.close()
        return None

    try:
        if (db := current()) is not None:
            return db
    except (duckdb.IOException, duckdb.ConnectionException):
        # A writer may be updating in place or retiring the resolved generation.
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_file(path.with_suffix(".lock")):
        try:
            if (db := current()) is not None:
                return db
        except (duckdb.IOException, duckdb.ConnectionException) as locked:
            raise EtudeError("statsdb_in_use", 409) from locked
        _write_catch_up(path, experiment, allowed, listing, rollups, rebuild=rebuild)
        return duckdb.connect(path.resolve(), read_only=True)


def _publish_generation(path: Path, populate: Callable[[Path], None]) -> Path:
    """Populate a fresh generation beside `path` and publish it atomically via symlink."""
    generation = path.with_name(f"{path.stem}.g{time.time_ns()}.{os.getpid()}.duckdb")
    link = path.with_suffix(".link")
    try:
        populate(generation)
        link.unlink(missing_ok=True)
        link.symlink_to(generation.name)
        link.replace(path)
    except BaseException:
        link.unlink(missing_ok=True)
        generation.unlink(missing_ok=True)
        Path(f"{generation}.wal").unlink(missing_ok=True)
        raise
    return generation


def _write_catch_up(
    path: Path,
    experiment: str,
    allowed: frozenset[str],
    listing: RunListing,
    rollups: Sequence[Rollup],
    *,
    rebuild: bool,
) -> None:
    """Catch `path` up in place when uncontested, or publish a new generation when readers hold it."""
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    current = path.resolve() if path.exists() else None
    if not rebuild and current is None:
        # Cold start: publish an empty-schema generation under `.lock` so in-place
        # catch-up below preserves committed chunks across an interrupt.
        def seed_empty(target: Path) -> None:
            with closing(duckdb.connect(target)) as writer:
                _ensure_schema(writer, experiment, listing.storage, rollups, summarize=True)

        current = _publish_generation(path, seed_empty).resolve()
    if not rebuild and current is not None:
        try:
            db = duckdb.connect(current)
        except (duckdb.IOException, duckdb.ConnectionException):
            db = None
        if db is not None:
            try:
                rebuild = _schema(db) not in (None, SCHEMA)
                if not rebuild:
                    _catch_up(db, experiment, allowed, listing, rollups, per_chunk_summaries=True)
                    _sweep_stale_generations(path, current)
                    return
            finally:
                db.close()

    # DuckDB shares connections by realpath in one process. A distinct file
    # lets old readers keep their snapshot while new readers open the new one.
    old_generation = current if path.is_symlink() else None

    def build_generation(target: Path) -> None:
        if current is not None and not rebuild:
            try:
                with closing(duckdb.connect(current, read_only=True)) as reader:
                    if _schema(reader) == SCHEMA:
                        shutil.copy2(current, target)
                        wal = Path(f"{current}.wal")
                        if wal.exists():
                            shutil.copy2(wal, Path(f"{target}.wal"))
            except (duckdb.IOException, duckdb.ConnectionException) as locked:
                raise EtudeError("statsdb_in_use", 409) from locked
        with closing(duckdb.connect(target)) as writer:
            _catch_up(writer, experiment, allowed, listing, rollups, per_chunk_summaries=False)
            writer.execute("checkpoint")

    generation = _publish_generation(path, build_generation)
    if old_generation is not None:
        old_generation.unlink(missing_ok=True)
        Path(f"{old_generation}.wal").unlink(missing_ok=True)
    Path(f"{path}.wal").unlink(missing_ok=True)
    _sweep_stale_generations(path, generation.resolve())


def _sweep_stale_generations(path: Path, keep: Path | None, *, min_age_s: float = 60.0) -> None:
    """Remove orphaned generation snapshots left behind by crashed writers."""
    cutoff = time.time() - min_age_s
    for candidate in path.parent.glob(f"{path.stem}.g*.duckdb"):
        if keep is not None and candidate.resolve() == keep:
            continue
        try:
            if candidate.stat().st_mtime > cutoff:
                continue
        except FileNotFoundError:
            continue
        candidate.unlink(missing_ok=True)
        Path(f"{candidate}.wal").unlink(missing_ok=True)


@dataclass
class _Ingested:
    """The run declarations and object names already represented in one database."""

    declarations: dict[str, str | None]
    kinds: dict[str, str | None]
    environments: set[str]
    seen: dict[str, set[str]]

    @classmethod
    def read(cls, db: duckdb.DuckDBPyConnection) -> _Ingested:
        seen: dict[str, set[str]] = defaultdict(set)
        for run, name in db.execute("select run, name from kept.seen").fetchall():
            seen[run].add(name)
        return cls(
            declarations=dict(
                db.execute("select folder, experiment from kept.declarations").fetchall()
            ),
            kinds=dict(db.execute("select run, kind from kept.runs").fetchall()),
            environments={
                run for (run,) in db.execute("select run from kept.environments").fetchall()
            },
            seen=seen,
        )


def _needs_catch_up(
    db: duckdb.DuckDBPyConnection,
    allowed: frozenset[str],
    listing: RunListing,
    rollups: Sequence[Rollup],
) -> bool:
    """Whether the listing or requested rollups contain anything new for this database."""
    if _schema(db) != SCHEMA:
        return True
    if rollups:
        held_rollups = dict(db.execute("select name, digest from kept.rollups").fetchall())
        if any(held_rollups.get(rollup.name) != rollup.digest for rollup in rollups):
            return True
    kept = _Ingested.read(db)
    for run, folders in listing.folders.items():
        for folder in folders:
            if folder not in kept.declarations:
                if folder + "run.json" in listing.objects:
                    return True
                continue
            if kept.declarations[folder] not in allowed:
                continue
            if kept.kinds[run] == KIND and run not in kept.environments:
                if folder + _ENVIRONMENT_FILE in listing.objects:
                    return True
                continue
            if any(name not in kept.seen[run] for name in listing.names_in(folder)):
                return True
    return (
        db.execute(
            "select 1 from kept.rollups r cross join kept.blocks b"
            " anti join kept.summed s on s.name = r.name and s.run = b.run and s.block = b.block"
            " limit 1"
        ).fetchone()
        is not None
    )


def _kept_path(experiment: str, storage: Storage, *, suffix: str = "") -> Path:
    store = hashlib.sha256(store_identity(storage).encode()).hexdigest()[:16]
    tail = f".{suffix}" if suffix else ""
    return home() / "statsdb" / f"{experiment}.{store}{tail}.duckdb"


def _schema(db: duckdb.DuckDBPyConnection) -> int | None:
    if not _exists(db, "kept", "statsdb"):
        return None
    version = db.execute("select schema from kept.statsdb").fetchone()
    return None if version is None else int(version[0])


def _create(db: duckdb.DuckDBPyConnection, experiment: str, storage: Storage) -> None:
    db.execute("create schema kept")
    db.execute("create table kept.statsdb (schema BIGINT, experiment VARCHAR, store VARCHAR)")
    db.execute("insert into kept.statsdb values (?, ?, ?)", [SCHEMA, experiment, storage.name])
    # Each folder's experiment is retained separately: the same run name can
    # occur in several experiments, and copied folders may sit at any depth.
    db.execute("create table kept.declarations (folder VARCHAR PRIMARY KEY, experiment VARCHAR)")
    db.execute(
        "create table kept.runs (run VARCHAR PRIMARY KEY, experiment VARCHAR, kind VARCHAR, state VARCHAR)"
    )
    # Files of the experiment's runs already read, by run and name within its folder.
    db.execute("create table kept.seen (run VARCHAR, name VARCHAR)")
    db.execute("create table kept.environments (run VARCHAR, environment JSON)")
    db.execute("create table kept.tournament_runs (run VARCHAR, held JSON)")
    db.execute("create table kept.players (player VARCHAR, name VARCHAR, player_json JSON)")
    # The block each author table's rows of a match came from: the latest holding them.
    db.execute(
        'create table kept.table_files ("table" VARCHAR, run VARCHAR, match VARCHAR, file VARCHAR)'
    )
    # Where each blob's bytes lie: its pack's key in the store, and their range.
    db.execute(
        "create table kept.blobs (run VARCHAR, sha256 VARCHAR, pack VARCHAR, start BIGINT,"
        " size_bytes BIGINT)"
    )
    # Per-file names of eval runs a converted block holds.
    db.execute("create table kept.replaced (run VARCHAR, name VARCHAR)")
    # Each rollup held, by name and the digest of its definition; its per-match
    # partial totals are `kept.NAME`.
    db.execute("create table kept.rollups (name VARCHAR, digest VARCHAR, rollup JSON)")
    # Each block read, without its blobs and replaced names, to sum a rollup it lacks.
    db.execute("create table kept.blocks (run VARCHAR, block VARCHAR, held JSON)")
    # The blocks whose partial totals of each rollup are added, from the block or its tables.
    db.execute("create table kept.summed (name VARCHAR, run VARCHAR, block VARCHAR)")
    db.execute("create table blobs (run VARCHAR, match VARCHAR, sha256 VARCHAR, size_bytes BIGINT)")
    db.execute(
        "create table matches (run VARCHAR, match VARCHAR, config JSON, games BIGINT,"
        " seed BIGINT, state VARCHAR, retry BOOLEAN, error VARCHAR, wall_ms BIGINT, execution JSON,"
        " newest BIGINT)"
    )
    # DuckDB keeps deleted unique keys until commit; reconciliation deletes and
    # inserts in one transaction.
    db.execute("create index matches_identity on matches(run, match)")
    db.execute(
        "create table seats (run VARCHAR, match VARCHAR, seat BIGINT, player VARCHAR,"
        ' name VARCHAR, commit VARCHAR, source VARCHAR, "at" JSON, line VARCHAR)'
    )
    db.execute('create table "groups" (run VARCHAR, match VARCHAR, "group" VARCHAR, seat BIGINT)')
    db.execute(
        "create table training_checkpoints (run VARCHAR, experiment VARCHAR, ordinal BIGINT,"
        ' checkpoint VARCHAR, artifact VARCHAR, wall_ms BIGINT, "at" JSON)'
    )


@dataclass
class _EvalSlice:
    """One chunk's portion of an eval run's newly arrived files."""

    run: str
    experiment: str
    located: Mapping[str, str]
    blocks: list[tuple[str, str]]
    seen: list[str]
    tournament_key: str | None = None
    outcome: tuple[str, str] | None = None
    old: list[str] = field(default_factory=list)


@dataclass
class _TrainingSlice:
    """One chunk's portion of a training run's newly arrived files."""

    run: str
    experiment: str
    checkpoints: list[tuple[str, str]]
    seen: list[str]


@dataclass
class _Chunk:
    """Work committed and checkpointed in one catch-up transaction."""

    declarations: list[tuple[str, str | None]] = field(default_factory=list)
    runs: list[tuple[str, str, str | None]] = field(default_factory=list)
    environments: list[tuple[str, str]] = field(default_factory=list)
    evals: list[_EvalSlice] = field(default_factory=list)
    trainings: list[_TrainingSlice] = field(default_factory=list)
    weight: int = 0

    def has_work(self) -> bool:
        return bool(
            self.declarations or self.runs or self.environments or self.evals or self.trainings
        )


def _map_pool[T, R](
    pool: ThreadPoolExecutor | None,
    fn: Callable[[T], R],
    items: Sequence[T],
) -> list[R]:
    """Apply `fn` to `items`, concurrently on `pool` with contextvars when more than one item."""
    if not items:
        return []
    if pool is None or len(items) == 1:
        return [fn(item) for item in items]
    futures = [pool.submit(copy_context().run, fn, item) for item in items]
    try:
        return [future.result() for future in futures]
    except BaseException:
        for future in futures:
            future.cancel()
        raise


def _fetch_all(
    pool: ThreadPoolExecutor | None,
    storage: Storage,
    keys: Sequence[str],
) -> dict[str, bytes]:
    """Fetch distinct `keys` from `storage`, concurrently when `pool` is given."""
    unique = list(dict.fromkeys(keys))
    return dict(zip(unique, _map_pool(pool, storage.get, unique), strict=True))


def _fetch_files(
    pool: ThreadPoolExecutor | None,
    storage: Storage,
    targets: Sequence[tuple[str, Path]],
) -> None:
    """Copy each `(key, destination)` from `storage` to local disk, skipping existing files."""
    if isinstance(storage, DirectoryStorage):
        return
    pending: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for key, path in targets:
        if path in seen or path.exists():
            continue
        seen.add(path)
        pending.append((key, path))

    def write(target: tuple[str, Path]) -> None:
        key, path = target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(storage.get(key))

    _map_pool(pool, write, pending)


@contextmanager
def _transaction(db: duckdb.DuckDBPyConnection, *, checkpoint: bool = False) -> Iterator[None]:
    """Run one DuckDB transaction with rollback on error and optional post-commit checkpoint."""
    db.execute("begin")
    try:
        yield
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise
    if checkpoint:
        db.execute("checkpoint")


def _located(listing: RunListing, folders: Sequence[str]) -> dict[str, str]:
    """Map each file name in a run's folders to its first containing folder."""
    located: dict[str, str] = {}
    for place in folders:
        for name in listing.names_in(place):
            located.setdefault(name, place)
    return located


def _pack_chunks[T](
    items: Sequence[T],
    chunk: _Chunk,
    flush: Callable[[_Chunk], None],
    append: Callable[[_Chunk, list[T], bool, bool], None],
) -> _Chunk:
    """Slice `items` across `_CATCH_UP_CHUNK`-bounded chunks, emitting once when empty."""
    if not items:
        append(chunk, [], True, True)
        chunk.weight += 1
        if chunk.weight >= _CATCH_UP_CHUNK:
            flush(chunk)
            return _Chunk()
        return chunk
    cursor = 0
    total = len(items)
    while cursor < total:
        room = max(1, _CATCH_UP_CHUNK - chunk.weight)
        end = min(total, cursor + room)
        piece = list(items[cursor:end])
        append(chunk, piece, cursor == 0, end == total)
        chunk.weight += len(piece)
        cursor = end
        if chunk.weight >= _CATCH_UP_CHUNK:
            flush(chunk)
            chunk = _Chunk()
    return chunk


def _ensure_schema(
    db: duckdb.DuckDBPyConnection,
    experiment: str,
    storage: Storage,
    rollups: Sequence[Rollup],
    *,
    summarize: bool,
) -> bool:
    """Create the database schema when absent and register requested rollups; return whether created."""
    if _schema(db) is None:
        with _transaction(db, checkpoint=True):
            _create(db, experiment, storage)
            for rollup in rollups:
                _hold(db, rollup)
            if summarize:
                _summaries(db)
        return True
    if rollups:
        held_rollups = dict(db.execute("select name, digest from kept.rollups").fetchall())
        if any(held_rollups.get(rollup.name) != rollup.digest for rollup in rollups):
            with _transaction(db):
                for rollup in rollups:
                    _hold(db, rollup)
    return False


def _catch_up(
    db: duckdb.DuckDBPyConnection,
    experiment: str,
    allowed: frozenset[str],
    listing: RunListing,
    rollups: Sequence[Rollup],
    *,
    per_chunk_summaries: bool = True,
) -> None:
    """Read what arrived in `storage` since the last open, in committed, checkpointed chunks.

    Each chunk fetches its objects concurrently before opening its DuckDB
    transaction, then commits its rows and `kept.seen` names and checkpoints so
    an interrupted in-place catch-up resumes from the last committed chunk.
    In-place catch-up updates summary tables on every eval chunk so the database
    stays complete after each commit; private generations run `_summaries` once
    at the end before publishing. Taking in a block is a fixed number of
    statements however many matches and blobs it holds (`etude.eval.block.stage`).
    """
    storage = listing.storage
    created = _ensure_schema(db, experiment, storage, rollups, summarize=per_chunk_summaries)
    kept = _Ingested.read(db)
    eval_changed = False
    with _fetch_pool(storage) as pool:

        def flush(pending: _Chunk) -> None:
            nonlocal eval_changed
            if _flush_chunk(db, storage, pool, pending, summarize=per_chunk_summaries):
                eval_changed = True

        chunk = _Chunk()
        for batch in itertools.batched(sorted(listing.folders.items()), _CATCH_UP_CHUNK):
            _prefetch_declarations(pool, listing, batch, kept)
            matched_runs: list[tuple[str, list[str], str]] = []
            for run, folders in batch:
                matched: list[tuple[str, str]] = []
                for folder in folders:
                    owner = _resolve_folder(listing, run, folder, allowed, kept, chunk)
                    if owner is not None:
                        matched.append((folder, owner))
                if matched:
                    matched_runs.append((run, [folder for folder, _ in matched], matched[0][1]))
            env_needing = [
                (run, folders)
                for run, folders, _ in matched_runs
                if kept.kinds[run] == KIND and run not in kept.environments
            ]
            env_bodies = _fetch_environments(pool, storage, listing, env_needing)
            for run, folders in env_needing:
                if (encoded := _pick_environment(folders, env_bodies)) is not None:
                    kept.environments.add(run)
                    chunk.environments.append((run, encoded))
            for run, folders, run_experiment in matched_runs:
                eval_run = kept.kinds[run] == KIND
                if eval_run and run not in kept.environments:
                    continue  # an eval run counts once its environment arrives
                # One run may be copied to several places; a file's name is the same in each.
                located = _located(listing, folders)
                new = sorted(located.keys() - kept.seen[run])
                if not new:
                    continue
                if eval_run:
                    chunk = _queue_eval(db, flush, chunk, run, run_experiment, located, new)
                else:
                    chunk = _queue_training(db, flush, chunk, run, run_experiment, located, new)
        if chunk.has_work():
            flush(chunk)
        _backfill_rollups(db, storage, pool, listing, allowed, kept)
    if not per_chunk_summaries and (created or eval_changed):
        with _transaction(db):
            _summaries(db)


@contextmanager
def _fetch_pool(storage: Storage) -> Iterator[ThreadPoolExecutor | None]:
    if isinstance(storage, DirectoryStorage):
        yield None
        return
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        yield pool


def _prefetch_declarations(
    pool: ThreadPoolExecutor | None,
    listing: RunListing,
    batch: Sequence[tuple[str, Sequence[str]]],
    kept: _Ingested,
) -> None:
    unknown = [
        folder
        for _, folders in batch
        for folder in folders
        if folder not in kept.declarations and folder + "run.json" in listing.objects
    ]
    if pool is None or len(unknown) <= 1:
        return
    _map_pool(pool, listing.declaration, unknown)


def _resolve_folder(
    listing: RunListing,
    run: str,
    folder: str,
    allowed: frozenset[str],
    kept: _Ingested,
    chunk: _Chunk,
) -> str | None:
    """Select a folder by its own declaration; record unseen declarations in `chunk`."""
    if folder in kept.declarations:
        owner = kept.declarations[folder]
        return owner if owner is not None and owner in allowed else None
    declaration = listing.declaration(folder)
    if declaration is None:
        return None
    # A group is an operational run. Only its tournaments supply scientific rows.
    owner = declaration.experiment if declaration.members is None else None
    kept.declarations[folder] = owner
    chunk.declarations.append((folder, owner))
    if owner is None or owner not in allowed:
        return None
    kept.kinds.setdefault(run, declaration.kind)
    chunk.runs.append((run, owner, declaration.kind))
    return owner


def _fetch_environments(
    pool: ThreadPoolExecutor | None,
    storage: Storage,
    listing: RunListing,
    needing: Sequence[tuple[str, Sequence[str]]],
) -> dict[str, bytes]:
    keys = [
        place + _ENVIRONMENT_FILE
        for _, folders in needing
        for place in folders
        if place + _ENVIRONMENT_FILE in listing.objects
    ]
    unique = list(dict.fromkeys(keys))

    def read_one(key: str) -> bytes | None:
        try:
            return storage.get(key)
        except FileNotFoundError:
            return None

    return {
        key: body
        for key, body in zip(unique, _map_pool(pool, read_one, unique), strict=True)
        if body is not None
    }


def _pick_environment(folders: Sequence[str], bodies: Mapping[str, bytes]) -> str | None:
    for place in folders:
        body = bodies.get(place + _ENVIRONMENT_FILE)
        if body is None:
            continue
        try:
            environment = Environment.model_validate_json(body)
        except ValidationError:
            continue
        return environment.model_dump_json()
    return None


def _queue_eval(
    db: duckdb.DuckDBPyConnection,
    flush: Callable[[_Chunk], None],
    chunk: _Chunk,
    run: str,
    experiment: str,
    located: Mapping[str, str],
    new: Sequence[str],
) -> _Chunk:
    """Slice one eval run's new files across chunks, flushing each full chunk."""
    old = [name for name in new if per_file(name)]
    blocks: list[tuple[str, str]] = []
    block_ids: set[str] = set()
    for name in new:
        if found := BLOCK_FILE.match(name):
            blocks.append((located[name], found[1]))
            block_ids.add(found[1])
    # Refuse purely unconverted runs before reading any of their objects.
    if old and not blocks:
        _refuse_unconverted(db, run, old)

    by_block: dict[str, list[str]] = defaultdict(list)
    other: list[str] = []
    has_tournament = False
    for name in new:
        if name == TOURNAMENT_FILE:
            has_tournament = True
        elif per_file(name):
            continue
        elif name.startswith(f"{EVAL_BLOCKS}/"):
            parts = name.split("/", 2)
            if len(parts) == 3 and parts[1] in block_ids:
                by_block[parts[1]].append(name)
            else:
                other.append(name)
        else:
            other.append(name)

    outcome_name = next((name for name in _OUTCOME_FILES if name in located), None)
    outcome = (located[outcome_name], outcome_name) if outcome_name is not None else None
    tournament_key = located[TOURNAMENT_FILE] + TOURNAMENT_FILE if has_tournament else None

    def append_eval(target: _Chunk, piece: list[tuple[str, str]], first: bool, last: bool) -> None:
        seen = [TOURNAMENT_FILE] if first and has_tournament else []
        for _, block_name in piece:
            seen.extend(by_block[block_name])
        if last:
            seen.extend(other)
            seen.extend(old)
        target.evals.append(
            _EvalSlice(
                run=run,
                experiment=experiment,
                located=located,
                blocks=piece,
                seen=seen,
                tournament_key=tournament_key if first else None,
                outcome=outcome if last else None,
                old=old if last else [],
            )
        )

    return _pack_chunks(blocks, chunk, flush, append_eval)


def _queue_training(
    db: duckdb.DuckDBPyConnection,
    flush: Callable[[_Chunk], None],
    chunk: _Chunk,
    run: str,
    experiment: str,
    located: Mapping[str, str],
    new: Sequence[str],
) -> _Chunk:
    """Slice one training run's new files across chunks, flushing each full chunk."""
    old = [name for name in new if per_file(name)]
    if old:
        _refuse_unconverted(db, run, old)
    checkpoints = [
        (name, located[name] + name)
        for name in new
        if name.startswith("checkpoints/") and name.endswith(".json")
    ]
    checkpoint_names = {name for name, _ in checkpoints}
    other = [name for name in new if name not in checkpoint_names]

    def append_training(
        target: _Chunk, piece: list[tuple[str, str]], _first: bool, last: bool
    ) -> None:
        seen = [name for name, _ in piece]
        if last:
            seen.extend(other)
        target.trainings.append(
            _TrainingSlice(run=run, experiment=experiment, checkpoints=piece, seen=seen)
        )

    return _pack_chunks(checkpoints, chunk, flush, append_training)


def _flush_chunk(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    pool: ThreadPoolExecutor | None,
    chunk: _Chunk,
    *,
    summarize: bool,
) -> bool:
    """Prefetch a chunk's objects, ingest them in one transaction, checkpoint, and drop copies."""
    with tempfile.TemporaryDirectory(prefix="etude-statsdb-") as copies_root:
        copies = Path(copies_root)
        json_keys: list[str] = []
        for part in chunk.evals:
            if part.tournament_key is not None:
                json_keys.append(part.tournament_key)
            for place, block_name in part.blocks:
                json_keys.append(place + file_key(block_name, EVAL_BLOCK_INDEX))
            if part.outcome is not None:
                json_keys.append(part.outcome[0] + part.outcome[1])
        for training in chunk.trainings:
            for _, key in training.checkpoints:
                json_keys.append(key)
        fetched = _fetch_all(pool, storage, json_keys)

        parsed: dict[tuple[str, str], Block] = {}
        parquet_targets: list[tuple[str, Path]] = []
        for part in chunk.evals:
            for place, block_name in part.blocks:
                index_key = place + file_key(block_name, EVAL_BLOCK_INDEX)
                original = Block.model_validate_json(fetched[index_key])
                parsed[(part.run, block_name)] = original
                for table in original.tables:
                    file = file_key(block_name, f"{table}.parquet")
                    parquet_targets.append((place + file, copies / part.run / file))
                for rollup in original.summed:
                    file = file_key(block_name, f"{rollup.name}.parquet")
                    parquet_targets.append((place + file, copies / part.run / file))
        _fetch_files(pool, storage, parquet_targets)

        with _transaction(db, checkpoint=True):
            for folder, owner in chunk.declarations:
                db.execute(
                    "insert into kept.declarations values (?, ?) on conflict do nothing",
                    [folder, owner],
                )
            for run, owner, kind in chunk.runs:
                db.execute(
                    "insert into kept.runs values (?, ?, ?, null) on conflict do nothing",
                    [run, owner, kind],
                )
            eval_changed = False
            for run, encoded in chunk.environments:
                db.execute("insert into kept.environments values (?, ?)", [run, encoded])
                eval_changed = True
            for part in chunk.evals:
                if part.tournament_key is not None:
                    held = TournamentRun.model_validate_json(
                        fetched[part.tournament_key], context={"experiment": part.experiment}
                    )
                    db.execute(
                        "insert into kept.tournament_runs values (?, ?)",
                        [part.run, held.model_dump_json()],
                    )
                    eval_changed = True
                block_names: list[str] = []
                for place, block_name in part.blocks:
                    _block(
                        db,
                        storage,
                        part.run,
                        place,
                        block_name,
                        copies,
                        experiment=part.experiment,
                        original=parsed[(part.run, block_name)],
                    )
                    block_names.append(block_name)
                    eval_changed = True
                if block_names:
                    _sum_missing(
                        db,
                        storage,
                        part.run,
                        part.located,
                        copies,
                        block_names,
                        experiment=part.experiment,
                    )
                if part.outcome is not None:
                    place, name = part.outcome
                    _ended(db, part.run, fetched[place + name])
                    eval_changed = True
                if part.old:
                    _refuse_unconverted(db, part.run, part.old)
                if part.seen:
                    _put(
                        db,
                        "kept.seen",
                        {"run": "VARCHAR", "name": "VARCHAR"},
                        [[part.run, name] for name in part.seen],
                    )
            for training in chunk.trainings:
                _training_checkpoints(
                    db, training.run, training.experiment, training.checkpoints, fetched
                )
                if training.seen:
                    _put(
                        db,
                        "kept.seen",
                        {"run": "VARCHAR", "name": "VARCHAR"},
                        [[training.run, name] for name in training.seen],
                    )
            if summarize and eval_changed:
                _summaries(db)
        return eval_changed


def _ended(db: duckdb.DuckDBPyConnection, run: str, raw: bytes) -> None:
    """Record how the eval run last ended, from its `outcome.json`.

    Read when it first arrives and again whenever the run delivers new files:
    a resumed run seals new blocks before it writes its end again.
    """
    ended = RunOutcome.model_validate_json(raw)
    db.execute("update kept.runs set state = ? where run = ?", [ended.state, run])


def _refuse_unconverted(db: duckdb.DuckDBPyConnection, run: str, names: Sequence[str]) -> None:
    old = [name for name in names if per_file(name)]
    replaced = db.execute(
        "select name from kept.replaced where run = ? and name in (select unnest(?::VARCHAR[]))",
        [run, old],
    ).fetchall()
    refuse_unconverted(run, old, (name for (name,) in replaced))


def _training_checkpoints(
    db: duckdb.DuckDBPyConnection,
    run: str,
    experiment: str,
    checkpoints: Sequence[tuple[str, str]],
    fetched: Mapping[str, bytes],
) -> None:
    """Take in a training run's prefetched checkpoint files."""
    rows: list[list[object]] = []
    for _, key in checkpoints:
        stored = CheckpointRecord.model_validate_json(fetched[key])
        rows.append(
            [run, experiment, stored.ordinal,
             stored.model_copy(update={"experiment": experiment, "run": run}).key,
             stored.artifact.sha256, stored.wall_ms, json.dumps(stored.at)]
        )  # fmt: skip
    if not rows:
        return
    stage(
        db,
        "_arrived_checkpoints",
        {"run": "VARCHAR", "experiment": "VARCHAR", "ordinal": "BIGINT", "checkpoint": "VARCHAR",
         "artifact": "VARCHAR", "wall_ms": "BIGINT", "at": "VARCHAR"},
        rows,
    )  # fmt: skip
    # One run copied to several places records each checkpoint once.
    db.execute(
        "insert into training_checkpoints select a.* from _arrived_checkpoints a anti join"
        " training_checkpoints t on t.run = a.run and t.ordinal = a.ordinal"
        " qualify row_number() over (partition by a.ordinal) = 1"
    )
    db.execute("drop table _arrived_checkpoints")


def _block(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    place: str,
    name: str,
    copies: Path,
    *,
    experiment: str,
    original: Block,
) -> None:
    """Take in block `name`: its match records, where its blobs lie, then its tables and rollups."""
    # Kept as stored: `_sum_missing` reads its tables' own match keys again.
    held = original.model_dump_json(
        exclude={"blobs": True, "replaces": True, "matches": {"__all__": {"blobs"}}}
    )
    db.execute(f"insert into kept.blocks values ({quote(run)}, {quote(name)}, {quote(held)})")
    with _qualified(db, experiment, original) as block:
        pack = place + file_key(name, EVAL_BLOCK_PACK)
        _put(
            db,
            "kept.blobs",
            {"run": "VARCHAR", "sha256": "VARCHAR", "pack": "VARCHAR", "start": "BIGINT",
             "size_bytes": "BIGINT"},
            [[run, digest, pack, span.offset, span.size_bytes] for digest, span in block.blobs.items()],
        )  # fmt: skip
        _matches(db, run, block)
        # A converted run's files that no match named.
        named = {digest for record in block.matches for digest in record.blobs}
        stage(
            db,
            "_unnamed_blobs",
            {"sha256": "VARCHAR", "size_bytes": "BIGINT"},
            [
                [digest, span.size_bytes]
                for digest, span in block.blobs.items()
                if digest not in named
            ],
        )
        db.execute(
            f"insert into blobs select {quote(run)}, null, a.sha256, a.size_bytes from _unnamed_blobs a"
            f" anti join blobs b on b.run = {quote(run)} and b.match is null and b.sha256 = a.sha256",
        )
        db.execute("drop table _unnamed_blobs")
        _put(db, "kept.replaced", {"run": "VARCHAR", "name": "VARCHAR"},
             [[run, each] for each in block.replaces])  # fmt: skip
        for table in block.tables:
            file = file_key(name, f"{table}.parquet")
            _table(db, run, table, name, _local(storage, place + file, copies / run / file))
        for rollup in block.summed:
            file = file_key(name, f"{rollup.name}.parquet")
            _rollup(db, run, rollup, name, _local(storage, place + file, copies / run / file))
        _put(db, "kept.summed", {"name": "VARCHAR", "run": "VARCHAR", "block": "VARCHAR"},
             [[rollup.name, run, name] for rollup in block.summed])  # fmt: skip


@contextmanager
def _qualified(db: duckdb.DuckDBPyConnection, experiment: str, original: Block) -> Iterator[Block]:
    """The block with released identities qualified by `experiment`.

    Within, `_table` and `_rollup` translate the block's rows alike, from the
    staged `_qualified_matches` and `_qualified_players`.
    """
    block = Block.model_validate(original.model_dump(), context={"experiment": experiment})
    matches = {
        a.match.key: b.match.key for a, b in zip(original.matches, block.matches, strict=True)
    }
    players = {
        a.key: b.key
        for old, new in zip(original.matches, block.matches, strict=True)
        for a, b in zip(old.match.players, new.match.players, strict=True)
    }
    stage(
        db,
        "_qualified_matches",
        {"stored": "VARCHAR", "qualified": "VARCHAR"},
        [[a, b] for a, b in matches.items() if a != b],
    )
    stage(
        db,
        "_qualified_players",
        {"stored": "VARCHAR", "qualified": "VARCHAR"},
        [[a, b] for a, b in players.items() if a != b],
    )
    try:
        yield block
    finally:
        db.execute("drop table _qualified_matches")
        db.execute("drop table _qualified_players")


def _matches(db: duckdb.DuckDBPyConnection, run: str, block: Block) -> None:
    """Hold the block's standing record of each match (`outcomes`), unless one held is done.

    So across the run's blocks, in the order they are read, a done record
    replaces a failure and the first done one stays; newer failures update the retry decision.
    """
    records = list(outcomes(block.matches).values())
    stage(
        db,
        "_arrived_matches",
        {"record": "BIGINT", "match": "VARCHAR", "config": "VARCHAR", "games": "BIGINT",
         "seed": "BIGINT", "state": "VARCHAR", "retry": "BOOLEAN", "error": "VARCHAR", "wall_ms": "BIGINT",
         "execution": "VARCHAR"},
        [[index, record.match.key, json.dumps(as_config(record.match.config)), record.match.games,
          record.match.seed, record.state, record.retry, record.error, record.wall_ms,
          json.dumps(record.execution)]
         for index, record in enumerate(records)],
    )  # fmt: skip
    db.execute(
        "create or replace temp table _taken as select a.* from _arrived_matches a"
        f" left join matches m on m.run = {quote(run)} and m.match = a.match"
        " where m.match is null or (m.state <> 'done' and a.state = 'done')"
        " or (m.state = 'failed' and a.state = 'failed' and a.wall_ms > m.wall_ms)"
    )
    for table in ("matches", "seats", '"groups"', "blobs"):
        db.execute(
            f"delete from {table} t using _taken a where t.run = {quote(run)} and t.match = a.match"
        )
    # `newest` is ranked once everything has arrived (`_summaries`).
    db.execute(
        f"insert into matches select {quote(run)}, match, config, games, seed, state, retry, error, wall_ms,"
        " execution, null from _taken order by record"
    )
    stage(
        db,
        "_arrived_players",
        {"record": "BIGINT", "seat": "BIGINT", "player": "VARCHAR", "name": "VARCHAR",
         "player_json": "VARCHAR", "commit": "VARCHAR", "source": "VARCHAR", "at": "VARCHAR",
         "line": "VARCHAR"},
        [
            [index, seat, player.key, player.name, player.model_dump_json(),
             player.version.commit, player.version.source,
             _canonical(checkpoint.at) if checkpoint is not None and checkpoint.at else None,
             checkpoint.line if checkpoint is not None else None]
            for index, record in enumerate(records)
            for seat, player in enumerate(record.match.players)
            for checkpoint in (player_checkpoint(player),)
        ],
    )  # fmt: skip
    db.execute(
        "create or replace temp table _arrived_seats as select s.*, a.match"
        " from _arrived_players s join _taken a using (record)"
    )
    db.execute(
        f'insert into seats select {quote(run)}, match, seat, player, name, "commit", source, "at", line'
        " from _arrived_seats order by record, seat"
    )
    # Every player a held match seats, as first given under each name.
    db.execute(
        "insert into kept.players select s.player, s.name, s.player_json from _arrived_seats s"
        " anti join kept.players k on k.player = s.player and k.name = s.name"
        " qualify row_number() over (partition by s.player, s.name order by s.record, s.seat) = 1"
        " order by s.record, s.seat"
    )
    stage(
        db,
        "_arrived_groups",
        {"record": "BIGINT", "group": "VARCHAR", "seat": "BIGINT"},
        [[index, name, seat] for index, record in enumerate(records)
         for name, seats in record.match.groups.items() for seat in seats],
    )  # fmt: skip
    db.execute(
        f'insert into "groups" select {quote(run)}, a.match, g."group", g.seat from _arrived_groups g'
        " join _taken a using (record) order by g.record"
    )
    stage(
        db,
        "_arrived_blobs",
        {"record": "BIGINT", "sha256": "VARCHAR", "size_bytes": "BIGINT"},
        [[index, digest, block.blobs[digest].size_bytes]
         for index, record in enumerate(records) for digest in record.blobs],
    )  # fmt: skip
    db.execute(
        f"insert into blobs select {quote(run)}, a.match, b.sha256, b.size_bytes from _arrived_blobs b"
        " join _taken a using (record) order by b.record"
    )
    for table in ("_taken", "_arrived_seats", "_arrived_matches", "_arrived_players",
                  "_arrived_groups", "_arrived_blobs"):  # fmt: skip
        db.execute(f"drop table {table}")


def _put(
    db: duckdb.DuckDBPyConnection,
    table: str,
    columns: Mapping[str, str],
    rows: Sequence[Sequence[object]],
) -> None:
    """Insert `rows` into `table` by column name."""
    stage(db, "_put", columns, rows)
    db.execute(f"insert into {table} by name select * from _put")
    db.execute("drop table _put")


def _canonical(value: object) -> str:
    """JSON with sorted keys, so equal `at`s compare equal as text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _qualify_arrived(db: duckdb.DuckDBPyConnection, *, players: bool = False) -> None:
    """Translate released table identities with the block's own match and player records."""
    if not db.execute("select 1 from _qualified_matches limit 1").fetchone():
        return
    db.execute(
        "update _arrived set match = q.qualified from _qualified_matches q where match = q.stored"
    )
    if not players:
        return
    columns = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
    if columns.get("player") == "VARCHAR":
        db.execute(
            "update _arrived set player = q.qualified from _qualified_players q where player = q.stored"
        )
    if columns.get("opponents") == "VARCHAR[]":
        db.execute(
            "create or replace temp table _arrived as select a.* replace "
            "(list_transform(opponents, p -> coalesce(map_extract_value(q.codes, p), p)) as opponents) "
            "from _arrived a cross join "
            "(select map(list(stored), list(qualified)) as codes from _qualified_players) q"
        )
    elif columns.get("opponents") == "VARCHAR":
        db.execute(
            "update _arrived set opponents = q.qualified from _qualified_players q where opponents = q.stored"
        )


def _table(db: duckdb.DuckDBPyConnection, run: str, table: str, file: str, path: Path) -> None:
    """Add one block's rows of a table, keeping each match's rows from the latest block holding them.

    Blocks holding one match may arrive in either order; their names order by
    their zero-padded seal time.
    """
    db.execute(
        f"create or replace temp table _arrived as select * from read_parquet({quote(path)})"
    )
    _qualify_arrived(db)
    db.execute(
        f'delete from _arrived using kept.table_files f where f."table" = {quote(table)} and f.run = _arrived.run'
        f" and f.match = _arrived.match and f.file > {quote(file)}"
    )
    arrived = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
    if db.execute(f"select 1 from kept.rollups where name = {quote(table)}").fetchone():
        raise EtudeError("rollup_name_conflict", 409) from ValueError(table)
    if not _exists(db, "main", table):
        db.execute(f'create table "{table}" as select * from _arrived')
    else:
        _widen(db, f'"{table}"', arrived)
        db.execute(
            f'delete from "{table}" t using (select distinct run, match from _arrived) a'
            " where t.run = a.run and t.match = a.match"
        )
        db.execute(f'insert into "{table}" by name select * from _arrived')
    db.execute(
        "delete from kept.table_files f using (select distinct run, match from _arrived) a"
        f' where f."table" = {quote(table)} and f.run = a.run and f.match = a.match'
    )
    db.execute(
        f"insert into kept.table_files select distinct {quote(table)}, run, match, {quote(file)} from _arrived"
    )
    db.execute("drop table _arrived")


def _rollup(db: duckdb.DuckDBPyConnection, run: str, rollup: Rollup, file: str, path: Path) -> None:
    """Add one block's partial totals of a rollup into its held totals, table `rollup.name`.

    Partials are held per match (`kept.NAME`), so a match an earlier block
    also holds has that block's partials taken off as the later's are added:
    it counts once, from the latest block. Nothing earlier is summed again.
    """
    name = rollup.name
    db.execute(
        f"create or replace temp table _arrived as select * from read_parquet({quote(path)})"
    )
    _qualify_arrived(db, players=True)
    db.execute(
        f'delete from _arrived using kept.table_files f where f."table" = {quote(name)} and f.run = _arrived.run'
        f" and f.match = _arrived.match and f.file > {quote(file)}"
    )
    arrived = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
    if arrived.get("opponents") == "VARCHAR":
        # Sealed comma-joined by etude 0.3.0 or earlier (`Rollup`): one element, unsplit.
        db.execute(
            "create or replace temp table _arrived as select * replace"
            " (case when opponents is null then null else [opponents] end as opponents)"
            " from _arrived"
        )
        arrived["opponents"] = "VARCHAR[]"
    _hold(db, rollup)
    if not _exists(db, "kept", name):
        db.execute(f'create table kept."{name}" as select * from _arrived where false')
        db.execute(f'create table "{name}" as select * exclude (match) from _arrived where false')
    _widen(db, f'kept."{name}"', arrived)
    _widen(db, f'"{name}"', {c: kind for c, kind in arrived.items() if c != "match"})
    keys = ["run", *rollup.by]
    totals = [ROLLUP_ROWS, *rollup.sums]
    matched = " and ".join(f't."{c}" is not distinct from d."{c}"' for c in keys)
    listed = ", ".join(f'"{c}"' for c in keys)
    db.execute(
        f"create or replace temp table _delta as select {listed},"
        + ", ".join(f'sum("{c}") as "{c}"' for c in totals)
        + f" from (select * exclude (match) from _arrived union all by name"
        f" select {listed}, "
        + ", ".join(f'-p."{c}" as "{c}"' for c in totals)
        + f' from kept."{name}" p semi join (select distinct run, match from _arrived) a'
        f" on p.run = a.run and p.match = a.match) group by {listed}"
    )
    db.execute(
        f'update "{name}" t set '
        + ", ".join(f'"{c}" = t."{c}" + d."{c}"' for c in totals)
        + f" from _delta d where {matched}"
    )
    db.execute(
        f'insert into "{name}" by name select * from _delta d'
        f' where not exists (select 1 from "{name}" t where {matched})'
    )
    db.execute(f'delete from "{name}" where {ROLLUP_ROWS} = 0')
    db.execute(
        f'delete from kept."{name}" p using (select distinct run, match from _arrived) a'
        " where p.run = a.run and p.match = a.match"
    )
    db.execute(f'insert into kept."{name}" by name select * from _arrived')
    db.execute(
        "delete from kept.table_files f using (select distinct run, match from _arrived) a"
        f' where f."table" = {quote(name)} and f.run = a.run and f.match = a.match'
    )
    db.execute(
        f"insert into kept.table_files select distinct {quote(name)}, run, match, {quote(file)} from _arrived"
    )
    db.execute("drop table _arrived")
    db.execute("drop table _delta")


def _hold(db: duckdb.DuckDBPyConnection, rollup: Rollup) -> None:
    """Hold `rollup` from now on, so every block's partial totals of it are added.

    A rollup is its name and the digest of its definition: another definition
    under a held name raises `rollup_conflict`, and a name an author or Etude
    table has, `rollup_name_conflict`.
    """
    identity = rollup.digest
    held = db.execute(
        f"select digest from kept.rollups where name = {quote(rollup.name)}"
    ).fetchone()
    if held is None:
        if _exists(db, "main", rollup.name):
            raise EtudeError("rollup_name_conflict", 409) from ValueError(rollup.name)
        db.execute(
            f"insert into kept.rollups values ({quote(rollup.name)}, {quote(identity)},"
            f" {quote(rollup.model_dump_json())})"
        )
    elif held[0] != identity:
        raise EtudeError("rollup_conflict", 409) from ValueError(rollup.name)


def _sum_missing(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    located: Mapping[str, str],
    copies: Path,
    blocks: Sequence[str],
    *,
    experiment: str,
) -> None:
    """Add each held rollup's partial totals of the run's newly ingested blocks that lack them.

    A block lacks a rollup it was sealed without. Its partials are summed
    from its tables here, once: they are kept in this database and never
    written to the store, and the block is not summed for that rollup again.
    """
    wanted = ", ".join(quote(block_name) for block_name in blocks)
    missing = db.execute(
        "select r.rollup, b.block, b.held from kept.rollups r cross join kept.blocks b"
        " anti join kept.summed s on s.name = r.name and s.run = b.run and s.block = b.block"
        f" where b.run = {quote(run)} and b.block in ({wanted})"
        " order by b.block, r.name"
    ).fetchall()
    if not missing:
        return
    entries = [
        (name, Rollup.model_validate_json(text), Block.model_validate_json(held))
        for text, name, held in missing
    ]
    _sum_entries(db, storage, run, located, copies, entries, experiment=experiment)


def _sum_entries(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    located: Mapping[str, str],
    copies: Path,
    entries: Sequence[tuple[str, Rollup, Block]],
    *,
    experiment: str,
) -> None:
    summed: list[list[object]] = []
    for name, rollup, block in entries:
        summed.append([rollup.name, run, name])
        needed = (rollup.table, *rollup.join)
        if not set(needed) <= set(block.tables):
            continue
        place = located[file_key(name, EVAL_BLOCK_INDEX)]
        tables: dict[str, list[Path]] = {}
        for table in needed:
            file = file_key(name, f"{table}.parquet")
            tables[table] = [_local(storage, place + file, copies / run / file)]
        target = copies / run / "summed" / name / f"{rollup.name}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        if partial_totals(db, rollup, tables, [record.match for record in block.matches], target):
            with _qualified(db, experiment, block):
                _rollup(db, run, rollup, name, target)
    _put(db, "kept.summed", {"name": "VARCHAR", "run": "VARCHAR", "block": "VARCHAR"}, summed)


def _backfill_rollups(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    pool: ThreadPoolExecutor | None,
    listing: RunListing,
    allowed: frozenset[str],
    kept: _Ingested,
) -> None:
    """Sum newly requested rollups for blocks ingested in earlier opens, in chunks."""
    missing = db.execute(
        "select b.run, b.block, r.rollup, b.held from kept.rollups r cross join kept.blocks b"
        " anti join kept.summed s on s.name = r.name and s.run = b.run and s.block = b.block"
        " order by b.run, b.block, r.name"
    ).fetchall()
    if not missing:
        return
    run_info: dict[str, tuple[str, dict[str, str]]] = {}
    for run, folders in listing.folders.items():
        matched = [
            (folder, owner)
            for folder in folders
            if (owner := kept.declarations.get(folder)) is not None and owner in allowed
        ]
        if not matched:
            continue
        run_info[run] = (matched[0][1], _located(listing, [folder for folder, _ in matched]))
    grouped: list[tuple[str, str, list[tuple[str, Rollup, Block]]]] = []
    for (run, block_name), items in itertools.groupby(missing, key=lambda row: (row[0], row[1])):
        if run not in run_info:
            continue
        entries = [
            (block_name, Rollup.model_validate_json(rollup_json), Block.model_validate_json(held))
            for _, _, rollup_json, held in items
        ]
        grouped.append((run, block_name, entries))
    for batch in itertools.batched(grouped, _CATCH_UP_CHUNK):
        with tempfile.TemporaryDirectory(prefix="etude-statsdb-") as copies_root:
            copies = Path(copies_root)
            parquet_targets: list[tuple[str, Path]] = []
            for run, block_name, entries in batch:
                _, located = run_info[run]
                index_name = file_key(block_name, EVAL_BLOCK_INDEX)
                if index_name not in located:
                    continue
                place = located[index_name]
                for _, rollup, block in entries:
                    needed = (rollup.table, *rollup.join)
                    if not set(needed) <= set(block.tables):
                        continue
                    for table in needed:
                        file = file_key(block_name, f"{table}.parquet")
                        parquet_targets.append((place + file, copies / run / file))
            _fetch_files(pool, storage, parquet_targets)
            with _transaction(db, checkpoint=True):
                for run, _, entries in batch:
                    run_experiment, located = run_info[run]
                    _sum_entries(
                        db, storage, run, located, copies, entries, experiment=run_experiment
                    )


def _exists(db: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        db.execute(
            f"select 1 from duckdb_tables() where schema_name = {quote(schema)}"
            f" and table_name = {quote(table)}"
        ).fetchone()
    )


def _widen(db: duckdb.DuckDBPyConnection, table: str, arrived: dict[str, str]) -> None:
    """Make a held table take arrived rows: add their new columns, and widen whole
    numbers to fractions. A column holding only nulls takes the arrived type."""
    held = {column: kind for column, kind, *_ in db.execute(f"describe {table}").fetchall()}
    for column, kind in arrived.items():
        if column not in held:
            db.execute(f'alter table {table} add column "{column}" {kind}')
        elif (held[column], kind) == ("BIGINT", "DOUBLE"):
            db.execute(f'alter table {table} alter "{column}" type DOUBLE')
        elif (
            held[column] != kind
            and not db.execute(
                f'select 1 from {table} where "{column}" is not null limit 1'
            ).fetchone()
        ):
            db.execute(f'alter table {table} alter "{column}" type {kind}')


def _local(storage: Storage, key: str, copy: Path) -> Path:
    """A file of `storage` on this computer, read at most once per catch-up."""
    if isinstance(storage, DirectoryStorage):
        return storage.root / key
    if not copy.exists():
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(storage.get(key))
    return copy


def read_blobs(
    db: duckdb.DuckDBPyConnection, store: Storage | Path | str, digests: Iterable[str]
) -> Iterator[tuple[str, bytes]]:
    """Each blob's SHA256 and verified bytes, for the blobs `db` holds, read from `store`.

    `store` is the one `statsdb` opened `db` from. Blobs of one pack are read
    in pack order, one range per run of adjacent members; a bucket keeps each
    range on this computer (`etude.cache`), so reading them again reads
    nothing from storage. Raises `EtudeError("blob_unknown")` before reading
    when `db` holds no such blob, `blob_unavailable` when its pack has not
    arrived, and `blob_mismatch` when bytes are not what the block recorded.
    """
    storage = as_storage(store)
    wanted = list(dict.fromkeys(digests))
    located = {
        digest: (pack, int(start), int(size))
        for digest, pack, start, size in db.execute(
            "select sha256, min(pack), min_by(start, pack), min_by(size_bytes, pack)"
            " from kept.blobs where sha256 in (select unnest(?::VARCHAR[])) group by sha256",
            [wanted],
        ).fetchall()
    }
    unknown = [digest for digest in wanted if digest not in located]
    if unknown:
        raise EtudeError("blob_unknown", 404) from ValueError(", ".join(unknown[:5]))
    by_pack = sorted(wanted, key=lambda digest: (located[digest][0], located[digest][1]))
    for pack, members in itertools.groupby(by_pack, key=lambda digest: located[digest][0]):
        for adjacent in _adjacent([(digest, *located[digest][1:]) for digest in members]):
            start = adjacent[0][1]
            length = adjacent[-1][1] + adjacent[-1][2] - start
            try:
                body = storage.get_range(pack, start, length)
            except FileNotFoundError as missing:
                raise EtudeError("blob_unavailable", 404) from missing
            for digest, offset, size in adjacent:
                blob = body[offset - start : offset - start + size]
                if len(blob) != size or hashlib.sha256(blob).hexdigest() != digest:
                    raise EtudeError("blob_mismatch", 502) from ValueError(f"{pack}: {digest}")
                yield digest, blob


def _adjacent(members: Sequence[tuple[str, int, int]]) -> Iterator[list[tuple[str, int, int]]]:
    """Members (digest, offset, size) in offset order, grouped where each starts near the last's end."""
    group: list[tuple[str, int, int]] = []
    for member in members:
        if group and member[1] - (group[-1][1] + group[-1][2]) > ADJACENT_BYTES:
            yield group
            group = []
        group.append(member)
    if group:
        yield group


def _summaries(db: duckdb.DuckDBPyConnection) -> None:
    """Rebuild `runs`, `tournaments`, `schedule` and `players`, and rank `matches.newest`,
    from what the database holds."""
    held = {
        run: TournamentRun.model_validate_json(text)
        for run, text in db.execute("select run, held from kept.tournament_runs").fetchall()
    }
    db.execute(
        "create or replace table runs (run VARCHAR, experiment VARCHAR, environment VARCHAR,"
        " environment_name VARCHAR, environment_config JSON, tournament VARCHAR, state VARCHAR)"
    )
    described: list[list[object]] = []
    for run, experiment, state, text in db.execute(
        "select r.run, r.experiment, r.state, e.environment from kept.runs r"
        " join kept.environments e using (run) order by r.run"
    ).fetchall():
        environment = Environment.model_validate_json(text)
        described.append(
            [run, experiment, environment.identity, environment.name,
             json.dumps(environment.config),
             held[run].tournament.name if run in held else None, state]
        )  # fmt: skip
    _put(
        db,
        "runs",
        {"run": "VARCHAR", "experiment": "VARCHAR", "environment": "VARCHAR",
         "environment_name": "VARCHAR", "environment_config": "VARCHAR", "tournament": "VARCHAR",
         "state": "VARCHAR"},
        described,
    )  # fmt: skip
    db.execute(
        "update matches t set newest = n.newest from (select m.run, m.match, row_number() over"
        f" (partition by r.environment, m.match order by {_NEWEST_FIRST}) as newest"
        " from matches m join runs r using (run) where m.state = 'done') n"
        " where t.run = n.run and t.match = n.match"
    )
    db.execute(
        "create or replace table tournaments (tournament VARCHAR, format VARCHAR,"
        " environment VARCHAR, conflict VARCHAR)"
    )
    db.execute(
        "create or replace table schedule (tournament VARCHAR, match VARCHAR, seating VARCHAR,"
        " checkpoint VARCHAR, tags VARCHAR[])"
    )
    by_name: dict[str, list[TournamentRun]] = {}
    for run in sorted(held):
        by_name.setdefault(held[run].tournament.name, []).append(held[run])
    scheduled: list[Scheduled] = []
    for name, runs in by_name.items():
        # Every eval run agrees with the first on format and environment, or conflicts.
        first = runs[0].tournament
        conflict: str | None = None
        try:
            schedule = assemble(runs).schedule()
        except EtudeError as refused:
            conflict, schedule = refused.code, []
        db.execute(
            "insert into tournaments values (?, ?, ?, ?)",
            [name, "benchmark" if isinstance(first, Benchmark) else "round_robin",
             first.environment.identity, conflict],
        )  # fmt: skip
        _put(
            db,
            "schedule",
            {
                "tournament": "VARCHAR",
                "match": "VARCHAR",
                "seating": "VARCHAR",
                "checkpoint": "VARCHAR",
                "tags": "VARCHAR[]",
            },
            [
                [
                    name,
                    each.match.key,
                    each.seating,
                    None if each.checkpoint is None else str(each.checkpoint),
                    list(each.tags),
                ]  # fmt: skip
                for each in schedule
            ],
        )
        scheduled += schedule
    # Players of matches not yet held, so every scheduled player has a name.
    players: dict[str, list[Player]] = {}
    for (text,) in db.execute("select player_json from kept.players order by rowid").fetchall():
        player = _PLAYER.validate_json(text)
        players.setdefault(player.key, []).append(player)
    for each in scheduled:
        for player in each.match.players:
            players.setdefault(player.key, []).append(player)
    _players_table(db, players)


def _players_table(db: duckdb.DuckDBPyConnection, players: dict[str, list[Player]]) -> None:
    """One row per key; each scalar config key, dotted when nested, as a column."""
    flat = {identity: _flattened(given[0].config) for identity, given in players.items()}
    kinds: dict[str, set[ColumnType]] = {}
    for values in flat.values():
        for key, value in values.items():
            if value is not None:
                kinds.setdefault(key, set()).add(COLUMN_TYPES[type(value)])
    columns: dict[str, ColumnType] = {}
    for key, seen in sorted(kinds.items()):
        if seen == {"BIGINT", "DOUBLE"}:
            seen = {"DOUBLE"}
        if len(seen) == 1:
            columns[key] = seen.pop()
        else:
            warnings.warn(f"player config {key!r} has types {sorted(seen)}; only in `config`")
    seated_at = _seat_values(db, '"at"')
    lines = _seat_values(db, "line")
    names = _seat_values(db, "name")
    db.execute(
        "create or replace table players (player VARCHAR, name VARCHAR, names JSON, kind VARCHAR,"
        " checkpoint VARCHAR, artifact VARCHAR, uri VARCHAR, config JSON, seated_at JSON,"
        " lines JSON" + "".join(f', "config.{key}" {kind}' for key, kind in columns.items()) + ")"
    )
    rows: list[list[object]] = []
    for identity, given in players.items():
        # A player only scheduled has its names from the schedule.
        named = list(dict.fromkeys([*names[identity], *(each.name for each in given)]))
        rows.append(
            [
                identity,
                named[0],
                json.dumps(named),
                given[0].kind,
                *_checkpoint(given[0]),
                json.dumps(given[0].config),
                json.dumps([json.loads(each) for each in seated_at[identity]]),
                json.dumps(lines[identity]),
                *(flat[identity].get(key) for key in columns),
            ]
        )
    _put(
        db,
        "players",
        {
            "player": "VARCHAR",
            "name": "VARCHAR",
            "names": "VARCHAR",
            "kind": "VARCHAR",
            "checkpoint": "VARCHAR",
            "artifact": "VARCHAR",
            "uri": "VARCHAR",
            "config": "VARCHAR",
            "seated_at": "VARCHAR",
            "lines": "VARCHAR",
            **{f"config.{key}": kind for key, kind in columns.items()},
        },
        rows,
    )


def _checkpoint(player: Player) -> tuple[str | None, str | None, str | None]:
    """A model's checkpoint key, digest and file; nothing for code."""
    checkpoint = player_checkpoint(player)
    if checkpoint is None:
        return None, None, None
    return str(checkpoint.key), checkpoint.artifact.sha256, checkpoint.artifact.uri


def _seat_values(db: duckdb.DuckDBPyConnection, column: str) -> defaultdict[str, list[str]]:
    """Each player's distinct values of a seats column, the newest match record's first."""
    found: defaultdict[str, list[str]] = defaultdict(list)
    for identity, value in db.execute(
        f"select s.player, s.{column} from seats s join matches m using (run, match)"
        f" where s.{column} is not null order by {_NEWEST_FIRST}"
    ).fetchall():
        if value not in found[identity]:
            found[identity].append(value)
    return found


def _flattened(config: dict[str, JsonValue], prefix: str = "") -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            found.update(_flattened(value, f"{prefix}{key}."))
        elif not isinstance(value, list):
            found[prefix + key] = value
    return found


class Comparison(Record):
    """A subject's value against an opponent, within one environment and split."""

    environment: Digest
    # Player keys, encoded: one for a seat, a group's in sorted order.
    subject: tuple[str, ...]
    # The same for the opponent; None means against every opponent.
    opponent: tuple[str, ...] | None
    split: dict[str, JsonValue]
    value: float
    # None with fewer than two matches.
    interval: tuple[float, float] | None
    rows: int
    matches: int


class _Compared(NamedTuple):
    environment: str
    subject: tuple[str, ...]
    opponent: tuple[str, ...] | None
    split: tuple[JsonValue, ...]

    def order(self) -> tuple[str, ...]:
        return tuple(
            "" if v is None else repr(v)
            for v in (self.environment, self.subject, self.opponent, *self.split)
        )


class _MatchTotal(NamedTuple):
    comparison: _Compared
    numerator: float
    denominator: float
    rows: int


@dataclass
class _MatchScores:
    """Per-match totals from one query, reusable across overlapping report selections."""

    by: Sequence[str]
    totals: dict[tuple[str, str], list[_MatchTotal]]

    def comparisons(
        self, matches: Iterable[tuple[str, str]], *, against_all: bool = False
    ) -> Iterator[Comparison]:
        units: dict[_Compared, dict[tuple[str, str], tuple[float, float]]] = {}
        rows: dict[_Compared, int] = defaultdict(int)
        for pair in matches:
            for key, numerator, denominator, count in self.totals.get(pair, ()):
                if against_all and key.opponent is not None:
                    continue
                units.setdefault(key, {})[pair] = (numerator, denominator)
                rows[key] += count
        estimated = _estimate_groups(units)
        for key in sorted(estimated, key=_Compared.order):
            result = estimated[key]
            yield Comparison(
                environment=key.environment,
                subject=key.subject,
                opponent=key.opponent,
                split=dict(zip(self.by, key.split, strict=True)),
                value=result.value,
                interval=result.interval,
                rows=rows[key],
                matches=len(units[key]),
            )


def _match_scores(
    db: duckdb.DuckDBPyConnection,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
    by: Sequence[str] = (),
    has_groups: bool | None = None,
) -> _MatchScores:
    """Aggregate the author's rows by match and seat before expanding subjects and opponents."""
    for name in (table, value, *([per] if per else []), *by):
        if not IDENTIFIER.match(name):
            raise EtudeError("column_name_refused", 422) from ValueError(name)
    if table in ETUDE_TABLES:
        raise EtudeError("compare_needs_an_author_table", 422)
    if has_groups is None:
        has_groups = db.execute('select 1 from "groups" limit 1').fetchone() is not None
    by_cols = "".join(f', "{name}"' for name in by)
    split = "".join(f', r."{name}"' for name in by)
    per_expr = f'"{per}"' if per else "1"
    if has_groups:
        joins = """
        r_id as (
            select row_number() over () as _row, * from r
        ),
        teams as (
            select g.run, g.match, g."group", list(s.player order by s.player) as team
            from "groups" g join seats s using (run, match, seat)
            where (g.run, g.match) in (select distinct run, match from r)
            group by all
        ),
        subjects as (
            select r.*, [s.player] as subject from r_id r join seats s using (run, match, seat)
            union all by name
            select r.*, t.team as subject from r_id r join teams t using (run, match, "group")
        ),
        opponents as (
            select distinct x._row, [s.player] as opponent
            from subjects x join seats s
                on s.run = x.run and s.match = x.match and s.seat <> x.seat
            union
            select distinct x._row, t.team
            from subjects x join teams t
                on t.run = x.run and t.match = x.match
                and t."group" <> x."group" and t."group" <> 'all'
        ),
        pairs as (
            select x.*, null::VARCHAR[] as opponent from subjects x
            union all
            select x.*, o.opponent from subjects x join opponents o using (_row)
        )
        """
    else:
        joins = """
        subjects as (
            select r.*, [s.player] as subject from r join seats s using (run, match, seat)
        ),
        match_opponents as (
            select distinct x.run, x.match, x.seat, [s.player] as opponent
            from (select distinct run, match, seat from subjects) x
            join seats s on s.run = x.run and s.match = x.match and s.seat <> x.seat
        ),
        pairs as (
            select x.*, null::VARCHAR[] as opponent from subjects x
            union all
            select x.*, o.opponent from subjects x join match_opponents o using (run, match, seat)
        )
        """
    group_col = ', "group"' if has_groups else ""
    query = f"""
        with r as (
            select run, match, seat {group_col} {by_cols},
                sum(cast("{value}" as DOUBLE)) as _value,
                sum(cast({per_expr} as DOUBLE)) as _per,
                count(*) as _rows
            from "{table}"
            where "{value}" is not null and ({where or "true"})
            group by all
        ), {joins}
        select r.run, r.match, n.environment, r.subject, r.opponent,
            sum(r._value), sum(r._per), sum(r._rows) {split}
        from pairs r join runs n using (run)
        group by all
    """
    totals: dict[tuple[str, str], list[_MatchTotal]] = defaultdict(list)
    for run, match, environment, subject, opponent, num, den, rows, *values in db.execute(
        query
    ).fetchall():
        key = _Compared(
            environment,
            tuple(subject),
            None if opponent is None else tuple(opponent),
            tuple(values),
        )
        totals[(run, match)].append(_MatchTotal(key, num, den, rows))
    return _MatchScores(by, totals)


def _estimate_groups[K](
    units: Mapping[K, Mapping[tuple[str, str], tuple[float, float]]],
) -> dict[K, Estimate]:
    """Estimate every group's ratio and bootstrap interval, batching groups of equal unit count."""
    by_size: dict[int, list[K]] = defaultdict(list)
    for key, matches in units.items():
        by_size[len(matches)].append(key)
    results: dict[K, Estimate] = {}
    for size, keys in by_size.items():
        # Bound temporary (REPLICATES x chunk) matrix size while vectorizing BLAS multiplies.
        for batch in itertools.batched(keys, 2048):
            numerators = np.empty((size, len(batch)), dtype=np.float64)
            denominators = np.empty((size, len(batch)), dtype=np.float64)
            for col, key in enumerate(batch):
                matches = units[key]
                for row, match_key in enumerate(sorted(matches)):
                    numerators[row, col], denominators[row, col] = matches[match_key]
            for key, result in zip(
                batch,
                _estimate_ordered(numerators, denominators, denominators.sum(axis=0)),
                strict=True,
            ):
                if result is not None:
                    results[key] = result
    return results


def compare(
    db: duckdb.DuckDBPyConnection,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
    by: Sequence[str] = (),
) -> list[Comparison]:
    """Every subject against every opponent, and against all, on `table`'s `value` column.

    The value is sum(`value`) / sum(`per`), or the mean of `value` without
    `per`, over rows matching the SQL condition `where`, split by the columns
    `by`. The 95% interval is a percentile bootstrap resampling whole matches,
    so a match's correlated games move together; its seed is fixed.
    """
    scores = _match_scores(db, table=table, value=value, per=per, where=where, by=by)
    return list(scores.comparisons(scores.totals))


# Draws by count of resampling units, least recently used first.
_drawn: dict[int, npt.NDArray[np.float64]] = {}
_drawn_lock = threading.Lock()


def _weights(n: int) -> npt.NDArray[np.float64]:
    """How often each of `n` resampling units is drawn in each replicate, kept for the next
    call with `n`.

    A page of estimates over many selections asks for a few unit counts again
    and again. Each kept draw holds REPLICATES × n floats (16 kB per unit);
    the least recently used go once the kept draws pass `WEIGHTS_KEPT` floats,
    though the latest is always kept.
    """
    with _drawn_lock:
        weights = _drawn.pop(n, None)
        if weights is None:
            weights = _draw(n)
        _drawn[n] = weights
        while len(_drawn) > 1 and sum(kept.size for kept in _drawn.values()) > WEIGHTS_KEPT:
            del _drawn[next(iter(_drawn))]
    return weights


def _draw(n: int) -> npt.NDArray[np.float64]:
    """Draw how often each of `n` units is drawn in each replicate: fixed by the seed and `n` alone.

    Each replicate draws `n` units uniformly with replacement, counted with
    one `bincount` (a multinomial draw, linear in `n`). Counts are held as
    floats, exact at any `n`, so a matrix product needs no conversion.
    """
    drawn = np.random.default_rng(SEED).integers(0, n, size=(REPLICATES, n))
    drawn += np.arange(REPLICATES)[:, None] * n
    counts = np.bincount(drawn.ravel(), minlength=REPLICATES * n).reshape(REPLICATES, n)
    weights = counts.astype(np.float64)
    weights.setflags(write=False)
    return weights


def _percentiles(
    samples: npt.NDArray[np.float64], percents: Sequence[float]
) -> npt.NDArray[np.float64]:
    """Percents × columns of `samples`' columns ignoring NaN, equal to `np.nanpercentile(axis=0)`.

    Once any value is NaN, numpy takes a 1-D quantile per column; this sorts
    every column at once (NaN sorts last) and interpolates each
    at numpy's default "linear" position, (count - 1) × percent / 100,
    with the same arithmetic. A column of only NaN gives NaN.
    """
    ordered = np.sort(samples, axis=0)
    counts = np.count_nonzero(~np.isnan(samples), axis=0)
    positions = (counts - 1) * (np.asarray(percents, dtype=np.float64) / 100)[:, None]
    below = np.floor(positions)
    gamma = positions - below
    # A position never passes the last value; an empty column's floors to -1, also its last.
    previous = below.astype(np.intp)
    following = np.minimum(previous + 1, counts - 1)
    low = np.take_along_axis(ordered, previous, axis=0)
    high = np.take_along_axis(ordered, following, axis=0)
    step = high - low
    # numpy's `_lerp`: from the nearer end, so an exact neighbour stays exact.
    found: npt.NDArray[np.float64] = np.where(
        gamma >= 0.5, high - step * (1 - gamma), low + step * gamma
    )
    found[:, counts == 0] = np.nan
    return found


@dataclass(frozen=True)
class Estimate:
    """A ratio of totals and its 95% interval, None with fewer than two resampling units."""

    value: float
    interval: tuple[float, float] | None


def estimate(
    keys: Sequence[SupportsRichComparison],
    numerators: npt.ArrayLike,
    denominators: npt.ArrayLike,
) -> list[Estimate | None]:
    """Each column's ratio of totals over units × columns `numerators` and `denominators`.

    A unit is what the bootstrap resamples whole, such as a match,
    so its correlated rows move together. Every column shares one bootstrap:
    one draw of unit weights gives each column's 95% percentile interval.
    Units are ordered by their `keys` before resampling, so the result does
    not depend on the order they were read in; keys must be distinct. A column without opportunities is None: a rate without them
    has no value.
    """
    numerators = np.asarray(numerators, dtype=np.float64)
    denominators = np.asarray(denominators, dtype=np.float64)
    if numerators.ndim != 2 or numerators.shape != denominators.shape:
        raise ValueError(f"units × columns {numerators.shape} and {denominators.shape}")
    if len(numerators) != len(keys):
        raise ValueError(f"{len(keys)} keys for {len(numerators)} units")
    order = sorted(range(len(keys)), key=keys.__getitem__)
    if any(keys[a] == keys[b] for a, b in itertools.pairwise(order)):
        raise ValueError("unit keys repeat")
    return _estimate_ordered(numerators[order], denominators[order], denominators.sum(axis=0))


def _estimate_ordered(
    numerators: npt.NDArray[np.float64],
    denominators: npt.NDArray[np.float64],
    opportunities: npt.NDArray[np.float64],
) -> list[Estimate | None]:
    """Estimate columns whose resampling units are already in deterministic order."""
    held = np.flatnonzero(opportunities)
    numerators, denominators = numerators[:, held], denominators[:, held]
    values = numerators.sum(axis=0) / opportunities[held]
    intervals: list[tuple[float, float] | None] = [None] * len(held)
    if len(numerators) >= 2 and len(held):
        weights = _weights(len(numerators))
        resampled = weights @ denominators
        # A replicate drawing no opportunities for a column has no ratio in it.
        with np.errstate(divide="ignore", invalid="ignore"):
            ratios = np.where(resampled > 0, (weights @ numerators) / resampled, np.nan)
        lows, highs = _percentiles(ratios, (2.5, 97.5))
        intervals = [(float(low), float(high)) for low, high in zip(lows, highs, strict=True)]
    estimates: list[Estimate | None] = [None] * len(opportunities)
    for column, value, interval in zip(held, values, intervals, strict=True):
        estimates[column] = Estimate(float(value), interval)
    return estimates
