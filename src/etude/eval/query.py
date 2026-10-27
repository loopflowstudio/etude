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
the store once and reads only the files it has not read before: match
records, table files, tournament and checkpoint files are written once, so
nothing already held is read again. A failed match's record is replaced by
the done record that follows it. The few tables that summarize the rest
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
import json
import os
import tempfile
import warnings
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator, Sequence
from functools import cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from etude.eval.match import ETUDE_TABLES, IDENTIFIER, KIND, quote
from etude.eval.records import (
    COLUMN_TYPES,
    TABLE_FILE,
    TOURNAMENT_FILE,
    Benchmark,
    CheckpointKey,
    ColumnType,
    Environment,
    MatchRecord,
    Model,
    Player,
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
SEED = 0
# Column types `compare` can take as a value.
NUMERIC = ("BOOLEAN", "BIGINT", "DOUBLE")
# Keys and coordinates of an author table's rows: never a value.
KEYS = ("seat", "group", "game", "run", "match")
# A kept database of another schema is deleted and read again from its store.
SCHEMA = 4

_ENVIRONMENT_FILE = "environment.json"
_PLAYER: TypeAdapter[Player] = TypeAdapter(Player)


def statsdb(
    experiment: str, store: Storage | Path | str | None = None, *, rebuild: bool = False
) -> duckdb.DuckDBPyConnection:
    """The experiment's database of its eval runs, caught up with `store` as it opens.

    `store` is a directory or bucket searched at any depth; by default, where
    runs are written on this computer. `rebuild` deletes the kept file first,
    so everything is read again. One process opens a kept file at a time:
    another open raises `EtudeError("statsdb_in_use")`.
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
        _catch_up(db, experiment, storage)
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
    # The file each author table's rows of a match came from: the latest holding them.
    db.execute(
        'create table kept.table_files ("table" VARCHAR, run VARCHAR, match VARCHAR, file VARCHAR)'
    )
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


def _catch_up(db: duckdb.DuckDBPyConnection, experiment: str, storage: Storage) -> None:
    """Read what arrived in `storage` since the last open, in one transaction.

    An interrupted catch-up commits nothing, so the next open repeats it.
    """
    keys = sorted(storage.keys(""))
    db.execute("begin")
    try:
        created = _schema(db) is None
        if created:
            _create(db, experiment, storage)
        changed = created
        with tempfile.TemporaryDirectory(prefix="etude-statsdb-") as copies:
            for run, folders in sorted(run_folders(keys).items()):
                if not _ours(db, storage, run, folders, experiment):
                    continue
                if _kind(db, run) == KIND and not _environment(db, storage, run, folders):
                    continue  # an eval run counts once its environment arrives
                # One run may be copied to several places; a file's name is the same in each.
                located: dict[str, str] = {}
                for place in folders:
                    for key in _under(keys, place):
                        located.setdefault(key[len(place) :], place)
                seen = {name for (name,) in db.execute(
                    "select name from kept.seen where run = ?", [run]).fetchall()}  # fmt: skip
                for name in sorted(located.keys() - seen):
                    if _read(db, storage, run, experiment, located[name], name, Path(copies)):
                        changed = True
                    db.execute("insert into kept.seen values (?, ?)", [run, name])
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


def _read(
    db: duckdb.DuckDBPyConnection,
    storage: Storage,
    run: str,
    experiment: str,
    place: str,
    name: str,
    copies: Path,
) -> bool:
    """Take in one file of the run not read before; whether any table changed."""
    if _kind(db, run) != KIND:
        if not (name.startswith("checkpoints/") and name.endswith(".json")):
            return False
        stored = _StoredCheckpoint.model_validate_json(storage.get(place + name))
        # One run copied to several places records each checkpoint once.
        db.execute(
            "insert into training_checkpoints select ?, ?, ?, ?, ?, ?, ? where not exists"
            " (select 1 from training_checkpoints where run = ? and ordinal = ?)",
            [run, experiment, stored.ordinal,
             str(CheckpointKey(run=run, name=PurePosixPath(stored.artifact.uri).name)),
             stored.artifact.sha256, stored.wall_ms, json.dumps(stored.at), run, stored.ordinal],
        )  # fmt: skip
        return True
    if name == TOURNAMENT_FILE:
        held = TournamentRun.model_validate_json(storage.get(place + name))
        db.execute("insert into kept.tournament_runs values (?, ?)", [run, held.model_dump_json()])
        return True
    if name.startswith("matches/") and name.endswith(".json"):
        return _match(db, run, MatchRecord.model_validate_json(storage.get(place + name)))
    if parsed := TABLE_FILE.match(name):
        _table(db, run, parsed[1], name, _local(storage, place + name, copies / run / name))
        return True
    return False


def _match(db: duckdb.DuckDBPyConnection, run: str, record: MatchRecord) -> bool:
    """Hold a match's record unless a done one is already held; a done one replaces a failure."""
    match = record.match
    found = db.execute(
        "select state from matches where run = ? and match = ?", [run, match.key]
    ).fetchone()
    if found is not None and (found[0] == "done" or record.state != "done"):
        return False
    for table in ("matches", "seats", '"groups"'):
        db.execute(f"delete from {table} where run = ? and match = ?", [run, match.key])
    db.execute(
        "insert into matches values (?, ?, ?, ?, ?, ?, ?, ?)",
        [run, match.key, json.dumps(match.conditions), match.games, match.seed,
         record.state, record.error, record.wall_ms],
    )  # fmt: skip
    _insert(
        db,
        "insert into seats values (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [
                run,
                match.key,
                seat,
                player.key,
                player.version.commit,
                player.version.source,
                _canonical(player.checkpoint.at)
                if isinstance(player, Model) and player.checkpoint.at
                else None,
                player.checkpoint.line if isinstance(player, Model) else None,
            ]
            for seat, player in enumerate(match.players)
        ],  # fmt: skip
    )
    _insert(
        db,
        'insert into "groups" values (?, ?, ?, ?)',
        [[run, match.key, name, seat] for name, seats in match.groups.items() for seat in seats],
    )
    _remember_players(db, match.players)
    return True


def _canonical(value: object) -> str:
    """JSON with sorted keys, so equal positions compare equal as text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _remember_players(db: duckdb.DuckDBPyConnection, players: Sequence[Player]) -> None:
    for player in players:
        db.execute(
            "insert into kept.players select ?, ?, ? where not exists"
            " (select 1 from kept.players where player = ? and name = ?)",
            [player.key, player.name, player.model_dump_json(), player.key, player.name],
        )


def _table(db: duckdb.DuckDBPyConnection, run: str, table: str, name: str, path: Path) -> None:
    """Add one table file's rows, keeping each match's rows from the latest file holding them.

    A merged file and the files it replaces may both arrive, in either order.
    Within a table, file names order by their zero-padded write time.
    """
    file = name.removeprefix("tables/")
    db.execute(
        f"create or replace temp table _arrived as select * from read_parquet({quote(path)})"
    )
    db.execute(
        'delete from _arrived using kept.table_files f where f."table" = ? and f.run = _arrived.run'
        " and f.match = _arrived.match and f.file > ?",
        [table, file],
    )
    arrived = {column: kind for column, kind, *_ in db.execute("describe _arrived").fetchall()}
    if "player" in arrived and "seat" not in arrived:
        db.execute("alter table _arrived rename column player to seat")
        arrived = {
            ("seat" if column == "player" else column): kind for column, kind in arrived.items()
        }
    exists = db.execute(
        "select 1 from information_schema.tables where table_schema = 'main' and table_name = ?",
        [table],
    ).fetchone()
    if exists is None:
        db.execute(f'create table "{table}" as select * from _arrived')
    else:
        held = {column: kind for column, kind, *_ in db.execute(f'describe "{table}"').fetchall()}
        for column, kind in arrived.items():
            if column not in held:
                db.execute(f'alter table "{table}" add column "{column}" {kind}')
            elif (held[column], kind) == ("BIGINT", "DOUBLE"):
                db.execute(f'alter table "{table}" alter "{column}" type DOUBLE')
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


def _local(storage: Storage, key: str, copy: Path) -> Path:
    if isinstance(storage, DirectoryStorage):
        return storage.root / key
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(storage.get(key))
    return copy


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
    for run, experiment, text in db.execute(
        "select r.run, r.experiment, e.environment from kept.runs r"
        " join kept.environments e using (run) order by r.run"
    ).fetchall():
        environment = Environment.model_validate_json(text)
        db.execute(
            "insert into runs values (?, ?, ?, ?, ?, ?)",
            [run, experiment, environment.identity, environment.name,
             json.dumps(environment.config),
             held[run].tournament.name if run in held else None],
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
        _insert(
            db,
            "insert into schedule values (?, ?, ?, ?, ?)",
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


def _insert(
    db: duckdb.DuckDBPyConnection, statement: str, rows: Sequence[Sequence[object]]
) -> None:
    # DuckDB refuses an empty batch.
    if rows:
        db.executemany(statement, rows)


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
    _insert(
        db,
        f"insert into players values ({', '.join('?' for _ in range(11 + len(columns)))})",
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


def _weights(n: int) -> npt.NDArray[np.int64]:
    """How often each of `n` blocks is drawn in each replicate: fixed by the seed and `n` alone.

    A report asks for thousands of estimates over a few matches each, so small
    counts are drawn once per process (at most 33 MB for every count to 64).
    """
    return _kept_weights(n) if n <= 64 else _draw(n)


def _draw(n: int) -> npt.NDArray[np.int64]:
    return np.random.default_rng(SEED).multinomial(n, np.full(n, 1 / n), size=REPLICATES)


@cache
def _kept_weights(n: int) -> npt.NDArray[np.int64]:
    drawn = _draw(n)
    drawn.setflags(write=False)
    return drawn


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
