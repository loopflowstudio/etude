"""Tournaments: stored structures that arrange eval runs to produce a set of matches.

A `Benchmark` (`etude.eval.records`) is named matchups a model's checkpoints
are held for; a `RoundRobin` is hand-picked agents, every two of them once.
`hold` stores the tournament and what it adds, then plays, as one eval run
(`etude.eval.match.evaluate`), the matches added. `curves` follows each
matchup over training; `standings` ranks a round robin. Both read a database
`etude.eval.query.connect` opened.

A tournament lives under `tournaments/NAME/` beside the runs, in the local
runs directory and every storage, as files written once and never replaced,
so many small eval runs may grow it at once. Its schedule is derived from
those files: each matchup for each checkpoint held, or each pair of entrants.
Adding a matchup, a checkpoint or an entrant only adds matches. A match held
by two of the tournament's eval runs counts once, from the later.

A hold reads one listing of the tournament's folder per place. Holding a new
checkpoint, or new entrants, also reads nothing else of the tournament: the
matches it adds can be held nowhere yet. Any other hold skips what the
tournament's eval runs delivered, by listing them.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from etude.eval.match import ETUDE_TABLES, IDENTIFIER, TOURNAMENTS, EvalRun, Tables, evaluate
from etude.eval.query import compare, estimate
from etude.eval.records import (
    Agent,
    Benchmark,
    Match,
    Matchup,
    RoundRobin,
    TournamentRun,
    digest,
)
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Digest, Record
from etude.run.storage import DirectoryStorage, Storage, default_root, run_prefix

if TYPE_CHECKING:
    import duckdb


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
    """Store the tournament and what is given, then play the matches added as one eval run.

    A benchmark held with `checkpoint` (on the go) plays each of the given
    matchups with the checkpoint in the model's seats. Held without one
    (batch), it plays every matchup ever defined for every checkpoint held,
    skipping what was delivered, so a matchup added later is played for
    every checkpoint so far. A round robin enters the agents given and plays
    their pairs; with none, every pair not yet delivered.

    `share=(k, n)` plays only the matches whose key is k modulo n, so n
    machines may split one hold. The eval run is named for the tournament,
    what is given and the share, so the same call resumes it. Raises
    `EtudeError("tournament_conflict")` when the stored tournament has another
    format or environment, `EtudeError("matchup_conflict")` when a matchup's
    name is stored with another definition, and whatever `evaluate` raises.
    """
    k, n = share
    if not 0 <= k < n:
        raise EtudeError("share_out_of_range", 422)
    base = Path(directory) if directory is not None else default_root()
    stored = _Stored(tournament.name, [DirectoryStorage(base), *storages])
    if isinstance(tournament, Benchmark):
        if enter:
            raise EtudeError("benchmark_holds_checkpoints_not_entrants", 422)
        stored.settle("benchmark.json", tournament.model_copy(update={"matchups": ()}))
        matches, fresh = _benchmark_matches(stored, tournament, checkpoint)
    else:
        if checkpoint is not None:
            raise EtudeError("round_robin_holds_entrants_not_checkpoints", 422)
        stored.settle("round_robin.json", tournament)
        matches, fresh = _round_robin_matches(stored, tournament, enter)
    # Two matchups may share a match; it is played once.
    pending = [each for key, each in sorted(matches.items()) if int(key, 16) % n == k]
    if not fresh:
        delivered = stored.delivered()
        pending = [each for each in pending if each.key not in delivered]
    model = checkpoint.sha256 if checkpoint is not None else None
    entrants = tuple(sorted({each.identity for each in enter}))
    # Named for the arguments, so the same hold resumes the same eval run and
    # rewrites nothing.
    run = f"{tournament.name}.{digest([model, list(entrants), [k, n]])[:12]}"
    added = TournamentRun(run=run, checkpoint=model, entrants=entrants, share=share)
    stored.write(f"runs/{run}.json", added.model_dump_json())
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


def _benchmark_matches(
    stored: _Stored, benchmark: Benchmark, checkpoint: ArtifactRef | None
) -> tuple[dict[Digest, Match], bool]:
    """The matches a hold adds, by key, and whether none of them can be delivered yet."""
    given = {each.file: each for each in benchmark.matchups}
    # A file name's first part is the matchup name's digest. Two holds may also
    # define one name at once, so stored files are checked against each other too.
    names = Counter(file.split(".")[0] for file in stored.listed("matchups/") | given.keys())
    if clashing := {name for name, files in names.items() if files > 1}:
        redefined = [each.name for file, each in given.items() if file.split(".")[0] in clashing]
        raise EtudeError("matchup_conflict", 409) from ValueError(redefined or sorted(clashing))
    for file, each in given.items():
        stored.write(file, each.model_dump_json())
    models = stored.listed("models/")
    if checkpoint is not None:
        if checkpoint.sha256 is None:
            raise EtudeError("checkpoint_agent_requires_digest", 422)
        model = f"models/{checkpoint.sha256}.json"
        fresh = model not in models
        stored.write(model, checkpoint.model_dump_json())
        return {m.key: m for each in given.values() for m in each.matches(checkpoint)}, fresh
    matchups = [
        given.get(file) or Matchup.model_validate_json(stored.read(file))
        for file in sorted(stored.listed("matchups/"))
    ]
    held = [ArtifactRef.model_validate_json(stored.read(file)) for file in sorted(models)]
    return {m.key: m for each in matchups for ref in held for m in each.matches(ref)}, False


def _round_robin_matches(
    stored: _Stored, round_robin: RoundRobin, enter: Sequence[Agent]
) -> tuple[dict[Digest, Match], bool]:
    """The matches a hold adds, by key, and whether none of them can be delivered yet."""
    entering = {each.identity: each for each in enter}
    listed = stored.listed("entrants/")
    files = {f"entrants/{identity}.json": each for identity, each in entering.items()}
    fresh = bool(entering) and not files.keys() & listed
    entrants = [
        files.get(file) or Agent.model_validate_json(stored.read(file)) for file in sorted(listed)
    ]
    for file, each in files.items():
        stored.write(file, each.model_dump_json())
    matches = round_robin.matches([*entrants, *entering.values()])
    return {
        m.key: m
        for m in matches
        if not entering or {seated.identity for seated in m.agents} & entering.keys()
    }, fresh


class _Stored:
    """One tournament's folder in each place it is kept: the runs directory and every storage."""

    def __init__(self, name: str, places: Sequence[Storage]) -> None:
        self.prefix = f"{TOURNAMENTS}/{name}/"
        self.places = list({place.name: place for place in places}.values())
        # Each place's files, relative to the folder, from one listing.
        self.present = {
            place.name: {key[len(self.prefix) :] for key in place.keys(self.prefix)}
            for place in self.places
        }

    def settle(self, file: str, tournament: Benchmark | RoundRobin) -> None:
        """Store the tournament's own file where it is missing; refuse where it differs."""
        other = "round_robin.json" if file == "benchmark.json" else "benchmark.json"
        if any(other in present for present in self.present.values()):
            raise EtudeError("tournament_conflict", 409)
        for place in self.places:
            if (
                file in self.present[place.name]
                and type(tournament).model_validate_json(place.get(self.prefix + file))
                != tournament
            ):
                raise EtudeError("tournament_conflict", 409)
        self.write(file, tournament.model_dump_json())

    def listed(self, folder: str) -> set[str]:
        """The files under `folder` in any place."""
        return {
            file for present in self.present.values() for file in present if file.startswith(folder)
        }

    def read(self, file: str) -> bytes:
        place = next(p for p in self.places if file in self.present[p.name])
        return place.get(self.prefix + file)

    def write(self, file: str, body: str) -> None:
        """Store a file in every place not already holding it; tournament files are never replaced."""
        for place in self.places:
            if file not in self.present[place.name]:
                place.put(self.prefix + file, body.encode())
                self.present[place.name].add(file)

    def delivered(self) -> set[Digest]:
        """The keys of every match an eval run of the tournament holds as done, from listings alone."""
        found: set[Digest] = set()
        for place in self.places:
            for file in self.present[place.name]:
                if not file.startswith("runs/"):
                    continue
                run = file.removeprefix("runs/").removesuffix(".json")
                for held in place.keys(run_prefix(run) + "matches/"):
                    name = held.rsplit("/", 1)[-1]
                    if name.endswith(".json") and not name.endswith(".failed.json"):
                        found.add(name.removesuffix(".json"))
        return found


@contextmanager
def _held(db: duckdb.DuckDBPyConnection, tournament: str) -> Iterator[tuple[int, int]]:
    """Temp table `_held` (run, match) of each scheduled match held; yields (scheduled, held).

    Each scheduled match counts once, from the tournament's eval run that recorded it last.
    """
    if (
        db.execute("select 1 from tournaments where tournament = ?", [tournament]).fetchone()
        is None
    ):
        raise EtudeError("tournament_not_found", 404)
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
