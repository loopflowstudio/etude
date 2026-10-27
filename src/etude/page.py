"""An experiment's report page, and an index of every experiment in a folder: built, written or served.

    storage = storage_from_uri("r2://bucket/runs")
    page = build(storage, Path("experiments/ppo.py"))
    write(render(page), target(storage, "ppo"))  # ./reports/ppo.html
    pages = Pages(storage).build_each(Path("experiments"))  # every experiment file there
    write(render_index(index(storage, pages, href=lambda stem: f"{stem}.html")), target(storage, "index"))

The file names the experiment, by its stem, and may define `report()`
(`etude.report.Report`) and `eval(state)` (`etude.experiment`). Without
`report()` the page is `Report()` and says so at its top. The experiment's
training runs are drawn by `etude.train.report.training`; an experiment with
eval runs alone is drawn as evaluation only.

Evals come from the experiment's eval database (`etude.eval.query.statsdb`).
A model is placed where its seat says it stood (`players.at`, from
`etude.eval.records.CheckpointRef.at`), else, for a key a training run of the
experiment recorded, where that run's clocks stood (`players.checkpoint`
joined to `training_checkpoints`). Where both give a clock and differ, the
seat's value is drawn and noted. Its line, the learner whose other
checkpoints `Earlier`, `At` and `Crosstable` compare it with, is its seat's
`line`, else its training run's name. A bound model is drawn in its run's
job; every other placed model is drawn in its line's curve, or in the one
curve of checkpoints with no line. Two keys holding the same bytes are two
models. Every unplaced player (code, a checkpoint seated without `at` from
outside the experiment, an etude-sdk 0.2 digest) is fixed, and drawn once as
an anchor.

What each tournament has played is `etude.eval.tournament.played`: for a
tournament `eval(state)` returns, what it wants now; for any other the
experiment's eval runs hold, everything they were held for. Each delivered
match counts once, from the eval run `played` says delivered it last, so at
its latest version. Matches of eval runs held for no tournament are one more
selection, each counting once per environment from its latest record. Values
are computed by `compare` over those matches, once for each selection
(`Matches`: each tournament, each of its tags, each matchup a panel names,
and the matches outside a tournament), each score and each combination of
the columns panels name. So any panel, under any filter the page offers,
shows a value `compare` computed, never one pooled in the browser. One
comparison played in several environments shows the environment whose latest
match is newest, and the page notes the rest.

Gaps stay visible. A checkpoint nothing played is not evaluated; one with a
match no eval run delivered is not yet played; failed matches are counted
and listed.

The index (`index.html`) is a row per experiment file under a folder
(`experiment_files`), drawn from each one's `Page` with its line data left
out, so every fact on it, such as each job's latest score, is the report's
own, computed by the same code (`charts.js`).

Each page is one HTML file with its data and the vendored uPlot inlined: no
network, no server. This module composes training and evaluation, which
never import each other.
"""

from __future__ import annotations

import itertools
import json
import runpy
import sys
import threading
import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from etude.experiment_file import experiment_named
from etude.report import Curve, Flame, Matches, Report, Score
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, declared, run_folders, runs
from etude.run.records import Record
from etude.run.storage import DirectoryStorage, S3Storage, Storage, default_root
from etude.train.read import read_all
from etude.train.report import WALL_MEANING, Training, training

if TYPE_CHECKING:
    import duckdb

    from etude.eval.records import Match, Scheduled, Tournament
    from etude.eval.tournament import Played


class Player(Record):
    """A player the eval runs seated: a placed checkpoint, or else a fixed one.

    `run` and `ordinal` name the training run's checkpoint its key binds it
    to, None for any other. `at` is where it is drawn, by clock: its latest
    seat's position, else its training record's; empty for a fixed player.
    `line` is the learner it belongs to: its latest seat's, else its training
    run's name; None for a fixed player or a checkpoint of no line.
    """

    key: str  # its key (`etude.eval.records.Model.key` or `Code.key`), as the database holds it
    name: str
    run: str | None
    ordinal: int | None
    # Canonical JSON: one checkpoint at two configs is two models.
    config: str
    at: dict[str, float] = {}
    line: str | None = None

    @property
    def placed(self) -> bool:
        """Whether it is drawn on a curve: a job's, or one of checkpoints no run saved."""
        return self.run is not None or bool(self.at)


class Environment(Record):
    """An environment the experiment's matches were played in."""

    name: str
    config: dict[str, JsonValue]


class Cell(Record):
    """`compare`'s value for one subject, against one opponent, in one selection and split.

    `subject` and `opponent` index `Page.players`, and `environment`
    `Page.environments`; an opponent of None is every opponent, kept only
    where a subject met several. `fixed` cells count only matches that seat
    no placed checkpoint: an anchor's strength. `split` holds exactly the columns it is split by.
    `commit` is None for each match's latest result, whichever version
    played it; a commit, for the matches as played at it (`Commit`).
    """

    selection: int
    commit: str | None
    environment: int
    score: int
    subject: int
    opponent: int | None
    fixed: bool
    split: dict[str, JsonValue]
    value: float
    interval: tuple[float, float] | None
    rows: int
    matches: int


class Commit(Record):
    """A commit the experiment's matches were played at, offered where there are several.

    Its view counts each match as played at this commit, from the latest
    eval run that played it there. `etude eval` never plays a delivered match
    again, so a commit's matches are those first played at it, and any an
    eval run held again at it.
    """

    commit: str
    matches: int


class Coverage(Record):
    """How far one checkpoint's matches in one tournament have got."""

    run: str
    ordinal: int
    tournament: str
    played: int
    # Failed, and not played since.
    failed: int
    # Wanted by `eval(state)` and delivered by no eval run.
    unplayed: int


class Failure(Record):
    """A match that failed and was not played since."""

    tournament: str | None
    players: tuple[int, ...]  # by seat, indexing `Page.players`
    error: str | None


class Page(Record):
    """Everything the page draws. Equal pages draw the same, so a watch rewrites only on change."""

    experiment: str
    file: str
    source: str
    report: Report
    # "FILE:report" when the file defines it; None for the default.
    origin: str | None
    # What the reader must know first, such as why the report is the default.
    notes: tuple[str, ...]
    wall_meaning: str = WALL_MEANING
    training: Training
    scores: tuple[Score, ...]
    # Here Matches(tournament=None) means the matches played outside any tournament,
    # not a report panel's "the section's control".
    selections: tuple[Matches, ...]
    players: tuple[Player, ...]
    environments: tuple[Environment, ...]
    commits: tuple[Commit, ...]
    cells: tuple[Cell, ...]
    coverage: tuple[Coverage, ...]
    failures: tuple[Failure, ...]
    latest_result_ms: int | None
    absent: tuple[str, ...]  # "TABLE.COLUMN" a panel names that its table lacks


class Entry(Record):
    """One experiment file on the index: its page without line data, or why it has none."""

    file: str
    experiment: str
    href: str
    page: Page | None
    error: str | None


class Index(Record):
    source: str
    entries: tuple[Entry, ...]


def experiment_files(directory: Path) -> list[Path]:
    """The experiment files at any depth below `directory`, outside hidden folders.

    A file is recognised by its text, without running it (`experiment_named`);
    raises `EtudeError("experiment_named_twice")` for two files with one stem.
    """
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob("*.py")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if experiment_named(path) is None:
            continue
        if path.stem in found:
            raise EtudeError("experiment_named_twice", 409) from ValueError(
                f"{found[path.stem]} and {path}"
            )
        found[path.stem] = path
    return list(found.values())


class Pages:
    """Experiments' pages from one source, each built again only when its inputs change.

    A page's inputs are its file, by modification time and size, and the
    experiment's runs, by the version of every object in their folders
    (`Storage.versions`): under the source, and under this computer's runs
    directory, whose eval runs count too (`etude.eval.tournament.played`).
    Checking them is one listing of each, plus a run's declaration the first
    time its version is seen, so an unchanged page costs no new bucket bytes;
    a changed one is built from the files as they are, so a page always
    reflects them.
    """

    def __init__(self, source: Storage | Path | str) -> None:
        self.storage = as_storage(source)
        here = DirectoryStorage(default_root())
        self._places = list({each.name: each for each in (self.storage, here)}.values())
        self._built: dict[Path, tuple[_Inputs, Page]] = {}
        # Each run's experiment by place and run, with the version of the declaration read.
        self._declared: dict[tuple[str, str], tuple[str, str | None]] = {}

    def build(self, file: Path) -> Page:
        """`build`'s page, kept from the last call while its inputs are the same."""
        return self._build(file, self._listings())

    def build_each(self, directory: Path) -> dict[Path, Page | str]:
        """Each experiment file's page, or why it could not be built, so one never hides the rest."""
        listings = self._listings()
        built: dict[Path, Page | str] = {}
        for file in experiment_files(directory):
            try:
                built[file] = self._build(file, listings)
            except Exception as error:
                # An author's file may fail in any way; its row says how and the others go on.
                built[file] = f"{type(error).__name__}: {error}"
        return built

    def _listings(self) -> list[dict[str, str]]:
        return [place.versions("") for place in self._places]

    def _build(self, file: Path, listings: Sequence[dict[str, str]]) -> Page:
        try:
            stat = file.stat()
        except FileNotFoundError:
            return build(self.storage, file)  # which says why there is no page
        runs = tuple(
            self._versions(place, file.stem, listed)
            for place, listed in zip(self._places, listings, strict=True)
        )
        inputs = _Inputs(file=(stat.st_mtime_ns, stat.st_size), runs=runs)
        kept = self._built.get(file)
        if kept is not None and kept[0] == inputs:
            return kept[1]
        page = build(self.storage, file)
        self._built[file] = (inputs, page)
        return page

    def _versions(
        self, place: Storage, experiment: str, listed: dict[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """The version of every object in the folders of the experiment's runs in one place."""
        keys = sorted(listed)
        found: list[tuple[str, str]] = []
        for run, folders in sorted(run_folders(keys).items()):
            if self._experiment(place, run, folders, listed) != experiment:
                continue
            for folder in folders:
                start = bisect_left(keys, folder)
                for key in itertools.takewhile(lambda k: k.startswith(folder), keys[start:]):
                    found.append((key, listed[key]))
        return tuple(found)

    def _experiment(
        self, place: Storage, run: str, folders: Sequence[str], listed: dict[str, str]
    ) -> str | None:
        """The run's experiment, from its declaration, read again only once it has changed."""
        version = next((listed[f + "run.json"] for f in folders if f + "run.json" in listed), "")
        kept = self._declared.get((place.name, run))
        if kept is None or kept[0] != version:
            file = declared(place, folders)
            kept = (version, None if file is None else file.experiment)
            self._declared[place.name, run] = kept
        return kept[1]


@dataclass(frozen=True)
class _Inputs:
    """What a page is built from, as `Pages` compares it."""

    file: tuple[int, int]  # modification time in nanoseconds, and size in bytes
    # Per place: (key, version) of each object in the experiment's runs' folders.
    runs: tuple[tuple[tuple[str, str], ...], ...]


def index(
    source: Storage | Path | str, pages: dict[Path, Page | str], *, href: Callable[[str], str]
) -> Index:
    """The index of built pages. Each page keeps all but its line data, which the index never draws."""
    storage = as_storage(source)
    entries = []
    for file, built in pages.items():
        page = None
        if isinstance(built, Page):
            # The index draws each job's curve in the page's first view: one score, unsplit.
            kept = tuple(
                c
                for c in built.cells
                if c.score == 0 and not c.split and not c.fixed and c.commit is None
            )
            lean = built.training.model_copy(update={"series": {}, "bands": {}, "timers": ()})
            page = built.model_copy(update={"cells": kept, "training": lean})
        entries.append(
            Entry(
                file=str(file),
                experiment=file.stem,
                href=href(file.stem),
                page=page,
                error=None if isinstance(built, Page) else built,
            )
        )
    return Index(source=_source_name(storage), entries=tuple(entries))


def _source_name(storage: Storage) -> str:
    """The source as a person names it: a directory's path or a bucket's URI, else its name."""
    if isinstance(storage, DirectoryStorage):
        return str(storage.root)
    if isinstance(storage, S3Storage):
        return storage.uri("").rstrip("/")
    return storage.name


def build(source: Storage | Path | str, file: Path) -> Page:
    """The page of the experiment `file` names, from the runs under `source`.

    The file is run each time, so an edited `report()` shows on the next
    build. Raises `ValueError` naming the experiments `source` holds when it
    holds no run of this one, and `EtudeError` for a file that is not an
    experiment's.
    """
    storage = as_storage(source)
    experiment = file.stem
    names = _run_file(file)
    report, origin, notes = _report(names.get("report"), file)
    data = read_all(storage, experiment)
    if not data:
        held = sorted({name for name, _ in runs(storage)})
        raise ValueError(
            f"no runs of {experiment!r} in {_source_name(storage)}"
            + (f"; it holds {', '.join(held)}" if held else "; it holds no runs")
        )
    trained = [run for run in data if run.file is None or run.file.declaration.kind != "evaluation"]
    found = names.get("eval")
    want: Callable[..., object] | None = None
    if found is not None:
        if not callable(found):
            raise EtudeError("experiment_eval_is_not_a_function", 422) from ValueError(file)
        want = found
    evals = (
        _read_evals(storage, experiment, report, want)
        if want is not None or len(trained) < len(data)
        else _Evals()
    )
    flames = [panel.prefix for panel in (*report.perf, *report.ml) if isinstance(panel, Flame)]
    return Page(
        experiment=experiment,
        file=str(file),
        source=_source_name(storage),
        report=report,
        origin=origin,
        notes=(*notes, *evals.notes),
        training=training(trained, prefixes=flames),
        scores=evals.scores or ((report.score,) if report.score is not None else ()),
        selections=evals.selections,
        players=evals.players,
        environments=evals.environments,
        commits=evals.commits,
        cells=evals.cells,
        coverage=evals.coverage,
        failures=evals.failures,
        latest_result_ms=evals.latest_result_ms,
        absent=evals.absent,
    )


def _run_file(file: Path) -> dict[str, object]:
    """The file's top-level names, run as `etude eval` runs it: it may import its neighbours."""
    if file.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422) from ValueError(file)
    if not file.is_file():
        raise EtudeError("experiment_file_not_found", 404) from FileNotFoundError(file)
    folder = str(file.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return runpy.run_path(str(file.resolve()))


def _report(made: object, file: Path) -> tuple[Report, str | None, tuple[str, ...]]:
    if made is None:
        return Report(), None, (f"{file.name} defines no report(), so this is the default report.",)
    if not callable(made):
        raise EtudeError("experiment_report_is_not_a_function", 422) from ValueError(file)
    report = made()
    if not isinstance(report, Report):
        raise EtudeError("experiment_report_returns_a_report", 422) from TypeError(
            f"{file.name}: report() returned a {type(report).__name__}"
        )
    return report, f"{file.name}:report", ()


@dataclass(frozen=True)
class _Evals:
    """The evals half of a page, empty for an experiment with no eval run and no eval()."""

    scores: tuple[Score, ...] = ()
    selections: tuple[Matches, ...] = ()
    players: tuple[Player, ...] = ()
    environments: tuple[Environment, ...] = ()
    commits: tuple[Commit, ...] = ()
    cells: tuple[Cell, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    failures: tuple[Failure, ...] = ()
    latest_result_ms: int | None = None
    absent: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _read_evals(
    storage: Storage, experiment: str, report: Report, want: Callable[..., object] | None
) -> _Evals:
    # The optional `eval` extra (DuckDB, and etude.eval over it), imported where it is
    # used: only an experiment with evaluations reads it, so a report without loads none.
    from etude.eval.query import statsdb  # noqa: PLC0415

    standing, refused = _standing(want, storage, experiment)
    db = statsdb(experiment, storage)
    try:
        players, placement = _players(db)
        outside = _load(db, standing, players)
        commits = _commits(db)
        selections, notes = _selections(standing, report, outside=outside)
        environments = _environments(db)
        scores, cells, absent, mixed = _cells(
            db, selections, players, environments, commits, report
        )
        latest = db.sql("select max(wall_ms) from matches").fetchone()
        return _Evals(
            scores=scores,
            selections=tuple(selections),
            players=tuple(players),
            environments=tuple(environments.values()),
            commits=commits,
            cells=tuple(cells),
            coverage=_coverage(db, standing),
            failures=_failures(db, standing, players),
            latest_result_ms=None if latest is None else latest[0],
            absent=absent,
            notes=(*refused, *placement, *_opponents_named(report, players), *notes, *mixed),
        )
    finally:
        db.close()


@dataclass(frozen=True)
class _Standing:
    """One tournament: every match it calls for, with matchup and tags, and how far it got."""

    name: str
    schedule: tuple[Scheduled, ...]
    played: Played


def _standing(
    want: Callable[..., object] | None, storage: Storage, experiment: str
) -> tuple[list[_Standing], tuple[str, ...]]:
    """Each tournament's standing, with a note for each one `etude eval` would refuse.

    A tournament `eval(state)` returns is what it wants now; any other the
    experiment's eval runs hold is everything they were held for.
    """
    # The optional `eval` extra, imported where it is used (`_read_evals`).
    from etude.eval.records import assemble  # noqa: PLC0415
    from etude.eval.tournament import played, tournament_runs  # noqa: PLC0415

    wanted: dict[str, Tournament] = {}
    if want is not None:
        for tournament in _wanted(want, storage, experiment):
            wanted.setdefault(tournament.definition.name, tournament)
    refused: list[str] = []
    for name, held in tournament_runs(experiment, storages=[storage]).items():
        if name in wanted:
            continue
        try:
            wanted[name] = assemble(held)
        except EtudeError as error:
            refused.append(
                f"Tournament {name} is not shown: its eval runs disagree ({error.code})."
            )
    standing = []
    for name, tournament in sorted(wanted.items()):
        try:
            found = played(
                tournament.definition,
                experiment=experiment,
                checkpoints=tournament.checkpoints,
                enter=tournament.entrants,
                storages=[storage],
            )
        except EtudeError as error:
            refused.append(f"Tournament {name} is not shown: it is refused ({error.code}).")
            continue
        standing.append(_Standing(name, tuple(tournament.schedule()), found))
    return standing, tuple(refused)


def _wanted(want: Callable[..., object], storage: Storage, experiment: str) -> list[Tournament]:
    """The tournaments `eval(state)` returns, each with the checkpoints or entrants it holds."""
    # The optional `eval` extra, imported where it is used (`_read_evals`): eval(state)
    # and its evaluation jobs are built on etude.eval.
    from etude.eval.records import Tournament  # noqa: PLC0415
    from etude.experiment import asked, observe  # noqa: PLC0415
    from etude.jobs import evaluation_spec  # noqa: PLC0415

    wanted = []
    for job in asked(want, observe(storage, experiment)):
        spec = evaluation_spec(job)
        wanted.append(
            Tournament(
                definition=spec.tournament,
                checkpoints=tuple(each.ref for each in spec.checkpoints),
                entrants=spec.entrants,
            )
        )
    return wanted


def _players(db: duckdb.DuckDBPyConnection) -> tuple[list[Player], tuple[str, ...]]:
    """Every player seated, placed by its seat, else by the training run that saved it;
    and notes on what could not be placed as recorded.

    Where seat and training record differ on a clock, the seat's value is
    drawn: it is what the evaluation declared. Where seats disagree, on a
    position or a line, the latest is drawn, so a later evaluation corrects a
    wrong one.
    """
    bound = _checkpoints(db)
    found = db.sql(
        'select player, name, kind, config, checkpoint, "at", at_conflict, line, line_conflict'
        " from players order by player"
    ).fetchall()
    players: list[Player] = []
    notes: list[str] = []
    unplaced: list[str] = []
    for key, name, kind, config, checkpoint, at, conflict, line, lines in found:
        saved = bound.get(checkpoint)
        recorded = {} if saved is None else saved.clocks
        seated: dict[str, float] = json.loads(at) if at else {}
        if conflict:
            earlier = "; ".join(_position(each) for each in json.loads(conflict) if each != seated)
            notes.append(
                f"{name} is drawn at {_position(seated)}, as its latest seat says;"
                f" earlier seats said {earlier}."
            )
        if lines:
            earlier = ", ".join(each for each in json.loads(lines) if each != line)
            notes.append(
                f"{name} is in line {line}, as its latest seat says; earlier seats said {earlier}."
            )
        for clock in sorted(seated.keys() & recorded.keys()):
            if seated[clock] != recorded[clock]:
                notes.append(
                    f"{name}: seated at {clock} {seated[clock]:g};"
                    f" its training record says {recorded[clock]:g}."
                )
        if kind == "model" and saved is None and not seated:
            unplaced.append(name)
        players.append(
            Player(
                key=key,
                name=name,
                run=None if saved is None else saved.run,
                ordinal=None if saved is None else saved.ordinal,
                config=json.dumps(json.loads(config or "{}"), sort_keys=True),
                at=recorded | seated,
                line=line if line is not None else None if saved is None else saved.run,
            )
        )
    if unplaced:
        notes.append(
            f"Drawn as anchors, seated without a position and saved by no training run of the"
            f" experiment: {', '.join(unplaced)}."
        )
    return players, tuple(notes)


def _position(at: dict[str, float]) -> str:
    return ", ".join(f"{clock} {value:g}" for clock, value in sorted(at.items()))


@dataclass(frozen=True)
class _Saved:
    """The training run's checkpoint a key names, and where the run's clocks stood."""

    run: str
    ordinal: int
    clocks: dict[str, float]


def _checkpoints(db: duckdb.DuckDBPyConnection) -> dict[str, _Saved]:
    """Each checkpoint key, encoded, to the training run's checkpoint that saved it.

    A run that saved two ordinals under one file name holds one key; it is
    bound to the later.
    """
    found = db.sql(
        "select checkpoint, run, max(ordinal), arg_max(clocks, ordinal) from training_checkpoints"
        " group by checkpoint, run"
    ).fetchall()
    return {key: _Saved(run, ordinal, json.loads(clocks)) for key, run, ordinal, clocks in found}


def _opponents_named(report: Report, players: Sequence[Player]) -> tuple[str, ...]:
    """A note for each curve whose opponent names no player seated."""
    names = {each.name for each in players}
    return tuple(
        f"A Curve panel ({panel.title}) names opponent {panel.opponent!r}, which no player is called."
        for panel in report.evals
        if isinstance(panel, Curve) and isinstance(panel.opponent, str)
        if panel.opponent not in names
    )


def _load(
    db: duckdb.DuckDBPyConnection, standing: Sequence[_Standing], players: Sequence[Player]
) -> bool:
    """Temp tables `compare` selects rows by; whether any match was held outside a tournament.

    `_page_held` (view, tournament, run, match): for view '', each delivered
    match and the eval run whose rows count, under tournament '' for those
    held outside any; for a commit, each match as played at it (`_commits`).
    `_page_scheduled` (tournament, match, matchup, tag): what each match was
    called for as. `_page_checkpoints` (player): the placed models.
    """
    db.execute(
        "create or replace temp table _page_held"
        " (view VARCHAR, tournament VARCHAR, run VARCHAR, match VARCHAR)"
    )
    db.execute(
        "create or replace temp table _page_scheduled"
        " (tournament VARCHAR, match VARCHAR, matchup VARCHAR, tag VARCHAR)"
    )
    db.execute("create or replace temp table _page_checkpoints (player VARCHAR)")
    held = [["", each.name, d.run, d.match.key] for each in standing for d in each.played.delivered]
    scheduled = [
        [each.name, called.match.key, called.matchup, tag]
        for each in standing
        for called in each.schedule
        for tag in (called.tags or (None,))
    ]
    placed = [[each.key] for each in players if each.placed]
    for table, rows in (
        ("_page_held", held),
        ("_page_scheduled", scheduled),
        ("_page_checkpoints", placed),
    ):
        if rows:  # DuckDB refuses an empty batch
            db.executemany(f"insert into {table} values ({', '.join('?' * len(rows[0]))})", rows)
    # Each done match outside a tournament once per environment, from its latest record, in
    # the order `etude.eval.tournament` takes a tournament's.
    db.execute(
        """
        insert into _page_held
        select '', '', run, match from (
            select m.run, m.match, row_number() over (
                partition by r.environment, m.match order by m.wall_ms desc, m.run desc
            ) as latest
            from matches m join runs r using (run)
            where m.state = 'done' and r.tournament is null
        ) where latest = 1
        """
    )
    found = db.sql("select count(*) from runs where tournament is null").fetchone()
    return found is not None and found[0] > 0


def _commits(db: duckdb.DuckDBPyConnection) -> tuple[Commit, ...]:
    """Each commit's view in `_page_held`, where the delivered matches were played at several.

    A commit's view holds each delivered match some done record seats it
    in, from the latest such eval run in the environment the match's latest
    result is in, so it never pools environments the latest view keeps apart.
    """
    db.execute(
        """
        insert into _page_held
        select commit, tournament, run, match from (
            select s.commit, h.tournament, m.run, m.match, row_number() over (
                partition by s.commit, h.tournament, m.match order by m.wall_ms desc, m.run desc
            ) as latest
            from (select h.tournament, h.match, r.environment
                  from _page_held h join runs r using (run) where h.view = '') h
            join matches m on m.match = h.match and m.state = 'done'
            join runs r on r.run = m.run and r.environment = h.environment
            join (select distinct run, match, commit from seats where commit is not null) s
              on s.run = m.run and s.match = m.match
        ) where latest = 1
        """
    )
    found = db.sql(
        "select view, count(distinct (tournament, match)) from _page_held"
        " where view != '' group by view order by view"
    ).fetchall()
    if len(found) < 2:
        db.execute("delete from _page_held where view != ''")
        return ()
    return tuple(Commit(commit=commit, matches=matches) for commit, matches in found)


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _where(selection: Matches, *, fixed: bool, view: str = "") -> str:
    """The rows of a selection's matches in a view of `_page_held`, as a condition
    `compare` applies to an author table."""
    tournament = _literal(selection.tournament or "")
    held = (
        "(run, match) in (select run, match from _page_held"
        f" where view = {_literal(view)} and tournament = {tournament})"
    )
    scheduled = f"select match from _page_scheduled where tournament = {tournament}"
    if selection.matchup is not None:
        held += f" and match in ({scheduled} and matchup = {_literal(selection.matchup)})"
    if selection.tag is not None:
        held += f" and match in ({scheduled} and tag = {_literal(selection.tag)})"
    if fixed:
        held += (
            " and match not in (select match from seats"
            " where player in (select player from _page_checkpoints))"
        )
    return held


def _selections(
    standing: Sequence[_Standing], report: Report, *, outside: bool
) -> tuple[list[Matches], tuple[str, ...]]:
    """Each tournament, the matches outside one, each tag and each matchup a panel names;
    and what is amiss."""
    tournaments = [each.name for each in standing]
    tags = sorted({(each.name, tag) for each in standing for c in each.schedule for tag in c.tags})
    matchups = sorted(
        {(each.name, c.matchup) for each in standing for c in each.schedule if c.matchup}
    )
    selections = [Matches(tournament=name) for name in tournaments]
    selections += [Matches()] if outside else []
    selections += [Matches(tournament=t, tag=tag) for t, tag in tags]
    notes: list[str] = []
    for panel in report.evals:
        chosen, title = panel.matches, f"{type(panel).__name__} panel"
        if chosen is None:
            continue
        if chosen.tournament is not None and chosen.tournament not in tournaments:
            notes.append(f"A {title} names tournament {chosen.tournament!r}, which nothing holds.")
        if chosen.tag is not None and not any(tag == chosen.tag for _, tag in tags):
            notes.append(f"A {title} names tag {chosen.tag!r}, which no matchup has.")
        if chosen.matchup is None:
            continue
        named = [
            Matches(tournament=t, matchup=m)
            for t, m in matchups
            if m == chosen.matchup and chosen.tournament in (None, t)
        ]
        if not named:
            notes.append(f"A {title} names matchup {chosen.matchup!r}, which nothing holds.")
        selections += [each for each in named if each not in selections]
    return selections, tuple(notes)


def _environments(db: duckdb.DuckDBPyConnection) -> dict[str, Environment]:
    """Each environment the eval runs played in, by identity, the newest latest match first."""
    found = db.sql(
        "select r.environment, any_value(r.environment_name), any_value(r.environment_config),"
        " max(m.wall_ms) as latest from runs r left join matches m using (run)"
        " group by r.environment order by latest desc nulls last, r.environment"
    ).fetchall()
    return {
        identity: Environment(name=name, config=json.loads(config or "{}"))
        for identity, name, config, _ in found
    }


def _cells(
    db: duckdb.DuckDBPyConnection,
    selections: Sequence[Matches],
    players: Sequence[Player],
    environments: dict[str, Environment],
    commits: Sequence[Commit],
    report: Report,
) -> tuple[tuple[Score, ...], list[Cell], tuple[str, ...], tuple[str, ...]]:
    """Every value the eval panels may show, the scores offered, absent columns, and a note
    for each environment whose values another, newer one hides.

    `environments` are by identity, the newest latest match first.
    """
    # The optional `eval` extra, imported where it is used (`_read_evals`).
    from etude.eval.match import ETUDE_TABLES  # noqa: PLC0415
    from etude.eval.query import KEYS, NUMERIC, compare  # noqa: PLC0415

    columns: dict[str, dict[str, str]] = {
        table: {name: kind for name, kind, *_ in db.sql(f'describe "{table}"').fetchall()}
        for (table,) in db.sql("show tables").fetchall()
        if table not in ETUDE_TABLES
    }
    scores = (
        (report.score,)
        if report.score is not None
        else tuple(
            Score(table=table, value=name)
            for table in sorted(columns)
            for name, kind in columns[table].items()
            if kind in NUMERIC and name not in KEYS
        )
    )
    index = {each.key: number for number, each in enumerate(players)}
    anchors = sum(not each.placed for each in players)
    place = {identity: number for number, identity in enumerate(environments)}
    cells: list[Cell] = []
    absent: list[str] = []
    for number, score in enumerate(scores):
        held = columns.get(score.table, {})
        if score.value not in held:
            absent.append(f"{score.table}.{score.value}")
            continue
        named = [c for c in report.columns() if c in held]
        absent += [f"{score.table}.{c}" for c in report.columns() if c not in held]
        # An anchor's strength needs two anchors to have met.
        views = itertools.product(
            (None, *(each.commit for each in commits)),
            enumerate(selections),
            (False, True) if anchors > 1 else (False,),
            (by for size in range(len(named) + 1) for by in itertools.combinations(named, size)),
        )
        for commit, (chosen, selection), fixed, by in views:
            for found in compare(
                db,
                table=score.table,
                value=score.value,
                per=score.per,
                where=_where(selection, fixed=fixed, view=commit or ""),
                by=by,
            ):
                if len(found.subject) != 1 or (found.opponent and len(found.opponent) != 1):
                    continue  # a team
                if fixed and found.opponent is not None:
                    continue  # pairs of anchors are in the unrestricted cells
                cells.append(
                    Cell(
                        selection=chosen,
                        commit=commit,
                        environment=place[found.environment],
                        score=number,
                        subject=index[found.subject[0]],
                        opponent=None if found.opponent is None else index[found.opponent[0]],
                        fixed=fixed,
                        split=found.split,
                        value=found.value,
                        interval=found.interval,
                        rows=found.rows,
                        matches=found.matches,
                    )
                )
    cells, mixed = _newest_environment(cells, list(environments.values()))
    # "Against all" repeats the only opponent a subject met.
    met: dict[tuple[int, str | None, int, int], set[int]] = {}
    for cell in cells:
        if cell.opponent is not None:
            met.setdefault(_met(cell), set()).add(cell.opponent)
    kept = [c for c in cells if c.opponent is not None or c.fixed or len(met.get(_met(c), ())) != 1]
    return scores, kept, tuple(dict.fromkeys(absent)), mixed


def _newest_environment(
    cells: Sequence[Cell], environments: Sequence[Environment]
) -> tuple[list[Cell], tuple[str, ...]]:
    """Of one comparison's cells in several environments, the newest environment's; and a
    note per environment that loses any. Environments are never pooled."""
    # A lower index is a newer environment.
    newest: dict[tuple[object, ...], int] = {}
    for cell in cells:
        same = _comparison(cell)
        newest[same] = min(cell.environment, newest.get(same, cell.environment))
    hidden = Counter(c.environment for c in cells if c.environment != newest[_comparison(c)])
    return [c for c in cells if c.environment == newest[_comparison(c)]], tuple(
        f"{count} values were also played in {_environment_name(environments[number])};"
        " the page shows each from the environment whose latest match is newest."
        for number, count in sorted(hidden.items())
    )


def _comparison(cell: Cell) -> tuple[object, ...]:
    """What a cell compares, whatever environment it was played in."""
    return (
        cell.selection, cell.commit, cell.score, cell.subject, cell.opponent, cell.fixed,
        json.dumps(cell.split, sort_keys=True),
    )  # fmt: skip


def _environment_name(environment: Environment) -> str:
    if not environment.config:
        return environment.name
    return f"{environment.name} {json.dumps(environment.config, sort_keys=True)}"


def _met(cell: Cell) -> tuple[int, str | None, int, int]:
    """The opponents a subject met are counted within one selection, view and score."""
    return (cell.selection, cell.commit, cell.score, cell.subject)


def _coverage(db: duckdb.DuckDBPyConnection, standing: Sequence[_Standing]) -> tuple[Coverage, ...]:
    """Each checkpoint's matches in each tournament: played, failed since, and not yet played."""
    bound = _checkpoints(db)
    counts: dict[tuple[str, int, str], list[int]] = {}
    for each in standing:
        states: tuple[tuple[int, Sequence[Match]], ...] = (
            (0, [d.match for d in each.played.delivered]),
            (1, each.played.failed),
            (2, each.played.new),
        )
        for state, matches in states:
            for match in matches:
                keys = {str(s.checkpoint.key) for s in match.players if s.kind == "model"}
                for key in keys & bound.keys():
                    saved = bound[key]
                    counts.setdefault((saved.run, saved.ordinal, each.name), [0, 0, 0])[state] += 1
    return tuple(
        Coverage(run=run, ordinal=ordinal, tournament=name, played=n[0], failed=n[1], unplayed=n[2])
        for (run, ordinal, name), n in sorted(counts.items())
    )


def _failures(
    db: duckdb.DuckDBPyConnection, standing: Sequence[_Standing], players: Sequence[Player]
) -> tuple[Failure, ...]:
    """Each failed match `played` counts, and each outside a tournament that no eval run has
    played since in its environment, with the error its eval run recorded."""
    index = {each.key: number for number, each in enumerate(players)}
    errors = dict(
        db.sql(
            "select match, max(error) from matches where state = 'failed' group by match"
        ).fetchall()
    )
    held = tuple(
        Failure(
            tournament=each.name,
            players=tuple(index[seat.key] for seat in match.players),
            error=errors.get(match.key),
        )
        for each in standing
        for match in each.played.failed
    )
    outside = db.sql(
        """
        select r.environment, m.match, list(s.player order by s.seat), max(m.error)
        from matches m join runs r using (run)
        join seats s on s.run = m.run and s.match = m.match
        where m.state = 'failed' and r.tournament is null
            and not exists (
                select 1 from matches d join runs e using (run)
                where d.state = 'done' and d.match = m.match and e.environment = r.environment
            )
        group by r.environment, m.run, m.match order by m.match
        """
    ).fetchall()
    # A match is one failure per environment, however many eval runs failed it there.
    unique = {
        (environment, match): (seated, error) for environment, match, seated, error in outside
    }
    return held + tuple(
        Failure(tournament=None, players=tuple(index[key] for key in seated), error=error)
        for seated, error in unique.values()
    )


def render(page: Page, *, refresh: float | None = None) -> str:
    """The page as one HTML file; `refresh` is the seconds between the open page reloading."""
    return _inline("page.html", page.experiment, page.model_dump(mode="json"), refresh)


def render_index(found: Index, *, refresh: float | None = None) -> str:
    """The index as one HTML file, like a page."""
    return _inline("index.html", "Experiments", found.model_dump(mode="json"), refresh)


def _inline(template: str, title: str, data: dict[str, object], refresh: float | None) -> str:
    here = Path(__file__).parent
    stamp = {"generated_ms": time.time_ns() // 1_000_000, "refresh": refresh}
    payload = json.dumps(data | stamp, allow_nan=False)
    # Each placeholder is replaced once, the data last, so its text is never taken for one.
    html = (here / template).read_text()
    for name, text in (
        ("__UPLOT_CSS__", (here / "vendor/uPlot.min.css").read_text()),
        ("__PAGE_CSS__", (here / "page.css").read_text()),
        ("__UPLOT_JS__", (here / "vendor/uPlot.iife.min.js").read_text()),
        ("__CHARTS_JS__", (here / "charts.js").read_text()),
        ("__TITLE__", escape(title)),
        ("__DATA__", payload.replace("</", "<\\/")),
    ):
        before, found, after = html.partition(name)
        if not found:
            raise AssertionError(f"{template} has no {name}")
        html = before + text + after
    return html


def target(storage: Storage, experiment: str, out: Path | None = None) -> Path:
    """Where the page is written: `out`, else `reports/` beside a directory's runs or here."""
    directory = out or (
        storage.root / "reports" if isinstance(storage, DirectoryStorage) else Path("reports")
    )
    return directory / f"{experiment}.html"


def write(html: str, path: Path) -> Path:
    """Replace the file in one step, so a reloading page never reads half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(html)
    partial.replace(path)
    return path


class Server(ThreadingHTTPServer):
    """Serves an experiment file's page at `/`, or a folder's index at `/` and each of its
    experiments at `/STEM`, from the files as they are at each request (`Pages`).

    It has no login: keep it on this computer unless told otherwise.
    """

    def __init__(
        self,
        source: Storage | Path | str,
        path: Path,
        address: tuple[str, int],
        *,
        refresh: float | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.pages, self.path, self.refresh = Pages(source), path, refresh
        # Requests are handled on threads; a page is built by one at a time.
        self._building = threading.Lock()

    def page(self, route: str = "/") -> str | None:
        """The HTML served at `route`, or None where nothing is."""
        with self._building:
            if not self.path.is_dir():
                if route != "/":
                    return None
                return render(self.pages.build(self.path), refresh=self.refresh)
            if route == "/":
                built = self.pages.build_each(self.path)
                found = index(self.pages.storage, built, href=lambda stem: f"/{stem}")
                return render_index(found, refresh=self.refresh)
            for file in experiment_files(self.path):
                if route == f"/{file.stem}":
                    return render(self.pages.build(file), refresh=self.refresh)
            return None


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        try:
            served = self.server.page(self.path.split("?", 1)[0])
            found, body = served is not None, (served or "Not found.").encode()
        except ValueError as error:  # no runs of the experiment yet
            found, body = False, str(error).encode()
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", f"text/{'html' if found else 'plain'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Requests are not printed; the command's own output stays readable."""
