"""Live run and experiment pages. One ingestion loop publishes the chart index for all readers.

Run files and launcher standing are joined by state_of. Only derived chart data
and display facts live in the index. Publication is one SQLite transaction
in WAL mode. Readers keep the last committed generation through a slow or
interrupted ingestion. Reports build on demand in separate request threads.
"""

from __future__ import annotations

import shlex
import signal
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from types import FrameType
from urllib.parse import quote, unquote, urlsplit

from pydantic import TypeAdapter

from etude.cache import Cache
from etude.errors import explain
from etude.experiment import state_of
from etude.home import home
from etude.identity import run_address
from etude.jobs import EvalRunSummary, TrainingRunSummary
from etude.launch.listing import describe_machine, launcher_facts, machine_warning
from etude.launch.logs import ConsoleTail
from etude.launch.records import MachineStatus, Standing, Status
from etude.launch.server import LauncherObservation, PythonClient, selected
from etude.launch.source import line
from etude.page import Pages, read_report, render, render_run, report_sources, run_chart_data
from etude.report import Report
from etude.run import LocatedRun, as_storage, located_runs, owning_folder
from etude.spec import number
from etude.storage import ListedStorage, Storage
from etude.train.read import RunData, read_located
from etude.view_link import advertise, run_path

_REFRESH = 5
_TRAINING_TABS = "".join(
    f'<a href="#{section.lower()}" data-s="{section.lower()}">{section}</a>'
    for section in ("Console", "Config", "Checkpoints")
)
_REMOTE_STANDING = 30
_SILENT = timedelta(minutes=10)
_CLOCKS = TypeAdapter(dict[str, float])

# A changed schema starts a fresh rebuildable index.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, job TEXT NOT NULL, state TEXT NOT NULL,
    last_write TEXT, status TEXT, launched_as TEXT, max_attempts INTEGER,
    position TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS launcher_address ON runs(launched_as);
CREATE INDEX IF NOT EXISTS experiment_runs ON runs(experiment, address);
CREATE TABLE IF NOT EXISTS charts (
    address TEXT PRIMARY KEY, inputs TEXT NOT NULL, reached TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consoles (
    run TEXT PRIMARY KEY, body TEXT NOT NULL, collected TEXT, gaps INTEGER NOT NULL, final_attempt INTEGER
);
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, launcher_at TEXT, last_loop TEXT, server TEXT, machines TEXT
);
"""


@dataclass(frozen=True)
class _RunFiles:
    versions: str
    data: RunData


class ChartIndex:
    def __init__(self, source: Storage, path: Path | None) -> None:
        self.source = source
        # A view of edited report code and one using the default panels may
        # run side by side. Neither may serve the other's last generation.
        identity = "\0".join(
            (
                source.uri("run.json"),
                str(path.resolve()) if path else "",
                version("etude-sdk"),
                _SCHEMA,
            )
        )
        directory = home() / "views"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = directory / (sha256(identity.encode()).hexdigest() + ".sqlite")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
        self._cache = Cache()
        self._files: dict[str, dict[str, _RunFiles]] = {}
        self._reports: dict[Path | None, tuple[tuple[tuple[str, int, int], ...], Report]] = {}
        self._launcher = LauncherObservation()
        self._wanted: set[str] = set()
        self._console_lock = threading.Lock()
        self._server: str | None = None
        self._launcher_at: datetime | None = None
        self._next_standing = 0.0
        self._launcher_error: str | None = None
        self.error: str | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def _poll_launcher(self) -> None:
        if time.monotonic() < self._next_standing:
            return
        remote = selected()
        try:
            with self._console_lock:
                wanted, self._wanted = self._wanted, set()
            consoles = tuple(sorted(wanted))
            self._launcher = (
                PythonClient(remote).observation(consoles)
                if remote
                else LauncherObservation.read(consoles, machines=True)
            )
            self._server = remote
            self._launcher_at = datetime.now(UTC)
            self._launcher_error = None
        except Exception as error:
            self._launcher_error = explain(error)
        self._next_standing = time.monotonic() + (_REMOTE_STANDING if remote else 0)

    def _read_run(self, storage: ListedStorage, members: list[str], run: LocatedRun) -> _RunFiles:
        # Compare listed versions, including removals and replacement at one
        # key. The point reader resolves compaction, attempts and duplicate copies.
        digest = sha256()
        for key in members:
            digest.update(f"{key}\0{storage.objects[key]}\0".encode())
        version = digest.hexdigest()
        previous = self._files.get(run.file.experiment, {}).get(run.name)
        if previous and previous.versions == version:
            if previous.data.last_write == run.last_write:
                return previous
            # An open block may advance the write time without changing the sealed curves.
            return _RunFiles(
                version, previous.data.model_copy(update={"last_write": run.last_write})
            )
        # Lazy block reads keep this listing alive. Keep only this run's keys.
        local = ListedStorage(self.source, {k: storage.objects[k] for k in members}, self._cache)
        data = read_located(local, run, sorted(members), whole_blocks=True)
        return _RunFiles(version, data)

    def refresh(self, files: dict[str, Path]) -> None:
        """Ingest once. Only the owning loop calls this; HTTP readers never do."""
        # WAL readers keep their committed snapshot while one view writes the next
        # generation. Another view can skip this poll before doing any storage I/O.
        with self._connect() as db:
            db.execute("PRAGMA busy_timeout=0")
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    return
                raise
            observed = datetime.now(UTC)
            self._poll_launcher()
            if (
                self._launcher_error
                and self._launcher_at is None
                and db.execute(
                    "SELECT 1 FROM runs WHERE launched_as IS NOT NULL LIMIT 1"
                ).fetchone()
            ):
                # On restart, an unavailable launcher has not released its indexed runs.
                self.error = self._launcher_error
                return
            current = self._read_files()
            self._index_runs(db, current, files)
            self._publish_observation(db, observed)
        self.error = self._launcher_error
        self._files = current
        self._launcher = self._launcher.model_copy(update={"consoles": {}})

    def _read_files(self) -> dict[str, dict[str, _RunFiles]]:
        objects = self.source.versions("")
        storage = ListedStorage(self.source, objects, self._cache)
        keys = sorted(objects)
        runs = located_runs(storage, keys, objects=objects)
        roots = {folder for run in runs for folder in run.folders}
        members: dict[str, list[str]] = defaultdict(list)
        for key in keys:
            folder = owning_folder(key, roots)
            if folder and not key.endswith(".open"):
                members[folder].append(key)
        files: dict[str, dict[str, _RunFiles]] = defaultdict(dict)
        for run in runs:
            keys = [key for folder in run.folders for key in members[folder]]
            files[run.file.experiment][run.name] = self._read_run(storage, keys, run)
        return files

    def _report(self, file: Path | None) -> Report:
        kept = self._reports.get(file)
        if kept is None or kept[0] != report_sources(file):
            report = read_report(file)
            # Loading the declaration can import new neighbours.
            kept = self._reports[file] = (report_sources(file), report)
        return kept[1]

    def _index_runs(
        self, db: sqlite3.Connection, files: dict[str, dict[str, _RunFiles]], paths: dict[str, Path]
    ) -> None:
        removed = {row[0] for row in db.execute("SELECT address FROM runs")}
        charts = {
            row["address"]: row for row in db.execute("SELECT address,inputs,reached FROM charts")
        }
        standing: dict[str, dict[str, Standing]] = defaultdict(dict)
        for held in self._launcher.runs:
            standing[held.record.experiment][held.record.id] = held
        for experiment in sorted(files.keys() | standing.keys()):
            stored = files.get(experiment, {})
            retained = standing[experiment]
            state = state_of(
                [f.data for f in stored.values()], experiment, retained=tuple(retained.values())
            )
            report = (
                self._report(paths.get(experiment))
                if any(r.name in stored for r in state.runs)
                else None
            )
            runs: tuple[TrainingRunSummary | EvalRunSummary, ...] = (*state.runs, *state.eval_runs)
            for run in runs:
                inputs = stored.get(run.name)
                data = inputs.data if inputs else None
                item = retained[run.status.run] if run.status else None
                if data and data.file:
                    address = run_address(data.file.id, experiment)
                else:
                    assert item is not None  # Every summary has files or a launcher record.
                    address = item.record.id
                reached: dict[str, float] = {}
                if isinstance(run, TrainingRunSummary) and report and inputs:
                    reached = self._chart(db, address, inputs, report, charts.pop(address, None))
                position = _position(item, reached, report.clock if report else None, data)
                _store_run(db, address, experiment, run, data, item, position)
                removed.discard(address)
        db.executemany("DELETE FROM charts WHERE address=?", ((a,) for a in charts))
        db.executemany("DELETE FROM runs WHERE address=?", ((a,) for a in removed))

    def _chart(
        self,
        db: sqlite3.Connection,
        address: str,
        files: _RunFiles,
        report: Report,
        previous: sqlite3.Row | None,
    ) -> dict[str, float]:
        inputs = files.versions + report.model_dump_json(exclude_unset=True)
        if previous is not None and previous["inputs"] == inputs:
            return _CLOCKS.validate_json(previous["reached"])
        # Charting materializes block arrays. Keep only metadata on the cached RunData.
        run = files.data.model_copy()
        payload = run_chart_data(self.source, run, report)
        reached = run.reached
        db.execute(
            "INSERT OR REPLACE INTO charts(address,inputs,reached,payload) VALUES (?,?,?,?)",
            (address, inputs, _CLOCKS.dump_json(reached).decode(), payload),
        )
        return reached

    def _publish_observation(self, db: sqlite3.Connection, observed: datetime) -> None:
        if self._launcher_at is None:
            # On a failed first poll, advance only the file observation.
            db.execute(
                """INSERT INTO observation(id,at) VALUES(1,?)
                   ON CONFLICT(id) DO UPDATE SET at=excluded.at""",
                (observed.isoformat(),),
            )
            return
        # An address can belong to another run after switching servers.
        db.execute(
            """DELETE FROM consoles WHERE EXISTS (SELECT 1 FROM observation WHERE server IS NOT ?)
               OR NOT EXISTS (SELECT 1 FROM runs WHERE launched_as=consoles.run)""",
            (self._server,),
        )
        machines = self._launcher.machines
        db.execute(
            """INSERT OR REPLACE INTO observation(id,at,launcher_at,last_loop,server,machines)
               VALUES (1,?,?,?,?,?)""",
            (
                observed.isoformat(),
                self._launcher_at.isoformat(),
                self._launcher.last_loop.isoformat() if self._launcher.last_loop else None,
                self._server,
                _machines_table(db, machines) if machines is not None else None,
            ),
        )
        db.executemany(
            """INSERT OR REPLACE INTO consoles(run,body,collected,gaps,final_attempt)
               VALUES(:run,:body,:collected,:gaps,:final_attempt)""",
            (
                tail.model_dump(mode="json") | {"run": run}
                for run, tail in self._launcher.consoles.items()
            ),
        )

    def run(self, address: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT runs.*, charts.payload, observation.at, observation.launcher_at, observation.last_loop,
                       consoles.body, consoles.collected, consoles.gaps, consoles.final_attempt AS console_attempt FROM runs
                LEFT JOIN consoles ON consoles.run=runs.launched_as
                LEFT JOIN charts USING(address) JOIN observation ON observation.id=1
                WHERE runs.address=? OR runs.launched_as=?
                """,
                (address, address),
            ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC)
        state = _display_state(row, now)
        status = _status(row)
        details = [f"last write {_age(row['last_write'], now)}"]
        details.extend(_observation_ages(row, now, launcher=bool(row["launched_as"])))
        evaluation = row["kind"] == "evaluation"
        experiment = escape(row["experiment"])
        page = f"/experiments/{quote(row['experiment'])}"
        title = f"Evaluation · {escape(address)}" if evaluation else escape(address)
        heading = (
            f'<a href="{page}">{experiment}</a>'
            f'<header class="head"><h1>{title}</h1></header>'
            f"<p>{_run_line(row, state, status)}</p>"
            f'<p class="context">{escape(" · ".join(details))}</p>'
        )
        if evaluation:
            heading += f'<p>Scores are in the <a href="{page}#report">{experiment} report</a>.</p>'
        if state == "no end recorded":
            heading += f"<p>No new file for at least {_SILENT // timedelta(minutes=1)} minutes.</p>"
        if self.error:
            heading += f'<p role="status">{escape(self.error)}</p>'
        # Session shutdown can still retain final logs after the run itself ended.
        awaiting_console = status is not None and (
            not status.done or row["console_attempt"] != status.attempts
        )
        if awaiting_console:
            with self._console_lock:
                self._wanted.add(row["launched_as"])
        if _is_open(status, state) or awaiting_console or self.error:
            heading += _reload()
        if evaluation and status is None:
            return _document(address, heading)
        below = _console(
            row, now, unavailable=bool(self._launcher_error and self._launcher_at is None)
        )
        if evaluation:
            return _document(address, heading + below)
        below += row["details"]
        if row["payload"] is None:
            tabs = f'<nav class="tabs" aria-label="Run sections">{_TRAINING_TABS}</nav>'
            return _document(address, heading + tabs + "<p>No metric curves recorded.</p>" + below)
        return render_run(address, row["payload"], heading, below, _TRAINING_TABS)

    def _list_runs(
        self, experiment: str | None = None
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        # Both tables and their ages come from one generation. Leave large chart,
        # console and machine payloads out of these reads.
        query = (
            "SELECT experiment,address,job,state,position,status,max_attempts,last_write FROM runs"
        )
        with self._connect() as db:
            db.execute("BEGIN")
            observation = db.execute(
                "SELECT at,last_loop,launcher_at FROM observation WHERE id=1"
            ).fetchone()
            rows = (
                db.execute(query + " ORDER BY experiment,address")
                if experiment is None
                else db.execute(query + " WHERE experiment=? ORDER BY address", (experiment,))
            )
            return observation, rows.fetchall()

    def experiments(self) -> str:
        observation, rows = self._list_runs()
        now = datetime.now(UTC)
        opened: list[str] = []
        completed: list[str] = []
        stale = False
        for name, runs in groupby(rows, itemgetter("experiment")):
            counts: Counter[str] = Counter()
            active = []
            latest: str | None = None
            for run in runs:
                state = _display_state(run, now)
                counts[state] += 1
                # The index stores UTC write times in ISO order.
                written = run["last_write"]
                if written and (latest is None or written > latest):
                    latest = written
                status = _status(run)
                if _is_open(status, state):
                    address = run["address"]
                    detail = _run_line(run, state, status)
                    active.append(f"<div>{_run_link(address)} · {detail}</div>")
            totals = " · ".join(f"{counts[state]} {escape(state)}" for state in sorted(counts))
            stale |= "no end recorded" in counts
            group = opened if active else completed
            group.append(
                f'<tr><th scope="row"><a href="/experiments/{quote(name)}">{escape(name)}</a></th>'
                f"<td>{totals}{''.join(active)}</td><td>{_age(latest, now)}</td></tr>"
            )
        if observation:
            body = (
                _experiments_table("Open", opened) + _experiments_table("Completed", completed)
                if rows
                else "<p>Waiting for runs.</p>"
            )
            ages = _observation_ages(observation, now, launcher=any(row["status"] for row in rows))
            body += f'<p class="context">{escape(" · ".join(ages))}</p>'
        else:
            body = "<p>Waiting for the first observation.</p>"
        return _live_status(body, active=bool(opened) or not rows, stale=stale, error=self.error)

    def machines(self) -> str:
        if remote := selected():
            return _live_status(
                f"<p>The launcher database is on {escape(remote)}. "
                "Run <code>etude machines</code> to list its machines.</p>",
                active=False,
            )
        with self._connect() as db:
            row = db.execute(
                "SELECT at,launcher_at,last_loop,server,machines FROM observation WHERE id=1"
            ).fetchone()
        active = True
        if row is None or row["server"] is not None:
            body = "<p>Waiting for the first local observation.</p>"
        elif row["machines"] is not None:
            ages = " · ".join(_observation_ages(row, datetime.now(UTC), launcher=True))
            body = row["machines"] + f'<p class="context">{escape(ages)}</p>'
        elif self.error:
            body = "<p>Machine listing unavailable.</p>"
        else:
            body = "<p>No launcher is used here.</p>"
            active = False
        return _live_status(body, active=active, error=self.error)

    def has_experiment(self, experiment: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM runs WHERE experiment=? LIMIT 1", (experiment,)
                ).fetchone()
                is not None
            )

    def experiment_status(self, experiment: str, *, declared: bool) -> str | None:
        observation, rows = self._list_runs(experiment)
        if not declared and not rows:
            return None
        now = datetime.now(UTC)
        active = observation is None
        stale = False
        body = []
        for row in rows:
            state = _display_state(row, now)
            stale |= state == "no end recorded"
            status = _status(row)
            active |= _is_open(status, state)
            address = row["address"]
            body.append(
                f"<tr><td>{escape(row['job'])}</td>"
                f"<td>{_run_link(address)}</td>"
                f"<td>{_run_line(row, state, status)}</td><td>{_age(row['last_write'], now)}</td></tr>"
            )
        if body:
            table = _table(("Job", "Run", "Status", "Last write"), body)
        elif observation:
            table = "<p>No runs recorded.</p>"
        else:
            table = "<p>Waiting for the first observation.</p>"
        ages = ""
        if observation:
            launcher = any(row["status"] for row in rows)
            ages = " · ".join(_observation_ages(observation, now, launcher=launcher))
        return _live_status(
            '<section class="sec"><h2>Run status</h2>'
            f'{table}<p class="context">{escape(ages)}</p>'
            '<p class="lbl">No machine samples recorded.</p></section>',
            active=active,
            stale=stale,
            error=self.error,
        )


def _store_run(
    db: sqlite3.Connection,
    address: str,
    experiment: str,
    run: TrainingRunSummary | EvalRunSummary,
    stored: RunData | None,
    item: Standing | None,
    position: str,
) -> None:
    status = run.status
    job: str | None
    if isinstance(run, TrainingRunSummary):
        job = run.job
    elif item:
        job = item.record.launch.spec.name
    else:
        job = (stored.file.job_name or stored.job) if stored and stored.file else None
    db.execute(
        """
        -- EXCEPT leaves unchanged rows alone, including rows with missing launcher facts.
        INSERT OR REPLACE INTO runs(address,experiment,job,state,last_write,status,
                                    launched_as,max_attempts,position,kind,details)
        SELECT ?,?,?,?,?,?,?,?,?,?,?
        EXCEPT SELECT address,experiment,job,state,last_write,status,
                      launched_as,max_attempts,position,kind,details FROM runs WHERE address=?
        """,
        (
            address,
            experiment,
            job or "Evaluation",
            run.state,
            run.last_write.isoformat() if run.last_write else None,
            status.model_dump_json() if status else None,
            status.run if status else None,
            item.record.launch.spec.max_attempts if item else None,
            position,
            "training" if isinstance(run, TrainingRunSummary) else "evaluation",
            _run_details(stored, item) if isinstance(run, TrainingRunSummary) else "",
            address,
        ),
    )


def _run_link(address: str) -> str:
    return f'<a href="{run_path(address)}">{escape(address)}</a>'


def _machines_table(db: sqlite3.Connection, machines: tuple[MachineStatus, ...]) -> str:
    if not machines:
        return "<p>No machines held.</p>"
    rows = []
    for status in machines:
        machine = status.machine
        runs = []
        for run in status.runs:
            row = db.execute(
                "SELECT address,state,position,status,max_attempts FROM runs WHERE launched_as=?",
                (run,),
            ).fetchone()
            address = row["address"] if row else run
            detail = _run_line(row, row["state"], _status(row)) if row else ""
            runs.append(f"<div>{_run_link(address)}{(' · ' + detail) if detail else ''}</div>")
        description = escape(describe_machine(status))
        if warning := machine_warning(status):
            description += f"<div>{escape(warning)}</div>"
        rows.append(
            f'<tr><th scope="row">{escape(machine.name)}</th><td>{escape(machine.supply)}</td>'
            f"<td>{description}</td><td>{''.join(runs) or 'Idle'}</td></tr>"
        )
    return _table(("Machine", "Pool or on-demand", "State, window and cost", "Runs"), rows)


def _status(row: sqlite3.Row) -> Status | None:
    return Status.model_validate_json(row["status"]) if row["status"] is not None else None


def _run_line(row: sqlite3.Row, state: str, status: Status | None) -> str:
    parts = [f'<span class="pill">{escape(state)}</span>']
    facts = [row["position"]]
    if status:
        if status.phase == "queued":
            facts.append(status.reason)
        elif status.done:
            if status.outcome != state and (state, status.outcome) != ("finished", "succeeded"):
                facts.append(status.outcome)
            facts.append(status.cause)
        elif status.phase in ("renting", "setting_up", "stopping"):
            facts.append(status.phase.replace("_", " "))
        facts.extend(launcher_facts(status, row["max_attempts"]))
    parts.extend(f"<span>{escape(fact)}</span>" for fact in facts if fact)
    return " · ".join(parts)


def _is_open(status: Status | None, state: str) -> bool:
    return not status.done if status else state in ("queued", "running")


def _live_status(body: str, *, active: bool, stale: bool = False, error: str | None = None) -> str:
    if error:
        body = f'<p role="status">{escape(error)}</p>' + body
    if stale:
        body += (
            f"<p>Runs with no launcher record show “no end recorded” after "
            f"{_SILENT // timedelta(minutes=1)} minutes without a write.</p>"
        )
    refresh = f' data-refresh="{_REFRESH * 1000}"' if active or error else ""
    return f'<div id="experiment-status"{refresh}>{body}</div>'


def _table(columns: tuple[str, ...], rows: list[str]) -> str:
    headers = "".join(f"<th>{escape(column)}</th>" for column in columns)
    return (
        f'<div class="scroll"><table><thead><tr>{headers}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _experiments_table(title: str, rows: list[str]) -> str:
    table = (
        _table(("Experiment", "Runs", "Last write"), rows)
        if rows
        else f"<p>No {title.lower()} experiments.</p>"
    )
    return f'<section class="sec"><h2>{title}</h2>{table}</section>'


def _status_page(title: str, status: str, *, before: str = "", after: str = "") -> str:
    script = Path(__file__).with_name("refresh_status.js").read_text()
    return _document(
        title,
        f'{before}<header class="head"><h1>{escape(title)}</h1></header>{status}'
        f'<p id="status-error" role="status"></p><script>{script}</script>{after}',
    )


def _display_state(row: sqlite3.Row, now: datetime) -> str:
    state: str = row["state"]
    if state == "running" and row["status"] is None and row["last_write"]:
        if now - datetime.fromisoformat(row["last_write"]) >= _SILENT:
            return "no end recorded"
    return state


def _observation_ages(row: sqlite3.Row, now: datetime, *, launcher: bool) -> list[str]:
    ages = [f"indexed {_age(row['at'], now)}"]
    if launcher:
        if row["last_loop"]:
            ages.append(f"last loop {_age(row['last_loop'], now)}")
        if row["launcher_at"] and (
            datetime.fromisoformat(row["at"]) - datetime.fromisoformat(row["launcher_at"])
            > timedelta(seconds=_REFRESH)
        ):
            ages.append(f"launcher observed {_age(row['launcher_at'], now)}")
    return ages


def _position(
    item: Standing | None, reached: dict[str, float], preferred: str | None, stored: RunData | None
) -> str:
    clocks = reached if stored is not None or item is None else item.status.clocks
    if item and (target := item.status.until):
        limit = number(target.value)
        at = clocks.get(target.clock)
        return (
            f"Target {target.clock} {limit}"
            if at is None
            else f"{target.clock} {number(at)} / {limit}"
        )
    if preferred not in clocks and stored:
        preferred = next((c.name for c in stored.clocks if c.name in clocks), None)
    clock = preferred if preferred in clocks else next(iter(clocks), None)
    return f"{clock} {number(clocks[clock])}" if clock else ""


def _console(row: sqlite3.Row, now: datetime, *, unavailable: bool) -> str:
    if row["body"] is not None:
        console = (
            '<pre id="retained-output" class="console" tabindex="0" aria-label="Retained console">'
            f"{escape(row['body'])}</pre>"
            '<script>{const output = document.getElementById("retained-output");'
            "output.scrollTop = output.scrollHeight;}</script>"
        )
        if row["gaps"]:
            console += "<p>Some console output is missing from retention.</p>"
        if row["collected"]:
            console = f"<p>Last collected {_age(row['collected'], now)}</p>" + console
    elif row["launched_as"]:
        console = "<p>Waiting for retained console.</p>"
    elif unavailable:
        console = "<p>Launcher observation unavailable.</p>"
    else:
        console = "<p>Console was not recorded.</p>"
    if row["launched_as"]:
        console += (
            f"<p>Showing up to {ConsoleTail.max_lines} lines. Read the full retained console with "
            f"<code>etude logs --run {escape(shlex.quote(row['launched_as']))}</code></p>"
        )
    return f'<section class="sec" id="console"><h2>Console</h2>{console}</section>'


def _run_details(stored: RunData | None, item: Standing | None) -> str:
    config = stored.file.config_json if stored and stored.file else None
    source = ""
    if item:
        record = item.record
        if config is None:
            config = record.config_json
        job = record.launch.spec
        if job.repo and job.revision:
            provenance = line(record.launch.source, job.revision)
            source = f"<p>Source {escape(provenance)}</p>"
            if record.launch.source:
                source += f"<p>Commit <code>{escape(job.revision)}</code></p>"
    shown = (
        f'<pre class="scroll">{escape(config)}</pre>'
        if config is not None
        else "<p>No config recorded.</p>"
    )
    rows = []
    for checkpoint in stored.checkpoints if stored else ():
        # Wall clocks are fractional hours; four significant digits read as a position.
        clocks = ", ".join(
            f"{k} {number(v) if float(v).is_integer() else f'{v:.4g}'}"
            for k, v in checkpoint.at.items()
        )
        rows.append(
            f"<tr><td>{checkpoint.ordinal}</td><td>{escape(checkpoint.name)}</td><td>{escape(clocks)}</td></tr>"
        )
    checkpoints = (
        _table(("Ordinal", "Checkpoint", "Clock position"), rows)
        if rows
        else "<p>No checkpoints recorded.</p>"
    )
    return f'<section class="sec" id="config"><h2>Config</h2>{shown}{source}</section><section class="sec" id="checkpoints"><h2>Checkpoints</h2>{checkpoints}</section>'


def _age(at: str | None, now: datetime) -> str:
    if at is None:
        return "unknown"
    seconds = max(0, int((now - datetime.fromisoformat(at)).total_seconds()))
    for unit, scale in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= scale:
            return f"{seconds // scale} {unit} ago"
    return f"{seconds} s ago"


def _reload() -> str:
    return f"<script>setInterval(() => {{ if (!document.hidden) location.reload(); }}, {_REFRESH * 1000});</script>"


def _document(title: str, body: str) -> str:
    css = Path(__file__).with_name("page.css").read_text()
    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)} · Etude</title><style>{css}</style><main class="wrap">{body}</main></html>'
    )


class Server(ThreadingHTTPServer):
    def __init__(
        self, source: Storage | Path | str, path: Path | None, address: tuple[str, int]
    ) -> None:
        self.path = path
        self.pages = Pages(source)
        self.index = ChartIndex(as_storage(source), path)
        self.files: dict[str, Path] = {}
        self._find_files()
        self._building = threading.Lock()
        self._lock = threading.Lock()
        self._html: dict[str, str] = {}
        self._pending: dict[str, Future[str]] = {}
        self._address = ExitStack()
        super().__init__(address, _Handler)
        host = address[0] if address[0] not in ("", "0.0.0.0") else "127.0.0.1"
        self.origin = f"http://{host}:{self.server_port}"
        try:
            self._reclaim = self._address.enter_context(advertise(self.origin))
        except BaseException:
            self.server_close()
            raise

    def server_close(self) -> None:
        try:
            self._address.close()
        finally:
            super().server_close()

    def _find_files(self) -> str | None:
        """Update `files`, or say why the folder's files cannot be told apart.

        The last known files keep their reports meanwhile; runs come from the
        source and keep refreshing.
        """
        if self.path is None:
            return None
        try:
            files = self.pages.find_files(self.path) if self.path.is_dir() else [self.path]
        except Exception as error:
            return explain(error)
        self.files = {file.stem: file for file in files}
        return None

    def ingest(self) -> None:
        unfound = self._find_files()
        try:
            self._reclaim()
            self.index.refresh(self.files)
        except Exception as error:
            self.index.error = explain(error)
            return
        # A poll skipped for a concurrent view keeps the previous error, which may hold it.
        if unfound and unfound not in (self.index.error or ""):
            self.index.error = "\n".join(filter(None, (self.index.error, unfound)))

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        stop = threading.Event()

        def ingest() -> None:
            while not stop.is_set():
                self.ingest()
                stop.wait(_REFRESH)

        writer = threading.Thread(target=ingest, name="etude-view-index")
        writer.start()
        try:
            super().serve_forever(poll_interval)
        finally:
            stop.set()
            writer.join()

    def page(self, route: str, *, refresh: bool = False) -> str | None:
        path = unquote(urlsplit(route).path)
        if path.startswith("/runs/"):
            return self.index.run(path.removeprefix("/runs/"))
        if path in ("/", "/status"):
            overview = self.index.experiments()
            return (
                _status_page("Experiments", overview, before='<a href="/machines">Machines</a>')
                if path == "/"
                else overview
            )
        if path in ("/machines", "/machines/status"):
            listing = self.index.machines()
            return (
                _status_page("Machines", listing, before='<a href="/">Experiments</a>')
                if path == "/machines"
                else listing
            )
        if not path.startswith("/experiments/"):
            return None
        experiment, _, component = path.removeprefix("/experiments/").partition("/")
        declared = experiment in self.files
        if component == "report":
            if declared or self.index.has_experiment(experiment):
                return self._report(experiment, refresh)
            return None
        if component not in ("", "status"):
            return None
        status = self.index.experiment_status(experiment, declared=declared)
        if component or status is None:
            return status
        return _status_page(
            experiment,
            status,
            before='<a href="/">Experiments</a>',
            after=f'<iframe id="report" title="Experiment report" src="/experiments/{quote(experiment)}/report" style="width:100%;height:80vh;border:0"></iframe>',
        )

    def _report(self, experiment: str, refresh: bool) -> str:
        """Join concurrent refreshes. A failed report leaves its last good HTML visible."""
        with self._lock:
            if not refresh and experiment in self._html:
                return self._html[experiment]
            pending = self._pending.get(experiment)
            owner = pending is None
            if pending is None:
                pending = Future()
                self._pending[experiment] = pending
        if owner:
            try:
                with self._building:
                    file = self.files.get(experiment)
                    page = self.pages.build(file, experiment=experiment)
                    control = (
                        '<button id="refresh-report">Refresh report</button> '
                        '<span id="refresh-status" role="status"></span>'
                        f"<script>{Path(__file__).with_name('refresh_report.js').read_text()}</script>"
                    )
                    html = render(page, heading=control)
                with self._lock:
                    self._html[experiment] = html
                    pending.set_result(html)
                    del self._pending[experiment]
            except BaseException as error:
                with self._lock:
                    pending.set_exception(error)
                    del self._pending[experiment]
        return pending.result()


class _Handler(BaseHTTPRequestHandler):
    server: Server

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/favicon.ico":
            # Browsers ask on every page; the view has no icon and says so without an error.
            self.send_response(204)
            self.end_headers()
            return
        self._respond(refresh=False)

    def do_POST(self) -> None:  # noqa: N802
        if not urlsplit(self.path).path.endswith("/report"):
            self.send_error(405)
            return
        self._respond(refresh=True)

    def _respond(self, *, refresh: bool) -> None:
        html: str | None = None
        try:
            html = self.server.page(self.path, refresh=refresh)
            status = 200 if html else 404
            if html is None and urlsplit(self.path).path.startswith("/runs/"):
                # A train prints its link before the next listing can discover it.
                index = self.server.index
                source = index.source.uri("run.json").removesuffix("run.json")
                message = f"Waiting for the run to appear in {source}."
                if index.error:
                    message += " " + index.error
                html = _document("Run", f'<p role="status">{escape(message)}</p>{_reload()}')
            body = (html or "Not found.").encode()
        except ValueError as error:
            body, status = explain(error).encode(), 404
        except Exception as error:
            body, status = explain(error).encode(), 500
        self.send_response(status)
        self.send_header("Content-Type", f"text/{'html' if html else 'plain'}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve(source: Storage, path: Path | None, address: tuple[str, int]) -> None:
    with Server(source, path, address) as server:

        def stop_view(signum: int, frame: FrameType | None) -> None:
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, stop_view)
        try:
            print(server.origin + "/", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous)
