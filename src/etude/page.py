"""An experiment's report page, and an index of every experiment in a folder: built, written or served.

    storage = storage_from_uri("r2://bucket/runs")
    page = build(storage, Path("experiments/ppo.py"))
    write(render(page), target(storage, "ppo"))  # ./reports/ppo.html
    pages = Pages(storage).build_each(Path("experiments"))  # every experiment file there
    write(render_index(index(storage, pages, href=lambda stem: f"{stem}.html")), target(storage, "index"))

The file defaults the experiment to its stem; an explicit name overrides it. It may define `report()`
(`etude.report.Report`) and `eval_jobs(state)` (`etude.experiment`). Without
`report()` the page is `Report(other_metrics=True)`, drawing every metric its
runs logged, and says so at its top. The experiment's
training runs are drawn by `etude.train.report.training`; an experiment with
eval runs alone is drawn as evaluation only.

Evals come from the experiment's eval database (`etude.eval.query.statsdb`).
A model is placed where its seats say it stood (`players.seated_at`, from
`etude.eval.records.CheckpointRef.at`, merged with the latest winning), else,
for a key a training run of the experiment recorded, where that run's clocks
stood (`players.checkpoint` joined to `training_checkpoints`). Where both
give a clock and differ, the seat's value is drawn and noted. Its line, the
learner whose other checkpoints `Crosstable` compares it with, is its latest
seat's (`players.lines`), else its training run's line: the first run of the
chain of continuations that led to it (`etude.experiment.Checkpoint.line`).
A bound model is drawn in its run's job; every other placed model is drawn in
its line's curve, or in the one curve of checkpoints with no line. Two keys
holding the same bytes are two models. Every unplaced player (code, a
checkpoint seated with no `at` from outside the experiment, an etude-sdk
0.2 digest) is fixed, and drawn once as an anchor.

What each tournament has played is `etude.eval.tournament.played`: for a
tournament `eval_jobs(state)` returns, what it wants now; for any other the
experiment's eval runs hold, or one `eval_jobs(state)` now defines otherwise than
they were held with, everything they were held for. Each delivered
match counts once, from the eval run `played` says delivered it last, so at
its latest version. Matches of eval runs held for no tournament are one more
selection, each counting once per environment from its latest record. Values
are computed by `compare` over those matches, once for each selection
(`Matches`: each tournament, each of its tags, each seating or tag a panel names,
over every tournament unless it names one, and panels' unions; and
`OutsideTournaments`), each score and each combination of
the columns panels name. So any panel, under any filter the page offers,
shows a value `compare` computed, never one pooled in the browser. A value
played in several environments shows the environment whose latest match is
newest, and the page notes the rest.

A curve of seatings draws points (`Point`): each checkpoint a seating was
held for, where it stood, with the first seat's score against the second
over that checkpoint's matches, or why it has none. Relative seats find
their checkpoints among the experiment's training lines
(`etude.jobs.ExperimentState.lines`), read once with its runs.

Gaps stay visible. A checkpoint nothing played is not evaluated; one with a
match no eval run delivered is not yet played; one whose relative seat has
no checkpoint says so, not yet or never; failed matches are counted and
listed.

The index (`index.html`) is a row per experiment file under a folder
(`experiment_files`), drawn from each one's `Page` with its line data left
out, so every fact on it, such as each job's latest score, is the report's
own, computed by the same code (`charts.js`).

Each page is one HTML file with its data and the vendored uPlot inlined: no
network, no server. This module composes training and evaluation, which
never import each other.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import runpy
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import cache_from_source
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from pydantic import JsonValue, ValidationError

from etude.errors import EtudeError, explain
from etude.eval.records import Gap, Scheduled, Tournament
from etude.experiment_file import experiment_named, refuse_renamed
from etude.identity import SAFE_NAME
from etude.records import BUILT_IN_NAMES, Record
from etude.report import Counters, Curve, Flame, Matches, Report, Score, Series
from etude.run import LocatedRun, as_storage, declared, located_runs, run_folders, under
from etude.storage import DirectoryStorage, S3Storage, Storage, default_root
from etude.train.read import RunData, read_all
from etude.train.report import Training, training

if TYPE_CHECKING:
    import duckdb

    from etude.eval.tournament import Played
    from etude.jobs import ExperimentState


class Placement(Record):
    """A player the eval runs seated, and where the page draws it: a placed checkpoint, or
    else a fixed one.

    `run` and `ordinal` name the training run's checkpoint its key binds it
    to, None for any other. `at` is where it is drawn, by clock: its seats'
    `at`, else its training record's; empty for a fixed player. `line` is
    the learner it belongs to: its latest seat's, else its training run's
    line; None for a fixed player or a checkpoint of no line. `placed`: it is
    drawn on a curve, a job's or one of checkpoints no run saved, so bound
    to a run or given an `at`; else it is an anchor.
    """

    key: str  # its key (`etude.eval.records.CheckpointPlayer.key` or `Code.key`), as the database holds it
    # Every name its seats gave it, the latest first, joined by "/".
    name: str
    run: str | None
    ordinal: int | None
    # Canonical JSON: one checkpoint at two configs is two models.
    config: str
    at: dict[str, float] = {}
    line: str | None = None
    placed: bool = False


class EnvironmentLabel(Record):
    """An environment the experiment's matches were played in, as the page names it."""

    name: str
    config: dict[str, JsonValue]


class OutsideTournaments(Record):
    """The selection of matches eval runs held outside any tournament, each counted once per
    environment from its latest record. A `Matches` never means these."""

    kind: Literal["outside"] = "outside"


# What a selection counts: as a panel names it, a union of several, or the matches outside.
type Selection = Matches | tuple[Matches, ...] | OutsideTournaments


class Drawn(Record):
    """How a selection is drawn, beside it in `Page.selections`.

    `seated`: every part names a seating, so a curve draws its points
    (`Point`), each held checkpoint as seated. `tournaments`: those its
    matches were called for in, whose coverage says why a checkpoint has no
    value in it.
    """

    seated: bool
    tournaments: tuple[str, ...]


class PanelSelection(Record):
    """The selection an eval panel draws, beside it in `Report.evals`: `selection`, or with
    `control` the one the Evals section's Matches control picks; neither where no
    selection holds the matches the panel names."""

    selection: int | None = None
    control: bool = False


class Absent(Record):
    """A column a panel or score names that its table lacks."""

    table: str
    column: str


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


class Point(Record):
    """A point of a curve of seatings: a checkpoint a seating was held for, where it
    stood, and its score or why it has none.

    `selection` indexes `Page.selections`: one of seatings. It is drawn where
    `run`'s checkpoint `ordinal` stood, or, for one no training run saved, at
    `at` in `line`. A scored point is the first seat's score against the
    second, in one view (`commit`), `score` and `split` as a `Cell` is: the
    mean over `opponents`, the players it met in the second seat, indexing
    `Page.players`, with `interval` where it met one. A point with no
    `value` has `gap`, what its relative seat looked for, in every view. `whole` marks a `seeds="one"`
    seating's point, which stands for its job though one seed's checkpoint
    was held for it.
    """

    selection: int
    run: str | None
    ordinal: int | None
    line: str | None
    at: dict[str, float]
    whole: bool = False
    commit: str | None = None
    score: int | None = None
    split: dict[str, JsonValue] = {}
    value: float | None = None
    interval: tuple[float, float] | None = None
    rows: int = 0
    matches: int = 0
    opponents: tuple[int, ...] = ()
    gap: Gap | None = None


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
    # Wanted by `eval_jobs(state)` and delivered by no eval run.
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
    training: Training
    scores: tuple[Score, ...]
    selections: tuple[Selection, ...]
    # By selection, how it is drawn.
    drawn: tuple[Drawn, ...]
    # By eval panel of the report, the selection it draws.
    panel_selections: tuple[PanelSelection, ...]
    players: tuple[Placement, ...]
    environments: tuple[EnvironmentLabel, ...]
    commits: tuple[Commit, ...]
    cells: tuple[Cell, ...]
    points: tuple[Point, ...] = ()
    coverage: tuple[Coverage, ...]
    failures: tuple[Failure, ...]
    latest_result_ms: int | None
    absent: tuple[Absent, ...]


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


def experiment_files(directory: Path, *, known: Collection[Path] = ()) -> list[Path]:
    """The experiment files at any depth below `directory`, outside hidden folders.

    A file is recognised by its text, without running it (`experiment_named`);
    Previously recognised files stay experiments until removed, even while being edited.
    Raises `EtudeError("experiment_named_twice")` for two files with one stem.
    """
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob("*.py")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if path not in known and experiment_named(path) is None:
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

    def __init__(self, source: Storage | Path | str, *, experiment: str | None = None) -> None:
        self.experiment = experiment
        self.storage = as_storage(source)
        here = DirectoryStorage(default_root())
        self._places = list({each.name: each for each in (self.storage, here)}.values())
        self._built: dict[Path, tuple[_Inputs, Page]] = {}
        self._known: set[Path] = set()
        # Each folder's experiment by place and prefix, with its declaration's version.
        self._declared: dict[tuple[str, str], tuple[str, str | None]] = {}

    def build(self, file: Path) -> Page:
        """`build`'s page, kept from the last call while its inputs are the same."""
        return self._build(file, self._listings())

    def find_files(self, directory: Path) -> list[Path]:
        """Discover experiments, retaining known files through incomplete edits."""
        found = experiment_files(directory, known=self._known)
        self._known.update(found)
        return found

    def build_each(self, directory: Path) -> dict[Path, Page | str]:
        """Each experiment file's page, or why it could not be built, so one never hides the rest."""
        listings = self._listings()
        built: dict[Path, Page | str] = {}
        for file in self.find_files(directory):
            try:
                built[file] = self._build(file, listings)
            except Exception as error:
                # An author's file may fail in any way; its row says how and the others go on.
                built[file] = explain(error)
        return built

    def _listings(self) -> list[dict[str, str]]:
        return [place.versions("") for place in self._places]

    def _build(self, file: Path, listings: Sequence[dict[str, str]]) -> Page:
        if not file.is_file():
            return build(self.storage, file, experiment=self.experiment)
        experiment = self.experiment or file.stem
        runs = tuple(
            self._versions(_with_versions(place, listed), experiment, listed)
            for place, listed in zip(self._places, listings, strict=True)
        )
        kept = self._built.get(file)
        if kept is not None and kept[0] == _Inputs(files=_sources(file), runs=runs):
            return kept[1]
        page = _build_page(self.storage, file, experiment, listed=listings[0])
        # After the build, so the neighbours this run imported are among its inputs.
        self._built[file] = (_Inputs(files=_sources(file), runs=runs), page)
        return page

    def _versions(
        self, place: Storage, experiment: str, listed: dict[str, str]
    ) -> tuple[tuple[str, str], ...]:
        """The version of every object in the folders of the experiment's runs in one place."""
        keys = sorted(listed)
        found: list[tuple[str, str]] = []
        for folders in run_folders(keys).values():
            for folder in folders:
                if self._experiment(place, folder, listed) == experiment:
                    found.extend((key, listed[key]) for key in under(keys, folder))
        return tuple(found)

    def _experiment(self, place: Storage, folder: str, listed: dict[str, str]) -> str | None:
        """The run's experiment, from its declaration, read again only once it has changed."""
        version = listed.get(folder + "run.json", "")
        kept = self._declared.get((place.name, folder))
        if kept is None or kept[0] != version:
            file = declared(place, folder, listed)
            kept = (version, None if file is None else file.experiment)
            self._declared[place.name, folder] = kept
        return kept[1]


@dataclass(frozen=True)
class _Inputs:
    """What a page is built from, as `Pages` compares it."""

    # The experiment file and its imported neighbours: (path, modification time in ns, bytes).
    files: tuple[tuple[str, int, int], ...]
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
            curves = [p for p in built.report.evals if isinstance(p, Curve)]
            score = 0
            if curves and all(p.score is not None for p in curves):
                score = next(i for i, s in enumerate(built.scores) if s == curves[0].score)
            kept = tuple(
                c
                for c in built.cells
                if c.score == score and not c.split and not c.fixed and c.commit is None
            )
            points = tuple(
                p
                for p in built.points
                if p.value is None or (p.score == score and not p.split and p.commit is None)
            )
            lean = built.training.model_copy(update={"series": {}, "bands": {}, "timers": ()})
            page = built.model_copy(update={"cells": kept, "points": points, "training": lean})
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


def _with_versions(storage: Storage, versions: Mapping[str, str]) -> Storage:
    return storage._with_versions(versions) if isinstance(storage, S3Storage) else storage


def build(source: Storage | Path | str, file: Path, *, experiment: str | None = None) -> Page:
    """The page of the experiment `file` names, from the runs under `source`.

    The file is run each time, so an edited `report()` shows on the next
    build. Raises `ValueError` naming the experiments `source` holds when it
    holds no run of this one, and `EtudeError` for a file that is not an
    experiment's.
    """
    return _build_page(as_storage(source), file, experiment or file.stem)


# Bump whenever derived Page fields change without changing inputs.
_PAGE_CACHE_VERSION = 1


def _page_cache_path(experiment: str, storage: Storage) -> Path:
    home = Path(os.environ.get("ETUDE_HOME", "~/.etude")).expanduser()
    # Bucket display names flatten slashes and omit the endpoint. Hints belong to one prefix.
    source = (
        storage._artifact("").location if isinstance(storage, S3Storage) else _source_name(storage)
    )
    store = hashlib.sha256(source.encode()).hexdigest()[:16]
    stem = (
        f"{experiment}.{store}"
        if SAFE_NAME.match(experiment)
        else hashlib.sha256(f"{experiment}\0{source}".encode()).hexdigest()[:24]
    )
    return home / "pages" / f"{stem}.json"


def _page_cache_key(
    *,
    experiment: str,
    file: Path,
    source: str,
    report: Report,
    origin: str | None,
    notes: Sequence[str],
    versions: Mapping[str, str],
    state: ExperimentState | None,
    wanted: Sequence[Tournament],
    local_versions: Mapping[str, str],
) -> str:
    """Digest of every evaluated input that determines `Page`, including external files read by code."""
    inputs = {
        "v": _PAGE_CACHE_VERSION,
        "experiment": experiment,
        "file": str(file),
        "source": source,
        "report": report.model_dump_json(),
        "ml_set": "ml" in report.model_fields_set,
        "origin": origin,
        "notes": notes,
        "versions": versions,
        "local": local_versions,
        "wanted": [t.model_dump_json() for t in wanted],
        # RunData on a summary is lazy. Fingerprint its stored source via versions above.
        "state": None
        if state is None
        else {
            "committed_usd": str(state.committed_usd),
            "runs": [
                (
                    r.name,
                    r.state,
                    r.job,
                    r.launch,
                    r.line,
                    r.seed,
                    [c.model_dump(mode="json") for c in r.checkpoints],
                )
                for r in state.runs
            ],
            "eval_runs": [(e.name, e.state, e.matches) for e in state.eval_runs],
        },
    }
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _PageCacheHeader(Record):
    key: str
    split_points: tuple[str, ...]

    @classmethod
    def read(cls, stream: BinaryIO) -> _PageCacheHeader:
        # 47 S3 keys of at most 1,024 bytes, including worst-case JSON escaping.
        return cls.model_validate_json(stream.readline(512 * 1024))


def _list_page_versions(storage: Storage, path: Path) -> dict[str, str]:
    if isinstance(storage, S3Storage):
        try:
            with path.open("rb") as stream:
                bounds = _PageCacheHeader.read(stream).split_points
        except (OSError, ValidationError):
            bounds = ()
        return storage.versions("", split_points=bounds)
    return storage.versions("")


def _load_cached_page(path: Path, expected_key: str) -> Page | None:
    try:
        with path.open("rb") as stream:
            if _PageCacheHeader.read(stream).key != expected_key:
                return None
            return Page.model_validate_json(stream.read())
    except (OSError, ValidationError):
        return None


def _save_cached_page(path: Path, header: _PageCacheHeader, page: Page) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as staging:
            pending = Path(staging) / path.name
            with pending.open("wb") as stream:
                stream.write(header.model_dump_json().encode())
                stream.write(b"\n")
                stream.write(page.model_dump_json().encode())
            pending.replace(path)
    except OSError:
        pass


def _build_page(
    storage: Storage,
    file: Path,
    experiment: str,
    *,
    listed: Mapping[str, str] | None = None,
) -> Page:
    names = _run_file(file)
    report, origin, notes = _report(names.get("report"), file)
    flames = tuple(panel.prefix for panel in (*report.perf, *report.ml) if isinstance(panel, Flame))
    cache_path = _page_cache_path(experiment, storage)
    versions = _list_page_versions(storage, cache_path) if listed is None else listed
    keys = sorted(versions)
    storage = _with_versions(storage, versions)
    declarations = located_runs(storage, keys)
    data = read_all(
        storage,
        experiment,
        metrics=None if report.other_metrics else frozenset(report.keys()),
        prefixes=flames,
        keys=keys,
        declarations=declarations,
    )
    if not data:
        held = sorted({run.file.experiment for run in declarations})
        raise ValueError(
            f"no runs of {experiment!r} in {_source_name(storage)}"
            + (f"; it holds {', '.join(held)}" if held else "; it holds no runs")
        )
    trained = [run for run in data if run.file is None or run.file.kind != "evaluation"]
    want = names.get("eval_jobs")
    if want is not None and not callable(want):
        raise EtudeError("experiment_eval_is_not_a_function", 422) from ValueError(file)
    state = None
    wanted: list[Tournament] = []
    local_versions: dict[str, str] = {}
    if want is not None or len(trained) < len(data):
        # Only reports with evaluations need the optional eval dependency.
        from etude.experiment import state_of  # noqa: PLC0415

        state = state_of(data, experiment)
        if want is not None:
            wanted = _wanted(want, state)
        here = DirectoryStorage(default_root())
        if here.name != storage.name:
            local_versions = here.versions("")
    source_name = _source_name(storage)
    cache_key = _page_cache_key(
        experiment=experiment,
        file=file,
        source=source_name,
        report=report,
        origin=origin,
        notes=notes,
        versions=versions,
        state=state,
        wanted=wanted,
        local_versions=local_versions,
    )
    if (cached := _load_cached_page(cache_path, cache_key)) is not None:
        return cached
    evals = (
        _read_evals(storage, state, report, wanted, keys=keys, declarations=declarations)
        if state is not None
        else _Evals()
    )
    # Blocks arrive as they are sealed, so a run with no recorded end shows what it played so far.
    unended = [
        run.name
        for run in data
        if run.file and run.file.kind == "evaluation" and run.outcome is None
    ]
    partial = (
        (f"Partial results: no recorded end yet for {', '.join(unended)}.",) if unended else ()
    )
    measured = training(trained, prefixes=flames)
    if "ml" not in report.model_fields_set:
        # Defaults offer the RL metrics that exist; explicit panels still
        # diagnose missing keys, including a misspelt metric name.
        report = report.model_copy(
            update={
                "ml": tuple(
                    panel.model_copy(update={"keys": kept_keys})
                    for panel in report.ml
                    if isinstance(panel, Series | Counters)
                    and (kept_keys := tuple(key for key in panel.keys if key in measured.metrics))
                )
            }
        )
    page = Page(
        experiment=experiment,
        file=str(file),
        source=source_name,
        report=report,
        origin=origin,
        notes=(*notes, *partial, *_without_hours(trained), *evals.notes),
        training=measured,
        scores=evals.scores or report.scores(),
        selections=evals.selections,
        drawn=evals.drawn,
        panel_selections=tuple(
            _panel_selection(panel.matches, evals.selections) for panel in report.evals
        ),
        players=evals.players,
        environments=evals.environments,
        commits=evals.commits,
        cells=evals.cells,
        points=evals.points,
        coverage=evals.coverage,
        failures=evals.failures,
        latest_result_ms=evals.latest_result_ms,
        absent=evals.absent,
    )
    bounds = storage._split_points(keys) if isinstance(storage, S3Storage) else ()
    _save_cached_page(cache_path, _PageCacheHeader(key=cache_key, split_points=bounds), page)
    return page


def _without_hours(trained: Sequence[RunData]) -> tuple[str, ...]:
    """A note naming the runs whose checkpoints lack Etude's clocks, and why.

    A checkpoint saved before Etude recorded them gets them from its run's
    attempt records (`etude.train.read`); without those it stands nowhere on
    them, so an axis or a seat on one leaves it out.
    """
    lacking = [
        run
        for run in trained
        if any(clock not in saved.at for saved in run.checkpoints for clock in BUILT_IN_NAMES)
    ]
    if not lacking:
        return ()
    unread = [run.name for run in lacking if not run.writers]
    why = (
        f" The attempt records (metrics/WRITER.json) of {', '.join(unread)} are not in this"
        " source to derive them from."
        if unread
        else ""
    )
    return (
        f"No {' or '.join(BUILT_IN_NAMES)} for some checkpoints of"
        f" {', '.join(run.name for run in lacking)}, saved before Etude recorded these clocks:"
        f" charts and seats on them leave those checkpoints out.{why}",
    )


def _run_file(file: Path) -> dict[str, object]:
    """The file's top-level names, run as `etude eval` runs it: it may import its neighbours."""
    if file.suffix != ".py":
        raise EtudeError("experiment_file_must_be_python", 422) from ValueError(file)
    if not file.is_file():
        raise EtudeError("experiment_file_not_found", 404) from FileNotFoundError(file)
    refuse_renamed(file)
    folder = str(file.resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    # A neighbour edited since the last build is imported afresh, as the file itself is run.
    for name, source in _neighbours(file).items():
        # Bytecode timestamps have only second precision; same-size edits within
        # that second still need to load the source the page's inputs detected.
        if source.suffix == ".py":
            Path(cache_from_source(str(source))).unlink(missing_ok=True)
        sys.modules.pop(name, None)
    return runpy.run_path(str(file.resolve()))


def _neighbours(file: Path) -> dict[str, Path]:
    """Imported modules defined in the file's repository, outside hidden folders and Python installs.

    A repository's shared report modules sit beside its experiments' folder, not
    below it, so an edited one is reread too. Etude's own modules are never
    included: the page's records must stay the classes it checks against, even
    when experiments sit in an Etude checkout.
    """
    folder = str(_repository(file)) + "/"
    installed = tuple(
        str(Path(p).resolve()) + "/" for p in {sys.prefix, sys.base_prefix, sys.exec_prefix}
    )
    found: dict[str, Path] = {}
    for name, module in list(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if not isinstance(source, str) or source.startswith(installed):
            continue
        # An editable install's modules may be imported through a symlinked path.
        resolved = str(Path(source).resolve())
        if not resolved.startswith(folder) or resolved.startswith(installed):
            continue
        if name == "etude" or name.startswith("etude."):
            continue
        parts = Path(resolved[len(folder) :]).parts[:-1]
        if not any(p.startswith(".") or p in ("site-packages", "__pycache__") for p in parts):
            found[name] = Path(resolved)
    return found


def _repository(file: Path) -> Path:
    """The nearest folder above the file holding `pyproject.toml` or `.git`, else its own folder."""
    folder = file.resolve().parent
    for candidate in (folder, *folder.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    return folder


def _sources(file: Path) -> tuple[tuple[str, int, int], ...]:
    """What a kept page's files are compared by; a deleted neighbour reads as absent."""
    found = []
    for path in sorted({file.resolve(), *_neighbours(file).values()}):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        found.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(found)


def _report(made: object, file: Path) -> tuple[Report, str | None, tuple[str, ...]]:
    if made is None:
        # Nothing is declared, so the page draws everything logged rather than nothing.
        note = f"{file.name} defines no report(), so this is the default report."
        return Report(other_metrics=True), None, (note,)
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
    selections: tuple[Selection, ...] = ()
    drawn: tuple[Drawn, ...] = ()
    players: tuple[Placement, ...] = ()
    environments: tuple[EnvironmentLabel, ...] = ()
    commits: tuple[Commit, ...] = ()
    cells: tuple[Cell, ...] = ()
    points: tuple[Point, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    failures: tuple[Failure, ...] = ()
    latest_result_ms: int | None = None
    absent: tuple[Absent, ...] = ()
    notes: tuple[str, ...] = ()


def _read_evals(
    storage: Storage,
    state: ExperimentState,
    report: Report,
    wanted: Sequence[Tournament],
    *,
    keys: Sequence[str],
    declarations: Sequence[LocatedRun],
) -> _Evals:
    # The optional `eval` extra (DuckDB, and the etude.eval modules over it), imported where
    # it is used: only an experiment with evaluations reads it, so a report without loads none.
    from etude.eval.query import _open_statsdb  # noqa: PLC0415

    held, refused = _held_tournaments(wanted, state, storage, keys=keys, declarations=declarations)
    db = _open_statsdb(state.experiment, storage, keys=keys)
    try:
        saved = _saved_checkpoints(db)
        players, placement = _players(db, saved, {run.name: run.line for run in state.runs})
        outside = _load(db, held, players)
        commits = _commits(db)
        selections, notes = _selections(held, report, outside=outside)
        environments = _environments(db)
        scores, cells, absent, mixed = _cells(
            db, selections, players, environments, commits, report
        )
        latest = db.sql("select max(wall_ms) from matches").fetchone()
        return _Evals(
            scores=scores,
            selections=tuple(selections),
            drawn=tuple(_drawn(selection, held) for selection in selections),
            players=tuple(players),
            environments=tuple(environments.values()),
            commits=commits,
            cells=tuple(cells),
            points=_points(held, selections, players, saved, cells),
            coverage=_coverage(saved, held),
            failures=_failures(db, held, players),
            latest_result_ms=None if latest is None else latest[0],
            absent=absent,
            notes=(*refused, *placement, *notes, *mixed),
        )
    finally:
        db.close()


@dataclass(frozen=True)
class _HeldTournament:
    """One tournament: every match it calls for, with seating and tags, the held checkpoints
    its relative seats find nothing for, and how far it got."""

    name: str
    tournament: Tournament
    schedule: tuple[Scheduled, ...]
    gaps: tuple[Gap, ...]
    played: Played


def _held_tournaments(
    asked: Sequence[Tournament],
    state: ExperimentState,
    storage: Storage,
    *,
    keys: Sequence[str],
    declarations: Sequence[LocatedRun],
) -> tuple[list[_HeldTournament], tuple[str, ...]]:
    """Each tournament as held so far, and a note for each one drawn otherwise than asked.

    A tournament `eval_jobs(state)` returns is what it wants now; any other the
    experiment's eval runs hold is everything they were held for. Where
    `eval_jobs(state)` now defines one otherwise than its eval runs were held
    with, such as a later revision of its file with another environment
    digest, the page draws what they recorded: reading needs no agreement,
    only scheduling new games (`etude eval`) does.
    """
    # The optional `eval` extra, imported where it is used (`_read_evals`).
    from etude.eval.records import TournamentRun, assemble  # noqa: PLC0415
    from etude.eval.tournament import played, stored_runs  # noqa: PLC0415
    from etude.experiment import read_tournament_lines  # noqa: PLC0415 - optional eval dependency

    wanted: dict[str, Tournament] = {}
    for tournament in asked:
        wanted.setdefault(tournament.definition.name, tournament)
    experiment = state.experiment
    refused: list[str] = []
    # Read once for every tournament drawn: a page plays nothing, so nothing changes meanwhile.
    runs = stored_runs(experiment, storages=[storage], keys=keys, declarations=declarations)
    for name, held in runs.tournaments.items():
        try:
            recorded = assemble(held)
        except EtudeError as error:
            wanted.pop(name, None)
            refused.append(
                f"Tournament {name} is not shown: its eval runs disagree ({error.code})."
            )
            continue
        requested = wanted.get(name)
        if requested is not None:
            try:
                assemble([*held, TournamentRun(tournament=requested.definition)])
                continue
            except EtudeError as error:
                refused.append(
                    f"Tournament {name} is drawn as its eval runs recorded it:"
                    f" eval_jobs(state) now defines it otherwise ({error.code})."
                )
        wanted[name] = recorded
    lines = read_tournament_lines(
        storage,
        state,
        [
            TournamentRun(tournament=t.definition, checkpoints=t.checkpoints)
            for t in wanted.values()
        ],
        keys=keys,
        declarations=declarations,
    )
    held_tournaments = []
    for name, given in sorted(wanted.items()):
        try:
            found = played(
                given.definition,
                experiment=experiment,
                checkpoints=given.checkpoints,
                enter=given.entrants,
                lines=lines,
                runs=runs,
            )
        except EtudeError as error:
            refused.append(f"Tournament {name} is not shown: it is refused ({error.code}).")
            continue
        tournament, gaps = given.resolve(lines, experiment=state.experiment)
        held_tournaments.append(
            _HeldTournament(name, tournament, tuple(tournament.schedule()), gaps, found)
        )
    return held_tournaments, tuple(refused)


def _wanted(want: Callable[..., object], state: ExperimentState) -> list[Tournament]:
    """The tournaments `eval_jobs(state)` returns, each with the checkpoints or entrants it holds."""
    # The optional `eval` extra, imported where it is used (`_read_evals`): eval_jobs(state)
    # and its evaluation jobs are built on etude.eval.
    from etude.controller.declarations import asked  # noqa: PLC0415
    from etude.eval.records import Tournament  # noqa: PLC0415

    wanted = []
    for job in asked(want, state):
        recorded = job.recorded()
        wanted.append(
            Tournament(
                definition=recorded.tournament,
                checkpoints=recorded.checkpoints,
                entrants=recorded.entrants,
            )
        )
    return wanted


def _players(
    db: duckdb.DuckDBPyConnection, saved: dict[str, _Saved], lines: dict[str, str]
) -> tuple[list[Placement], tuple[str, ...]]:
    """Every player seated, placed by its seat, else by the training run that saved it;
    and notes on what could not be placed as recorded.

    `lines` gives each training run's line. Where seat and training record
    differ on a clock, the seat's value is drawn: it is what the evaluation
    declared. Where seats disagree, on an `at` or a line, the latest is
    drawn, so a later evaluation corrects a wrong one.
    """
    found = db.sql(
        "select player, names, kind, config, checkpoint, seated_at, lines"
        " from players order by player"
    ).fetchall()
    players: list[Placement] = []
    notes: list[str] = []
    unplaced: list[str] = []
    for key, names, kind, config, checkpoint, given, named in found:
        name = "/".join(json.loads(names))
        bound = saved.get(checkpoint)
        recorded = {} if bound is None else bound.at
        seated_at: list[dict[str, float]] = json.loads(given)
        seated = _merged(seated_at)
        if _disagree(seated_at):
            earlier = "; ".join(_described(each) for each in seated_at if each != seated)
            notes.append(
                f"{name} is drawn at {_described(seated)}, as its latest seat says;"
                f" earlier seats said {earlier}."
            )
        seat_lines: list[str] = json.loads(named)
        line = seat_lines[0] if seat_lines else None
        if len(seat_lines) > 1:
            notes.append(
                f"{name} follows {line}, as its latest seat says;"
                f" earlier seats said {', '.join(seat_lines[1:])}."
            )
        for clock in sorted(seated.keys() & recorded.keys()):
            if seated[clock] != recorded[clock]:
                notes.append(
                    f"{name}: seated at {clock} {seated[clock]:g};"
                    f" its training record says {recorded[clock]:g}."
                )
        if (
            kind in {"model", "composition"}
            and checkpoint is not None
            and bound is None
            and not seated
        ):
            unplaced.append(name)
        at = recorded | seated
        players.append(
            Placement(
                key=key,
                name=name,
                run=None if bound is None else bound.run,
                ordinal=None if bound is None else bound.ordinal,
                config=json.dumps(json.loads(config or "{}"), sort_keys=True),
                at=at,
                # A run read after the training runs were is its own line until the next build.
                line=line if line is not None or bound is None else lines.get(bound.run, bound.run),
                placed=bound is not None or bool(at),
            )
        )
    if unplaced:
        notes.append(
            f"Drawn as anchors, seated without an `at` and saved by no training run of the"
            f" experiment: {', '.join(unplaced)}."
        )
    return players, tuple(notes)


def _merged(seated_at: list[dict[str, float]]) -> dict[str, float]:
    """Where seats put a player, latest first: their `at`s merged, the latest winning, so a
    match recorded before Etude's built-in hour clocks agrees with one that also says where
    they stood."""
    merged: dict[str, float] = {}
    for at in reversed(seated_at):
        merged.update(at)
    return merged


def _disagree(seated_at: list[dict[str, float]]) -> bool:
    """Whether two `at`s give a clock they both name different values."""
    seen: dict[str, float] = {}
    for at in seated_at:
        if any(seen.get(clock, value) != value for clock, value in at.items()):
            return True
        seen.update(at)
    return False


def _described(at: dict[str, float]) -> str:
    return ", ".join(f"{clock} {value:g}" for clock, value in sorted(at.items()))


@dataclass(frozen=True)
class _Saved:
    """The training run's checkpoint a key names, and where the run's clocks stood."""

    run: str
    ordinal: int
    at: dict[str, float]


def _saved_checkpoints(db: duckdb.DuckDBPyConnection) -> dict[str, _Saved]:
    """Each checkpoint key, encoded, to the training run's checkpoint that saved it.

    A run that saved two ordinals under one file name holds one key; it is
    bound to the later.
    """
    found = db.sql(
        'select checkpoint, run, max(ordinal), arg_max("at", ordinal) from training_checkpoints'
        " group by checkpoint, run"
    ).fetchall()
    return {key: _Saved(run, ordinal, json.loads(at)) for key, run, ordinal, at in found}


@dataclass(frozen=True, order=True)
class _HeldFor:
    """A seating of a tournament held for one checkpoint, by the checkpoint's key, encoded."""

    tournament: str
    seating: str
    checkpoint: str


def _points(
    held: Sequence[_HeldTournament],
    selections: Sequence[Selection],
    players: Sequence[Placement],
    saved: dict[str, _Saved],
    cells: Sequence[Cell],
) -> tuple[Point, ...]:
    """Each selection of seatings' points: per checkpoint a seating was held for, its first
    seat's score over the matches held for it, or why its relative seat found no checkpoint.
    A fixed seating is held for its first seat's checkpoint, so a fixed pair is drawn once,
    where that seat stood; with code in that seat it is not drawn as seated.

    A checkpoint's matches are those the selection's seatings call for it,
    by their first two seats, each pair once whichever tournaments hold it;
    their cells are the selection's own, so each view, score and split has
    its point. Several pairs, such as each line of a job, are their mean.
    """
    # The optional `eval` extra, as in `_read_evals`.
    from etude.eval.records import Benchmark, player_checkpoint  # noqa: PLC0415

    index = {each.key: i for i, each in enumerate(players)}
    refs = {str(ref.key): ref for each in held for ref in each.tournament.checkpoints}
    pairs: dict[_HeldFor, set[tuple[int, int]]] = {}
    for each in held:
        for called in each.schedule:
            if called.seating is None:
                continue
            held_for = called.checkpoint
            if held_for is None:
                leaf = player_checkpoint(called.match.players[0])
                if leaf is None:
                    continue
                held_for = leaf.key
                refs.setdefault(str(held_for), leaf)
            seated = pairs.setdefault(_HeldFor(each.name, called.seating, str(held_for)), set())
            # A player no eval run seated yet has played nothing to draw.
            first, second = (index.get(p.key) for p in called.match.players[:2])
            if first is not None and second is not None:
                seated.add((first, second))
    gaps = {
        _HeldFor(each.name, gap.seating, str(gap.checkpoint)): gap
        for each in held
        for gap in each.gaps
    }
    one_seed = {
        (each.name, seating.name)
        for each in held
        if isinstance(each.tournament.definition, Benchmark)
        for seating in each.tournament.definition.seatings
        if seating.seeds == "one"
    }
    scored: dict[tuple[int, int, int], list[tuple[Cell, str]]] = {}
    for cell in cells:
        if cell.opponent is not None and not cell.fixed:
            scored.setdefault((cell.selection, cell.subject, cell.opponent), []).append(
                (cell, json.dumps(cell.split, sort_keys=True))
            )
    by_seating: dict[str, list[_HeldFor]] = {}
    for key in pairs.keys() | gaps.keys():
        by_seating.setdefault(key.seating, []).append(key)
    points: list[Point] = []
    for number, selection in enumerate(selections):
        if not _seated(selection):
            continue
        parts = _parts(selection)
        matched: set[_HeldFor] = set()
        for part in parts:
            if part.seating is None:
                continue
            for key in by_seating.get(part.seating, ()):
                if _holds(part, key.tournament, key.seating):
                    matched.add(key)
        chosen: dict[str, list[_HeldFor]] = {}
        for key in sorted(matched):
            chosen.setdefault(key.checkpoint, []).append(key)
        for checkpoint, here in chosen.items():
            ref, place = refs[checkpoint], saved.get(checkpoint)
            stood = Point(
                selection=number,
                run=None if place is None else place.run,
                ordinal=None if place is None else place.ordinal,
                line=ref.line,
                at=ref.at or ({} if place is None else place.at),
                whole=any((key.tournament, key.seating) in one_seed for key in here),
            )
            # By view, score and split: the cells of this checkpoint's pairs.
            met: dict[tuple[str | None, int, str], list[Cell]] = {}
            for subject, opponent in sorted({pair for key in here for pair in pairs.get(key, ())}):
                for cell, split_key in scored.get((number, subject, opponent), ()):
                    met.setdefault((cell.commit, cell.score, split_key), []).append(cell)
            for counted in met.values():
                points.append(
                    stood.model_copy(
                        update={
                            "commit": counted[0].commit,
                            "score": counted[0].score,
                            "split": counted[0].split,
                            "value": sum(cell.value for cell in counted) / len(counted),
                            "interval": counted[0].interval if len(counted) == 1 else None,
                            "rows": sum(cell.rows for cell in counted),
                            "matches": sum(cell.matches for cell in counted),
                            "opponents": tuple(cell.opponent for cell in counted),
                        }
                    )
                )
            gap = next((gaps[key] for key in here if key in gaps), None)
            if gap is not None:
                points.append(stood.model_copy(update={"gap": gap}))
    return tuple(points)


def _parts(selection: Selection) -> tuple[Matches, ...]:
    """What a selection names, as panels name matches: none for the matches outside."""
    if isinstance(selection, OutsideTournaments):
        return ()
    return selection if isinstance(selection, tuple) else (selection,)


def _seated(selection: Selection) -> bool:
    """Whether every part of a selection names a seating, so it is drawn as seated."""
    parts = _parts(selection)
    return bool(parts) and all(part.seating is not None for part in parts)


def _holds(part: Matches, tournament: str, seating: str) -> bool:
    """Whether a selection's part counts what `tournament` called for as `seating`."""
    return part.seating == seating and part.tournament in (None, tournament)


def _drawn(selection: Selection, held: Sequence[_HeldTournament]) -> Drawn:
    """How a selection is drawn: as seated or not, and the tournaments its matches come from."""
    parts = _parts(selection)
    tournaments = [
        each.name
        for each in held
        if any(
            part.tournament in (None, each.name)
            and (part.seating is None or any(c.seating == part.seating for c in each.schedule))
            and (part.tag is None or any(part.tag in c.tags for c in each.schedule))
            for part in parts
        )
    ]
    return Drawn(seated=_seated(selection), tournaments=tuple(tournaments))


def _panel_selection(
    wanted: Matches | tuple[Matches, ...] | None, selections: Sequence[Selection]
) -> PanelSelection:
    """The selection a panel naming `wanted` draws: the control's with none, or naming
    no part; else the one `_selections` made for it, or the one its parts fit."""
    if wanted is None or wanted == Matches():
        return PanelSelection(control=True)
    if wanted in selections:
        return PanelSelection(selection=selections.index(wanted))
    if isinstance(wanted, tuple):
        return PanelSelection()
    fits = next(
        (
            i
            for i, s in enumerate(selections)
            if isinstance(s, Matches)
            and wanted.tournament in (None, s.tournament)
            and (s.seating, s.tag) == (wanted.seating, wanted.tag)
        ),
        None,
    )
    return PanelSelection(selection=fits)


def _load(
    db: duckdb.DuckDBPyConnection, held: Sequence[_HeldTournament], players: Sequence[Placement]
) -> bool:
    """Temp tables `compare` selects rows by; whether any match was held outside a tournament.

    `_page_held` (view, tournament, run, match): for view '', each delivered
    match and the eval run whose rows count, under tournament '' for those an
    eval run held outside any (`OutsideTournaments`); for a commit, each match
    as played at it (`_commits`).
    `_page_scheduled` (tournament, match, seating, tag): what each match was
    called for as. `_page_checkpoints` (player): the placed models.
    """
    # The optional `eval` extra, imported where it is used (`_read_evals`).
    from etude.eval.block import stage  # noqa: PLC0415

    stage(
        db,
        "_page_held",
        {"view": "VARCHAR", "tournament": "VARCHAR", "run": "VARCHAR", "match": "VARCHAR"},
        [["", each.name, d.run, d.match.key] for each in held for d in each.played.delivered],
    )
    stage(
        db,
        "_page_scheduled",
        {"tournament": "VARCHAR", "match": "VARCHAR", "seating": "VARCHAR", "tag": "VARCHAR"},
        [
            [each.name, called.match.key, called.seating, tag]
            for each in held
            for called in each.schedule
            for tag in (called.tags or (None,))
        ],
    )
    stage(
        db,
        "_page_checkpoints",
        {"player": "VARCHAR"},
        [[each.key] for each in players if each.placed],
    )
    # Each match an eval run delivered outside a tournament, once per environment from its
    # latest record (`matches.newest`), as a tournament counts its own.
    db.execute(
        """
        insert into _page_held
        select '', '', m.run, m.match from matches m join runs r using (run)
        where m.newest = 1 and (r.environment, m.match) in (
            select e.environment, d.match from matches d join runs e using (run)
            where d.state = 'done' and e.tournament is null
        )
        """
    )
    found = db.sql("select count(*) from runs where tournament is null").fetchone()
    return found is not None and found[0] > 0


def _commits(db: duckdb.DuckDBPyConnection) -> tuple[Commit, ...]:
    """Each commit's view in `_page_held`, where the delivered matches were played at several.

    A commit's view holds each delivered match some done record seats it
    in, from the latest such eval run in each environment the latest view
    holds, so it never pools environments the latest view keeps apart.
    """
    db.execute(
        """
        insert into _page_held
        select commit, tournament, run, match from (
            select s.commit, h.tournament, m.run, m.match, row_number() over (
                partition by s.commit, h.tournament, r.environment, m.match order by m.newest
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
        "select view, count(distinct (h.tournament, r.environment, h.match))"
        " from _page_held h join runs r using (run)"
        " where view != '' group by view order by view"
    ).fetchall()
    if len(found) < 2:
        db.execute("delete from _page_held where view != ''")
        return ()
    return tuple(Commit(commit=commit, matches=matches) for commit, matches in found)


@dataclass(frozen=True)
class _HeldIndex:
    """In-memory index of `_page_held` and `_page_scheduled` for partitioning compare rows."""

    match_rank: dict[tuple[str, str], tuple[str, int]]
    by_view_tournament: dict[tuple[str, str], set[tuple[str, str]]]
    by_tournament_seating: dict[tuple[str, str], set[str]]
    by_tournament_tag: dict[tuple[str, str], set[str]]
    checkpoint_matches: set[str]

    def newest_once(self, pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        """Each match in `pairs` once per environment, from its newest delivery (`matches.newest`)."""
        best: dict[tuple[str, str], tuple[int, str]] = {}
        for run, match in pairs:
            environment, newest = self.match_rank[(run, match)]
            key = (environment, match)
            prior = best.get(key)
            if prior is None or newest < prior[0]:
                best[key] = (newest, run)
        return {(run, match) for (_, match), (_, run) in best.items()}

    def matches(self, selection: Selection, *, view: str) -> set[tuple[str, str]]:
        """The `(run, match)` pairs a selection counts in `view`."""
        if isinstance(selection, tuple):
            unioned: set[tuple[str, str]] = set()
            for part in selection:
                unioned.update(self.matches(part, view=view))
            return self.newest_once(unioned)
        if isinstance(selection, OutsideTournaments):
            return set(self.by_view_tournament.get((view, ""), ()))
        if selection.tournament is None:
            pairs = {
                pair
                for held_view, tournament in self.by_view_tournament
                if held_view == view and tournament
                for pair in self._selected(tournament, selection, view)
            }
            return self.newest_once(pairs)
        return self._selected(selection.tournament, selection, view)

    def _selected(self, tournament: str, selection: Matches, view: str) -> set[tuple[str, str]]:
        pairs = self.by_view_tournament.get((view, tournament), set())
        if selection.seating is not None:
            allowed = self.by_tournament_seating.get((tournament, selection.seating), set())
        elif selection.tag is not None:
            allowed = self.by_tournament_tag.get((tournament, selection.tag), set())
        else:
            return pairs
        return {pair for pair in pairs if pair[1] in allowed}


def _held_index(db: duckdb.DuckDBPyConnection) -> _HeldIndex:
    match_rank: dict[tuple[str, str], tuple[str, int]] = {}
    by_view_tournament: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for view, tournament, run, match, environment, newest in db.sql(
        "select h.view, h.tournament, h.run, h.match, r.environment, m.newest"
        " from _page_held h join runs r using (run) join matches m using (run, match)"
    ).fetchall():
        pair = (str(run), str(match))
        match_rank[pair] = (str(environment), int(newest) if newest is not None else sys.maxsize)
        by_view_tournament.setdefault((str(view), str(tournament)), set()).add(pair)
    by_tournament_seating: dict[tuple[str, str], set[str]] = {}
    by_tournament_tag: dict[tuple[str, str], set[str]] = {}
    for tournament, match, seating, tag in db.sql(
        "select tournament, match, seating, tag from _page_scheduled"
    ).fetchall():
        t_name, m_key = str(tournament), str(match)
        if seating is not None:
            by_tournament_seating.setdefault((t_name, str(seating)), set()).add(m_key)
        if tag is not None:
            by_tournament_tag.setdefault((t_name, str(tag)), set()).add(m_key)
    checkpoint_matches = {
        str(match)
        for (match,) in db.sql(
            "select distinct match from seats where player in (select player from _page_checkpoints)"
        ).fetchall()
    }
    return _HeldIndex(
        match_rank=match_rank,
        by_view_tournament=by_view_tournament,
        by_tournament_seating=by_tournament_seating,
        by_tournament_tag=by_tournament_tag,
        checkpoint_matches=checkpoint_matches,
    )


def _selections(
    held: Sequence[_HeldTournament], report: Report, *, outside: bool
) -> tuple[list[Selection], tuple[str, ...]]:
    """Each tournament, the matches outside one, each tag and each seating a panel names;
    and what is amiss."""
    tournaments = [each.name for each in held]
    tags = sorted({(each.name, tag) for each in held for c in each.schedule for tag in c.tags})
    seatings = sorted(
        {(each.name, c.seating) for each in held for c in each.schedule if c.seating}
        | {(each.name, gap.seating) for each in held for gap in each.gaps}
    )
    selections: list[Selection] = [Matches(tournament=name) for name in tournaments]
    selections += [OutsideTournaments()] if outside else []
    selections += [Matches(tournament=t, tag=tag) for t, tag in tags]
    notes: list[str] = []
    requested = [
        (chosen, f"{type(panel).__name__} panel")
        for panel in report.evals
        if panel.matches is not None
        for chosen in (panel.matches if isinstance(panel.matches, tuple) else (panel.matches,))
    ]
    for chosen, title in requested:
        if chosen.tournament is not None and chosen.tournament not in tournaments:
            notes.append(f"A {title} names tournament {chosen.tournament!r}, which nothing holds.")
        if chosen.tag is not None and not any(
            tag == chosen.tag and chosen.tournament in (None, t) for t, tag in tags
        ):
            notes.append(f"A {title} names tag {chosen.tag!r}, which no seating has.")
            continue
        if chosen.seating is not None and not any(
            m == chosen.seating and chosen.tournament in (None, t) for t, m in seatings
        ):
            notes.append(f"A {title} names seating {chosen.seating!r}, which nothing holds.")
            continue
        # A seating or tag named without a tournament covers every tournament holding it.
        if (chosen.seating, chosen.tag) != (None, None) and chosen not in selections:
            selections.append(chosen)
    # Union selections follow their components so result-aware resolution can use
    # the component cells. SQL unions rows before compare computes any statistic.
    selections += list(
        dict.fromkeys(panel.matches for panel in report.evals if isinstance(panel.matches, tuple))
    )
    return selections, tuple(notes)


def _resolve_matches(
    wanted: Matches, selections: Sequence[Selection], present: set[int]
) -> Matches | None:
    """The selection a union's part counts: itself where the page made it, else one its
    names fit, preferring one with values in this view and score (`present`)."""
    if wanted in selections:
        return wanted
    candidates = [
        (i, s)
        for i, s in enumerate(selections)
        if isinstance(s, Matches)
        and (wanted.tournament is None or s.tournament == wanted.tournament)
        and s.tag == wanted.tag
        and s.seating == wanted.seating
    ]
    return next(
        (s for i, s in candidates if i in present), candidates[0][1] if candidates else None
    )


def _environments(db: duckdb.DuckDBPyConnection) -> dict[str, EnvironmentLabel]:
    """Each environment the eval runs played in, by identity, the newest latest match first."""
    found = db.sql(
        "select r.environment, any_value(r.environment_name), any_value(r.environment_config),"
        " max(m.wall_ms) as latest from runs r left join matches m using (run)"
        " group by r.environment order by latest desc nulls last, r.environment"
    ).fetchall()
    return {
        identity: EnvironmentLabel(name=name, config=json.loads(config or "{}"))
        for identity, name, config, _ in found
    }


def _cells(
    db: duckdb.DuckDBPyConnection,
    selections: Sequence[Selection],
    players: Sequence[Placement],
    environments: dict[str, EnvironmentLabel],
    commits: Sequence[Commit],
    report: Report,
) -> tuple[tuple[Score, ...], list[Cell], tuple[Absent, ...], tuple[str, ...]]:
    """Every value the eval panels may show, the scores offered, absent columns, and a note
    for each environment whose values another, newer one hides.

    `environments` are by identity, the newest latest match first.
    """
    # The optional `eval` extra, imported where it is used (`_read_evals`).
    from etude.eval.query import (  # noqa: PLC0415
        KEYS,
        NUMERIC,
        _match_scores,
    )
    from etude.eval.run import ETUDE_TABLES  # noqa: PLC0415

    # A rollup's totals have no matches to compare over.
    rollups = {name for (name,) in db.sql("select name from kept.rollups").fetchall()}
    columns: dict[str, dict[str, str]] = {
        table: {name: kind for name, kind, *_ in db.sql(f'describe "{table}"').fetchall()}
        for (table,) in db.sql("show tables").fetchall()
        if table not in ETUDE_TABLES and table not in rollups
    }
    scores = report.scores(
        tuple(
            Score(table=table, value=name)
            for table in sorted(columns)
            for name, kind in columns[table].items()
            if kind in NUMERIC and name not in KEYS
        )
    )
    index = {each.key: number for number, each in enumerate(players)}
    anchors = sum(not each.placed for each in players)
    place = {identity: number for number, identity in enumerate(environments)}
    held_idx = _held_index(db)
    has_groups = db.sql('select 1 from "groups" limit 1').fetchone() is not None
    views = (None, *(each.commit for each in commits))
    # Cache non-union selection match sets once across all scores and splits.
    selection_sets: dict[tuple[str, int], set[tuple[str, str]]] = {
        (commit or "", chosen): held_idx.matches(selection, view=commit or "")
        for commit in views
        for chosen, selection in enumerate(selections)
        if not isinstance(selection, tuple)
    }
    cells: list[Cell] = []
    absent: list[Absent] = []
    teams: set[str] = set()
    held_where = "(run, match) in (select distinct run, match from _page_held)"
    for number, score in enumerate(scores):
        held = columns.get(score.table, {})
        if missing := [c for c in (score.value, score.per) if c is not None and c not in held]:
            absent.extend(Absent(table=score.table, column=c) for c in missing)
            continue
        named = [c for c in report.columns() if c in held]
        absent += [Absent(table=score.table, column=c) for c in report.columns() if c not in held]
        splits = [
            by for size in range(len(named) + 1) for by in itertools.combinations(named, size)
        ]
        # Fetch per-match sufficient stats once per (score, split), then partition in memory.
        split_rows = {
            by: _match_scores(
                db,
                table=score.table,
                value=score.value,
                per=score.per,
                where=held_where,
                by=by,
                has_groups=has_groups,
            )
            for by in splits
            if held_idx.match_rank
        }
        for commit in views:
            view = commit or ""
            # Components precede unions; only this view's results resolve them.
            recorded: set[int] = set()
            for chosen, selection in enumerate(selections):
                if isinstance(selection, tuple):
                    resolved_selection = tuple(
                        resolved
                        for wanted in selection
                        if (resolved := _resolve_matches(wanted, selections, recorded)) is not None
                    )
                    active_matches = held_idx.matches(resolved_selection, view=view)
                else:
                    active_matches = selection_sets[(view, chosen)]
                if not active_matches:
                    continue
                # An anchor's strength needs two anchors to have met.
                for fixed in (False, True) if anchors > 1 else (False,):
                    pairs = (
                        {
                            pair
                            for pair in active_matches
                            if pair[1] not in held_idx.checkpoint_matches
                        }
                        if fixed
                        else active_matches
                    )
                    for scores_by_match in split_rows.values():
                        for found in scores_by_match.comparisons(pairs, against_all=fixed):
                            if len(found.subject) != 1 or (
                                found.opponent and len(found.opponent) != 1
                            ):
                                teams.add(f"{score.table}.{score.value}")
                                continue
                            recorded.add(chosen)
                            cells.append(
                                Cell(
                                    selection=chosen,
                                    commit=commit,
                                    environment=place[found.environment],
                                    score=number,
                                    subject=index[found.subject[0]],
                                    opponent=None
                                    if found.opponent is None
                                    else index[found.opponent[0]],
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
    notes = (
        (
            f"Team results for {', '.join(sorted(teams))} are recorded, "
            "but report charts only draw individual players. "
            "Read the team scores with etude.eval.query.statsdb or compare.",
        )
        if teams
        else ()
    )
    return scores, kept, tuple(dict.fromkeys(absent)), (*mixed, *notes)


def _newest_environment(
    cells: Sequence[Cell], environments: Sequence[EnvironmentLabel]
) -> tuple[list[Cell], tuple[str, ...]]:
    """Of a value's cells in several environments, the newest environment's; and a
    note per environment that loses any. Environments are never pooled."""
    # A lower index is a newer environment.
    keyed = [(_without_environment(cell), cell) for cell in cells]
    newest: dict[tuple[object, ...], int] = {}
    for key, cell in keyed:
        newest[key] = min(cell.environment, newest.get(key, cell.environment))
    kept = [cell for key, cell in keyed if cell.environment == newest[key]]
    hidden = Counter(cell.environment for key, cell in keyed if cell.environment != newest[key])
    return kept, tuple(
        f"{count} values were also played in {_environment_name(environments[number])};"
        " the page shows each from the environment whose latest match is newest."
        for number, count in sorted(hidden.items())
    )


def _without_environment(cell: Cell) -> tuple[object, ...]:
    """A cell's identity, whatever environment it was played in."""
    return (
        cell.selection, cell.commit, cell.score, cell.subject, cell.opponent, cell.fixed,
        json.dumps(cell.split, sort_keys=True),
    )  # fmt: skip


def _environment_name(environment: EnvironmentLabel) -> str:
    if not environment.config:
        return environment.name
    return f"{environment.name} {json.dumps(environment.config, sort_keys=True)}"


def _met(cell: Cell) -> tuple[int, str | None, int, int]:
    """The opponents a subject met are counted within one selection, view and score."""
    return (cell.selection, cell.commit, cell.score, cell.subject)


@dataclass(frozen=True, order=True)
class _CheckpointIn:
    """A training run's checkpoint in one tournament."""

    run: str
    ordinal: int
    tournament: str


def _coverage(saved: dict[str, _Saved], held: Sequence[_HeldTournament]) -> tuple[Coverage, ...]:
    """Each checkpoint's matches in each tournament: played, failed since, and not yet played."""
    # Report entry point: evaluation imports stay out of non-evaluation pages.
    from etude.eval.records import player_checkpoints  # noqa: PLC0415

    played: Counter[_CheckpointIn] = Counter()
    failed: Counter[_CheckpointIn] = Counter()
    unplayed: Counter[_CheckpointIn] = Counter()
    for each in held:
        for counts, matches in (
            (played, [d.match for d in each.played.delivered]),
            (failed, each.played.failed),
            (unplayed, each.played.new),
        ):
            for match in matches:
                keys = {str(c.key) for player in match.players for c in player_checkpoints(player)}
                for key in keys & saved.keys():
                    counts[_CheckpointIn(saved[key].run, saved[key].ordinal, each.name)] += 1
    return tuple(
        Coverage(
            run=key.run,
            ordinal=key.ordinal,
            tournament=key.tournament,
            played=played[key],
            failed=failed[key],
            unplayed=unplayed[key],
        )
        for key in sorted(played.keys() | failed.keys() | unplayed.keys())
    )


def _failures(
    db: duckdb.DuckDBPyConnection, held: Sequence[_HeldTournament], players: Sequence[Placement]
) -> tuple[Failure, ...]:
    """Each failed match `played` counts, and each outside a tournament that no eval run has
    played since in its environment, with the error its eval run recorded."""
    index = {each.key: number for number, each in enumerate(players)}
    errors = dict(
        db.sql(
            "select match, max(error) from matches where state = 'failed' group by match"
        ).fetchall()
    )
    in_tournaments = tuple(
        Failure(
            tournament=each.name,
            players=tuple(index[seat.key] for seat in match.players),
            error=errors.get(match.key),
        )
        for each in held
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
    return in_tournaments + tuple(
        Failure(tournament=None, players=tuple(index[key] for key in seated), error=error)
        for seated, error in unique.values()
    )


def render(page: Page, *, refresh: float | None = None, served: bool = False) -> str:
    """The page as one HTML file; `refresh` is the seconds between the open page reloading."""
    return _inline("page.html", page.experiment, page.model_dump(mode="json"), refresh, served)


def render_index(found: Index, *, refresh: float | None = None, served: bool = False) -> str:
    """The index as one HTML file, like a page."""
    return _inline("index.html", "Experiments", found.model_dump(mode="json"), refresh, served)


def _inline(
    template: str, title: str, data: dict[str, object], refresh: float | None, served: bool
) -> str:
    here = Path(__file__).parent
    stamp = {"generated_ms": time.time_ns() // 1_000_000, "refresh": refresh, "served": served}
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
    experiments at `/STEM`. Views reuse HTML; explicit refreshes join one generation.

    It has no login: keep it on this computer unless told otherwise.
    """

    def __init__(
        self,
        source: Storage | Path | str,
        path: Path,
        address: tuple[str, int],
        *,
        refresh: float | None = None,
        experiment: str | None = None,
    ) -> None:
        super().__init__(address, _Handler)
        self.pages, self.path, self.refresh = Pages(source, experiment=experiment), path, refresh
        # Requests are handled on threads; a page is built by one at a time.
        self._building = threading.Lock()
        self._lock = threading.Lock()
        self._html: dict[str, str] = {}
        self._pending: dict[str, Future[str | None]] = {}

    def page(self, route: str = "/", *, refresh: bool = False) -> str | None:
        """Reuse a generated page, or join its refresh. A failure preserves the last HTML."""
        with self._lock:
            if not refresh and route in self._html:
                return self._html[route]
            pending = self._pending.get(route)
            owner = pending is None
            if pending is None:
                pending = Future()
                self._pending[route] = pending
        if owner:
            try:
                # Pages shares imported experiment files and input state across routes.
                with self._building:
                    html = self._render(route)
                with self._lock:
                    if html is not None:
                        self._html[route] = html
                    pending.set_result(html)
                    del self._pending[route]
            except BaseException as error:
                with self._lock:
                    pending.set_exception(error)
                    del self._pending[route]
        return pending.result()

    def _render(self, route: str) -> str | None:
        if not self.path.is_dir():
            if route != "/":
                return None
            return render(self.pages.build(self.path), refresh=self.refresh, served=True)
        if route == "/":
            built = self.pages.build_each(self.path)
            # Index facts and links must describe the same generation. Keep a
            # failed detail's last successful page, while its index row says why.
            with self._lock:
                for file, page in built.items():
                    if isinstance(page, Page):
                        self._html[f"/{file.stem}"] = render(
                            page, refresh=self.refresh, served=True
                        )
            found = index(self.pages.storage, built, href=lambda stem: f"/{stem}")
            return render_index(found, refresh=self.refresh, served=True)
        for file in self.pages.find_files(self.path):
            if route == f"/{file.stem}":
                return render(self.pages.build(file), refresh=self.refresh, served=True)
        return None


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        self._respond(refresh=False)

    def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
        self._respond(refresh=True)

    def _respond(self, *, refresh: bool) -> None:
        try:
            served = self.server.page(self.path.split("?", 1)[0], refresh=refresh)
            found, body = served is not None, (served or "Not found.").encode()
            status = 200 if found else 404
        except ValueError as error:  # no runs of the experiment yet
            found, body, status = False, explain(error).encode(), 404
        except Exception as error:
            found, body, status = False, explain(error).encode(), 500
        self.send_response(status)
        self.send_header("Content-Type", f"text/{'html' if found else 'plain'}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Requests are not printed; the command's own output stays readable."""
