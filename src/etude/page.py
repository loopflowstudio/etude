"""An experiment's report page, and an index of every experiment in a folder: built, written or served.

    storage = storage_from_uri("r2://bucket/runs")
    page = build(storage, Path("experiments/ppo.py"))
    write(render(page), target(storage, "ppo"))  # ./reports/ppo.html
    pages = build_each(storage, Path("experiments"))  # every experiment file there
    write(render_index(index(storage, pages, href=lambda stem: f"{stem}.html")), target(storage, "index"))

The file names the experiment, by its stem, and may define `report()`
(`etude.report.Report`) and `eval(state)` (`etude.experiment`). Without
`report()` the page is `Report()` and says so at its top. The experiment's
training runs are drawn by `etude.train.report.training`.

Evals come from the experiment's eval database (`etude.eval.query.statsdb`).
A checkpoint agent is bound to its training run's checkpoint by key: the
database joins `agents.checkpoint` to `training_checkpoints`, and the page
draws the agent where that run's clocks stood. Two keys holding the same
bytes are two agents. Every other agent (code, a checkpoint from outside the
experiment, an etude-sdk 0.2 digest) is fixed, and drawn once as an anchor.

Values are computed by `compare` once for each selection of matches (each
tournament, each of its tags, each matchup a panel names), each score and
each combination of the columns panels name. Each scheduled match counts
once, from the eval run that recorded it last, so at its latest version. So
any panel, under any filter the page offers, shows a value `compare`
computed, never one pooled in the browser.

Gaps stay visible. A checkpoint nothing played is not evaluated; one that
`eval(state)` wants and no eval run delivered is not yet played
(`etude.eval.tournament.unplayed`); failed matches are counted and listed.

The index (`index.html`) is a row per experiment file under a folder
(`experiment_files`), drawn from each one's `Page` with its line data left
out, so every fact on it, such as each job's latest score, is the report's
own, computed by the same code (`charts.js`).

Each page is one HTML file with its data and the vendored uPlot inlined: no
network, no server. This module composes training and evaluation, which
never import each other.
"""

from __future__ import annotations

import ast
import itertools
import json
import runpy
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from etude.report import Flame, Report, Score
from etude.run.errors import EtudeError
from etude.run.listing import as_storage, runs
from etude.run.records import Record
from etude.run.storage import DirectoryStorage, Storage
from etude.train.read import read_all
from etude.train.report import WALL_MEANING, Training, training

if TYPE_CHECKING:
    import duckdb


class Selection(Record):
    """Matches values are computed over: a tournament's, or those of one of its matchups or tags."""

    tournament: str
    matchup: str | None = None
    tag: str | None = None


class Agent(Record):
    """An agent the eval runs seated: a checkpoint of the experiment's, or else a fixed one.

    `run` and `ordinal` name the training run's checkpoint its key binds it
    to; both are None for a fixed agent.
    """

    key: str  # its `etude.eval.records.AgentKey`, encoded, as the database holds it
    name: str
    run: str | None
    ordinal: int | None
    # Canonical JSON: one checkpoint at two configs is two agents.
    config: str


class Cell(Record):
    """`compare`'s value for one subject, against one opponent, in one selection and split.

    `subject` and `opponent` index `Page.agents`; an opponent of None is
    every opponent, kept only where a subject met several. `fixed` cells
    count only matches that seat no checkpoint of the experiment: an
    anchor's strength. `split` holds exactly the columns it is split by.
    """

    selection: int
    score: int
    subject: int
    opponent: int | None
    fixed: bool
    split: dict[str, JsonValue]
    value: float
    interval: tuple[float, float] | None
    rows: int
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
    agents: tuple[int, ...]  # by seat, indexing `Page.agents`
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
    selections: tuple[Selection, ...]
    agents: tuple[Agent, ...]
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


# What makes a Python file an experiment's: a top-level eval(state) or report().
EXPERIMENT_FUNCTIONS = ("eval", "report")


def experiment_files(directory: Path) -> list[Path]:
    """The experiment files at any depth below `directory`, outside hidden folders.

    A file is recognised by its text, without running it; raises
    `EtudeError("experiment_named_twice")` for two files with one stem.
    """
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob("*.py")):
        parts = path.relative_to(directory).parts
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            continue  # not a file `etude report` could run either
        defined = {
            each.name
            for each in tree.body
            if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if not defined & set(EXPERIMENT_FUNCTIONS):
            continue
        if path.stem in found:
            raise EtudeError("experiment_named_twice", 409) from ValueError(
                f"{found[path.stem]} and {path}"
            )
        found[path.stem] = path
    return list(found.values())


def build_each(source: Storage | Path | str, directory: Path) -> dict[Path, Page | str]:
    """Each experiment file's page, or why it could not be built, so one never hides the rest."""
    storage = as_storage(source)
    built: dict[Path, Page | str] = {}
    for file in experiment_files(directory):
        try:
            built[file] = build(storage, file)
        except Exception as error:
            # An author's file may fail in any way; its row says how and the others go on.
            built[file] = f"{type(error).__name__}: {error}"
    return built


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
            kept = tuple(c for c in built.cells if c.score == 0 and not c.split and not c.fixed)
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
    return str(storage.root) if isinstance(storage, DirectoryStorage) else storage.name


def build(source: Storage | Path | str, file: Path) -> Page:
    """The page of the experiment `file` names, from the runs under `source`.

    The file is run each time, so an edited `report()` shows on the next
    build. Raises `ValueError` naming the experiments `source` holds when it
    holds no training run of this one, and `EtudeError` for a file that is
    not an experiment's.
    """
    storage = as_storage(source)
    experiment = file.stem
    names = _run_file(file)
    report, origin, notes = _report(names.get("report"), file)
    data = read_all(storage, experiment)
    trained = [run for run in data if run.file is None or run.file.declaration.kind != "evaluation"]
    if not trained:
        held = sorted({name for name, _ in runs(storage)})
        raise ValueError(
            f"no training runs of {experiment!r} in {storage.name}"
            + (f"; it holds {', '.join(held)}" if held else "; it holds no runs")
        )
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
        agents=evals.agents,
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
    selections: tuple[Selection, ...] = ()
    agents: tuple[Agent, ...] = ()
    cells: tuple[Cell, ...] = ()
    coverage: tuple[Coverage, ...] = ()
    failures: tuple[Failure, ...] = ()
    latest_result_ms: int | None = None
    absent: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _read_evals(
    storage: Storage, experiment: str, report: Report, want: Callable[..., object] | None
) -> _Evals:
    # Evaluation is read only once the experiment has some: DuckDB is the optional `eval` extra.
    from etude.eval.query import statsdb  # noqa: PLC0415

    wanted, refused = _unplayed(want, storage, experiment) if want is not None else ({}, ())
    db = statsdb(experiment, storage)
    try:
        agents = _agents(db)
        _held(db, agents)
        selections, notes = _selections(db, report)
        scores, cells, absent = _cells(db, selections, agents, report)
        latest = db.sql("select max(wall_ms) from matches").fetchone()
        return _Evals(
            scores=scores,
            selections=tuple(selections),
            agents=tuple(agents),
            cells=tuple(cells),
            coverage=_coverage(db, wanted),
            failures=_failures(db, agents),
            latest_result_ms=None if latest is None else latest[0],
            absent=absent,
            notes=(*refused, *notes),
        )
    finally:
        db.close()


def _unplayed(
    want: Callable[..., object], storage: Storage, experiment: str
) -> tuple[dict[tuple[str, int, str], int], tuple[str, ...]]:
    """For each checkpoint and tournament, how many matches `eval(state)` wants undelivered;
    and a note for each tournament `etude eval` would refuse, as it would."""
    # Imported for a file that defines eval(state): experiments join the launcher's records.
    from etude.eval.tournament import unplayed  # noqa: PLC0415
    from etude.experiment import ExperimentState, observe  # noqa: PLC0415
    from etude.jobs import evaluation_spec  # noqa: PLC0415
    from etude.launch.records import Job  # noqa: PLC0415

    state: ExperimentState = observe(storage, experiment)
    returned = want(state)
    given: list[object] = list(returned) if isinstance(returned, list | tuple) else [returned]
    counts: dict[tuple[str, int, str], int] = {}
    refused: list[str] = []
    for job in given:
        if not isinstance(job, Job):
            raise EtudeError("eval_returns_evaluation_jobs", 422)
        spec = evaluation_spec(job)
        refs = tuple(each.ref for each in spec.checkpoints)
        try:
            new = unplayed(
                spec.tournament, experiment=experiment, checkpoints=refs, storages=[storage]
            )
        except EtudeError as error:
            refused.append(
                f"eval(state)'s {spec.tournament.name} is refused ({error.code}), "
                "so what it has not yet played is not shown."
            )
            continue
        # A failed match is counted from the eval runs, as failed rather than unplayed.
        for match in new.new:
            keys = {seat.key.checkpoint for seat in match.agents if seat.key.checkpoint is not None}
            for key in keys:
                saved = state.checkpoint(key)
                if saved is not None:
                    where = (saved.run, saved.ordinal, spec.tournament.name)
                    counts[where] = counts.get(where, 0) + 1
    return counts, tuple(refused)


def _agents(db: duckdb.DuckDBPyConnection) -> list[Agent]:
    """Every agent seated, bound by its checkpoint key to the training run that saved it.

    A run that saved two ordinals under one file name holds one key; it is
    bound to the later.
    """
    found = db.sql(
        "select a.agent, a.name, a.config, t.run, t.ordinal from agents a left join"
        " (select checkpoint, any_value(run) as run, max(ordinal) as ordinal"
        "  from training_checkpoints group by checkpoint) t on t.checkpoint = a.checkpoint"
        " order by a.agent"
    ).fetchall()
    return [
        Agent(
            key=key,
            name=name,
            run=run,
            ordinal=ordinal,
            config=json.dumps(json.loads(config or "{}"), sort_keys=True),
        )
        for key, name, config, run, ordinal in found
    ]


def _held(db: duckdb.DuckDBPyConnection, agents: Sequence[Agent]) -> None:
    """Temp tables: `_page_held` (tournament, run, match), each scheduled match of a
    tournament without a conflict once, from the eval run of its environment that recorded
    it last; and `_page_checkpoints`, the agents bound to a checkpoint of the experiment."""
    db.execute(
        "create or replace temp table _page_held as"
        " select s.tournament, arg_max(m.run, m.wall_ms) as run, m.match"
        " from matches m join runs r using (run)"
        " join tournaments t on t.environment = r.environment and t.conflict is null"
        " join (select distinct tournament, match from schedule) s"
        "  on s.tournament = t.tournament and s.match = m.match"
        " where m.state = 'done' group by s.tournament, m.match"
    )
    db.execute("create or replace temp table _page_checkpoints (agent VARCHAR)")
    bound = [[each.key] for each in agents if each.run is not None]
    if bound:
        db.executemany("insert into _page_checkpoints values (?)", bound)


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _where(selection: Selection, *, fixed: bool) -> str:
    """The rows of a selection's matches, as a condition `compare` applies to an author table."""
    tournament = _literal(selection.tournament)
    held = f"(run, match) in (select run, match from _page_held where tournament = {tournament})"
    scheduled = f"select match from schedule where tournament = {tournament}"
    if selection.matchup is not None:
        held += f" and match in ({scheduled} and matchup = {_literal(selection.matchup)})"
    if selection.tag is not None:
        held += f" and match in ({scheduled} and list_contains(tags, {_literal(selection.tag)}))"
    if fixed:
        held += (
            " and match not in (select match from seats"
            " where agent in (select agent from _page_checkpoints))"
        )
    return held


def _selections(
    db: duckdb.DuckDBPyConnection, report: Report
) -> tuple[list[Selection], tuple[str, ...]]:
    """Each tournament, each of its tags and each matchup a panel names; and what is amiss."""
    notes: list[str] = []
    tournaments: list[str] = []
    for name, conflict in db.sql(
        "select tournament, conflict from tournaments order by tournament"
    ).fetchall():
        if conflict is None:
            tournaments.append(name)
        else:
            notes.append(f"Tournament {name} is not shown: its eval runs disagree ({conflict}).")
    tags: list[tuple[str, str]] = db.sql(
        "select distinct tournament, unnest(tags) from schedule order by all"
    ).fetchall()
    matchups: list[tuple[str, str]] = db.sql(
        "select distinct tournament, matchup from schedule where matchup is not null order by all"
    ).fetchall()
    selections = [Selection(tournament=name) for name in tournaments]
    selections += [Selection(tournament=t, tag=tag) for t, tag in tags if t in tournaments]
    for panel in report.evals:
        title = f"{type(panel).__name__} panel"
        if panel.tournament is not None and panel.tournament not in tournaments:
            notes.append(f"A {title} names tournament {panel.tournament!r}, which nothing holds.")
        if panel.tag is not None and not any(tag == panel.tag for _, tag in tags):
            notes.append(f"A {title} names tag {panel.tag!r}, which no matchup has.")
        if panel.matchup is None:
            continue
        named = [
            Selection(tournament=t, matchup=m)
            for t, m in matchups
            if m == panel.matchup and t in tournaments and panel.tournament in (None, t)
        ]
        if not named:
            notes.append(f"A {title} names matchup {panel.matchup!r}, which nothing holds.")
        selections += [each for each in named if each not in selections]
    return selections, tuple(notes)


def _cells(
    db: duckdb.DuckDBPyConnection,
    selections: Sequence[Selection],
    agents: Sequence[Agent],
    report: Report,
) -> tuple[tuple[Score, ...], list[Cell], tuple[str, ...]]:
    """Every value the eval panels may show, the scores offered, and absent columns."""
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
    index = {each.key: number for number, each in enumerate(agents)}
    anchors = sum(each.run is None for each in agents)
    cells: list[Cell] = []
    absent: list[str] = []
    for number, score in enumerate(scores):
        held = columns.get(score.table, {})
        if score.value not in held:
            absent.append(f"{score.table}.{score.value}")
            continue
        named = [c for c in report.columns() if c in held]
        absent += [f"{score.table}.{c}" for c in report.columns() if c not in held]
        for chosen, selection in enumerate(selections):
            # An anchor's strength needs two anchors to have met.
            for fixed in (False, True) if anchors > 1 else (False,):
                for size in range(len(named) + 1):
                    for by in itertools.combinations(named, size):
                        for found in compare(
                            db,
                            table=score.table,
                            value=score.value,
                            per=score.per,
                            where=_where(selection, fixed=fixed),
                            by=by,
                        ):
                            if len(found.subject) != 1 or (
                                found.opponent and len(found.opponent) != 1
                            ):
                                continue  # a team
                            subject = index[found.subject[0]]
                            if fixed and found.opponent is not None:
                                continue  # pairs of anchors are in the unrestricted cells
                            cells.append(
                                Cell(
                                    selection=chosen,
                                    score=number,
                                    subject=subject,
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
    # "Against all" repeats the only opponent a subject met.
    met: dict[tuple[int, int, int], set[int]] = {}
    for cell in cells:
        if cell.opponent is not None:
            met.setdefault((cell.selection, cell.score, cell.subject), set()).add(cell.opponent)
    kept = [
        c
        for c in cells
        if c.opponent is not None
        or c.fixed
        or len(met.get((c.selection, c.score, c.subject), ())) != 1
    ]
    return scores, kept, tuple(dict.fromkeys(absent))


def _coverage(
    db: duckdb.DuckDBPyConnection, unplayed: dict[tuple[str, int, str], int]
) -> tuple[Coverage, ...]:
    """Each checkpoint's matches in each tournament: played, failed since, and wanted."""
    found = db.sql(
        """
        with seated as (
            select distinct t.run, t.ordinal, n.tournament, m.match, m.state
            from matches m join runs n using (run) join seats s using (run, match)
            join agents a on a.agent = s.agent
            join training_checkpoints t on t.checkpoint = a.checkpoint
            where n.tournament is not null
        )
        select run, ordinal, tournament,
            count(distinct match) filter (where state = 'done'),
            count(distinct match) filter (where state = 'failed'
                and match not in (select match from matches where state = 'done'))
        from seated group by all
        """
    ).fetchall()
    counts = {
        (run, ordinal, tournament): (played, failed)
        for run, ordinal, tournament, played, failed in found
    }
    return tuple(
        Coverage(
            run=run,
            ordinal=ordinal,
            tournament=tournament,
            played=counts.get((run, ordinal, tournament), (0, 0))[0],
            failed=counts.get((run, ordinal, tournament), (0, 0))[1],
            unplayed=unplayed.get((run, ordinal, tournament), 0),
        )
        for run, ordinal, tournament in sorted(counts.keys() | unplayed.keys())
    )


def _failures(db: duckdb.DuckDBPyConnection, agents: Sequence[Agent]) -> tuple[Failure, ...]:
    index = {each.key: number for number, each in enumerate(agents)}
    found = db.sql(
        "select n.tournament, m.error, list(s.agent order by s.player)"
        " from matches m join runs n using (run) join seats s using (run, match)"
        " where m.state = 'failed' and m.match not in (select match from matches where state = 'done')"
        " group by n.tournament, m.run, m.match, m.error order by all"
    ).fetchall()
    return tuple(
        Failure(tournament=tournament, agents=tuple(index[key] for key in keys), error=error)
        for tournament, error, keys in found
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
    experiments at `/STEM`, built from the files for each request.

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
        self.source, self.path, self.refresh = source, path, refresh

    def page(self, route: str = "/") -> str | None:
        """The HTML served at `route`, or None where nothing is."""
        if not self.path.is_dir():
            return (
                render(build(self.source, self.path), refresh=self.refresh)
                if route == "/"
                else None
            )
        if route == "/":
            built = build_each(self.source, self.path)
            found = index(self.source, built, href=lambda stem: f"/{stem}")
            return render_index(found, refresh=self.refresh)
        for file in experiment_files(self.path):
            if route == f"/{file.stem}":
                return render(build(self.source, file), refresh=self.refresh)
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
