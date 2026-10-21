"""Tournaments: structures that arrange eval runs to produce a set of matches.

A `Benchmark` (`etude.eval.records`) is named matchups a model's checkpoints
are held for; a `RoundRobin` is hand-picked agents, every two of them once.
`hold` plays, as one eval run (`etude.eval.match.evaluate`), the matches a
tournament adds. `curves` follows each matchup over training; `standings`
ranks a round robin. Both read a database `etude.eval.query.connect` opened.

A tournament is stored nowhere but in its eval runs: each keeps, in its own
folder, `tournament.json` (`TournamentRun`), the definition it was held with
and the checkpoint or entrants it added. The tournament is every eval run
naming it (`etude.eval.records.assemble`), so a run delivered from a rented
machine like any other run brings its part of the tournament with it, and
many small eval runs may grow one tournament at once. A match two of them
hold counts once, from the later.

Holding a checkpoint, or entrants, reads nothing of the tournament: it plays
the matches those add, which no other eval run need have played. Any other
hold lists each place once, reads every eval run's `tournament.json`, and
plays what the tournament calls for and none of its eval runs delivered.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from etude.eval.match import ETUDE_TABLES, IDENTIFIER, KIND, EvalRun, Tables, evaluate
from etude.eval.query import compare, estimate
from etude.eval.records import (
    TOURNAMENT_FILE,
    Agent,
    Benchmark,
    Match,
    RoundRobin,
    TournamentRun,
    assemble,
    digest,
)
from etude.run.declaration import declare
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Digest, Record
from etude.run.storage import DirectoryStorage, Storage, default_root

if TYPE_CHECKING:
    import duckdb

# A run's `tournament.json`, at any depth of a listing.
_IN_RUN = "/" + TOURNAMENT_FILE


def hold(
    tournament: Benchmark | RoundRobin,
    *,
    play: Callable[[Match], Tables],
    checkpoint: ArtifactRef | None = None,
    enter: Sequence[Agent] = (),
    workers: int = 1,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    share: tuple[int, int] = (0, 1),
) -> EvalRun:
    """Play, as one eval run, the matches the tournament adds; the run records what was given.

    A benchmark held with `checkpoint` (on the go) plays each of the given
    matchups with the checkpoint in the model's seats. Held without one
    (batch), it plays every matchup any eval run defined for every checkpoint
    any held, skipping what was delivered, so a matchup added later is played
    for every checkpoint so far. A round robin held with `enter` plays every
    pair of the agents given; with none, every pair of every entrant so far
    not yet delivered.

    `share=(k, n)` plays only the matches whose key is k modulo n, so n
    machines may split one hold. Under `etude launch` the eval run is the
    launched run (`ETUDE_RUN`); elsewhere it is named for what is given, so
    the same call resumes it. A batch hold raises
    `EtudeError("tournament_conflict")` when the tournament's eval runs or
    this definition disagree on its format, environment or rules, and
    `EtudeError("matchup_conflict")` when they define a matchup name two ways;
    an on-the-go hold leaves those to be found on read. Raises whatever
    `evaluate` raises.
    """
    k, n = share
    if not 0 <= k < n:
        raise EtudeError("share_out_of_range", 422)
    if isinstance(tournament, Benchmark) and enter:
        raise EtudeError("benchmark_holds_checkpoints_not_entrants", 422)
    if isinstance(tournament, RoundRobin) and checkpoint is not None:
        raise EtudeError("round_robin_holds_entrants_not_checkpoints", 422)
    if checkpoint is not None and checkpoint.sha256 is None:
        raise EtudeError("checkpoint_agent_requires_digest", 422)
    added = TournamentRun(
        tournament=tournament, checkpoint=checkpoint, entrants=tuple(enter), share=share
    )
    on_the_go = checkpoint is not None or bool(enter)
    delivered: set[Digest] = set()
    held = [added]
    if not on_the_go:
        base = Path(directory) if directory is not None else default_root()
        stored, delivered = _stored(tournament.name, [DirectoryStorage(base), *storages])
        held = [*stored, added]
    # Two matchups may share a match; it is played once.
    matches = {each.match.key: each.match for each in assemble(held).schedule()}
    pending = [
        match
        for key, match in sorted(matches.items())
        if int(key, 16) % n == k and key not in delivered
    ]
    run = os.environ.get("ETUDE_RUN") or (
        f"{tournament.name}.{digest(added.model_dump(mode='json'))[:12]}"
    )
    _record(added, run, directory)
    return evaluate(
        experiment=tournament.name,
        environment=tournament.environment,
        matches=pending,
        play=play,
        workers=workers,
        run=run,
        directory=directory,
        storages=storages,
    )


def _record(added: TournamentRun, run: str, directory: Path | str | None) -> None:
    """Write `tournament.json` in the eval run's folder; a resumed run must have been held alike."""
    declared = declare(
        experiment=added.tournament.name, clocks=(), run=run, kind=KIND, directory=directory
    )
    if declared.declaration.kind != KIND:
        raise EtudeError("run_is_not_an_eval_run", 409)
    path = declared.root / TOURNAMENT_FILE
    if not path.is_file():
        DirectoryStorage(declared.root).put(TOURNAMENT_FILE, added.model_dump_json().encode())
    elif TournamentRun.model_validate_json(path.read_bytes()) != added:
        raise EtudeError("eval_run_tournament_conflict", 409)


def _stored(name: str, places: Sequence[Storage]) -> tuple[list[TournamentRun], set[Digest]]:
    """The tournament's eval runs in every place, and the keys of every match they hold as done.

    One listing per place, and one read of each eval run's `tournament.json`.
    """
    held: dict[str, TournamentRun] = {}
    delivered: set[Digest] = set()
    for place in {place.name: place for place in places}.values():
        keys = place.keys("")
        folders = [key.removesuffix(TOURNAMENT_FILE) for key in keys if key.endswith(_IN_RUN)]
        mine: set[str] = set()
        for folder in folders:
            added = TournamentRun.model_validate_json(place.get(folder + TOURNAMENT_FILE))
            if added.tournament.name == name:
                mine.add(folder)
                held.setdefault(folder.rstrip("/").rsplit("/", 1)[-1], added)
        for key in keys:
            folder, _, file = key.rpartition("matches/")
            if folder in mine and file.endswith(".json") and not file.endswith(".failed.json"):
                delivered.add(file.removesuffix(".json"))
    return [held[run] for run in sorted(held)], delivered


@contextmanager
def _held(db: duckdb.DuckDBPyConnection, tournament: str) -> Iterator[tuple[int, int]]:
    """Temp table `_held` (run, match) of each scheduled match held; yields (scheduled, held).

    Each scheduled match counts once, from the tournament's eval run that recorded it last.
    Raises the conflict `connect` found among the tournament's eval runs, if any.
    """
    found = db.execute(
        "select conflict from tournaments where tournament = ?", [tournament]
    ).fetchone()
    if found is None:
        raise EtudeError("tournament_not_found", 404)
    if found[0] is not None:
        raise EtudeError(found[0], 409) from ValueError(tournament)
    db.execute(
        "create or replace temp table _held as"
        " select arg_max(m.run, m.wall_ms) as run, m.match from matches m join runs r using (run)"
        " where r.tournament = $1 and m.state = 'done'"
        " and m.match in (select match from schedule where tournament = $1)"
        " group by m.match",
        [tournament],
    )
    try:
        counted = db.execute(
            "select (select count(distinct match) from schedule where tournament = ?),"
            " (select count(*) from _held)",
            [tournament],
        ).fetchone()
        assert counted is not None
        yield counted[0], counted[1]
    finally:
        db.execute("drop table _held")


class CurvePoint(Record):
    """A matchup's value for one checkpoint, and where its training run stood when it was saved."""

    checkpoint: Digest
    # None for a checkpoint no training run under the source recorded.
    ordinal: int | None
    clocks: dict[str, float]
    value: float
    # A 95% interval resampling whole matches; None with fewer than two.
    interval: tuple[float, float] | None
    rows: int
    matches: int


class Curve(Record):
    """One matchup of a benchmark over one training run's checkpoints, in training order."""

    matchup: str
    # The training run and its experiment; None for checkpoints no run recorded.
    run: str | None
    experiment: str | None
    points: tuple[CurvePoint, ...]


def curves(
    db: duckdb.DuckDBPyConnection,
    benchmark: str,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
) -> list[Curve]:
    """Each matchup's first player on `table`'s `value`, per checkpoint, as `compare` measures it.

    Only the first player's (seat 0's) rows of the benchmark's scheduled
    matches count, so a matchup of the model against itself reads alike.
    `per` and `where` are as in `etude.eval.query.compare`.
    """
    for name in (table, value, *([per] if per else [])):
        if not IDENTIFIER.match(name):
            raise EtudeError("column_name_refused", 422) from ValueError(name)
    if table in ETUDE_TABLES:
        raise EtudeError("curves_need_an_author_table", 422)
    with _held(db, benchmark):
        found = db.execute(
            f"""
            with r as (
                select run, match, cast("{value}" as DOUBLE) as _value,
                    cast({f'"{per}"' if per else 1} as DOUBLE) as _per
                from "{table}"
                where player = 0 and "{value}" is not null and ({where or "true"})
            ),
            -- A checkpoint two training runs recorded belongs to the first.
            trained as (
                select artifact, run, experiment, ordinal, clocks from training_checkpoints
                qualify row_number() over (partition by artifact order by run, ordinal) = 1
            )
            select s.matchup, t.run, t.experiment, s.checkpoint, t.ordinal, t.clocks,
                sum(r._value), sum(r._per), count(*)
            from r join _held using (run, match)
            join schedule s on s.tournament = ? and s.match = r.match
            left join trained t on t.artifact = s.checkpoint
            group by s.matchup, t.run, t.experiment, s.checkpoint, t.ordinal, t.clocks, r.match
            """,
            [benchmark],
        ).fetchall()
    # Per point, where its checkpoint stood: each match's totals and rows.
    blocks: dict[
        tuple[str, str | None, str | None, str, int | None, str | None],
        list[tuple[float, float, int]],
    ] = {}
    for matchup, run, experiment, checkpoint, ordinal, clocks, total, per_total, rows in found:
        blocks.setdefault((matchup, run, experiment, checkpoint, ordinal, clocks), []).append(
            (float(total), float(per_total), int(rows))
        )
    lines: dict[tuple[str, str | None, str | None], list[CurvePoint]] = {}
    for (matchup, run, experiment, checkpoint, ordinal, clocks), totals in blocks.items():
        estimated = estimate([(total, opportunities) for total, opportunities, _ in totals])
        if estimated is None:
            continue
        lines.setdefault((matchup, run, experiment), []).append(
            CurvePoint(
                checkpoint=checkpoint,
                ordinal=ordinal,
                clocks=json.loads(clocks) if clocks else {},
                value=estimated[0],
                interval=estimated[1],
                rows=sum(rows for *_, rows in totals),
                matches=len(totals),
            )
        )
    return [
        Curve(
            matchup=matchup,
            run=run,
            experiment=experiment,
            points=tuple(
                sorted(points, key=lambda p: (p.ordinal is None, p.ordinal or 0, p.checkpoint))
            ),
        )
        for (matchup, run, experiment), points in sorted(
            lines.items(), key=lambda item: tuple("" if v is None else v for v in item[0])
        )
    ]


class Standing(Record):
    """An entrant's value against one opponent in a round robin, or against the field (`opponent` None)."""

    agent: Digest
    name: str
    opponent: Digest | None
    value: float
    # A 95% interval resampling whole matches; None with fewer than two.
    interval: tuple[float, float] | None
    rows: int
    matches: int


class Standings(Record):
    """A round robin's payoff matrix so far, entrants by their value against the field, best first."""

    tournament: str
    # Matches its entrants call for, and how many of them an eval run holds.
    scheduled: int
    held: int
    entries: tuple[Standing, ...]

    @property
    def complete(self) -> bool:
        return self.held == self.scheduled


def standings(
    db: duckdb.DuckDBPyConnection,
    round_robin: str,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
) -> Standings:
    """The round robin's entrants on `table`'s `value`, as `etude.eval.query.compare` measures it.

    Only the tournament's scheduled matches count, each once, from the eval
    run that recorded it last.
    """
    found = db.execute(
        "select format from tournaments where tournament = ?", [round_robin]
    ).fetchone()
    if found is not None and found[0] != "round_robin":
        raise EtudeError("standings_rank_round_robins", 422)
    with _held(db, round_robin) as (scheduled, held):
        compared = compare(
            db,
            table=table,
            value=value,
            per=per,
            where="run || '/' || match in (select run || '/' || match from _held)"
            + (f" and ({where})" if where else ""),
        )
    names: dict[str, str] = dict(db.sql("select agent, name from agents").fetchall())
    # Seats only: a group's row has a team for its subject.
    entries = [
        Standing(
            agent=c.subject[0],
            name=names[c.subject[0]],
            opponent=None if c.opponent is None else c.opponent[0],
            value=c.value,
            interval=c.interval,
            rows=c.rows,
            matches=c.matches,
        )
        for c in compared
        if len(c.subject) == 1 and (c.opponent is None or len(c.opponent) == 1)
    ]
    field = {e.agent: e.value for e in entries if e.opponent is None}

    def rank(agent: Digest | None) -> tuple[float, str]:
        # Against the field first, then opponents best first.
        if agent is None:
            return float("-inf"), ""
        return -field.get(agent, float("-inf")), names[agent]

    return Standings(
        tournament=round_robin,
        scheduled=scheduled,
        held=held,
        entries=tuple(sorted(entries, key=lambda e: (rank(e.agent), rank(e.opponent)))),
    )
