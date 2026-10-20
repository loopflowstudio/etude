"""Tournaments: a stored structure that arranges eval runs to produce a set of matches.

`benchmark` and `round_robin` describe a tournament; `hold` stores it and the
agents it adds, then plays, as one eval run (`etude.eval.match.evaluate`), the
matches those agents add; `standings` reads the result from a database
`etude.eval.query.connect` opened.

A tournament lives under `tournaments/NAME/` beside the runs, in the local
runs directory and every storage, as files written once and never replaced
(`etude.eval.records`), so many small eval runs may grow it at once. Its
schedule is derived from its agents: a benchmark pairs every entrant with
every benchmark, a round robin every two entrants once, each pairing one match
per condition, entrant first. Adding an agent only adds matches. A match held
by two of its eval runs counts once, from the later.

An on-the-go hold enters one new checkpoint and reads only the tournament's
listings, whatever its size: the matches it adds can be held nowhere yet. A
hold that adds nothing, or adds agents already stored, skips what any of the
tournament's eval runs delivered, by listing them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from etude.eval.match import TOURNAMENTS, EvalRun, Tables, as_config, evaluate
from etude.eval.query import compare
from etude.eval.records import (
    EVERYONE,
    Agent,
    Environment,
    Match,
    Tournament,
    TournamentRun,
    digest,
    match_key,
)
from etude.run.errors import EtudeError
from etude.run.records import Digest, Record
from etude.run.storage import SAFE_NAME, DirectoryStorage, Storage, default_root, run_prefix

if TYPE_CHECKING:
    import duckdb

type Role = Literal["entrant", "benchmark"]
_FOLDERS: dict[Role, str] = {"entrant": "entrants", "benchmark": "benchmarks"}


def benchmark(
    name: str,
    *,
    environment: Environment,
    conditions: Sequence[Mapping[str, object]],
    games: int,
    benchmarks: Sequence[Agent] = (),
) -> Tournament:
    """Every entrant against every benchmark, such as each checkpoint of a run against fixed opponents."""
    return _described("benchmark", name, environment, conditions, games, benchmarks)


def round_robin(
    name: str,
    *,
    environment: Environment,
    conditions: Sequence[Mapping[str, object]],
    games: int,
) -> Tournament:
    """Every entrant against every other once; seat swaps belong inside the author's match."""
    return _described("round_robin", name, environment, conditions, games, ())


def _described(
    format: Literal["benchmark", "round_robin"],
    name: str,
    environment: Environment,
    conditions: Sequence[Mapping[str, object]],
    games: int,
    benchmarks: Sequence[Agent],
) -> Tournament:
    if not SAFE_NAME.match(name):
        raise EtudeError("unsafe_tournament_name", 422)
    return Tournament(
        format=format,
        name=name,
        environment=environment,
        conditions=tuple(as_config(given) for given in conditions),
        games=games,
        benchmarks=tuple(benchmarks),
    )


def hold(
    tournament: Tournament,
    *,
    play: Callable[[Match], Tables],
    enter: Sequence[Agent] = (),
    add_benchmarks: Sequence[Agent] = (),
    workers: int = 1,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    share: tuple[int, int] = (0, 1),
) -> EvalRun:
    """Store the tournament and the agents given, then play the matches they add as one eval run.

    With no agents to add, plays every scheduled match no eval run of the
    tournament delivered. `share=(k, n)` plays only the matches whose key is k
    modulo n, so n machines may split one hold. The eval run is named for the
    tournament, the agents given and the share, so calling `hold` again with
    the same arguments resumes it. Raises `EtudeError("tournament_conflict")`
    when a stored tournament of this name differs, and whatever `evaluate`
    raises.
    """
    k, n = share
    if not 0 <= k < n:
        raise EtudeError("share_out_of_range", 422)
    if tournament.format == "round_robin" and add_benchmarks:
        raise EtudeError("round_robin_has_no_benchmarks", 422)
    base = Path(directory) if directory is not None else default_root()
    stored = _Stored(tournament.name, [DirectoryStorage(base), *storages])
    stored.settle(tournament)
    listed = {role: stored.listed(role) for role in _FOLDERS}
    entering = {each.identity: each for each in enter}
    benching = {each.identity: each for each in add_benchmarks}
    benching |= {
        each.identity: each
        for each in tournament.benchmarks
        if each.identity not in listed["benchmark"]
    }
    added = bool(entering or benching)
    # Nothing it adds is stored yet, so no eval run can hold a match it adds.
    fresh = added and not (
        entering.keys() & listed["entrant"] or benching.keys() & listed["benchmark"]
    )
    known = {each.identity: each for each in tournament.benchmarks} | entering | benching
    benchmarks = (
        (stored.read("benchmark", listed["benchmark"], known) | benching)
        if tournament.format == "benchmark"
        else {}
    )
    needs_entrants = tournament.format == "round_robin" or bool(benching) or not added
    entrants = (
        stored.read("entrant", listed["entrant"], known) if needs_entrants else {}
    ) | entering
    pairs: set[tuple[Digest, Digest]]
    if tournament.format == "benchmark":
        pairs = {(e, b) for e in (entrants if not added else entering) for b in benchmarks}
        pairs |= {(e, b) for e in entrants for b in benching}
    else:
        pairs = {
            (a, b)
            for a, b in combinations(sorted(entrants), 2)
            if not added or {a, b} & entering.keys()
        }
    agents = entrants | benchmarks
    matches = [
        Match(agents=(agents[a], agents[b]), conditions=conditions, games=tournament.games)
        for a, b in sorted(pairs)
        for conditions in tournament.conditions
    ]
    matches = [each for each in matches if int(each.key, 16) % n == k]
    if not fresh:
        delivered = stored.delivered()
        matches = [each for each in matches if each.key not in delivered]
    adding: tuple[tuple[Role, dict[Digest, Agent]], ...] = (
        ("entrant", entering),
        ("benchmark", benching),
    )
    for role, agents_added in adding:
        for identity, each in agents_added.items():
            stored.write(f"{_FOLDERS[role]}/{identity}.json", each.model_dump_json().encode())
    # Named for the arguments, so the same hold resumes the same eval run and
    # rewrites nothing.
    named = sorted(entering), sorted(each.identity for each in add_benchmarks)
    run = f"{tournament.name}.{digest([*named, [k, n]])[:12]}"
    held = TournamentRun(run=run, entrants=tuple(named[0]), benchmarks=tuple(named[1]), share=share)
    stored.write(f"runs/{run}.json", held.model_dump_json().encode())
    return evaluate(
        experiment=tournament.name,
        environment=tournament.environment,
        matches=matches,
        play=play,
        workers=workers,
        run=run,
        directory=directory,
        storages=storages,
    )


class _Stored:
    """One tournament's folder in each place it is kept: the runs directory and every storage."""

    def __init__(self, name: str, places: Sequence[Storage]) -> None:
        self.prefix = f"{TOURNAMENTS}/{name}/"
        self.places = list({place.name: place for place in places}.values())
        self.present: dict[str, set[str]] = {place.name: set() for place in self.places}

    def settle(self, tournament: Tournament) -> None:
        """Store `tournament.json` where it is missing; refuse where it differs."""
        fixed = tournament.model_copy(update={"benchmarks": ()})
        for place in self.places:
            try:
                found = Tournament.model_validate_json(place.get(self.prefix + "tournament.json"))
            except FileNotFoundError:
                place.put(self.prefix + "tournament.json", fixed.model_dump_json().encode())
                continue
            if found != fixed:
                raise EtudeError("tournament_conflict", 409)

    def listed(self, role: Role) -> set[Digest]:
        """The identities stored for a role in any place, remembering which place holds which."""
        found: set[Digest] = set()
        folder = self.prefix + _FOLDERS[role] + "/"
        for place in self.places:
            for key in place.keys(folder):
                self.present[place.name].add(key[len(self.prefix) :])
                found.add(key[len(folder) :].removesuffix(".json"))
        return found

    def read(
        self, role: Role, identities: set[Digest], known: Mapping[Digest, Agent]
    ) -> dict[Digest, Agent]:
        """Each listed agent, from what the caller holds or from the first place holding its file."""
        found: dict[Digest, Agent] = {}
        for identity in identities:
            if identity in known:
                found[identity] = known[identity]
                continue
            key = f"{_FOLDERS[role]}/{identity}.json"
            place = next(p for p in self.places if key in self.present[p.name])
            found[identity] = Agent.model_validate_json(place.get(self.prefix + key))
        return found

    def write(self, key: str, body: bytes) -> None:
        """Store a file in every place not already holding it; tournament files are never replaced."""
        for place in self.places:
            if key not in self.present[place.name]:
                place.put(self.prefix + key, body)
                self.present[place.name].add(key)

    def delivered(self) -> set[Digest]:
        """The keys of every match an eval run of the tournament holds as done, from listings alone."""
        found: set[Digest] = set()
        for place in self.places:
            for key in place.keys(self.prefix + "runs/"):
                run = key.rsplit("/", 1)[-1].removesuffix(".json")
                self.present[place.name].add(key[len(self.prefix) :])
                for held in place.keys(run_prefix(run) + "matches/"):
                    name = held.rsplit("/", 1)[-1]
                    if name.endswith(".json") and not name.endswith(".failed.json"):
                        found.add(name.removesuffix(".json"))
        return found


class TrainingClocks(Record):
    """Where a checkpoint entrant's training run stood when it saved the checkpoint."""

    run: str
    ordinal: int
    at: dict[str, float]


class Standing(Record):
    """An agent's value against one opponent in a tournament, or against the field (`opponent` None)."""

    agent: Digest
    name: str
    opponent: Digest | None
    value: float
    # A 95% interval resampling whole matches; None with fewer than two.
    interval: tuple[float, float] | None
    rows: int
    matches: int
    checkpoint: TrainingClocks | None = None


class Standings(Record):
    """A tournament's result so far.

    A benchmark's entries are each entrant against each benchmark and against
    all of them, in training order where entrants are checkpoints. A round
    robin's are the payoff matrix, entrants ordered by their value against
    the field, best first.
    """

    tournament: str
    format: Literal["benchmark", "round_robin"]
    # Matches its agents call for, and how many of them an eval run holds.
    scheduled: int
    held: int
    entries: tuple[Standing, ...]

    @property
    def complete(self) -> bool:
        return self.held == self.scheduled


def standings(
    db: duckdb.DuckDBPyConnection,
    tournament: str,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
) -> Standings:
    """The tournament's entrants on `table`'s `value`, as `etude.eval.query.compare` measures it.

    Only the tournament's scheduled matches count, each once, from the eval
    run that recorded it last.
    """
    found = db.execute(
        "select format, conditions, games from tournaments where tournament = ?", [tournament]
    ).fetchone()
    if found is None:
        raise EtudeError("tournament_not_found", 404)
    format, conditions, games = found
    roles: dict[str, list[Digest]] = {"entrant": [], "benchmark": []}
    for agent, role in db.execute(
        "select agent, role from entrants where tournament = ? order by agent", [tournament]
    ).fetchall():
        roles[role].append(agent)
    entrants = roles["entrant"]
    pairs = (
        [(e, b) for e in entrants for b in roles["benchmark"]]
        if format == "benchmark"
        else list(combinations(sorted(entrants), 2))
    )
    scheduled = {
        match_key(pair, {EVERYONE: (0, 1)}, given, games)
        for pair in pairs
        for given in json.loads(conditions)
    }
    latest = db.execute(
        "select m.match, arg_max(m.run, m.wall_ms) from matches m join runs r using (run)"
        " where r.tournament = ? and m.state = 'done' group by m.match",
        [tournament],
    ).fetchall()
    chosen = [(run, key) for key, run in latest if key in scheduled]
    db.execute("create or replace temp table _standing_matches (run VARCHAR, match VARCHAR)")
    try:
        db.executemany("insert into _standing_matches values (?, ?)", chosen)
        compared = compare(
            db,
            table=table,
            value=value,
            per=per,
            where="run || '/' || match in (select run || '/' || match from _standing_matches)"
            + (f" and ({where})" if where else ""),
        )
    finally:
        db.execute("drop table _standing_matches")
    names: dict[str, str] = dict(db.sql("select agent, name from agents").fetchall())
    clocks = _training_clocks(db, entrants)
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
            checkpoint=clocks.get(c.subject[0]),
        )
        for c in compared
        if len(c.subject) == 1
        and c.subject[0] in entrants
        and (c.opponent is None or len(c.opponent) == 1)
    ]
    # Rank: training order for a benchmark, value against the field for a round robin.
    field = {e.agent: e.value for e in entries if e.opponent is None}
    rank: dict[Digest | None, tuple[object, ...]] = {None: ()}
    for each in [*entrants, *roles["benchmark"]]:
        at = clocks.get(each)
        rank[each] = (
            (at is None, at.run if at else "", at.ordinal if at else 0, names.get(each, ""))
            if format == "benchmark"
            else (-field.get(each, 0.0), names.get(each, ""))
        )
    return Standings(
        tournament=tournament,
        format=format,
        scheduled=len(scheduled),
        held=len(chosen),
        entries=tuple(sorted(entries, key=lambda e: (rank[e.agent], rank[e.opponent]))),
    )


def _training_clocks(
    db: duckdb.DuckDBPyConnection, agents: Sequence[Digest]
) -> dict[Digest, TrainingClocks]:
    """Each checkpoint agent's training run and clocks, from the first run recording its artifact."""
    found: dict[Digest, TrainingClocks] = {}
    for agent, run, ordinal, at in db.execute(
        "select a.agent, t.run, t.ordinal, t.clocks from agents a join training_checkpoints t"
        " on t.artifact = a.artifact where a.kind = 'checkpoint' order by t.run, t.ordinal"
    ).fetchall():
        if agent in agents and agent not in found:
            found[agent] = TrainingClocks(run=run, ordinal=ordinal, at=json.loads(at))
    return found
