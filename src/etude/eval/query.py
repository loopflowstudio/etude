"""Querying tournaments: `connect` opens every tournament under a source in DuckDB; `compare` asks one question.

`connect` gives one table per author table, holding the rows of every
tournament (a match's rows from the latest file holding them), beside Etude's
own tables:

| table | columns |
|---|---|
| `tournaments` | tournament, experiment, environment (identity), environment_name, environment_config |
| `agents` | agent (identity), name, kind, artifact, uri, config, and `config.KEY` per scalar config key |
| `matches` | tournament, match, conditions, games, seed, state, error |
| `seats` | tournament, match, player, agent |
| `groups` | tournament, match, group, player |

Tables are read into memory, from a directory in place and from a bucket
through a temporary copy, so the connection outlives the files.

`compare` sets a subject against each opponent and against all of them. The
subject of a `player` row is its seat's agent; of a `group` row, the group's
agents. Opponents are the other seats' agents, or the match's other groups
except `all`. Results from different environments are never pooled.
"""

from __future__ import annotations

import json
import re
import tempfile
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
from pydantic import JsonValue, ValidationError

from etude.eval.records import Agent, Environment, MatchRecord
from etude.eval.tournament import ETUDE_TABLES, IDENTIFIER, KIND
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, declared, places
from etude.run.records import Digest, Record
from etude.run.storage import DirectoryStorage, Storage

if TYPE_CHECKING:
    import duckdb

REPLICATES = 2000
SEED = 0
# `tables/TABLE.WRITTEN.KEY.parquet`, WRITTEN being nanoseconds, zero-padded.
_TABLE_FILE = re.compile(r"^tables/([A-Za-z][A-Za-z0-9_]*)\.(\d{20})\.([0-9a-f]+)\.parquet$")


def connect(source: Storage | Path | str) -> duckdb.DuckDBPyConnection:
    """Every tournament under a directory or bucket, at any depth, as one DuckDB database."""
    # Imported here: DuckDB is the optional `eval` extra.
    import duckdb  # noqa: PLC0415

    storage = as_storage(source)
    db = duckdb.connect()
    tournaments: list[tuple[str, str, Environment]] = []
    records: list[tuple[str, MatchRecord]] = []
    with tempfile.TemporaryDirectory(prefix="etude-eval-") as copies:
        files: dict[str, list[Path]] = {}
        for run, folders in sorted(places(storage).items()):
            run_file = declared(storage, folders)
            found = _environment(storage, folders)
            if run_file is None or run_file.declaration.kind != KIND or found is None:
                continue
            tournaments.append((run, run_file.experiment, found))
            # One run may be copied to several places; a file's name is the same in each.
            located: dict[str, str] = {}
            for place in folders:
                for key in storage.keys(place):
                    located.setdefault(key[len(place) :], place)
            for name, place in sorted(located.items()):
                if name.startswith("matches/") and name.endswith(".json"):
                    records.append(
                        (run, MatchRecord.model_validate_json(storage.get(place + name)))
                    )
                elif parsed := _TABLE_FILE.match(name):
                    files.setdefault(parsed[1], []).append(
                        _local(storage, place + name, Path(copies) / run / name)
                    )
        _etude_tables(db, tournaments, records)
        for table, paths in sorted(files.items()):
            listed = ", ".join(_literal(path) for path in paths)
            # A match's rows come from the latest file holding them: a merged file
            # and the files it replaces may both be present for a while.
            db.execute(
                f'create table "{table}" as select * exclude (_file, _written) from ('
                f"select *, regexp_extract(_file, '\\.(\\d{{20}})\\.[0-9a-f]+\\.parquet$', 1)"
                f" as _written from read_parquet([{listed}], union_by_name = true,"
                " filename = '_file')) qualify _written = max(_written) over"
                " (partition by tournament, match)"
            )
    return db


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


def _literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _etude_tables(
    db: duckdb.DuckDBPyConnection,
    tournaments: Sequence[tuple[str, str, Environment]],
    records: Sequence[tuple[str, MatchRecord]],
) -> None:
    db.execute(
        "create table tournaments (tournament VARCHAR, experiment VARCHAR, environment VARCHAR,"
        " environment_name VARCHAR, environment_config JSON)"
    )
    db.executemany(
        "insert into tournaments values (?, ?, ?, ?, ?)",
        [
            [run, experiment, found.identity, found.name, json.dumps(found.config)]
            for run, experiment, found in tournaments
        ],
    )
    db.execute(
        "create table matches (tournament VARCHAR, match VARCHAR, conditions JSON, games BIGINT,"
        " seed BIGINT, state VARCHAR, error VARCHAR)"
    )
    db.execute(
        "create table seats (tournament VARCHAR, match VARCHAR, player BIGINT, agent VARCHAR)"
    )
    db.execute(
        'create table "groups" (tournament VARCHAR, match VARCHAR, "group" VARCHAR, player BIGINT)'
    )
    agents: dict[Digest, list[Agent]] = {}
    for run, record in records:
        match = record.match
        db.execute(
            "insert into matches values (?, ?, ?, ?, ?, ?, ?)",
            [run, match.key, json.dumps(match.conditions), match.games, match.seed,
             record.state, record.error],
        )  # fmt: skip
        db.executemany(
            "insert into seats values (?, ?, ?, ?)",
            [[run, match.key, seat, seated.identity] for seat, seated in enumerate(match.agents)],
        )
        db.executemany(
            'insert into "groups" values (?, ?, ?, ?)',
            [
                [run, match.key, name, seat]
                for name, seats in match.groups.items()
                for seat in seats
            ],
        )
        for seated in match.agents:
            agents.setdefault(seated.identity, []).append(seated)
    _agents_table(db, agents)


def _agents_table(db: duckdb.DuckDBPyConnection, agents: dict[Digest, list[Agent]]) -> None:
    """One row per identity; each scalar config key, dotted when nested, as a column."""
    flat = {identity: _flattened(given[0].config) for identity, given in agents.items()}
    kinds: dict[str, set[str]] = {}
    for values in flat.values():
        for key, value in values.items():
            if value is not None:
                kinds.setdefault(key, set()).add(_SQL_TYPES[type(value)])
    columns: dict[str, str] = {}
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
    db.executemany(
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


_SQL_TYPES: dict[type, str] = {bool: "BOOLEAN", int: "BIGINT", float: "DOUBLE", str: "VARCHAR"}


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
            select row_number() over () as _row, tournament, match, player, "group",
                cast("{value}" as DOUBLE) as _value,
                cast({f'"{per}"' if per else 1} as DOUBLE) as _per
                {"".join(f', "{name}"' for name in by)}
            from "{table}"
            where "{value}" is not null and ({where or "true"})
        ),
        teams as (
            select g.tournament, g.match, g."group", string_agg(s.agent, '+' order by s.agent) as team
            from "groups" g join seats s using (tournament, match, player)
            group by all
        ),
        subjects as (
            select r.*, s.agent as subject from r join seats s using (tournament, match, player)
            union all by name
            select r.*, t.team as subject from r join teams t using (tournament, match, "group")
        ),
        opponents as (
            select distinct x._row, s.agent as opponent
            from subjects x join seats s
                on s.tournament = x.tournament and s.match = x.match and s.player <> x.player
            union
            select distinct x._row, t.team
            from subjects x join teams t
                on t.tournament = x.tournament and t.match = x.match
                and t."group" <> x."group" and t."group" <> 'all'
        ),
        pairs as (
            select x.*, null::VARCHAR as opponent from subjects x
            union all
            select x.*, o.opponent from subjects x join opponents o using (_row)
        )
        select n.environment, r.subject, r.opponent {split}, r.tournament, r.match,
            sum(r._value) as numerator, sum(r._per) as denominator, count(*) as rows
        from pairs r join tournaments n using (tournament)
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
        totals = np.array(blocks[key])
        if totals[:, 1].sum() == 0:
            continue  # a rate with no opportunities has no value
        environment, subject, opponent, *values = key
        comparisons.append(
            Comparison(
                environment=str(environment),
                subject=tuple(str(subject).split("+")),
                opponent=None if opponent is None else tuple(str(opponent).split("+")),
                # DuckDB returns plain values for the author's scalar columns.
                split=dict(zip(by, cast(list[JsonValue], values))),
                value=float(totals[:, 0].sum() / totals[:, 1].sum()),
                interval=_interval(totals[:, 0], totals[:, 1]),
                rows=rows[key],
                matches=len(totals),
            )
        )
    return comparisons


def _interval(
    numerators: np.ndarray[tuple[int], np.dtype[np.float64]],
    denominators: np.ndarray[tuple[int], np.dtype[np.float64]],
) -> tuple[float, float] | None:
    """A 95% percentile bootstrap of the ratio of totals, resampling blocks."""
    n = len(numerators)
    if n < 2:
        return None
    weights = np.random.default_rng(SEED).multinomial(n, np.full(n, 1 / n), size=REPLICATES)
    totals = weights @ denominators
    kept = totals > 0
    ratios = (weights @ numerators)[kept] / totals[kept]
    low, high = np.percentile(ratios, [2.5, 97.5])
    return float(low), float(high)
