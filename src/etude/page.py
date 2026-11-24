"""Build and render an experiment report or a folder's printable index.

    storage = storage_from_uri("r2://bucket/runs")
    page = build(storage, Path("experiments/ppo.py"))
    write(render(page), target(storage, "ppo"))  # ./reports/ppo.html
    pages = Pages(storage).build_each(Path("experiments"))  # every experiment file there
    write(render_index(index(storage, pages)), target(storage, "index"))

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
import re
import runpy
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache, cached_property
from html import escape
from importlib.util import cache_from_source
from pathlib import Path
from statistics import fmean
from typing import TYPE_CHECKING, Literal

from pydantic import JsonValue, ValidationError

from etude.errors import EtudeError, explain
from etude.eval.records import Gap, Scheduled, Tournament, find_line_experiments
from etude.experiment_file import experiment_named, refuse_renamed
from etude.home import home
from etude.identity import SAFE_NAME
from etude.records import BUILT_IN_NAMES, Record
from etude.report import (
    Counters,
    Curve,
    Elapsed,
    Flame,
    Include,
    Matches,
    MetricPanel,
    Pace,
    Report,
    Score,
    Series,
)
from etude.run import DeclarationCache, RunListing, as_storage, under
from etude.storage import (
    DirectoryStorage,
    S3Storage,
    Storage,
    default_root,
    store_identity,
)
from etude.train.read import RunData, read_all, read_located
from etude.train.report import BUCKETS, Trace, Training, training

if TYPE_CHECKING:
    import duckdb

    from etude.eval.tournament import Played
    from etude.jobs import ExperimentState


# An authored file and its sibling imports share sys.modules across request threads.
_author_imports = threading.RLock()
DEFAULT_REPORT = Report(other_metrics=True)
type SourceVersions = tuple[tuple[str, int, int], ...]


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

    # A selective report need only retain one summary from outside its named panels.
    ungrouped_metric: str | None = None

    def _primary_curve(self) -> int | None:
        curves = [(i, p) for i, p in enumerate(self.report.evals) if isinstance(p, Curve)]
        return next((i for i, p in curves if p.score is None), curves[0][0] if curves else None)

    def _curve_score(self, curve: int | None) -> int:
        panel = self.report.evals[curve] if curve is not None else None
        return self.scores.index(panel.score) if isinstance(panel, Curve) and panel.score else 0

    def _report_clock(self) -> str:
        clocks = [c.name for c in self.training.clocks]
        return self.report.clock if self.report.clock in clocks else clocks[0]

    def headline(self) -> str | None:
        """Latest evaluated value per job or external line, else a training metric per run."""
        curve = self._primary_curve()
        if self.scores and curve is not None:
            score = self._curve_score(curve)
            resolved = self.panel_selections[curve]
            selection = resolved.selection
            if resolved.control:
                selection = next(
                    (
                        i
                        for i, s in enumerate(self.selections)
                        if isinstance(s, OutsideTournaments)
                        or isinstance(s, Matches)
                        and s.seating is None
                    ),
                    None,
                )
            if selection is not None and (values := _latest_evaluated(self, score, selection)):
                measure = self.scores[score]
                return f"{measure.table}.{measure.value} {_value_range(values)}"

        if self.training.clocks:
            clock = self._report_clock()
            measured = self.training.series.get(clock, {})
            for panel in _preview_panels(self.report, self.training, clock):
                if isinstance(panel, Series):
                    for key in panel.keys:
                        values = [
                            value
                            for trace in measured.get(key, ())
                            if (value := trace.latest(log=panel.scale == "log")) is not None
                        ]
                        if values:
                            return f"{key} {_value_range(values)} {panel.unit}".rstrip()
        return self.ungrouped_metric


def _value_range(values: Sequence[float]) -> str:
    low, high = min(values), max(values)
    return f"{low:.4g}" if low == high else f"{low:.4g}–{high:.4g}"


type _MarkKey = tuple[str, int] | tuple[str, tuple[tuple[str, float], ...]]


def _mark_key(player: Placement | Point) -> _MarkKey:
    if player.run is not None and player.ordinal is not None:
        return player.run, player.ordinal
    return player.line or "", tuple(sorted(player.at.items()))


@dataclass(frozen=True)
class _Evaluated:
    value: float
    whole: bool = False
    config: str | None = None


def _latest_evaluated(page: Page, score: int, selection: int) -> list[float]:
    """Apply the default curve selection, aligning seed checkpoints before taking the latest."""
    external = [p for p in page.players if p.placed and p.run is None]
    clocks = dict.fromkeys(k for p in external for k in p.at)
    clock = next(
        (c.name for c in page.training.clocks if c.name not in BUILT_IN_NAMES),
        page.report.clock if page.report.clock in clocks else next(iter(clocks), None),
    )
    values = _scored_marks(page, score, selection, clock)
    lines: dict[str, dict[float, _MarkKey]] = defaultdict(dict)
    for player in external:
        if clock and clock in player.at:
            lines[player.line or ""][player.at[clock]] = _mark_key(player)

    runs: dict[str, dict[float, _MarkKey]] = defaultdict(dict)
    for mark in page.training.checkpoints:
        runs[mark.run][mark.align] = (mark.run, mark.ordinal)

    # One external line has one member. A job needs every seed at a position,
    # except where its seating explicitly chose one seed for the whole job.
    groups = [[runs[name] for name in job.runs] for job in page.training.jobs]
    groups.extend([marks] for marks in lines.values())
    latest = []
    for members in groups:
        by_position: dict[float, list[_Evaluated]] = defaultdict(list)
        for marks in members:
            for position, key in marks.items():
                if key in values:
                    by_position[position].append(values[key])
        for position in sorted(by_position, reverse=True):
            seeds = by_position[position]
            whole = [v.value for v in seeds if v.whole]
            if whole:
                latest.append(fmean(whole))
                break
            if len(seeds) == len(members) and len({v.config for v in seeds}) == 1:
                latest.append(fmean(v.value for v in seeds))
                break
    return latest


def _scored_marks(
    page: Page, score: int, selection: int, clock: str | None
) -> dict[_MarkKey, _Evaluated]:
    values: dict[_MarkKey, _Evaluated] = {}
    if page.drawn[selection].seated:
        for p in page.points:
            if (
                p.selection == selection
                and p.score == score
                and p.commit is None
                and not p.split
                and p.value is not None
            ):
                values[_mark_key(p)] = _Evaluated(p.value, whole=p.whole)
        return values

    cells = [
        c
        for c in page.cells
        if c.selection == selection
        and c.score == score
        and c.commit is None
        and not c.split
        and not c.fixed
        and page.players[c.subject].placed
    ]
    anchors = {
        c.opponent for c in cells if c.opponent is not None and not page.players[c.opponent].placed
    }
    # JavaScript compares strings as UTF-16 code units. Both indexes pick the
    # same first anchor regardless of the host's or browser's locale.
    opponent = min(
        anchors,
        key=lambda i: (page.players[i].name.lower().encode("utf-16-be"), i),
        default=None,
    )
    # For all opponents, compare omits an aggregate identical to a single pair.
    by_subject = {c.subject: c for c in cells} if opponent is None else {}
    by_subject.update((c.subject, c) for c in cells if c.opponent == opponent)
    # Ambiguous positions and inference variants are gaps on the report's curve.
    positions = Counter(
        (p.line or "", p.config, p.at[clock])
        for p in page.players
        if p.placed and clock and clock in p.at
    )
    candidates: dict[_MarkKey, list[int]] = defaultdict(list)
    for i in sorted(by_subject):
        candidates[_mark_key(page.players[i])].append(i)
    for key, subjects in candidates.items():
        first = subjects[0]
        player = page.players[first]
        if any(page.players[i].config != player.config for i in subjects):
            continue
        same_position = (
            positions[player.line or "", player.config, player.at[clock]]
            if clock and clock in player.at
            else len(subjects)
        )
        if same_position == 1:
            values[key] = _Evaluated(by_subject[first].value, config=player.config)
    return values


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
    Etude's home is skipped: the sources it retains for launched runs are copies.
    Raises `EtudeError("experiment_named_twice")` for two files with one stem.
    """
    retained = home().resolve()
    candidates: list[Path] = []
    for folder, subdirs, names in directory.walk():
        if folder.resolve().is_relative_to(retained):
            subdirs.clear()
            continue
        subdirs[:] = [d for d in subdirs if not d.startswith(".") and d != "__pycache__"]
        candidates.extend(folder / n for n in names if n.endswith(".py") and not n.startswith("."))
    found: dict[str, Path] = {}
    for path in sorted(candidates):
        if path.is_symlink() and (not path.is_file() or path.resolve().is_relative_to(retained)):
            continue
        if path not in known and experiment_named(path) is None:
            continue
        if path.stem in found:
            first, second = (p.relative_to(directory) for p in (found[path.stem], path))
            raise EtudeError("experiment_named_twice", 409, detail=f"{first} and {second}")
        found[path.stem] = path
    return list(found.values())


@dataclass(frozen=True)
class _ReportRuns:
    """One build's source listing. Only evaluation can also depend on locally played matches."""

    source: RunListing
    local_storage: DirectoryStorage | None
    local_declarations: DeclarationCache

    @cached_property
    def local(self) -> RunListing | None:
        if self.local_storage is None:
            return None
        return RunListing.read(self.local_storage, known=self.local_declarations)

    def local_versions(self, experiment: str) -> dict[str, str] | None:
        return self.local.versions_for({experiment}) if self.local else None


class Pages:
    """Experiments' pages from one source, each built again only when its inputs change.

    A page's inputs are its file, by modification time and size, and the
    experiment's runs, by the version of each visible object they own
    (`Storage.versions`): under the source, and under this computer's runs
    directory when evaluation needs local results (`etude.eval.tournament.played`).
    Checking them takes one listing per source used, plus a run's declaration the first
    time its version is seen. Changed reports in the same build reuse that
    listing for run discovery.
    """

    def __init__(self, source: Storage | Path | str) -> None:
        self.storage = as_storage(source)
        here = DirectoryStorage(default_root())
        self._local = here if store_identity(here) != store_identity(self.storage) else None
        self._source_declarations: DeclarationCache = {}
        self._local_declarations: DeclarationCache = {}
        self._built: dict[tuple[Path | None, str], _BuiltPage] = {}
        self._known: set[Path] = set()

    def build(self, file: Path | None, *, experiment: str | None = None) -> Page:
        """`build`'s page, kept from the last call while its inputs are the same."""
        return self._build(file, self._listings(), experiment)

    def build_each(self, directory: Path) -> dict[Path, Page | str]:
        """Each experiment file's page, or why it could not be built, so one never hides the rest."""
        listings = self._listings()
        built: dict[Path, Page | str] = {}
        files = experiment_files(directory, known=self._known)
        self._known.update(files)
        for file in files:
            try:
                built[file] = self._build(file, listings)
            except Exception as error:
                # An author's file may fail in any way; its row says how and the others go on.
                built[file] = explain(error)
        return built

    def _listings(self) -> _ReportRuns:
        return _ReportRuns(
            RunListing.read(self.storage, known=self._source_declarations),
            self._local,
            self._local_declarations,
        )

    def _build(self, file: Path | None, runs: _ReportRuns, experiment: str | None = None) -> Page:
        name = _experiment_name(file, experiment)
        key = (file, name)
        kept = self._built.get(key)
        sources = _source_versions(file, kept.sources if kept else ())
        if (
            kept is not None
            and kept.sources == sources
            and kept.versions == runs.source.versions_for(kept.experiments)
            and (kept.local_versions is None or kept.local_versions == runs.local_versions(name))
        ):
            return kept.page
        built = _build_page(name, _ReportFile(file, sources), runs)
        self._built[key] = built
        return built.page


@dataclass(frozen=True)
class _BuiltPage:
    """One successful report and the inputs a watched build compares on refresh."""

    page: Page
    sources: SourceVersions
    experiments: set[str]
    versions: dict[str, str]
    local_versions: dict[str, str] | None


def index(source: Storage | Path | str, pages: dict[Path, Page | str]) -> Index:
    """The index of built pages. Each page keeps all but its line data, which the index never draws."""
    storage = as_storage(source)
    entries = []
    for file, built in pages.items():
        page = None
        if isinstance(built, Page):
            # The index draws each job's curve in the page's first view: one score, unsplit.
            score = built._curve_score(built._primary_curve())
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
                href=f"{file.stem}.html",
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


def _experiment_name(file: Path | None, experiment: str | None) -> str:
    if experiment is not None:
        return experiment
    if file is None:
        raise ValueError("a default report needs an experiment name")
    return file.stem


def build(
    source: Storage | Path | str, file: Path | None, *, experiment: str | None = None
) -> Page:
    """The page of the experiment `file` names, from the runs under `source`.

    The file is run each time, so an edited `report()` shows on the next
    build. Raises `ValueError` naming the experiments `source` holds when it
    holds no run of this one, and `EtudeError` for a file that is not an
    experiment's.
    """
    return Pages(source).build(file, experiment=experiment)


# Bump whenever derived Page fields change without changing inputs.
_PAGE_CACHE_VERSION = 5


def _page_cache_path(experiment: str, storage: Storage) -> Path:
    source = store_identity(storage)
    store = hashlib.sha256(source.encode()).hexdigest()[:16]
    stem = (
        f"{experiment}.{store}"
        if SAFE_NAME.match(experiment)
        else hashlib.sha256(f"{experiment}\0{source}".encode()).hexdigest()[:24]
    )
    return home() / "pages" / f"{stem}.json"


def _page_cache_key(
    *,
    experiment: str,
    file: Path | None,
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


def _shift_at(
    at: Mapping[str, float], offset: Mapping[str, float] | None
) -> dict[str, float]:
    if not offset:
        return dict(at)
    return {clock: value + offset.get(clock, 0.0) for clock, value in at.items()}


def _included_training(
    source: RunListing,
    includes: Sequence[Include],
    *,
    metrics: frozenset[str] | None,
    prefixes: Sequence[str],
) -> list[RunData]:
    """Read training runs named by `Report.include`."""
    extra: list[RunData] = []
    seen: set[str] = set()
    for inc in includes:
        for d in source.declarations:
            if (
                d.file.experiment != inc.experiment
                or d.file.kind == "evaluation"
                or (inc.jobs and d.file.job_name not in inc.jobs)
                or d.name in seen
            ):
                continue
            seen.add(d.name)
            extra.append(
                read_located(
                    source.storage,
                    d,
                    source.keys,
                    metrics=metrics,
                    prefixes=prefixes,
                )
            )
    return extra


def _start_offsets(
    source: RunListing, trained: Sequence[RunData], experiment: str
) -> dict[str, dict[str, float]]:
    """Resolve each warm-started run's clock offsets from its `start` checkpoint."""
    checkpoints: dict[tuple[str, str, str], dict[str, float]] = {}
    for run in trained:
        exp = run.file.experiment if run.file is not None else experiment
        for cp in run.checkpoints:
            checkpoints[(exp, run.name, cp.file)] = dict(cp.at)
    offsets: dict[str, dict[str, float]] = {}
    for run in trained:
        if run.file is None or not run.file.start:
            continue
        parts = run.file.start.split("/", 2)
        if len(parts) != 3:
            continue
        parent_exp, parent_run, cp_file = parts
        parent_at = checkpoints.get((parent_exp, parent_run, cp_file))
        if parent_at is None:
            for d in source.declarations:
                if (
                    d.file.experiment == parent_exp
                    and d.name == parent_run
                    and d.file.kind != "evaluation"
                ):
                    loaded = read_located(
                        source.storage,
                        d,
                        source.keys,
                        metrics=frozenset(),
                    )
                    for cp in loaded.checkpoints:
                        checkpoints[(parent_exp, loaded.name, cp.file)] = dict(cp.at)
            parent_at = checkpoints.get((parent_exp, parent_run, cp_file))
        if not parent_at:
            continue
        clocks = {c.name for c in run.clocks} | {"running_hours"}
        shift = {clock: float(value) for clock, value in parent_at.items() if clock in clocks}
        if shift:
            offsets[run.name] = shift
    return offsets


def _eval_run_matches_tournaments(run_name: str, tournaments: Sequence[str]) -> bool:
    return any(
        run_name == t or (run_name.startswith(f"{t}-") and len(run_name) == len(t) + 13)
        for t in tournaments
    )
def _load_cached_page(path: Path, expected_key: str) -> Page | None:
    try:
        with path.open("rb") as stream:
            if stream.readline(128).decode("ascii", "replace").strip() != expected_key:
                return None
            return Page.model_validate_json(stream.read())
    except (OSError, ValidationError):
        return None


def _save_cached_page(path: Path, key: str, page: Page) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as staging:
            pending = Path(staging) / path.name
            with pending.open("wb") as stream:
                stream.write(key.encode())
                stream.write(b"\n")
                stream.write(page.model_dump_json().encode())
            pending.replace(path)
    except OSError:
        pass


def _build_page(
    experiment: str,
    loaded: _ReportFile,
    runs: _ReportRuns,
) -> _BuiltPage:
    source = runs.source
    storage = source.storage
    report = loaded.report
    cache_path = _page_cache_path(experiment, storage)
    metrics_filter = None if report.other_metrics else frozenset(report.keys())
    flames = _flames(report)
    data = read_all(
        source,
        experiment,
        metrics=metrics_filter,
        prefixes=flames,
    )
    if not data:
        held = sorted({run.file.experiment for run in source.declarations})
        raise ValueError(
            f"no runs of {experiment!r} in {_source_name(storage)}"
            + (f"; it holds {', '.join(held)}" if held else "; it holds no runs")
        )
    trained = [run for run in data if run.file is None or run.file.kind != "evaluation"]
    included_trained = _included_training(
        source, report.include, metrics=metrics_filter, prefixes=flames
    )
    if included_trained:
        existing_names = {run.name for run in trained}
        trained = [*trained, *(run for run in included_trained if run.name not in existing_names)]
    offsets = _start_offsets(source, trained, experiment) if report.displace_starts else {}
    state = None
    wanted: list[Tournament] = []
    has_included_evals = any(inc.tournaments for inc in report.include)
    if (
        loaded.eval_jobs is not None
        or len(trained) < len(data) + len(included_trained)
        or has_included_evals
    ):
        # Only reports with evaluations need the optional eval dependency.
        from etude.experiment import state_of  # noqa: PLC0415

        state = state_of(data, experiment)
        wanted = loaded.tournaments(state)
    local_versions = runs.local_versions(experiment) if state is not None else None
    referenced = (
        {experiment, *(inc.experiment for inc in report.include)}
        | find_line_experiments(wanted)
    )
    versions = source.versions_for(referenced)
    cache_key = _page_cache_key(
        experiment=experiment,
        file=loaded.path,
        source=_source_name(storage),
        report=report,
        origin=loaded.origin,
        notes=loaded.notes,
        versions=versions,
        state=state,
        wanted=wanted,
        local_versions=local_versions or {},
    )
    if (cached := _load_cached_page(cache_path, cache_key)) is not None:
        return _BuiltPage(cached, loaded.sources, referenced, versions, local_versions)
    evals = (
        _read_evals(runs, state, report, wanted, offsets=offsets)
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
    page = _assemble(
        experiment,
        loaded.path,
        storage,
        report,
        loaded.origin,
        (*loaded.notes, *partial),
        trained,
        evals,
        offsets=offsets,
    )
    if not report.other_metrics and any(run.rows for run in trained) and page.headline() is None:
        # Named reports discard ungrouped columns. Inspect the same listing on a
        # cold build and retain only the first drawable metric's summary.
        metric = _ungrouped_metric(source, page)
        page = page.model_copy(update={"ungrouped_metric": metric})
    _save_cached_page(cache_path, cache_key, page)
    return _BuiltPage(page, loaded.sources, referenced, versions, local_versions)


def _ungrouped_metric(source: RunListing, page: Page) -> str | None:
    named, timers = set(page.report.keys()), _flames(page.report)
    clock = page._report_clock()
    keys: list[str] = []
    values: list[float] = []
    records = (
        r
        for r in source.declarations
        if r.file.experiment == page.experiment and r.file.kind != "evaluation"
    )
    for located in records:
        if clock not in BUILT_IN_NAMES and not any(c.name == clock for c in located.file.clocks):
            continue
        # Project individual block headers. Once a key has values, later runs
        # need only that key and earlier candidates that may have been gaps.
        run = read_located(
            source.storage,
            located,
            source.keys,
            metrics=frozenset(keys),
            prefixes=() if values else ("",),
        )
        logged = run.metrics()
        if not values:
            keys = list(
                dict.fromkeys(
                    [*keys, *(k for k in logged if k not in named and not k.startswith(timers))]
                )
            )
        present = set(logged)
        with run._reading_blocks():
            for i, key in enumerate(keys):
                if key not in present:
                    continue
                trace = Trace.from_buckets(run.name, run.chart(key, clock, BUCKETS))
                if (latest := trace.latest(log=False)) is not None:
                    if i < len(keys) - 1:
                        del keys[i + 1 :]
                        values.clear()
                    values.append(latest)
                    break
    return f"{keys[-1]} {_value_range(values)}" if values else None


def _flames(report: Report) -> tuple[str, ...]:
    return tuple(panel.prefix for panel in (*report.perf, *report.ml) if isinstance(panel, Flame))


class ReportDeclaration(Record):
    report: Report
    sources: SourceVersions

    def encoded(self) -> str:
        # An omitted ml selects the available defaults. Nested panels need their kind on disk.
        return self.model_dump_json(
            include={"sources": True, "report": self.report.model_fields_set}
        )


class _ReportFile:
    """The authored declarations for this build and the sources they used."""

    def __init__(self, path: Path | None, before: SourceVersions) -> None:
        self.path = path
        self._before = before
        self.sources: SourceVersions = ()
        with self._imports():
            names = _run_file(path) if path else {}
            self.report, self.origin, self.notes = _report(names.get("report"), path)
            self.eval_jobs = names.get("eval_jobs")

    @contextmanager
    def _imports(self) -> Iterator[None]:
        # Capture imports when each authored call returns. Another experiment
        # can replace them while this page reads runs or builds its tables.
        with _author_imports:
            yield
            if self.path:
                paths = {self.path.resolve(), *_neighbours(self.path).values()}
                paths.update(Path(p) for p, _, _ in self.sources)
                self.sources = _versions(paths, observed=(*self._before, *self.sources))

    def tournaments(self, state: ExperimentState) -> list[Tournament]:
        if self.eval_jobs is None:
            return []
        if not callable(self.eval_jobs):
            raise EtudeError("experiment_eval_is_not_a_function", 422) from ValueError(self.path)
        with self._imports():
            return _wanted(self.eval_jobs, state)


def read_report(file: Path, accepted: ReportDeclaration | None) -> ReportDeclaration:
    """Reuse accepted panels until one of their sources changes."""
    sources = _source_versions(file, accepted.sources if accepted else ())
    if accepted and sources == accepted.sources:
        return accepted
    loaded = _ReportFile(file, sources)
    return ReportDeclaration(report=loaded.report, sources=loaded.sources)


class Headline(Record):
    """One run's card curves and latest metrics, ranked across runs on the report clock."""

    order: int
    title: str
    clock: str
    unit: str = ""
    log: bool = False
    curves: dict[str, Trace]

    def latest(self) -> list[str]:
        """Latest value of each drawable metric, labelled for the run table."""
        values = []
        for key, trace in self.curves.items():
            if (value := trace.latest(log=self.log)) is not None:
                values.append(f"{key} {value:.4g} {self.unit}".rstrip())
        return values


def _preview_panels(report: Report, data: Training, clock: str) -> tuple[MetricPanel, ...]:
    """Authored ML panels followed by the first drawable ungrouped metric on this clock."""
    if report.other_metrics:
        named, timers = set(report.keys()), _flames(report)
        measured = data.series.get(clock, {})
        for key in data.metrics:
            if key in named or key.startswith(timers):
                continue
            if any(trace.latest(log=False) is not None for trace in measured.get(key, ())):
                return (*report.ml, Series(title=key, keys=(key,)))
    return report.ml


def _headline(report: Report, data: Training, clock: str) -> Headline | None:
    measured = data.series.get(clock, {})
    logged = set(data.metrics)
    panels = _preview_panels(report, data, clock)
    # The order is also compared across independently indexed runs. Reserve a
    # slot for ungrouped metrics even on runs without any.
    ranked = [
        (i if isinstance(panel, Series) else len(report.ml) + 1 + i, panel)
        for i, panel in enumerate(panels)
    ]
    for order, panel in sorted(ranked, key=lambda item: item[0]):
        curves: dict[str, Trace] = {}
        unit = ""
        if isinstance(panel, Series | Counters):
            if logged.isdisjoint(panel.keys):
                continue  # The report omits metric panels absent across its runs.
            curves = {k: measured[k][0] for k in panel.keys if k in measured}
            if isinstance(panel, Series):
                if not any(
                    trace.latest(log=panel.scale == "log") is not None for trace in curves.values()
                ):
                    continue
                unit = panel.unit
        elif isinstance(panel, Pace | Elapsed):
            unit = "per second" if isinstance(panel, Pace) else ""
            marks = data.checkpoints
            keys = (
                [c.name for c in data.clocks if c.kind == "step"]
                if isinstance(panel, Pace)
                else ["hours"]
            )
            for key in keys:
                curves[key] = Trace(
                    owner=data.runs[0].name,
                    x=[m.at.get(clock) for m in marks],
                    mean=[
                        m.pace.get(key) if isinstance(panel, Pace) else m.elapsed / 3600
                        for m in marks
                    ],
                )
        # A non-curve panel still occupies its place in the experiment's declaration.
        return Headline(
            order=order,
            title=panel.title,
            clock=clock,
            unit=unit,
            log=isinstance(panel, Series) and panel.scale == "log",
            curves=curves,
        )
    return None


@dataclass(frozen=True)
class RunCharts:
    payload: str
    overview: str
    headline: Headline | None


def _overview(page: Page) -> Page:
    # Diagnostics can occupy either slot. At most the first two measured metric
    # groups can appear, regardless of which diagnostics the report renderer draws.
    training = page.training
    panels = _preview_panels(page.report, training, page._report_clock())
    report = page.report.model_copy(update={"ml": panels})
    logged = set(training.metrics)
    groups = [
        p.keys
        for p in (*report.ml, *report.perf)
        if isinstance(p, Series | Counters) and not logged.isdisjoint(p.keys)
    ]
    keys = {k for group in groups[:2] for k in group}
    preview = training.model_copy(
        update={
            "metrics": tuple(k for k in training.metrics if k in keys),
            "series": {
                clock: {k: trace for k, trace in traces.items() if k in keys}
                for clock, traces in training.series.items()
            },
            "bands": {},  # Run charts draw one seed.
        }
    )
    return page.model_copy(update={"report": report, "training": preview})


def run_chart_data(storage: Storage, data: RunData, report: Report, clock: str) -> RunCharts:
    """Project the full run and the card from one reading of its bucketed curves."""
    # A run page draws every recorded metric, as charts.js does for run charts,
    # so its overview and headline also fall back to metrics no panel names.
    report = report.model_copy(update={"other_metrics": True})
    page = _assemble(data.experiment, None, storage, report, None, (), [data], _Evals())
    return RunCharts(
        _payload(page.model_dump(mode="json") | {"run_charts": True}, None),
        _payload(_overview(page).model_dump(mode="json") | {"run_charts": True}, None),
        _headline(report, page.training, clock),
    )


def render_overview(charts: str, beside: str) -> str:
    return f'<div class="run-overview"><div>{charts}</div><aside>{beside}</aside></div>'


def render_run(address: str, charts: str, heading: str, beside: str) -> str:
    """Show already-indexed chart data alongside the current run status."""
    return _inline(
        "page.html",
        address,
        charts,
        heading,
        beside=beside,
        sections=(("charts", "Charts"),)
        if beside
        else (("charts", "Charts"), ("performance", "Performance")),
    )


def _assemble(
    experiment: str,
    file: Path | None,
    storage: Storage,
    report: Report,
    origin: str | None,
    notes: tuple[str, ...],
    trained: Sequence[RunData],
    evals: _Evals,
    *,
    offsets: Mapping[str, Mapping[str, float]] = {},
) -> Page:
    measured = training(trained, prefixes=_flames(report), offsets=offsets)
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
    return Page(
        experiment=experiment,
        file=str(file) if file else experiment,
        source=_source_name(storage),
        report=report,
        origin=origin,
        notes=(*notes, *_without_hours(trained), *evals.notes),
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
    included: their records must keep their identity. The process entry point
    also stays loaded when its file lies inside an Etude checkout.
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
        if name in ("__main__", "etude") or name.startswith("etude."):
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


def _source_versions(file: Path | None, known: SourceVersions) -> SourceVersions:
    paths = {file.resolve(), *(Path(p) for p, _, _ in known)} if file else set()
    return _versions(paths)


def _versions(paths: set[Path], *, observed: SourceVersions = ()) -> SourceVersions:
    kept = {Path(entry[0]): entry for entry in observed}
    found = []
    for path in sorted(paths):
        if path in kept:
            found.append(kept[path])
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        found.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(found)


def _report(made: object, file: Path | None) -> tuple[Report, str | None, tuple[str, ...]]:
    if made is None:
        # Nothing is declared, so the page draws everything logged rather than nothing.
        notes = (
            (f"{file.name} defines no report(), so this is the default report.",) if file else ()
        )
        return DEFAULT_REPORT, None, notes
    assert file is not None
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
    runs: _ReportRuns,
    state: ExperimentState,
    report: Report,
    wanted: Sequence[Tournament],
    *,
    offsets: Mapping[str, Mapping[str, float]] = {},
) -> _Evals:
    # The optional `eval` extra (DuckDB, and the etude.eval modules over it), imported where
    # it is used: only an experiment with evaluations reads it, so a report without loads none.
    from etude.eval.query import _open_statsdb  # noqa: PLC0415

    held, refused = _held_tournaments(wanted, state, runs, includes=report.include)
    if any(inc.tournaments or inc.jobs for inc in report.include):
        scoped_listing, extra_exps, cache_suffix = _included_statsdb_listing(
            runs.source, state.experiment, report.include
        )
        db = _open_statsdb(
            state.experiment,
            scoped_listing,
            extra_experiments=extra_exps,
            cache_suffix=cache_suffix,
        )
    else:
        db = _open_statsdb(state.experiment, runs.source)
    try:
        saved = _saved_checkpoints(db, offsets=offsets)
        players, placement = _players(
            db, saved, {run.name: run.line for run in state.runs}, offsets=offsets
        )
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
            points=_points(held, selections, players, saved, cells, offsets=offsets),
            coverage=_coverage(saved, held),
            failures=_failures(db, held, players),
            latest_result_ms=None if latest is None else latest[0],
            absent=absent,
            notes=(*refused, *placement, *notes, *mixed),
        )
    finally:
        db.close()


def _included_statsdb_listing(
    source: RunListing,
    experiment: str,
    includes: Sequence[Include],
) -> tuple[RunListing, tuple[str, ...], str]:
    """Select only `experiment`'s keys plus the included runs/tournaments for a scoped statsdb."""
    selected: dict[str, str | None] = {}
    for d in source.declarations:
        if d.file.experiment == experiment:
            for folder in d.folders:
                for key in under(source.keys, folder):
                    selected[key] = source.objects.get(key)
    extra_exps: list[str] = []
    for inc in includes:
        if inc.experiment != experiment and inc.experiment not in extra_exps:
            extra_exps.append(inc.experiment)
        for d in source.declarations:
            if d.file.experiment != inc.experiment:
                continue
            if d.file.kind != "evaluation":
                if not inc.jobs or d.file.job_name in inc.jobs:
                    for folder in d.folders:
                        for key in under(source.keys, folder):
                            rel = key[len(folder) :]
                            if rel == "run.json" or (
                                rel.startswith("checkpoints/") and rel.endswith(".json")
                            ):
                                selected[key] = source.objects.get(key)
            elif inc.tournaments and _eval_run_matches_tournaments(d.name, inc.tournaments):
                for folder in d.folders:
                    for key in under(source.keys, folder):
                        selected[key] = source.objects.get(key)
    digest = hashlib.sha256(
        json.dumps([inc.model_dump(mode="json") for inc in includes], sort_keys=True).encode()
    ).hexdigest()[:12]
    scoped = RunListing(source.storage, selected, known=dict(source._known))
    return scoped, tuple(extra_exps), f"inc-{digest}"


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
    listings: _ReportRuns,
    *,
    includes: Sequence[Include] = (),
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
    from etude.eval.tournament import StoredRuns, played  # noqa: PLC0415
    from etude.experiment import read_tournament_lines  # noqa: PLC0415 - optional eval dependency

    wanted: dict[str, Tournament] = {}
    for tournament in asked:
        wanted.setdefault(tournament.definition.name, tournament)
    refused: list[str] = []
    # Read once for every tournament drawn: a page plays nothing, so nothing changes meanwhile.
    experiment = state.experiment
    places = (listings.local, listings.source) if listings.local is not None else (listings.source,)
    runs = StoredRuns.read(experiment, *places)
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
    lines = read_tournament_lines(listings.source, state, wanted.values())
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
    source = listings.source
    for inc in includes:
        if not inc.tournaments:
            continue
        inc_objects: dict[str, str | None] = {}
        for d in source.declarations:
            if (
                d.file.experiment == inc.experiment
                and d.file.kind == "evaluation"
                and _eval_run_matches_tournaments(d.name, inc.tournaments)
            ):
                for folder in d.folders:
                    for key in under(source.keys, folder):
                        inc_objects[key] = source.objects.get(key)
        if not inc_objects:
            continue
        inc_listing = RunListing(source.storage, inc_objects, known=dict(source._known))
        inc_runs = StoredRuns.read(inc.experiment, inc_listing)
        inc_wanted: dict[str, Tournament] = {}
        for t_name in inc.tournaments:
            held_runs = inc_runs.tournaments.get(t_name)
            if not held_runs:
                continue
            try:
                inc_wanted[t_name] = assemble(held_runs)
            except EtudeError as error:
                refused.append(
                    f"Included tournament {t_name} ({inc.experiment}) is not shown:"
                    f" its eval runs disagree ({error.code})."
                )
        inc_lines = read_tournament_lines(source, state, inc_wanted.values())
        for t_name, given in sorted(inc_wanted.items()):
            if any(h.name == t_name for h in held_tournaments):
                continue
            try:
                found = played(
                    given.definition,
                    experiment=inc.experiment,
                    checkpoints=given.checkpoints,
                    enter=given.entrants,
                    lines=inc_lines,
                    runs=inc_runs,
                )
            except EtudeError as error:
                refused.append(
                    f"Included tournament {t_name} ({inc.experiment}) is not shown:"
                    f" it is refused ({error.code})."
                )
                continue
            tournament, gaps = given.resolve(inc_lines, experiment=inc.experiment)
            held_tournaments.append(
                _HeldTournament(t_name, tournament, tuple(tournament.schedule()), gaps, found)
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
    db: duckdb.DuckDBPyConnection,
    saved: dict[str, _Saved],
    lines: dict[str, str],
    *,
    offsets: Mapping[str, Mapping[str, float]] = {},
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
        offset = offsets.get(bound.run) if bound is not None else None
        recorded = {} if bound is None else bound.at
        seated_at: list[dict[str, float]] = json.loads(given)
        seated = _shift_at(_merged(seated_at), offset)
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


def _saved_checkpoints(
    db: duckdb.DuckDBPyConnection,
    *,
    offsets: Mapping[str, Mapping[str, float]] = {},
) -> dict[str, _Saved]:
    """Each checkpoint key, encoded, to the training run's checkpoint that saved it.

    A run that saved two ordinals under one file name holds one key; it is
    bound to the later.
    """
    found = db.sql(
        'select checkpoint, run, max(ordinal), arg_max("at", ordinal) from training_checkpoints'
        " group by checkpoint, run"
    ).fetchall()
    return {
        key: _Saved(run, ordinal, _shift_at(json.loads(at), offsets.get(run)))
        for key, run, ordinal, at in found
    }


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
    *,
    offsets: Mapping[str, Mapping[str, float]] = {},
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
            offset = offsets.get(place.run) if place is not None else None
            at = _shift_at(ref.at, offset) if ref.at else ({} if place is None else place.at)
            stood = Point(
                selection=number,
                run=None if place is None else place.run,
                ordinal=None if place is None else place.ordinal,
                line=ref.line,
                at=at,
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


def render(page: Page, *, refresh: float | None = None, heading: str = "") -> str:
    """The report as one HTML file; a watched file reloads every `refresh` seconds."""
    return _inline(
        "page.html", page.experiment, _payload(page.model_dump(mode="json"), refresh), heading
    )


def render_index(found: Index, *, refresh: float | None = None) -> str:
    """The index as one HTML file, like a page."""
    return _inline("index.html", "Experiments", _payload(found.model_dump(mode="json"), refresh))


def _payload(data: dict[str, object], refresh: float | None) -> str:
    stamp = {"generated_ms": time.time_ns() // 1_000_000, "refresh": refresh}
    return json.dumps(data | stamp, allow_nan=False).replace("<", "\\u003c")


@cache
def read_asset(name: str) -> str:
    """Read a packaged page asset once. Authored report sources are versioned separately."""
    return (Path(__file__).parent / name).read_text(encoding="utf-8")


def _inline(
    template: str,
    title: str,
    payload: str,
    heading: str = "",
    *,
    beside: str = "",
    sections: tuple[tuple[str, str], ...] = (("perf", "Perf"), ("ml", "ML details")),
) -> str:
    # Substitute in the template once; run data and console text can contain any marker.
    metrics = "".join(
        f'<section class="sec" id="{key}"><h2>{label} <span id="{key}-sub"></span>'
        f'<div class="sec-controls" id="{key}-controls"></div></h2>'
        f'<div class="grid" id="{key}-body"></div></section>'
        for key, label in sections
    )
    fields = {
        "__UPLOT_CSS__": read_asset("vendor/uPlot.min.css"),
        "__PAGE_CSS__": read_asset("page.css"),
        "__UPLOT_JS__": read_asset("vendor/uPlot.iife.min.js"),
        "__CHARTS_JS__": read_asset("charts.js"),
        "__TITLE__": escape(title),
        "__DATA__": payload,
        "<!--view-heading-->": heading,
        "<!--metric-tabs-->": "".join(
            f'<a href="#{key}" data-s="{key}">{label}</a>' for key, label in sections
        ),
        "<!--metric-sections-->": (render_overview(metrics, beside) if beside else metrics),
    }
    return re.sub("|".join(map(re.escape, fields)), lambda m: fields[m[0]], read_asset(template))


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
