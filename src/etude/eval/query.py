"""Querying eval runs: `connect` opens every one under a source in DuckDB; `compare` asks one question.

`connect` gives one table per author table, holding the rows of every eval
run (a match's rows from the latest file holding them), beside Etude's own
tables:

| table | columns |
|---|---|
| `runs` | run, experiment, environment (identity), environment_name, environment_config, tournament |
| `agents` | agent (identity), name, kind, artifact, uri, config, and `config.KEY` per scalar config key |
| `matches` | run, match, conditions, games, seed, state, error, wall_ms |
| `seats` | run, match, player, agent |
| `groups` | run, match, group, player |
| `tournaments` | tournament, format (`benchmark` or `round_robin`), environment, conflict |
| `schedule` | tournament, match, matchup and checkpoint (artifact digest) of a benchmark's match |
| `training_checkpoints` | run, experiment, ordinal, artifact, wall_ms, clocks (where each stood) |

A tournament is assembled from the eval runs naming it in their
`tournament.json`. `schedule` holds every match it calls for, held or not.
When its eval runs disagree, `conflict` names how (`tournament_conflict`,
`matchup_conflict`) and it has no schedule.
`training_checkpoints` holds every checkpoint a training run recorded under
the source, read from its stored JSON, so a checkpoint agent joins to where
its run's clocks stood (`agents.artifact = training_checkpoints.artifact`).

Tables are read into memory, from a directory in place and from a bucket
through a temporary copy, so the connection outlives the files.

`compare` sets a subject against each opponent and against all of them. The
subject of a `player` row is its seat's agent; of a `group` row, the group's
agents. Opponents are the other seats' agents, or the match's other groups
except `all`. Results from different environments are never pooled.
"""

from __future__ import annotations

import json
import tempfile
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from etude.eval.match import ETUDE_TABLES, IDENTIFIER, KIND, quote
from etude.eval.records import (
    COLUMN_TYPES,
    TABLE_FILE,
    TOURNAMENT_FILE,
    Agent,
    Benchmark,
    ColumnType,
    Environment,
    MatchRecord,
    RoundRobin,
    Tournament,
    TournamentRun,
    assemble,
)
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, declared, places
from etude.run.records import Digest, Record
from etude.run.storage import DirectoryStorage, Storage

if TYPE_CHECKING:
    import duckdb

REPLICATES = 2000
SEED = 0


def connect(source: Storage | Path | str) -> duckdb.DuckDBPyConnection:
    """Every eval run, and the tournaments they make, under a directory or bucket at any depth, in DuckDB."""
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    storage = as_storage(source)
    db = duckdb.connect()
    found = _Found()
    with tempfile.TemporaryDirectory(prefix="etude-eval-") as copies:
        files: dict[str, list[Path]] = {}
        for run, folders in sorted(places(storage).items()):
            run_file = declared(storage, folders)
            if run_file is None:
                continue
            if run_file.declaration.kind != KIND:
                found.checkpoints += _checkpoints(storage, run, run_file.experiment, folders)
                continue
            environment = _environment(storage, folders)
            if environment is None:
                continue
            found.runs.append((run, run_file.experiment, environment))
            # One run may be copied to several places; a file's name is the same in each.
            located: dict[str, str] = {}
            for place in folders:
                for key in storage.keys(place):
                    located.setdefault(key[len(place) :], place)
            held: dict[str, MatchRecord] = {}
            for name, place in sorted(located.items()):
                if name.startswith("matches/") and name.endswith(".json"):
                    record = MatchRecord.model_validate_json(storage.get(place + name))
                    if record.match.key not in held or record.state == "done":
                        held[record.match.key] = record
                elif name == TOURNAMENT_FILE:
                    found.held.setdefault(run, TournamentRun.model_validate_json(
                        storage.get(place + name)))  # fmt: skip
                elif parsed := TABLE_FILE.match(name):
                    files.setdefault(parsed[1], []).append(
                        _local(storage, place + name, Path(copies) / run / name)
                    )
            found.records += [(run, record) for record in held.values()]
        _etude_tables(db, found)
        for table, paths in sorted(files.items()):
            listed = ", ".join(quote(path) for path in paths)
            # A match's rows come from the latest file holding them: a merged file
            # and the files it replaces may both be present for a while. Within a
            # table, file names order by their zero-padded write time.
            db.execute(
                f'create table "{table}" as select * exclude (_file) from read_parquet('
                f"[{listed}], union_by_name = true, filename = '_file') qualify"
                " parse_filename(_file) = max(parse_filename(_file))"
                " over (partition by run, match)"
            )
    return db


@dataclass
class _Found:
    """What `connect` read, before it becomes Etude's tables."""

    runs: list[tuple[str, str, Environment]] = field(default_factory=list)
    records: list[tuple[str, MatchRecord]] = field(default_factory=list)
    # Each eval run held for a tournament, by its name, and what it added.
    held: dict[str, TournamentRun] = field(default_factory=dict)
    checkpoints: list[tuple[str, str, _StoredCheckpoint]] = field(default_factory=list)


class _StoredArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sha256: str | None = None


class _StoredCheckpoint(BaseModel):
    """What evaluation reads of a training run's `checkpoints/NAME.json`; the rest is training's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ordinal: int
    artifact: _StoredArtifact
    at: dict[str, float]
    wall_ms: int


def _checkpoints(
    storage: Storage, run: str, experiment: str, folders: Sequence[str]
) -> list[tuple[str, str, _StoredCheckpoint]]:
    found: dict[int, _StoredCheckpoint] = {}
    for place in folders:
        for key in storage.keys(place + "checkpoints/"):
            if key.endswith(".json"):
                stored = _StoredCheckpoint.model_validate_json(storage.get(key))
                found.setdefault(stored.ordinal, stored)
    return [(run, experiment, found[ordinal]) for ordinal in sorted(found)]


def _tournaments(found: _Found) -> dict[str, Tournament | EtudeError]:
    """Each tournament its eval runs name, assembled from them, or the conflict among them."""
    by_name: dict[str, list[TournamentRun]] = {}
    for run in sorted(found.held):
        added = found.held[run]
        by_name.setdefault(added.tournament.name, []).append(added)
    assembled: dict[str, Tournament | EtudeError] = {}
    for name, held in sorted(by_name.items()):
        try:
            assembled[name] = assemble(held)
        except EtudeError as conflict:
            assembled[name] = conflict
    return assembled


def _environment(storage: Storage, folders: Sequence[str]) -> Environment | None:
    for place in folders:
        try:
            return Environment.model_validate_json(storage.get(place + "environment.json"))
        except (FileNotFoundError, ValidationError):
            continue
    return None


def _local(storage: Storage, key: str, copy: Path) -> Path:
    if isinstance(storage, DirectoryStorage):
        return storage.root / key
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(storage.get(key))
    return copy


def _etude_tables(db: duckdb.DuckDBPyConnection, found: _Found) -> None:
    db.execute(
        "create table runs (run VARCHAR, experiment VARCHAR, environment VARCHAR,"
        " environment_name VARCHAR, environment_config JSON, tournament VARCHAR)"
    )
    _insert(db, 
        "insert into runs values (?, ?, ?, ?, ?, ?)",
        [
            [run, experiment, environment.identity, environment.name,
             json.dumps(environment.config),
             found.held[run].tournament.name if run in found.held else None]
            for run, experiment, environment in found.runs
        ],
    )  # fmt: skip
    db.execute(
        "create table matches (run VARCHAR, match VARCHAR, conditions JSON, games BIGINT,"
        " seed BIGINT, state VARCHAR, error VARCHAR, wall_ms BIGINT)"
    )
    db.execute("create table seats (run VARCHAR, match VARCHAR, player BIGINT, agent VARCHAR)")
    db.execute('create table "groups" (run VARCHAR, match VARCHAR, "group" VARCHAR, player BIGINT)')
    agents: dict[Digest, list[Agent]] = {}
    for run, record in found.records:
        match = record.match
        db.execute(
            "insert into matches values (?, ?, ?, ?, ?, ?, ?, ?)",
            [run, match.key, json.dumps(match.conditions), match.games, match.seed,
             record.state, record.error, record.wall_ms],
        )  # fmt: skip
        _insert(
            db,
            "insert into seats values (?, ?, ?, ?)",
            [[run, match.key, seat, seated.identity] for seat, seated in enumerate(match.agents)],
        )
        _insert(
            db,
            'insert into "groups" values (?, ?, ?, ?)',
            [
                [run, match.key, name, seat]
                for name, seats in match.groups.items()
                for seat in seats
            ],
        )
        for seated in match.agents:
            agents.setdefault(seated.identity, []).append(seated)
    db.execute(
        "create table tournaments (tournament VARCHAR, format VARCHAR, environment VARCHAR,"
        " conflict VARCHAR)"
    )
    db.execute(
        "create table schedule (tournament VARCHAR, match VARCHAR, matchup VARCHAR,"
        " checkpoint VARCHAR)"
    )
    scheduled = []
    for name, assembled in _tournaments(found).items():
        if isinstance(assembled, EtudeError):
            first = next(
                run.tournament for run in found.held.values() if run.tournament.name == name
            )
            db.execute(
                "insert into tournaments values (?, ?, ?, ?)",
                [name, _format(first), first.environment.identity, assembled.code],
            )
            continue
        definition = assembled.definition
        db.execute(
            "insert into tournaments values (?, ?, ?, null)",
            [name, _format(definition), definition.environment.identity],
        )
        schedule = assembled.schedule()
        _insert(
            db,
            "insert into schedule values (?, ?, ?, ?)",
            [[name, each.match.key, each.matchup, each.checkpoint] for each in schedule],
        )
        scheduled += schedule
    # Agents of matches not yet held, so every scheduled agent has a name.
    for each in scheduled:
        for seated in each.match.agents:
            agents.setdefault(seated.identity, []).append(seated)
    db.execute(
        "create table training_checkpoints (run VARCHAR, experiment VARCHAR, ordinal BIGINT,"
        " artifact VARCHAR, wall_ms BIGINT, clocks JSON)"
    )
    _insert(db, 
        "insert into training_checkpoints values (?, ?, ?, ?, ?, ?)",
        [
            [run, experiment, stored.ordinal, stored.artifact.sha256, stored.wall_ms,
             json.dumps(stored.at)]
            for run, experiment, stored in found.checkpoints
        ],
    )  # fmt: skip
    _agents_table(db, agents)


def _format(definition: Benchmark | RoundRobin) -> str:
    return "benchmark" if isinstance(definition, Benchmark) else "round_robin"


def _insert(
    db: duckdb.DuckDBPyConnection, statement: str, rows: Sequence[Sequence[object]]
) -> None:
    # DuckDB refuses an empty batch.
    if rows:
        db.executemany(statement, rows)


def _agents_table(db: duckdb.DuckDBPyConnection, agents: dict[Digest, list[Agent]]) -> None:
    """One row per identity; each scalar config key, dotted when nested, as a column."""
    flat = {identity: _flattened(given[0].config) for identity, given in agents.items()}
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
            warnings.warn(f"agent config {key!r} has types {sorted(seen)}; only in `config`")
    db.execute(
        "create table agents (agent VARCHAR, name VARCHAR, kind VARCHAR, artifact VARCHAR,"
        " uri VARCHAR, config JSON"
        + "".join(f', "config.{key}" {kind}' for key, kind in columns.items())
        + ")"
    )
    _insert(
        db,
        f"insert into agents values ({', '.join('?' for _ in range(6 + len(columns)))})",
        [
            [
                identity,
                "/".join(dict.fromkeys(each.name for each in given)),
                given[0].kind,
                given[0].artifact,
                given[0].uri,
                json.dumps(given[0].config),
                *(flat[identity].get(key) for key in columns),
            ]
            for identity, given in agents.items()
        ],
    )


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
    # Agent identities: one for a player, a group's in sorted order.
    subject: tuple[Digest, ...]
    # The same for the opponent; None means against every opponent.
    opponent: tuple[Digest, ...] | None
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
            select row_number() over () as _row, run, match, player, "group",
                cast("{value}" as DOUBLE) as _value,
                cast({f'"{per}"' if per else 1} as DOUBLE) as _per
                {"".join(f', "{name}"' for name in by)}
            from "{table}"
            where "{value}" is not null and ({where or "true"})
        ),
        teams as (
            select g.run, g.match, g."group", string_agg(s.agent, '+' order by s.agent) as team
            from "groups" g join seats s using (run, match, player)
            group by all
        ),
        subjects as (
            select r.*, s.agent as subject from r join seats s using (run, match, player)
            union all by name
            select r.*, t.team as subject from r join teams t using (run, match, "group")
        ),
        opponents as (
            select distinct x._row, s.agent as opponent
            from subjects x join seats s
                on s.run = x.run and s.match = x.match and s.player <> x.player
            union
            select distinct x._row, t.team
            from subjects x join teams t
                on t.run = x.run and t.match = x.match
                and t."group" <> x."group" and t."group" <> 'all'
        ),
        pairs as (
            select x.*, null::VARCHAR as opponent from subjects x
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
        key = (environment, subject, opponent, *values)
        blocks.setdefault(key, []).append((float(numerator), float(denominator)))
        rows[key] = rows.get(key, 0) + count
    comparisons: list[Comparison] = []
    for key in sorted(blocks, key=lambda k: tuple("" if v is None else str(v) for v in k)):
        estimated = estimate(blocks[key])
        if estimated is None:
            continue
        environment, subject, opponent, *values = key
        comparisons.append(
            Comparison(
                environment=str(environment),
                subject=tuple(str(subject).split("+")),
                opponent=None if opponent is None else tuple(str(opponent).split("+")),
                # DuckDB returns plain values for the author's scalar columns.
                split=dict(zip(by, cast(list[JsonValue], values))),
                value=estimated[0],
                interval=estimated[1],
                rows=rows[key],
                matches=len(blocks[key]),
            )
        )
    return comparisons


def estimate(
    blocks: Sequence[tuple[float, float]],
) -> tuple[float, tuple[float, float] | None] | None:
    """The ratio of totals over (numerator, denominator) blocks, and its 95% interval.

    The interval is a percentile bootstrap resampling whole blocks, None with
    fewer than two. None overall when there are no opportunities: a rate
    without them has no value.
    """
    totals = np.array(blocks, dtype=np.float64).reshape(-1, 2)
    numerators, denominators = totals[:, 0], totals[:, 1]
    if denominators.sum() == 0:
        return None
    value = float(numerators.sum() / denominators.sum())
    n = len(totals)
    if n < 2:
        return value, None
    weights = np.random.default_rng(SEED).multinomial(n, np.full(n, 1 / n), size=REPLICATES)
    resampled = weights @ denominators
    kept = resampled > 0
    ratios = (weights @ numerators)[kept] / resampled[kept]
    low, high = np.percentile(ratios, [2.5, 97.5])
    return value, (float(low), float(high))
