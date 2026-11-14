"""Tournaments: structures that arrange eval runs to produce a set of matches.

A `Benchmark` (`etude.eval.records`) is named seatings a training run's
checkpoints are held for; a `RoundRobin` is hand-picked players, every two of
them once.
`evaluating` opens one EvalRun for the matches a tournament adds; its
`play` executes them. `scores` follows each seating over training; `standings`
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
`environment.json` when it was held for no tournament), and the index of
each block of those in the tournament's environment, and plays what the
tournament calls for and no eval run of the experiment in its environment
delivered.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue, ValidationError

from etude.checkpoints import CheckpointRef
from etude.errors import EtudeError
from etude.eval.block import Block, run_blocks
from etude.eval.match import KIND
from etude.eval.query import compare, estimate
from etude.eval.records import (
    IDENTIFIER,
    TOURNAMENT_FILE,
    Benchmark,
    Environment,
    Line,
    Match,
    MatchRecord,
    Player,
    Rollup,
    RoundRobin,
    Seated,
    Share,
    TournamentRun,
    assemble,
    digest,
    folder_name,
    outcomes,
)
from etude.eval.run import ETUDE_TABLES, EvalResult, EvalRun, Result, Tables, _evaluating_matches
from etude.eval.schedule import ordered
from etude.records import Digest, Record, RunRecord
from etude.run import declare, run_folders, under
from etude.storage import DirectoryStorage, Storage, default_root

if TYPE_CHECKING:
    import duckdb

# Every eval run's `etude.eval.records.Environment`.
_ENVIRONMENT_FILE = "environment.json"


@contextmanager
def evaluating(
    tournament: Benchmark | RoundRobin | Environment,
    *,
    experiment: str | None = None,
    checkpoint: CheckpointRef | None = None,
    checkpoints: Sequence[CheckpointRef] = (),
    enter: Sequence[Player] = (),
    matches: Sequence[Match] | None = None,
    workers: int = 1,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    share: Share = Share(),
    resume: bool = False,
    run: str | None = None,
    lines: Sequence[Line] = (),
) -> Iterator[EvalRun]:
    """Open one tournament's eval run, or an environment's explicit matches.

    A checkpoint fills benchmark inference seats; entrants fill a round robin.
    With neither, plan every missing match of the stored tournament. Relative
    seats use the supplied experiment lines; planned jobs carry these in their
    specification. Explicit matches are checked against the tournament before
    opening. Share partitions its ordered schedule into disjoint parts.
    Outside a launched attempt, resume must be explicit. Environment, rollups
    and tournament definitions must agree with the run already stored.
    """
    experiment = experiment or os.environ.get("ETUDE_EXPERIMENT")
    if experiment is None:
        raise EtudeError("experiment_not_named", detail="give experiment= outside a launch")
    resume = resume or bool(os.environ.get("ETUDE_ATTEMPT"))
    if isinstance(tournament, Environment):
        if matches is None:
            raise EtudeError("evaluation_needs_matches", 422)
        with _evaluating_matches(
            experiment=experiment,
            environment=tournament,
            matches=matches,
            workers=workers,
            run=run,
            directory=directory,
            storages=storages,
            resume=resume,
        ) as active:
            yield active
        return
    if checkpoint is not None and checkpoints:
        raise EtudeError("hold_one_checkpoint_on_the_go_or_add_checkpoints", 422)
    given = (checkpoint,) if checkpoint is not None else tuple(checkpoints)
    added = _added(tournament, given, enter, share)
    if matches is not None:
        resolved, _ = assemble([added]).resolve(lines)
        added = added.model_copy(update={"seated": resolved.seated})
        called = {each.match.key for each in resolved.schedule()}
        if not all(each.key in called for each in matches):
            raise EtudeError("match_not_scheduled", 422)
        pending = list(matches)
    else:
        on_the_go = checkpoint is not None or bool(enter)
        # On the go, nothing is read: a seating of fixed players is left to batch.
        runs = (
            None if on_the_go else stored_runs(experiment, directory=directory, storages=storages)
        )
        asked = _called(added, experiment=experiment, on_the_go=on_the_go, lines=lines, runs=runs)
        added, so_far = asked.recorded, asked.played
        every = [*(each.match for each in so_far.delivered), *so_far.failed, *so_far.new]
        # Contiguous runs of the play order, so each share seats fewer checkpoints.
        order = list(ordered(sorted(every, key=lambda match: match.key)))
        size, larger = divmod(len(order), share.count)
        start = share.index * size + min(share.index, larger)
        delivered = {each.match.key for each in so_far.delivered}
        pending = [
            match
            for match in order[start : start + size + (share.index < larger)]
            if match.key not in delivered
        ]
    run = (
        run
        or os.environ.get("ETUDE_RUN")
        or (f"{folder_name(tournament.name)}.{digest(added.model_dump(mode='json'))[:12]}")
    )
    _record(added, experiment, run, directory, resume=resume)
    with _evaluating_matches(
        experiment=experiment,
        environment=tournament.environment,
        matches=pending,
        workers=workers,
        run=run,
        directory=directory,
        storages=storages,
        rollups=tournament.rollups,
        resume=True,
    ) as active:
        yield active


def hold[C: BaseModel | Mapping[str, JsonValue]](
    tournament: Benchmark | RoundRobin,
    *,
    experiment: str,
    play: Callable[[Match[C]], Tables | Result],
    checkpoint: CheckpointRef | None = None,
    checkpoints: Sequence[CheckpointRef] = (),
    enter: Sequence[Player] = (),
    matches: Sequence[Match] | None = None,
    workers: int = 1,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    share: Share = Share(),
    rollups: Sequence[Rollup] = (),
    lines: Sequence[Line] = (),
) -> EvalResult:
    """Deprecated one-call spelling; use evaluating and run.play, removed in 0.5."""
    warnings.warn(
        "hold is now evaluating(...), then run.play(play); removed in 0.5",
        DeprecationWarning,
        stacklevel=2,
    )
    if rollups:
        tournament = tournament.model_copy(update={"rollups": tuple(rollups)})
    with evaluating(
        tournament,
        experiment=experiment,
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        enter=enter,
        matches=matches,
        workers=workers,
        directory=directory,
        storages=storages,
        share=share,
        lines=lines,
        resume=True,
    ) as active:
        return active.play(play)


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
    enter: Sequence[Player] = (),
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    lines: Sequence[Line] = (),
    runs: StoredRuns | None = None,
) -> Played:
    """How far exactly this definition has been played, without playing anything.

    Only what is given is wanted: a seating an earlier eval run defined and
    this definition leaves out is not, nor a checkpoint not given. What any
    eval run of the experiment in its environment delivered counts, whichever
    tournament it was held for; of several, the one that recorded it last.
    So one place can decide what is new and another, such as a launched
    machine that reads no other eval run, play it with `hold(matches=...)`,
    and a reader counts each match from the run whose rows are latest.
    Raises as a batch hold does, so a seating name defined two ways is refused.
    Relative seats find their checkpoints among `lines`, as in `hold`.

    Asking about several tournaments, pass `runs` (`stored_runs`), read once,
    instead of `directory` and `storages`, which are read on every call.
    """
    added = _added(tournament, tuple(checkpoints), enter, Share())
    if runs is None:
        runs = stored_runs(experiment, directory=directory, storages=storages)
    elif runs.experiment != experiment:
        raise EtudeError("stored_runs_of_another_experiment", 422)
    return _called(
        added, experiment=experiment, on_the_go=False, lines=lines, wanted_only=True, runs=runs
    ).played


def tournament_runs(
    experiment: str, *, directory: Path | str | None = None, storages: Sequence[Storage] = ()
) -> dict[str, list[TournamentRun]]:
    """What each eval run of the experiment held for a tournament added, by tournament, in
    run order: `assemble` makes each tournament of them.

    One listing per place, each eval run's `run.json` and `tournament.json`.
    """
    return stored_runs(experiment, directory=directory, storages=storages).tournaments()


@dataclass(frozen=True)
class _Folder:
    """One folder of one of the experiment's eval runs, with what it was held for."""

    place: Storage
    run: str
    folder: str
    # Its file names, relative to the folder.
    names: tuple[str, ...]
    # Its `tournament.json`; else None, and `environment` is its `environment.json`'s.
    held: TournamentRun | None
    environment: str | None


class StoredRuns:
    """The experiment's eval runs as stored, read once for several tournaments.

    Each place is listed once and each eval run's `run.json`, and its
    `tournament.json` or else `environment.json`, read once; a run's blocks
    are read the first time a tournament in its environment asks for them.
    """

    def __init__(self, experiment: str, folders: Sequence[_Folder]) -> None:
        self.experiment = experiment
        self._folders = tuple(folders)
        self._records: dict[tuple[str, str], list[MatchRecord]] = {}
        self._blocks: dict[str, dict[str, Block]] = {}

    def tournaments(self) -> dict[str, list[TournamentRun]]:
        """What each eval run held for a tournament added, by tournament, in run order."""
        found: dict[str, dict[str, TournamentRun]] = {}
        for each in self._folders:
            if each.held is not None:
                found.setdefault(each.held.tournament.name, {}).setdefault(each.run, each.held)
        return {name: [runs[run] for run in sorted(runs)] for name, runs in sorted(found.items())}

    def folders(self) -> tuple[_Folder, ...]:
        return self._folders

    def records(self, each: _Folder) -> list[MatchRecord]:
        """The match records of a folder's blocks, read the first time they are asked for."""
        key = (each.place.name, each.folder)
        if key not in self._records:
            self._records[key] = [
                record
                for block in run_blocks(
                    each.place,
                    each.folder,
                    each.run,
                    each.names,
                    self._blocks.setdefault(each.run, {}),
                )
                for record in block.matches
            ]
        return self._records[key]


def stored_runs(
    experiment: str, *, directory: Path | str | None = None, storages: Sequence[Storage] = ()
) -> StoredRuns:
    """The experiment's eval runs in this computer's eval folder and `storages`."""
    base = Path(directory) if directory is not None else default_root()
    folders: list[_Folder] = []
    for place in {each.name: each for each in [DirectoryStorage(base), *storages]}.values():
        keys = sorted(place.keys(""))
        listed = set(keys)
        for run, held_in in sorted(run_folders(keys).items()):
            for folder in held_in:
                held_file = folder + TOURNAMENT_FILE in listed
                if not held_file and folder + _ENVIRONMENT_FILE not in listed:
                    continue
                if _experiment(place, folder) != experiment:
                    continue
                names = tuple(key[len(folder) :] for key in under(keys, folder))
                if held_file:
                    held = TournamentRun.model_validate_json(place.get(folder + TOURNAMENT_FILE))
                    folders.append(_Folder(place, run, folder, names, held, None))
                else:
                    given = Environment.model_validate_json(place.get(folder + _ENVIRONMENT_FILE))
                    folders.append(_Folder(place, run, folder, names, None, given.identity))
    return StoredRuns(experiment, folders)


def _added(
    tournament: Benchmark | RoundRobin,
    checkpoints: tuple[CheckpointRef, ...],
    enter: Sequence[Player],
    share: Share,
) -> TournamentRun:
    if isinstance(tournament, Benchmark) and enter:
        raise EtudeError("benchmark_holds_checkpoints_not_entrants", 422)
    if isinstance(tournament, RoundRobin) and checkpoints:
        raise EtudeError("round_robin_holds_entrants_not_checkpoints", 422)
    return TournamentRun(
        tournament=tournament, checkpoints=checkpoints, entrants=tuple(enter), share=share
    )


@dataclass(frozen=True)
class _Called:
    """What a definition calls for with what is given, and what its hold records."""

    played: Played
    # The definition held, with what its relative seats found among the lines that no
    # stored eval run recorded.
    recorded: TournamentRun


def _called(
    added: TournamentRun,
    *,
    experiment: str,
    on_the_go: bool,
    lines: Sequence[Line],
    runs: StoredRuns | None,
    wanted_only: bool = False,
) -> _Called:
    """Each match the tournament calls for with what `added` adds, by whether an eval run
    of the experiment in its environment delivered it or failed it; and `added` with what
    its relative seats found among `lines`, for the hold to record.

    On the go, nothing is read and a seating of fixed players is left to batch:
    it is not the checkpoint's. `wanted_only` calls for what `added` defines
    alone, still refusing a definition the stored eval runs conflict with.
    """
    stored = _Stored(held=(), delivered={}, failed=set())
    if runs is not None:
        stored = _stored(added.tournament, runs)
    tournament = assemble([*stored.held, added])
    known = {_found(each) for each in tournament.seated}
    if wanted_only:
        tournament = assemble([added]).model_copy(update={"seated": tournament.seated})
    tournament, _ = tournament.resolve(lines)
    found = tuple(each for each in tournament.seated if _found(each) not in known)
    # Two seatings may share a match; it is played once.
    matches = {
        each.match.key: each.match
        for each in tournament.schedule()
        if not on_the_go or each.checkpoint is not None or not added.checkpoints
    }
    ordered_keys = sorted(matches)
    played = Played(
        delivered=tuple(
            Delivered(match=matches[key], run=stored.delivered[key])
            for key in ordered_keys
            if key in stored.delivered
        ),
        failed=tuple(matches[key] for key in ordered_keys if key in stored.failed),
        new=tuple(
            matches[key]
            for key in ordered_keys
            if key not in stored.delivered and key not in stored.failed
        ),
    )
    return _Called(
        played=played, recorded=added.model_copy(update={"seated": (*added.seated, *found)})
    )


def _found(seated: Seated) -> tuple[str, str, tuple[tuple[tuple[str | None, ...], int], ...]]:
    """What a `Seated` plays, by key: equal for one found again at another file."""
    pairings = tuple(
        (tuple(None if ref is None else str(ref.key) for ref in each.seats), each.games)
        for each in seated.pairings
    )
    return seated.seating, str(seated.checkpoint), pairings


def _record(
    added: TournamentRun, experiment: str, run: str, directory: Path | str | None, *, resume: bool
) -> None:
    """Write `tournament.json` in the eval run's folder; a resumed run must have been held alike."""
    declared = declare(
        experiment=experiment, clocks=(), run=run, kind=KIND, directory=directory, resume=resume
    )
    if declared.record.kind != KIND:
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


@dataclass(frozen=True)
class _Stored:
    """What the experiment's stored eval runs hold of a tournament and its environment."""

    # The tournament's eval runs, in run order.
    held: tuple[TournamentRun, ...]
    # Each match an eval run in the environment holds as done, to the run that recorded it
    # last: of runs holding it, the latest record's, then the last by name, as `statsdb`
    # ranks `matches.newest`.
    delivered: dict[Digest, str]
    # Each match that failed there, with no done record anywhere.
    failed: set[Digest]


def _stored(tournament: Benchmark | RoundRobin, runs: StoredRuns) -> _Stored:
    """The tournament's eval runs of the experiment, and how the experiment's eval runs in
    its environment played each match; each run's blocks read once however many
    tournaments ask (`StoredRuns`)."""
    held: dict[str, TournamentRun] = {}
    records: dict[str, list[MatchRecord]] = {}
    environment = tournament.environment.identity
    for each in runs.folders():
        if each.held is not None:
            if each.held.tournament.name == tournament.name:
                held.setdefault(each.run, each.held)
            # A tournament's eval run is played in the tournament's environment.
            alike = each.held.tournament.environment.identity == environment
        else:
            alike = each.environment == environment
        if alike:
            records.setdefault(each.run, []).extend(runs.records(each))
    newest: dict[Digest, tuple[int, str]] = {}
    failed: set[Digest] = set()
    for run, held_records in records.items():
        for key, record in outcomes(held_records).items():
            if record.state == "failed":
                failed.add(key)
            elif (record.wall_ms, run) > newest.get(key, (-1, "")):
                newest[key] = (record.wall_ms, run)
    return _Stored(
        held=tuple(held[run] for run in sorted(held)),
        delivered={key: run for key, (_, run) in newest.items()},
        failed=failed - newest.keys(),
    )


def _experiment(place: Storage, folder: str) -> str | None:
    """The experiment a run folder's `run.json` names; None before it arrives."""
    try:
        return RunRecord.model_validate_json(place.get(folder + "run.json")).experiment
    except (FileNotFoundError, ValidationError):
        return None


@contextmanager
def _held(db: duckdb.DuckDBPyConnection, tournament: str) -> Iterator[tuple[int, int]]:
    """Temp table `_held` (run, match) of each scheduled match held; yields (scheduled, held).

    Each scheduled match counts once, from whichever eval run of the
    tournament's environment recorded it last (`matches.newest`).
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
        " select m.run, m.match from matches m join runs r using (run)"
        " where r.environment = (select environment from tournaments where tournament = $1)"
        " and m.newest = 1"
        " and m.match in (select match from schedule where tournament = $1)",
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


class ScorePoint(Record):
    """A seating's value for one held checkpoint, and where its training run stood when it was saved."""

    # Its key (`etude.eval.records.CheckpointId`), encoded.
    checkpoint: str
    # What the checkpoint's players are called: the latest name its seats give, the least of
    # several players'. A benchmark's inference seat names it for its file (`Inference.model`).
    name: str
    # None for a checkpoint no training run under the source recorded.
    ordinal: int | None
    # Where each of its training run's clocks stood when it was saved; empty for no run's.
    at: dict[str, float]
    value: float
    # A 95% interval resampling whole matches; None with fewer than two.
    interval: tuple[float, float] | None
    rows: int
    matches: int


class Scores(Record):
    """One seating of a benchmark over one training run's held checkpoints, in training order.

    Checkpoints no training run recorded follow, by name with numbers in order.
    """

    seating: str
    # The training run and its experiment; None for checkpoints no run recorded.
    run: str | None
    experiment: str | None
    points: tuple[ScorePoint, ...]


def scores(
    db: duckdb.DuckDBPyConnection,
    benchmark: str,
    *,
    table: str,
    value: str,
    per: str | None = None,
    where: str | None = None,
) -> list[Scores]:
    """Each seating's first player on `table`'s `value`, per held checkpoint, as `compare`
    measures it.

    Only the first player's (seat 0's) rows of the benchmark's scheduled
    matches count, so a seating of a checkpoint against itself reads alike,
    and a relative seat's checkpoint is no point of its own. A seating whose
    seat found several checkpoints for one held, such as each line of a
    job, counts all of their matches in that point.
    `per` and `where` are as in `etude.eval.query.compare`.
    """
    for name in (table, value, *([per] if per else [])):
        if not IDENTIFIER.match(name):
            raise EtudeError("column_name_refused", 422) from ValueError(name)
    if table in ETUDE_TABLES:
        raise EtudeError("scores_need_an_author_table", 422)
    with _held(db, benchmark):
        found = db.execute(
            f"""
            with r as (
                select run, match, cast("{value}" as DOUBLE) as _value,
                    cast({f'"{per}"' if per else 1} as DOUBLE) as _per
                from "{table}"
                where seat = 0 and "{value}" is not null and ({where or "true"})
            ),
            named as (
                select checkpoint, min(name) as name from players where checkpoint is not null
                group by checkpoint
            )
            select s.seating, t.run, t.experiment, s.checkpoint, n.name, t.ordinal, t."at",
                r.match, sum(r._value), sum(r._per), count(*)
            from r join _held using (run, match)
            join schedule s on s.tournament = ? and s.match = r.match
            join named n on n.checkpoint = s.checkpoint
            left join training_checkpoints t on t.checkpoint = s.checkpoint
            group by s.seating, t.run, t.experiment, s.checkpoint, n.name, t.ordinal, t."at",
                r.match
            """,
            [benchmark],
        ).fetchall()
    # Per point, where its checkpoint stood: each match's totals and rows, its resampling unit.
    units: dict[_Point, dict[str, tuple[float, float, int]]] = {}
    for seating, run, experiment, checkpoint, name, ordinal, at, match, *sums in found:
        point = _Point(seating, run, experiment, checkpoint, name, ordinal, at)
        total, per_total, rows = sums
        units.setdefault(point, {})[match] = (float(total), float(per_total), int(rows))
    curves: dict[tuple[str, str | None, str | None], list[ScorePoint]] = {}
    for point, totals in units.items():
        [estimated] = estimate(
            list(totals),
            [[total] for total, _, _ in totals.values()],
            [[opportunities] for _, opportunities, _ in totals.values()],
        )
        if estimated is None:
            continue
        curves.setdefault((point.seating, point.run, point.experiment), []).append(
            ScorePoint(
                checkpoint=point.checkpoint,
                name=point.name,
                ordinal=point.ordinal,
                at=json.loads(point.at) if point.at else {},
                value=estimated.value,
                interval=estimated.interval,
                rows=sum(rows for *_, rows in totals.values()),
                matches=len(totals),
            )
        )
    return [
        Scores(
            seating=seating,
            run=run,
            experiment=experiment,
            points=tuple(sorted(points, key=_training_order)),
        )
        for (seating, run, experiment), points in sorted(
            curves.items(), key=lambda item: tuple("" if v is None else v for v in item[0])
        )
    ]


@dataclass(frozen=True)
class _Point:
    """A score point's identity: its seating and checkpoint, and where that checkpoint stood."""

    seating: str
    run: str | None
    experiment: str | None
    checkpoint: str
    name: str
    ordinal: int | None
    # The training checkpoint's `at` as stored JSON, decoded once per point.
    at: str | None


def _training_order(point: ScorePoint) -> tuple[bool, int, list[str | int], str]:
    # Digit runs compare as numbers, so `updates-900` comes before `updates-1000`.
    named = [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", point.name)]
    return point.ordinal is None, point.ordinal or 0, named, point.checkpoint


class Standing(Record):
    """An entrant's value against one opponent in a round robin, or against the field (`opponent` None)."""

    # Its key, encoded.
    player: str
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
            where="(run, match) in (select run, match from _held)"
            + (f" and ({where})" if where else ""),
        )
    # Every name a player was seated under, the latest first.
    names: dict[str, str] = {
        player: "/".join(json.loads(named))
        for player, named in db.sql("select player, names from players").fetchall()
    }
    # Seats only: a group's row has a team for its subject.
    entries = [
        Standing(
            player=c.subject[0],
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
    field = {e.player: e.value for e in entries if e.opponent is None}

    def rank(player: str | None) -> tuple[float, str]:
        # Against the field first, then opponents best first.
        if player is None:
            return float("-inf"), ""
        return -field.get(player, float("-inf")), names[player]

    return Standings(
        tournament=round_robin,
        scheduled=scheduled,
        held=held,
        entries=tuple(sorted(entries, key=lambda e: (rank(e.player), rank(e.opponent)))),
    )
