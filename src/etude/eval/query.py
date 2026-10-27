"""Querying an experiment's eval runs: `statsdb` opens its database; `compare` asks one question.

`statsdb` gives one table per author table, holding the rows of every eval
run of the experiment (a match's rows from the latest file holding them),
beside Etude's own tables:

| table | columns |
|---|---|
| `runs` | run, experiment, environment (identity), environment_name, environment_config, tournament |
| `players` | player (its key, encoded), name, kind (`model` or `code`), checkpoint (its key), artifact (digest), uri, config, at, at_conflict, line, line_conflict, and `config.KEY` per scalar config key |
| `matches` | run, match, conditions, games, seed, state, error, wall_ms |
| `seats` | run, match, seat, player, the version that played: commit, source (digest), at (where its checkpoint stood, or null) and line (the learner it belongs to, or null) |
| `groups` | run, match, group, seat |
| `tournaments` | tournament, format (`benchmark` or `round_robin`), environment, conflict |
| `schedule` | tournament, match, matchup, checkpoint (its key) and the matchup's tags of a benchmark's match; no checkpoint for a matchup of fixed players |
| `training_checkpoints` | run, experiment, ordinal, checkpoint (its key, `RUN/checkpoints/NAME`), artifact (digest), wall_ms, clocks (where each stood) |
| `blobs` | run, match (none for a converted run's other files), sha256, size_bytes |
| ROLLUP (one per `etude.eval.records.Rollup`) | run, the rollup's `by` columns, rows, and its `sums` |

A tournament is assembled from the experiment's eval runs naming it in their
`tournament.json`. `schedule` holds every match it calls for, held or not.
When its eval runs disagree, `conflict` names how (`tournament_conflict`,
`matchup_conflict`) and it has no schedule.
`training_checkpoints` holds every checkpoint the experiment's training runs
recorded, read from their stored JSON, so a model joins to where its
run's clocks stood by key (`players.checkpoint = training_checkpoints.checkpoint`),
never by its bytes. A seat may say where its checkpoint stood itself
(`etude.eval.records.CheckpointRef.at`): `players.at` is the position from the
latest match record giving one, and `players.at_conflict` lists every position
given when they differ, else null; `players.line` and `players.line_conflict`
are the same for the learner it belongs to (`CheckpointRef.line`). A match
counts once whichever version played it: the latest record holding it
(`seats.commit` tells which).

The database is a DuckDB file under `ETUDE_HOME/statsdb`, one per experiment
and store, derived from the store and never the authority. Opening it lists
the store once and reads only the files it has not read before: an eval
run's blocks (`etude.eval.block`), tournament and checkpoint files are
written once, so nothing already held is read again. A block costs one read
of its index and one of each table and rollup; its blobs are read only by
`read_blobs`. A rollup's totals are added to as blocks arrive (`_rollup`),
never summed again from earlier blocks or raw rows.
A failed match's record is replaced by the done record that follows it. An
eval run still kept a file per match raises `eval_run_not_converted`. The few tables that summarize the rest
(`runs`, `players`, `tournaments`, `schedule`) are rebuilt from what the
database holds whenever something arrives. Etude's bookkeeping lives in the
`kept` schema, out of `show tables`.

`compare` sets a subject against each opponent and against all of them. The
subject of a `seat` row is its seat's player; of a `group` row, the group's
players. Opponents are the other seats' players, or the match's other groups
except `all`.

Table files published etude-sdk wrote name the seat `player`; such a file has
no `seat` column, since every file written since has one, and its `player`
reads as `seat`. Results from different environments are never pooled.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import tempfile
import warnings
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from etude.carriage import EVAL_BLOCK_INDEX, EVAL_BLOCK_PACK
from etude.eval.block import (
    BLOCK_FILE,
    Block,
    file_key,
    partial_totals,
    per_file,
    quote,
    read,
    refuse_unconverted,
    unnest,
)
from etude.eval.match import ETUDE_TABLES, KIND, OLD_SEAT
from etude.eval.records import (
    COLUMN_TYPES,
    IDENTIFIER,
    ROLLUP_ROWS,
    TOURNAMENT_FILE,
    Benchmark,
    CheckpointKey,
    ColumnType,
    Environment,
    Model,
    Player,
    Rollup,
    Scheduled,
    TournamentRun,
    assemble,
)
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, run_folders
from etude.run.records import Digest, Record, RunFile
from etude.run.storage import SAFE_NAME, DirectoryStorage, Storage, default_root

if TYPE_CHECKING:
    import duckdb

REPLICATES = 2000
# Blobs this close together in a pack are read in one range: a member's header lies between.
ADJACENT_BYTES = 1024
SEED = 0
# Column types `compare` can take as a value.
NUMERIC = ("BOOLEAN", "BIGINT", "DOUBLE")
# Keys and coordinates of an author table's rows: never a value.
KEYS = ("seat", "group", "game", "run", "match")
# A kept database of another schema is deleted and read again from its store.
SCHEMA = 7

_ENVIRONMENT_FILE = "environment.json"
# Pydantic validates a `type` alias at runtime; its stubs accept only classes.
_PLAYER: TypeAdapter[Player] = TypeAdapter(Player)  # type: ignore[arg-type]


def statsdb(
    experiment: str,
    store: Storage | Path | str | None = None,
    *,
    rebuild: bool = False,
    rollups: Sequence[Rollup] = (),
) -> duckdb.DuckDBPyConnection:
    """The experiment's database of its eval runs, caught up with `store` as it opens.

    `store` is a directory or bucket searched at any depth; by default, where
    runs are written on this computer. `rebuild` deletes the kept file first,
    so everything is read again. One process opens a kept file at a time:
    another open raises `EtudeError("statsdb_in_use")`.

    `rollups` are held from now on beside those blocks bring: a block that
    lacks one has its partial totals summed once from its tables and kept
    here, never written to the store. A name held with another definition
    raises `rollup_conflict`.
    """
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    if not SAFE_NAME.match(experiment):
        raise EtudeError("unsafe_run_or_experiment_name", 422)
    storage = as_storage(default_root() if store is None else store)
    path = _kept_path(experiment, storage)
    if rebuild:
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db = duckdb.connect(path)
    except duckdb.IOException as locked:
        raise EtudeError("statsdb_in_use", 409) from locked
    if _schema(db) not in (None, SCHEMA):
        db.close()
        path.unlink()
        db = duckdb.connect(path)
    try:
        _catch_up(db, experiment, storage, rollups)
    except BaseException:
        db.close()
        raise
    return db


def _kept_path(experiment: str, storage: Storage) -> Path:
    home = Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser()
    store = hashlib.sha256(storage.name.encode()).hexdigest()[:16]
    return home / "statsdb" / f"{experiment}.{store}.duckdb"


def _schema(db: duckdb.DuckDBPyConnection) -> int | None:
    found = db.execute(
        "select 1 from information_schema.tables where table_schema = 'kept' and table_name = 'statsdb'"
    ).fetchone()
    if found is None:
        return None
    version = db.execute("select schema from kept.statsdb").fetchone()
    return None if version is None else int(version[0])


def _create(db: duckdb.DuckDBPyConnection, experiment: str, storage: Storage) -> None:
    db.execute("create schema kept")
    db.execute("create table kept.statsdb (schema BIGINT, experiment VARCHAR, store VARCHAR)")
    db.execute("insert into kept.statsdb values (?, ?, ?)", [SCHEMA, experiment, storage.name])
    # Every declared run of any experiment, so each `run.json` is read once.
    db.execute("create table kept.runs (run VARCHAR, experiment VARCHAR, kind VARCHAR)")
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
        "create table matches (run VARCHAR, match VARCHAR, conditions JSON, games BIGINT,"
        " seed BIGINT, state VARCHAR, error VARCHAR, wall_ms BIGINT)"
    )
    db.execute(
        "create table seats (run VARCHAR, match VARCHAR, seat BIGINT, player VARCHAR,"
        ' commit VARCHAR, source VARCHAR, "at" JSON, line VARCHAR)'
    )
    db.execute('create table "groups" (run VARCHAR, match VARCHAR, "group" VARCHAR, seat BIGINT)')
    db.execute(
        "create table training_checkpoints (run VARCHAR, experiment VARCHAR, ordinal BIGINT,"
        " checkpoint VARCHAR, artifact VARCHAR, wall_ms BIGINT, clocks JSON)"
    )


def _catch_up(
    db: duckdb.DuckDBPyConnection, experiment: str, storage: Storage, rollups: Sequence[Rollup]
) -> None:
    """Read what arrived in `storage` since the last open, in one transaction.

    An interrupted catch-up commits nothing, so the next open repeats it.
    Taking in a block is a fixed number of statements however many matches
    and blobs it holds: rows arrive as columns (`etude.eval.block.unnest`).
    """
    keys = sorted(storage.keys(""))
    db.execute("begin")
    try:
        created = _schema(db) is None
        if created:
            _create(db, experiment, storage)
        for rollup in rollups:
            _hold(db, rollup)
        changed = created
        with tempfile.TemporaryDirectory(prefix="etude-statsdb-") as copies:
            for run, folders in sorted(run_folders(keys).items()):
                if not _ours(db, storage, run, folders, experiment):
                    continue
                eval_run = _kind(db, run) == KIND
                if eval_run and not _environment(db, storage, run, folders):
                    continue  # an eval run counts once its environment arrives
                # One run may be copied to several places; a file's name is the same in each.
                located: dict[str, str] = {}
                for place in folders:
                    for key in _under(keys, place):
                        located.setdefault(key[len(place) :], place)
                seen = {name for (name,) in db.execute(
                    "select name from kept.seen where run = ?", [run]).fetchall()}  # fmt: skip
                new = sorted(located.keys() - seen)
                if eval_run:
                    for name in new:
                        changed |= _read(db, storage, run, located[name], name, Path(copies))
                    _sum_missing(db, storage, run, located, Path(copies))
                else:
                    changed |= _checkpoints(db, storage, run, experiment, located, new)
                select, values = unnest({"run": "VARCHAR", "name": "VARCHAR"},
                                        [[run, name] for name in new])  # fmt: skip
                db.execute(f"insert into kept.seen {select}", values)
                # A name seen before passed this check when it was first read.
                _refuse_unconverted(db, run, new)
        if changed:
            _summaries(db)
        db.execute("commit")
    except BaseException:
        db.execute("rollback")
        raise


def _ours(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    folders: Sequence[str],
    experiment: str,
) -> bool:
    """Whether the run is the experiment's, reading its declaration the first time only."""
    found = db.execute("select experiment from kept.runs where run = ?", [run]).fetchone()
    if found is None:
        declaration = _declared(storage, folders)
        if declaration is None:
            return False  # its `run.json` has not arrived yet
        db.execute(
            "insert into kept.runs values (?, ?, ?)",
            [run, declaration.experiment, declaration.declaration.kind],
        )
        return declaration.experiment == experiment
    return bool(found[0] == experiment)


def _refuse_unconverted(db: duckdb.DuckDBPyConnection, run: str, names: Sequence[str]) -> None:
    old = [name for name in names if per_file(name)]
    replaced = db.execute(
        "select name from kept.replaced where run = ? and name in (select unnest(?::VARCHAR[]))",
        [run, old],
    ).fetchall()
    refuse_unconverted(run, old, (name for (name,) in replaced))


def _under(keys: Sequence[str], prefix: str) -> Iterator[str]:
    """The keys under `prefix` of a sorted listing."""
    for key in keys[bisect_left(keys, prefix) :]:
        if not key.startswith(prefix):
            return
        yield key


def _environment(
    db: duckdb.DuckDBPyConnection, storage: Storage, run: str, folders: Sequence[str]
) -> bool:
    """Whether the eval run's environment is held, reading it until it arrives."""
    if db.execute("select 1 from kept.environments where run = ?", [run]).fetchone():
        return True
    for place in folders:
        try:
            environment = Environment.model_validate_json(storage.get(place + _ENVIRONMENT_FILE))
        except (FileNotFoundError, ValidationError):
            continue
        db.execute(
            "insert into kept.environments values (?, ?)", [run, environment.model_dump_json()]
        )
        return True
    return False


def _declared(storage: Storage, folders: Sequence[str]) -> RunFile | None:
    for place in folders:
        try:
            return RunFile.model_validate_json(storage.get(place + "run.json"))
        except (FileNotFoundError, ValidationError):
            continue
    return None


def _kind(db: duckdb.DuckDBPyConnection, run: str) -> str:
    found = db.execute("select kind from kept.runs where run = ?", [run]).fetchone()
    assert found is not None, "the run's declaration was read first"
    return str(found[0])


def _checkpoints(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    experiment: str,
    located: Mapping[str, str],
    names: Sequence[str],
) -> bool:
    """Take in a training run's checkpoint files among `names`; whether any arrived."""
    rows: list[list[object]] = []
    for name in names:
        if not (name.startswith("checkpoints/") and name.endswith(".json")):
            continue
        stored = _StoredCheckpoint.model_validate_json(storage.get(located[name] + name))
        rows.append(
            [run, experiment, stored.ordinal,
             str(CheckpointKey(run=run, name=PurePosixPath(stored.artifact.uri).name)),
             stored.artifact.sha256, stored.wall_ms, json.dumps(stored.at)]
        )  # fmt: skip
    select, values = unnest(
        {"run": "VARCHAR", "experiment": "VARCHAR", "ordinal": "BIGINT", "checkpoint": "VARCHAR",
         "artifact": "VARCHAR", "wall_ms": "BIGINT", "clocks": "VARCHAR"},
        rows,
    )  # fmt: skip
    # One run copied to several places records each checkpoint once.
    db.execute(
        f"insert into training_checkpoints select a.* from ({select}) a anti join"
        " training_checkpoints t on t.run = a.run and t.ordinal = a.ordinal"
        " qualify row_number() over (partition by a.ordinal) = 1",
        values,
    )
    return bool(rows)


def _read(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    place: str,
    name: str,
    copies: Path,
) -> bool:
    """Take in one file of the eval run not read before; whether any table changed."""
    if name == TOURNAMENT_FILE:
        held = TournamentRun.model_validate_json(storage.get(place + name))
        db.execute("insert into kept.tournament_runs values (?, ?)", [run, held.model_dump_json()])
        return True
    if found := BLOCK_FILE.match(name):
        _block(db, storage, run, place, found[1], copies)
        return True
    return False


def _block(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    place: str,
    name: str,
    copies: Path,
) -> None:
    """Take in block `name`: its match records, where its blobs lie, then its tables and rollups."""
    block = read(storage, place, name)
    held = block.model_dump_json(
        exclude={"blobs": True, "replaces": True, "matches": {"__all__": {"blobs"}}}
    )
    db.execute("insert into kept.blocks values (?, ?, ?)", [run, name, held])
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
    select, values = unnest(
        {"sha256": "VARCHAR", "size_bytes": "BIGINT"},
        [[digest, span.size_bytes] for digest, span in block.blobs.items() if digest not in named],
    )
    db.execute(
        f"insert into blobs select ?, null, a.sha256, a.size_bytes from ({select}) a"
        " anti join blobs b on b.run = ? and b.match is null and b.sha256 = a.sha256",
        [run, *values, run],
    )
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


def _matches(db: duckdb.DuckDBPyConnection, run: str, block: Block) -> None:
    """Hold the block's match records, each unless a done one is already held.

    A done record replaces a failure; of one block's records of a match, the
    first done one counts, else its first.
    """
    records = block.matches
    select, values = unnest(
        {"position": "BIGINT", "match": "VARCHAR", "conditions": "VARCHAR", "games": "BIGINT",
         "seed": "BIGINT", "state": "VARCHAR", "error": "VARCHAR", "wall_ms": "BIGINT"},
        [[at, record.match.key, json.dumps(record.match.conditions), record.match.games,
          record.match.seed, record.state, record.error, record.wall_ms]
         for at, record in enumerate(records)],
    )  # fmt: skip
    db.execute(
        f"create or replace temp table _taken as select a.* from ({select}) a"
        " left join matches m on m.run = ? and m.match = a.match"
        " where m.match is null or (m.state <> 'done' and a.state = 'done')"
        " qualify row_number() over (partition by a.match order by a.state = 'done' desc,"
        " a.position) = 1",
        [*values, run],
    )
    for table in ("matches", "seats", '"groups"', "blobs"):
        db.execute(
            f"delete from {table} t using _taken a where t.run = ? and t.match = a.match", [run]
        )
    db.execute(
        "insert into matches select ?, match, conditions, games, seed, state, error, wall_ms"
        " from _taken order by position",
        [run],
    )
    select, values = unnest(
        {"position": "BIGINT", "seat": "BIGINT", "player": "VARCHAR", "name": "VARCHAR",
         "player_json": "VARCHAR", "commit": "VARCHAR", "source": "VARCHAR", "at": "VARCHAR",
         "line": "VARCHAR"},
        [
            [at, seat, player.key, player.name, player.model_dump_json(),
             player.version.commit, player.version.source,
             _canonical(player.checkpoint.at)
             if isinstance(player, Model) and player.checkpoint.at else None,
             player.checkpoint.line if isinstance(player, Model) else None]
            for at, record in enumerate(records)
            for seat, player in enumerate(record.match.players)
        ],
    )  # fmt: skip
    db.execute(
        "create or replace temp table _arrived_seats as select s.*, a.match"
        f" from ({select}) s join _taken a using (position)",
        values,
    )
    db.execute(
        'insert into seats select ?, match, seat, player, "commit", source, "at", line'
        " from _arrived_seats order by position, seat",
        [run],
    )
    # Every player a held match seats, as first given under each name.
    db.execute(
        "insert into kept.players select s.player, s.name, s.player_json from _arrived_seats s"
        " anti join kept.players k on k.player = s.player and k.name = s.name"
        " qualify row_number() over (partition by s.player, s.name order by s.position, s.seat) = 1"
        " order by s.position, s.seat"
    )
    select, values = unnest(
        {"position": "BIGINT", "group": "VARCHAR", "seat": "BIGINT"},
        [[at, name, seat] for at, record in enumerate(records)
         for name, seats in record.match.groups.items() for seat in seats],
    )  # fmt: skip
    db.execute(
        f'insert into "groups" select ?, a.match, g."group", g.seat from ({select}) g'
        " join _taken a using (position) order by g.position",
        [run, *values],
    )
    select, values = unnest(
        {"position": "BIGINT", "sha256": "VARCHAR", "size_bytes": "BIGINT"},
        [[at, digest, block.blobs[digest].size_bytes]
         for at, record in enumerate(records) for digest in record.blobs],
    )  # fmt: skip
    db.execute(
        f"insert into blobs select ?, a.match, b.sha256, b.size_bytes from ({select}) b"
        " join _taken a using (position) order by b.position",
        [run, *values],
    )
    db.execute("drop table _taken")
    db.execute("drop table _arrived_seats")


def _put(
    db: duckdb.DuckDBPyConnection,
    table: str,
    columns: Mapping[str, str],
    rows: Sequence[Sequence[object]],
) -> None:
    """Insert `rows` into `table` by column name, in one statement."""
    select, values = unnest(columns, rows)
    db.execute(f"insert into {table} by name {select}", values)


def _canonical(value: object) -> str:
    """JSON with sorted keys, so equal positions compare equal as text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _table(db: duckdb.DuckDBPyConnection, run: str, table: str, file: str, path: Path) -> None:
    """Add one block's rows of a table, keeping each match's rows from the latest block holding them.

    Blocks holding one match may arrive in either order; their names order by
    their zero-padded seal time.
    """
    db.execute(
        f"create or replace temp table _arrived as select * from read_parquet({quote(path)})"
    )
    db.execute(
        'delete from _arrived using kept.table_files f where f."table" = ? and f.run = _arrived.run'
        " and f.match = _arrived.match and f.file > ?",
        [table, file],
    )
    arrived = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
    if OLD_SEAT in arrived:
        # Written before rows named the seat `seat`: `player` is the seat, and a
        # `seat` beside it is the game's own, which rows now call `game_seat`.
        renamed = {OLD_SEAT: "seat", "seat": "game_seat"}
        if "game_seat" in arrived and "seat" in arrived:
            raise EtudeError("old_table_has_seat_and_game_seat", 422) from ValueError(file)
        for column in ("seat", OLD_SEAT):
            if column in arrived:
                db.execute(f'alter table _arrived rename column "{column}" to {renamed[column]}')
        arrived = {renamed.get(column, column): kind for column, kind in arrived.items()}
    if db.execute("select 1 from kept.rollups where name = ?", [table]).fetchone():
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
        ' where f."table" = ? and f.run = a.run and f.match = a.match',
        [table],
    )
    db.execute(
        "insert into kept.table_files select distinct ?, run, match, ? from _arrived", [table, file]
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
    db.execute(
        'delete from _arrived using kept.table_files f where f."table" = ? and f.run = _arrived.run'
        " and f.match = _arrived.match and f.file > ?",
        [name, file],
    )
    arrived = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
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
        ' where f."table" = ? and f.run = a.run and f.match = a.match',
        [name],
    )
    db.execute(
        "insert into kept.table_files select distinct ?, run, match, ? from _arrived", [name, file]
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
    held = db.execute("select digest from kept.rollups where name = ?", [rollup.name]).fetchone()
    if held is None:
        if _exists(db, "main", rollup.name):
            raise EtudeError("rollup_name_conflict", 409) from ValueError(rollup.name)
        db.execute(
            "insert into kept.rollups values (?, ?, ?)",
            [rollup.name, identity, rollup.model_dump_json()],
        )
    elif held[0] != identity:
        raise EtudeError("rollup_conflict", 409) from ValueError(rollup.name)


def _sum_missing(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    located: Mapping[str, str],
    copies: Path,
) -> None:
    """Add each held rollup's partial totals of the run's blocks that lack them.

    A block lacks a rollup it was sealed without. Its partials are summed
    from its tables here, once: they are kept in this database and never
    written to the store, and the block is not summed for that rollup again.
    """
    missing = db.execute(
        "select r.rollup, b.block, b.held from kept.rollups r cross join kept.blocks b"
        " anti join kept.summed s on s.name = r.name and s.run = b.run and s.block = b.block"
        " where b.run = ? order by b.block, r.name",
        [run],
    ).fetchall()
    summed: list[list[object]] = []
    for text, name, held in missing:
        rollup, block = Rollup.model_validate_json(text), Block.model_validate_json(held)
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
            _rollup(db, run, rollup, name, target)
    _put(db, "kept.summed", {"name": "VARCHAR", "run": "VARCHAR", "block": "VARCHAR"}, summed)


def _exists(db: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return bool(
        db.execute(
            "select 1 from information_schema.tables where table_schema = ? and table_name = ?",
            [schema, table],
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


class _StoredArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    uri: str
    sha256: str | None = None


class _StoredCheckpoint(BaseModel):
    """What evaluation reads of a training run's `checkpoints/NAME.json`; the rest is training's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ordinal: int
    artifact: _StoredArtifact
    at: dict[str, float]
    wall_ms: int


def _summaries(db: duckdb.DuckDBPyConnection) -> None:
    """Rebuild `runs`, `tournaments`, `schedule` and `players` from what the database holds."""
    held = {
        run: TournamentRun.model_validate_json(text)
        for run, text in db.execute("select run, held from kept.tournament_runs").fetchall()
    }
    db.execute(
        "create or replace table runs (run VARCHAR, experiment VARCHAR, environment VARCHAR,"
        " environment_name VARCHAR, environment_config JSON, tournament VARCHAR)"
    )
    described: list[list[object]] = []
    for run, experiment, text in db.execute(
        "select r.run, r.experiment, e.environment from kept.runs r"
        " join kept.environments e using (run) order by r.run"
    ).fetchall():
        environment = Environment.model_validate_json(text)
        described.append(
            [run, experiment, environment.identity, environment.name,
             json.dumps(environment.config),
             held[run].tournament.name if run in held else None]
        )  # fmt: skip
    _put(
        db,
        "runs",
        {"run": "VARCHAR", "experiment": "VARCHAR", "environment": "VARCHAR",
         "environment_name": "VARCHAR", "environment_config": "VARCHAR", "tournament": "VARCHAR"},
        described,
    )  # fmt: skip
    db.execute(
        "create or replace table tournaments (tournament VARCHAR, format VARCHAR,"
        " environment VARCHAR, conflict VARCHAR)"
    )
    db.execute(
        "create or replace table schedule (tournament VARCHAR, match VARCHAR, matchup VARCHAR,"
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
                "matchup": "VARCHAR",
                "checkpoint": "VARCHAR",
                "tags": "VARCHAR[]",
            },
            [
                [
                    name,
                    each.match.key,
                    each.matchup,
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
    positions = _seated(db, '"at"')
    lines = _seated(db, "line")
    db.execute(
        "create or replace table players (player VARCHAR, name VARCHAR, kind VARCHAR,"
        ' checkpoint VARCHAR, artifact VARCHAR, uri VARCHAR, config JSON, "at" JSON,'
        " at_conflict JSON, line VARCHAR, line_conflict JSON"
        + "".join(f', "config.{key}" {kind}' for key, kind in columns.items())
        + ")"
    )
    _put(
        db,
        "players",
        {
            "player": "VARCHAR",
            "name": "VARCHAR",
            "kind": "VARCHAR",
            "checkpoint": "VARCHAR",
            "artifact": "VARCHAR",
            "uri": "VARCHAR",
            "config": "VARCHAR",
            "at": "VARCHAR",
            "at_conflict": "VARCHAR",
            "line": "VARCHAR",
            "line_conflict": "VARCHAR",
            **{f"config.{key}": kind for key, kind in columns.items()},
        },
        [
            [
                identity,
                "/".join(dict.fromkeys(each.name for each in given)),
                given[0].kind,
                *_checkpoint(given[0]),
                json.dumps(given[0].config),
                positions[identity][0] if positions[identity] else None,
                f"[{','.join(sorted(positions[identity]))}]"
                if len(positions[identity]) > 1
                else None,
                lines[identity][0] if lines[identity] else None,
                json.dumps(sorted(lines[identity])) if len(lines[identity]) > 1 else None,
                *(flat[identity].get(key) for key in columns),
            ]
            for identity, given in players.items()
        ],
    )


def _checkpoint(player: Player) -> tuple[str | None, str | None, str | None]:
    """A model's checkpoint key, digest and file; nothing for code."""
    if not isinstance(player, Model):
        return None, None, None
    checkpoint = player.checkpoint
    return str(checkpoint.key), checkpoint.artifact.sha256, checkpoint.artifact.uri


def _seated(db: duckdb.DuckDBPyConnection, column: str) -> defaultdict[str, list[str]]:
    """Each player's distinct values of a seats column, the latest match record's first."""
    found: defaultdict[str, list[str]] = defaultdict(list)
    for identity, value in db.execute(
        f"select s.player, s.{column} from seats s join matches m using (run, match)"
        f" where s.{column} is not null order by m.wall_ms desc, s.run desc"
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
    for name in (table, value, *([per] if per else []), *by):
        if not IDENTIFIER.match(name):
            raise EtudeError("column_name_refused", 422) from ValueError(name)
    if table in ETUDE_TABLES:
        raise EtudeError("compare_needs_an_author_table", 422)
    split = "".join(f', r."{name}"' for name in by)
    found = db.execute(
        f"""
        with r as (
            select row_number() over () as _row, run, match, seat, "group",
                cast("{value}" as DOUBLE) as _value,
                cast({f'"{per}"' if per else 1} as DOUBLE) as _per
                {"".join(f', "{name}"' for name in by)}
            from "{table}"
            where "{value}" is not null and ({where or "true"})
        ),
        teams as (
            select g.run, g.match, g."group", list(s.player order by s.player) as team
            from "groups" g join seats s using (run, match, seat)
            group by all
        ),
        subjects as (
            select r.*, [s.player] as subject from r join seats s using (run, match, seat)
            union all by name
            select r.*, t.team as subject from r join teams t using (run, match, "group")
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
        select n.environment, r.subject, r.opponent {split}, r.run, r.match,
            sum(r._value) as numerator, sum(r._per) as denominator, count(*) as rows
        from pairs r join runs n using (run)
        group by all
        """
    ).fetchall()
    # Per comparison, per match: [numerator, denominator], and the rows counted.
    blocks: dict[tuple[object, ...], list[tuple[float, float]]] = {}
    rows: dict[tuple[object, ...], int] = {}
    for environment, subject, opponent, *rest in found:
        values, (_, _, numerator, denominator, count) = rest[: len(by)], rest[len(by) :]
        held = None if opponent is None else tuple(opponent)
        key = (environment, tuple(subject), held, *values)
        blocks.setdefault(key, []).append((float(numerator), float(denominator)))
        rows[key] = rows.get(key, 0) + count
    comparisons: list[Comparison] = []
    for key in sorted(blocks, key=lambda k: tuple("" if v is None else repr(v) for v in k)):
        estimated = estimate(blocks[key])
        if estimated is None:
            continue
        environment, subject, opponent, *values = key
        comparisons.append(
            Comparison(
                environment=str(environment),
                subject=cast(tuple[str, ...], subject),
                opponent=cast(tuple[str, ...] | None, opponent),
                # DuckDB returns plain values for the author's scalar columns.
                split=dict(zip(by, cast(list[JsonValue], values))),
                value=estimated[0],
                interval=estimated[1],
                rows=rows[key],
                matches=len(blocks[key]),
            )
        )
    return comparisons


@lru_cache(maxsize=1)
def _weights(n: int) -> npt.NDArray[np.int64]:
    """How often each of `n` blocks is drawn in each replicate: fixed by the seed and `n` alone.

    Each replicate draws `n` blocks uniformly with replacement, counted with
    one `bincount` (a multinomial draw, linear in `n`). A report's estimates
    over one selection share `n`, so the latest draw is kept.
    """
    drawn = np.random.default_rng(SEED).integers(0, n, size=(REPLICATES, n))
    drawn += np.arange(REPLICATES)[:, None] * n
    weights = np.bincount(drawn.ravel(), minlength=REPLICATES * n).reshape(REPLICATES, n)
    weights.setflags(write=False)
    return weights


def estimate(
    blocks: Sequence[tuple[float, float]],
) -> tuple[float, tuple[float, float] | None] | None:
    """The ratio of totals over (numerator, denominator) blocks, and its 95% interval.

    The interval is a percentile bootstrap resampling whole blocks, None with
    fewer than two. None overall when there are no opportunities: a rate
    without them has no value. Blocks are sorted before resampling, so the
    interval does not depend on the order they were read in.
    """
    totals = np.array(sorted(blocks), dtype=np.float64).reshape(-1, 2)
    numerators, denominators = totals[:, 0], totals[:, 1]
    if denominators.sum() == 0:
        return None
    value = float(numerators.sum() / denominators.sum())
    n = len(totals)
    if n < 2:
        return value, None
    weights = _weights(n)
    resampled = weights @ denominators
    kept = resampled > 0
    ratios = (weights @ numerators)[kept] / resampled[kept]
    low, high = np.percentile(ratios, [2.5, 97.5])
    return value, (float(low), float(high))
