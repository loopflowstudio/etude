"""Tournaments: structures that arrange eval runs to produce a set of matches.

A `Benchmark` (`etude.eval.records`) is named matchups a model's checkpoints
are held for; a `RoundRobin` is hand-picked agents, every two of them once.
`hold` plays, as one eval run (`etude.eval.match.evaluate`), the matches a
tournament adds. `curves` follows each matchup over training; `standings`
ranks a round robin. Both read an experiment's database (`etude.eval.query.statsdb`).

A tournament is stored nowhere but in its eval runs: each keeps, in its own
folder, `tournament.json` (`TournamentRun`), the definition it was held with
and the checkpoint or entrants it added. The tournament is every eval run
naming it (`etude.eval.records.assemble`), so a run delivered from a rented
machine like any other run brings its part of the tournament with it, and
many small eval runs may grow one tournament at once.

A tournament only decides which matches to play; its results belong to the
experiment it was held for. A match is the same wherever it is played, so
its results are reused by key within one experiment and environment: a
scheduled match that any of the experiment's eval runs in the tournament's
environment holds counts, once, from the run that recorded it last, whether
that run was held for this tournament, another, or none.

Holding a checkpoint, or entrants, reads nothing of the tournament: it plays
the matches those add. Any other hold lists each place once, reads each eval
run's `run.json` and, for the experiment's, its `tournament.json` (or its
`environment.json` when it was held for no tournament), and plays what the
tournament calls for and no eval run of the experiment in its environment
delivered.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from etude.eval.match import ETUDE_TABLES, IDENTIFIER, KIND, EvalRun, Tables, evaluate
from etude.eval.query import compare, estimate
from etude.eval.records import (
    TOURNAMENT_FILE,
    Agent,
    Benchmark,
    CheckpointRef,
    Environment,
    Match,
    MatchRecord,
    RoundRobin,
    TournamentRun,
    assemble,
    digest,
)
from etude.eval.schedule import ordered
from etude.run.declaration import declare
from etude.run.errors import EtudeError
from etude.run.records import Digest, Record, RunFile
from etude.run.storage import DirectoryStorage, Storage, default_root

if TYPE_CHECKING:
    import duckdb

# Every eval run's `etude.eval.records.Environment`.
_ENVIRONMENT_FILE = "environment.json"


def hold(
    tournament: Benchmark | RoundRobin,
    *,
    experiment: str,
    play: Callable[[Match], Tables],
    checkpoint: CheckpointRef | None = None,
    checkpoints: Sequence[CheckpointRef] = (),
    enter: Sequence[Agent] = (),
    matches: Sequence[Match] | None = None,
    workers: int = 1,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    share: tuple[int, int] = (0, 1),
) -> EvalRun:
    """Play, as one eval run of `experiment`, the matches the tournament adds; the run records what was given.

    The tournament only decides which matches to play: its results are the
    experiment's, beside its training runs, wherever the experiment's
    database (`etude.eval.query.statsdb`) reads them.

    A benchmark held with `checkpoint` (on the go) plays each of the given
    matchups with the checkpoint in the model's seats. Held without one
    (batch), it plays every matchup any eval run defined for every checkpoint
    any held or `checkpoints` adds, and each matchup of fixed agents once,
    skipping what any eval run of its environment delivered, so a matchup
    added later is played for every checkpoint so far. A round robin held
    with `enter` plays every pair of the agents given; with none, every pair
    of every entrant so far not yet delivered.

    `matches` plays exactly those, as `played` found them new elsewhere, and
    reads nothing: each must be one the tournament calls for with what is
    given. Their checkpoints' files may lie elsewhere than the definition's,
    such as copied to this machine; a key never depends on where.

    `share=(k, n)` cuts the scheduled matches, in the order
    `etude.eval.schedule.ordered` plays them, into n contiguous parts and plays
    the kth, so n machines may split one hold with at most one match between
    their shares, each seating (and fetching) only some of the checkpoints.
    The deal covers delivered matches too, so a resumed share keeps its part.
    Under `etude launch` the eval run is the launched run (`ETUDE_RUN`);
    elsewhere it is named for what is given, so the same call resumes it. A
    batch hold raises `EtudeError("tournament_conflict")` when the
    tournament's eval runs or this definition disagree on its format,
    environment or rules, and `EtudeError("matchup_conflict")` when they
    define a matchup name two ways;
    an on-the-go hold leaves those to be found on read. Raises whatever
    `evaluate` raises.
    """
    k, n = share
    if not 0 <= k < n:
        raise EtudeError("share_out_of_range", 422)
    if checkpoint is not None and checkpoints:
        raise EtudeError("hold_one_checkpoint_on_the_go_or_add_checkpoints", 422)
    given = (checkpoint,) if checkpoint is not None else tuple(checkpoints)
    added = _added(tournament, given, enter, share)
    if matches is not None:
        called = {each.match.key for each in assemble([added]).schedule()}
        if not all(each.key in called for each in matches):
            raise EtudeError("match_not_scheduled", 422)
        pending = list(matches)
    else:
        on_the_go = checkpoint is not None or bool(enter)
        scheduled = _scheduled(
            added,
            experiment=experiment,
            on_the_go=on_the_go,
            directory=directory,
            storages=storages,
        )
        # Contiguous runs of the play order, so each share seats fewer checkpoints.
        dealt = list(ordered([match for match, _, _ in scheduled]))
        size, larger = divmod(len(dealt), n)
        start = k * size + min(k, larger)
        delivered = {match.key for match, state, _ in scheduled if state == "delivered"}
        pending = [
            match
            for match in dealt[start : start + size + (k < larger)]
            if match.key not in delivered
        ]
    run = os.environ.get("ETUDE_RUN") or (
        f"{tournament.name}.{digest(added.model_dump(mode='json'))[:12]}"
    )
    _record(added, experiment, run, directory)
    return evaluate(
        experiment=experiment,
        environment=tournament.environment,
        matches=pending,
        play=play,
        workers=workers,
        run=run,
        directory=directory,
        storages=storages,
    )


class Delivered(Record):
    """A match, and the eval run that delivered it last: the one whose rows count."""

    match: Match
    run: str


class Played(Record):
    """Every match a definition calls for with what is given, by its state, in key order."""

    # Delivered by an eval run of the experiment in the tournament's environment.
    delivered: tuple[Delivered, ...]
    # Failed, with nothing delivered since; counted, not replayed.
    failed: tuple[Match, ...]
    # Never attempted.
    new: tuple[Match, ...]


def played(
    tournament: Benchmark | RoundRobin,
    *,
    experiment: str,
    checkpoints: Sequence[CheckpointRef] = (),
    enter: Sequence[Agent] = (),
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
) -> Played:
    """How far exactly this definition has been played, without playing anything.

    Only what is given is wanted: a matchup an earlier eval run defined and
    this definition leaves out is not, nor a checkpoint not given. What any
    eval run of the experiment in its environment delivered counts, whichever
    tournament it was held for; of several, the one that recorded it last.
    So one place can decide what is new and another, such as a launched
    machine that reads no other eval run, play it with `hold(matches=...)`,
    and a reader counts each match from the run whose rows are latest.
    Raises as a batch hold does, so a matchup name defined two ways is refused.
    """
    added = _added(tournament, tuple(checkpoints), enter, (0, 1))
    scheduled = _scheduled(
        added,
        experiment=experiment,
        on_the_go=False,
        wanted_only=True,
        directory=directory,
        storages=storages,
    )
    return Played(
        delivered=tuple(
            Delivered(match=match, run=_latest(match.key, where))
            for match, state, where in scheduled
            if state == "delivered"
        ),
        failed=tuple(match for match, state, _ in scheduled if state == "failed"),
        new=tuple(match for match, state, _ in scheduled if state == "new"),
    )


def tournament_runs(
    experiment: str, *, directory: Path | str | None = None, storages: Sequence[Storage] = ()
) -> dict[str, list[TournamentRun]]:
    """What each eval run of the experiment held for a tournament added, by tournament, in
    run order: `assemble` makes each tournament of them.

    One listing per place, each eval run's `run.json` and `tournament.json`.
    """
    base = Path(directory) if directory is not None else default_root()
    found: dict[str, dict[str, TournamentRun]] = {}
    for place in {each.name: each for each in [DirectoryStorage(base), *storages]}.values():
        keys = place.keys("")
        for folder in sorted(_folders(keys, TOURNAMENT_FILE)):
            if _experiment(place, folder) != experiment:
                continue
            added = TournamentRun.model_validate_json(place.get(folder + TOURNAMENT_FILE))
            run = folder.rstrip("/").rsplit("/", 1)[-1]
            found.setdefault(added.tournament.name, {}).setdefault(run, added)
    return {name: [runs[run] for run in sorted(runs)] for name, runs in sorted(found.items())}


def _latest(key: Digest, where: Sequence[tuple[Storage, str]]) -> str:
    """The eval run that recorded match `key` last, of the folders delivering it.

    A match one run delivered needs no read; of several runs, each one's
    record says when it was written, and the latest wins.
    """
    runs = {folder.rstrip("/").rsplit("/", 1)[-1]: (place, folder) for place, folder in where}
    if len(runs) == 1:
        return next(iter(runs))
    written = {
        run: MatchRecord.model_validate_json(place.get(f"{folder}matches/{key}.json")).wall_ms
        for run, (place, folder) in runs.items()
    }
    return max(written, key=lambda run: (written[run], run))


def _added(
    tournament: Benchmark | RoundRobin,
    checkpoints: tuple[CheckpointRef, ...],
    enter: Sequence[Agent],
    share: tuple[int, int],
) -> TournamentRun:
    if isinstance(tournament, Benchmark) and enter:
        raise EtudeError("benchmark_holds_checkpoints_not_entrants", 422)
    if isinstance(tournament, RoundRobin) and checkpoints:
        raise EtudeError("round_robin_holds_entrants_not_checkpoints", 422)
    return TournamentRun(
        tournament=tournament, checkpoints=checkpoints, entrants=tuple(enter), share=share
    )


def _scheduled(
    added: TournamentRun,
    *,
    experiment: str,
    on_the_go: bool,
    directory: Path | str | None,
    storages: Sequence[Storage],
    wanted_only: bool = False,
) -> list[tuple[Match, Literal["new", "delivered", "failed"], tuple[tuple[Storage, str], ...]]]:
    """Each match the tournament calls for with what `added` adds, in key order, whether
    an eval run of the experiment in its environment delivered it or failed it, and the
    run folders, by place, that delivered it.

    On the go, nothing is read and a matchup of fixed agents is left to batch:
    it is not the checkpoint's. `wanted_only` calls for what `added` defines
    alone, still refusing a definition the stored eval runs conflict with.
    """
    held: list[TournamentRun] = [added]
    delivered: dict[Digest, list[tuple[Storage, str]]] = {}
    failed: set[Digest] = set()
    if not on_the_go:
        base = Path(directory) if directory is not None else default_root()
        stored, delivered, failed = _stored(
            added.tournament, experiment, [DirectoryStorage(base), *storages]
        )
        held = [*stored, added]
    tournament = assemble(held)
    if wanted_only:
        tournament = assemble([added])
    # Two matchups may share a match; it is played once.
    matches = {
        each.match.key: each.match
        for each in tournament.schedule()
        if not on_the_go or each.checkpoint is not None or not added.checkpoints
    }
    return [
        (
            match,
            "delivered" if key in delivered else "failed" if key in failed else "new",
            tuple(delivered.get(key, ())),
        )
        for key, match in sorted(matches.items())
    ]


def _record(added: TournamentRun, experiment: str, run: str, directory: Path | str | None) -> None:
    """Write `tournament.json` in the eval run's folder; a resumed run must have been held alike."""
    declared = declare(experiment=experiment, clocks=(), run=run, kind=KIND, directory=directory)
    if declared.declaration.kind != KIND:
        raise EtudeError("run_is_not_an_eval_run", 409)
    path = declared.root / TOURNAMENT_FILE
    if not path.is_file():
        DirectoryStorage(declared.root).put(TOURNAMENT_FILE, added.model_dump_json().encode())
    elif _resumed(TournamentRun.model_validate_json(path.read_bytes())) != _resumed(added):
        raise EtudeError("eval_run_tournament_conflict", 409)


def _resumed(added: TournamentRun) -> dict[str, object]:
    """What a resumed hold must repeat. A checkpoint is its key: a retry on another
    attempt finds the same file at another path."""
    return added.model_dump(
        mode="json", exclude={"checkpoints": {"__all__": {"artifact": {"uri"}}}}
    )


def _stored(
    tournament: Benchmark | RoundRobin, experiment: str, places: Sequence[Storage]
) -> tuple[list[TournamentRun], dict[Digest, list[tuple[Storage, str]]], set[Digest]]:
    """The tournament's eval runs of the experiment in every place, every match
    that an eval run of the experiment in its environment holds as done, with the
    place and folder of each run holding it, and every one that failed there with
    no done record anywhere.

    One listing per place, each eval run's `run.json`, and one more read of each
    of the experiment's: its `tournament.json`, or its `environment.json` when
    it has none.
    """
    held: dict[str, TournamentRun] = {}
    delivered: dict[Digest, list[tuple[Storage, str]]] = {}
    failed: set[Digest] = set()
    environment = tournament.environment.identity
    for place in {place.name: place for place in places}.values():
        keys = place.keys("")
        ours = {
            folder
            for folder in _folders(keys, TOURNAMENT_FILE) | _folders(keys, _ENVIRONMENT_FILE)
            if _experiment(place, folder) == experiment
        }
        # The experiment's eval run folders: those held for a tournament, and the rest.
        tournaments = _folders(keys, TOURNAMENT_FILE) & ours
        alike = {
            folder
            for folder in (_folders(keys, _ENVIRONMENT_FILE) & ours) - tournaments
            if Environment.model_validate_json(place.get(folder + _ENVIRONMENT_FILE)).identity
            == environment
        }
        for folder in tournaments:
            added = TournamentRun.model_validate_json(place.get(folder + TOURNAMENT_FILE))
            # A tournament's eval run is played in the tournament's environment.
            if added.tournament.environment.identity == environment:
                alike.add(folder)
            if added.tournament.name == tournament.name:
                held.setdefault(folder.rstrip("/").rsplit("/", 1)[-1], added)
        for key in keys:
            folder, _, file = key.rpartition("matches/")
            if folder not in alike or not file.endswith(".json"):
                continue
            if file.endswith(".failed.json"):
                failed.add(file.removesuffix(".failed.json"))
            else:
                delivered.setdefault(file.removesuffix(".json"), []).append((place, folder))
    return [held[run] for run in sorted(held)], delivered, failed - delivered.keys()


def _experiment(place: Storage, folder: str) -> str | None:
    """The experiment a run folder's `run.json` names; None before it arrives."""
    try:
        return RunFile.model_validate_json(place.get(folder + "run.json")).experiment
    except (FileNotFoundError, ValidationError):
        return None


def _folders(keys: Sequence[str], file: str) -> set[str]:
    """The run folders, at any depth of a listing, that hold `file`."""
    return {key.removesuffix(file) for key in keys if key.endswith("/" + file)}


@contextmanager
def _held(db: duckdb.DuckDBPyConnection, tournament: str) -> Iterator[tuple[int, int]]:
    """Temp table `_held` (run, match) of each scheduled match held; yields (scheduled, held).

    Each scheduled match counts once, from whichever eval run of the
    tournament's environment recorded it last.
    Raises the conflict `statsdb` found among the tournament's eval runs, if any.
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
        " where r.environment = (select environment from tournaments where tournament = $1)"
        " and m.state = 'done'"
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

    # Its key (`etude.eval.records.CheckpointKey`), encoded.
    checkpoint: str
    # What the checkpoint is called: the last part of its URI, as a training run names its files.
    name: str
    # None for a checkpoint no training run under the source recorded.
    ordinal: int | None
    clocks: dict[str, float]
    value: float
    # A 95% interval resampling whole matches; None with fewer than two.
    interval: tuple[float, float] | None
    rows: int
    matches: int


class Curve(Record):
    """One matchup of a benchmark over one training run's checkpoints, in training order.

    Checkpoints no training run recorded follow, by name with numbers in order.
    """

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
            named as (
                select checkpoint, min(name) as name from agents where checkpoint is not null
                group by checkpoint
            )
            select s.matchup, t.run, t.experiment, s.checkpoint, n.name, t.ordinal, t.clocks,
                sum(r._value), sum(r._per), count(*)
            from r join _held using (run, match)
            join schedule s on s.tournament = ? and s.match = r.match
            join named n on n.checkpoint = s.checkpoint
            left join training_checkpoints t on t.checkpoint = s.checkpoint
            group by s.matchup, t.run, t.experiment, s.checkpoint, n.name, t.ordinal, t.clocks,
                r.match
            """,
            [benchmark],
        ).fetchall()
    # Per point, where its checkpoint stood: each match's totals and rows.
    blocks: dict[
        tuple[str, str | None, str | None, str, str, int | None, str | None],
        list[tuple[float, float, int]],
    ] = {}
    for *point, total, per_total, rows in found:
        matchup, run, experiment, checkpoint, name, ordinal, clocks = point
        blocks.setdefault((matchup, run, experiment, checkpoint, name, ordinal, clocks), []).append(
            (float(total), float(per_total), int(rows))
        )
    lines: dict[tuple[str, str | None, str | None], list[CurvePoint]] = {}
    for (matchup, run, experiment, checkpoint, name, ordinal, clocks), totals in blocks.items():
        estimated = estimate([(total, opportunities) for total, opportunities, _ in totals])
        if estimated is None:
            continue
        lines.setdefault((matchup, run, experiment), []).append(
            CurvePoint(
                checkpoint=checkpoint,
                name=name,
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
            points=tuple(sorted(points, key=_training_order)),
        )
        for (matchup, run, experiment), points in sorted(
            lines.items(), key=lambda item: tuple("" if v is None else v for v in item[0])
        )
    ]


def _training_order(point: CurvePoint) -> tuple[bool, int, list[str | int], str]:
    # Digit runs compare as numbers, so `updates-900` comes before `updates-1000`.
    named = [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", point.name)]
    return point.ordinal is None, point.ordinal or 0, named, point.checkpoint


class Standing(Record):
    """An entrant's value against one opponent in a round robin, or against the field (`opponent` None)."""

    # Its key, encoded.
    agent: str
    name: str
    opponent: str | None
    opponent_name: str | None
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
            opponent_name=None if c.opponent is None else names[c.opponent[0]],
            value=c.value,
            interval=c.interval,
            rows=c.rows,
            matches=c.matches,
        )
        for c in compared
        if len(c.subject) == 1 and (c.opponent is None or len(c.opponent) == 1)
    ]
    field = {e.agent: e.value for e in entries if e.opponent is None}

    def rank(agent: str | None) -> tuple[float, str]:
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
