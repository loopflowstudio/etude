"""Live run and experiment pages. One ingestion loop publishes the chart index for all readers.

Run files and launcher standing are joined by state_of. Only derived chart data
and display facts live in the index. Publication is one SQLite transaction
after storage reads and rendering, so readers can keep using the previous
generation through a slow or interrupted ingestion. Reports build on demand
in separate request threads.
"""

from __future__ import annotations

import fcntl
import signal
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import closing, contextmanager
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
from etude.identity import run_address
from etude.jobs import EvalRunSummary, ExperimentState, TrainingRunSummary
from etude.launch.listing import describe_machine, describe_status, machine_warning
from etude.launch.logs import render as render_logs
from etude.launch.records import MachineStatus, Standing
from etude.launch.server import LauncherObservation, PythonClient, selected
from etude.launch.source import home, line
from etude.page import Pages, read_report, render, render_run, report_sources, run_chart_data
from etude.report import Report
from etude.run import LocatedRun, as_storage, located_runs, owning_folder
from etude.spec import number
from etude.storage import ListedStorage, Storage
from etude.train.read import RunData, read_located
from etude.view_link import advertise

_REFRESH = 5
_REMOTE_STANDING = 30
_SILENT = timedelta(minutes=10)
_CLOCKS = TypeAdapter(dict[str, float])

# A changed schema starts a fresh rebuildable index.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, job TEXT NOT NULL, state TEXT NOT NULL,
    last_write TEXT, launcher TEXT, launched_as TEXT, final_attempt INTEGER,
    position TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS launcher_address ON runs(launched_as);
CREATE INDEX IF NOT EXISTS experiment_runs ON runs(experiment, address);
CREATE TABLE IF NOT EXISTS charts (
    address TEXT PRIMARY KEY, inputs TEXT NOT NULL, reached TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consoles (
    run TEXT PRIMARY KEY, body TEXT NOT NULL, collected TEXT, final_attempt INTEGER
);
CREATE TABLE IF NOT EXISTS machine_list (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, launcher_at TEXT, last_loop TEXT, server TEXT
);
"""


@dataclass
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
        self._files: dict[str, _RunFiles] = {}
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

    def _records(self) -> tuple[Standing, ...]:
        if time.monotonic() >= self._next_standing:
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
        return self._launcher.runs

    def _read_run(self, storage: ListedStorage, members: list[str], run: LocatedRun) -> _RunFiles:
        # Compare listed versions, including removals and replacement at one
        # key. The point reader resolves compaction, attempts and duplicate copies.
        digest = sha256()
        for key in members:
            digest.update(f"{key}\0{storage.objects[key]}\0".encode())
        version = digest.hexdigest()
        address = run_address(run.file.id, run.file.experiment)
        previous = self._files.get(address)
        if previous and previous.versions == version:
            if run.last_write != previous.data.last_write:
                return _RunFiles(
                    version, previous.data.model_copy(update={"last_write": run.last_write})
                )
            return previous
        # Lazy block reads keep this listing alive. Keep only this run's keys.
        local = ListedStorage(self.source, {k: storage.objects[k] for k in members}, self._cache)
        data = read_located(local, run, sorted(members), whole_blocks=True)
        return _RunFiles(version, data)

    def refresh(self, files: dict[str, Path]) -> None:
        """Ingest once. Only the owning loop calls this; HTTP readers never do."""
        # Two views of the same source can reuse this index. Its SQLite writer
        # lock is held only at publication, never across a bucket listing.
        with self.database.with_suffix(".lock").open("a") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self._connect() as db:
                self._ingest(db, files)
            # A selected server is polled less often than storage. Its large console
            # snapshots have been published and can be released between polls.
            self._launcher = self._launcher.model_copy(update={"consoles": {}})

    def _ingest(self, db: sqlite3.Connection, files: dict[str, Path]) -> None:
        observed = datetime.now(UTC)
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
        by_experiment: dict[str, list[RunData]] = defaultdict(list)
        retained: dict[str, list[Standing]] = defaultdict(list)
        for item in self._records():
            retained[item.record.experiment].append(item)
        if (
            self._launcher_error
            and self._launcher_at is None
            and db.execute("SELECT 1 FROM runs WHERE launched_as IS NOT NULL LIMIT 1").fetchone()
        ):
            # After a restart, a failed first ask is no evidence that the
            # launcher released any previously indexed run.
            self.error = self._launcher_error
            return
        # A changed launcher target can use sealed clocks without decoding the curves.
        indexed = {
            row["address"]: row for row in db.execute("SELECT address,inputs,reached FROM charts")
        }
        positions: dict[str, dict[str, float]] = {}
        # Stage large payloads outside the main database, one run at a time.
        db.execute("PRAGMA temp_store=FILE")
        db.execute(
            "CREATE TEMP TABLE changed(address TEXT, inputs TEXT, reached TEXT, payload TEXT)"
        )
        current: dict[str, _RunFiles] = {}
        reports: dict[str, Report] = {}
        for located in runs:
            experiment = located.file.experiment
            address = run_address(located.file.id, experiment)
            inputs = self._read_run(
                storage, [k for f in located.folders for k in members[f]], located
            )
            current[address] = inputs
            by_experiment[experiment].append(inputs.data)
            if located.file.kind == "evaluation":
                continue
            if experiment not in reports:
                file = files.get(experiment)
                sources = report_sources(file)
                kept = self._reports.get(file)
                if kept is None or sources != kept[0]:
                    report = read_report(file)
                    kept = self._reports[file] = (report_sources(file), report)
                reports[experiment] = kept[1]
            report = reports[experiment]
            fingerprint = inputs.versions + report.model_dump_json(exclude_unset=True)
            previous = indexed.get(address)
            if previous is None or previous["inputs"] != fingerprint:
                # Rendering may materialize block arrays. Only metadata lives
                # through to the next poll; raw bytes belong to the shared cache.
                chart_run = inputs.data.model_copy()
                payload = run_chart_data(self.source, chart_run, report)
                reached = chart_run.reached
                del chart_run
                db.execute(
                    "INSERT INTO changed(address,inputs,reached,payload) VALUES (?,?,?,?)",
                    (address, fingerprint, _CLOCKS.dump_json(reached).decode(), payload),
                )
            else:
                reached = _CLOCKS.validate_json(previous["reached"])
            positions[address] = reached
        states = [
            state_of(by_experiment[name], name, retained=tuple(retained[name]))
            for name in sorted(by_experiment.keys() | retained.keys())
        ]
        db.execute(
            "INSERT OR REPLACE INTO charts(address,inputs,reached,payload) SELECT address,inputs,reached,payload FROM changed"
        )
        db.executemany(
            "DELETE FROM charts WHERE address=?", ((a,) for a in indexed.keys() - positions.keys())
        )
        launched = {item.record.id: item for item in self._launcher.runs}
        self._publish_states(db, states, by_experiment, launched, positions, reports)
        if self._launcher.machines is not None:
            db.execute(
                "INSERT OR REPLACE INTO machine_list(id,body) VALUES(1,?)",
                (_machines_table(db, self._launcher.machines),),
            )
        elif self._launcher_error is None:
            db.execute("DELETE FROM machine_list")
        # An address on a different selected server can belong to a different run.
        if db.execute(
            "SELECT 1 FROM observation WHERE id=1 AND server IS NOT ?", (self._server,)
        ).fetchone():
            db.execute("DELETE FROM consoles")
        for run, snapshot in self._launcher.consoles.items():
            collected = max(
                (s.observed_at for s in snapshot.streams if s.observed_at), default=None
            )
            db.execute(
                "INSERT OR REPLACE INTO consoles(run,body,collected,final_attempt) VALUES(?,?,?,?)",
                (
                    run,
                    render_logs(snapshot),
                    collected.isoformat() if collected else None,
                    launched[run].status.attempts if snapshot.settled else None,
                ),
            )
        db.execute(
            "DELETE FROM consoles WHERE NOT EXISTS (SELECT 1 FROM runs WHERE launched_as=consoles.run)"
        )
        db.execute(
            """INSERT INTO observation(id,at,launcher_at,last_loop,server) VALUES(1,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET at=excluded.at,
                 launcher_at=coalesce(excluded.launcher_at, observation.launcher_at),
                 last_loop=CASE WHEN excluded.launcher_at IS NULL THEN observation.last_loop ELSE excluded.last_loop END,
                 server=CASE WHEN excluded.launcher_at IS NULL THEN observation.server ELSE excluded.server END""",
            (
                observed.isoformat(),
                self._launcher_at.isoformat() if self._launcher_at else None,
                self._launcher.last_loop.isoformat() if self._launcher.last_loop else None,
                self._server,
            ),
        )
        self._files = current
        self.error = self._launcher_error

    @staticmethod
    def _publish_states(
        db: sqlite3.Connection,
        states: list[ExperimentState],
        data: dict[str, list[RunData]],
        launched: dict[str, Standing],
        positions: dict[str, dict[str, float]],
        reports: dict[str, Report],
    ) -> None:
        removed = {row[0] for row in db.execute("SELECT address FROM runs")}
        for state in states:
            files = {r.name: r for r in data[state.experiment]}
            runs: tuple[TrainingRunSummary | EvalRunSummary, ...] = (*state.runs, *state.eval_runs)
            for run in runs:
                status = run.status
                item = launched[status.run] if status else None
                stored = files.get(run.name)
                address = status.run if status else run_address(run.name, state.experiment)
                if stored and stored.file:
                    address = run_address(stored.file.id, state.experiment)
                detail = describe_status(item.status, item.record) if item else None
                job: str | None
                if isinstance(run, TrainingRunSummary):
                    job = run.job
                elif item:
                    job = item.record.launch.spec.name
                else:
                    job = (stored.file.job_name or stored.job) if stored and stored.file else None
                report = reports.get(state.experiment)
                removed.discard(address)
                db.execute(
                    """
                    INSERT INTO runs(address,experiment,job,state,last_write,launcher,launched_as,final_attempt,position,details) VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(address) DO UPDATE SET experiment=excluded.experiment,job=excluded.job,
                      state=excluded.state,last_write=excluded.last_write,launcher=excluded.launcher,launched_as=excluded.launched_as,
                      final_attempt=excluded.final_attempt,position=excluded.position,details=excluded.details
                    WHERE (experiment,job,state,last_write,launcher,launched_as,final_attempt,position,details) IS NOT
                          (excluded.experiment,excluded.job,excluded.state,excluded.last_write,excluded.launcher,excluded.launched_as,excluded.final_attempt,excluded.position,excluded.details)
                    """,
                    (
                        address,
                        state.experiment,
                        job or "Evaluation",
                        run.state,
                        run.last_write.isoformat() if run.last_write else None,
                        detail,
                        status.run if status else None,
                        status.attempts if status and status.done else None,
                        _position(
                            item,
                            positions.get(address, {}),
                            report.clock if report else None,
                            stored,
                        ),
                        _run_details(stored, item),
                    ),
                )
        db.executemany("DELETE FROM runs WHERE address=?", ((a,) for a in removed))

    def run(self, address: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT runs.*, charts.payload, observation.at, observation.launcher_at, observation.last_loop,
                       consoles.body, consoles.collected, consoles.final_attempt AS console_attempt FROM runs
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
        details = [row["launcher"]] if row["launcher"] else []
        details.append(f"last write {_age(row['last_write'], now)}")
        details.extend(_observation_ages(row, now, launcher=bool(row["launched_as"])))
        if row["launched_as"]:
            with self._console_lock:
                self._wanted.add(row["launched_as"])
        heading = (
            f'<a href="/experiments/{quote(row["experiment"])}">{escape(row["experiment"])}</a>'
            f'<header class="head"><h1>{escape(address)}</h1><span class="pill">{escape(state)}</span></header>'
            f'<p class="context">{escape(" · ".join(details))}</p>'
            f"<p>{escape(row['position'])}</p>"
        )
        if state == "no end recorded":
            heading += f"<p>No new file for at least {_SILENT // timedelta(minutes=1)} minutes.</p>"
        if self.error:
            heading += f'<p role="status">{escape(self.error)}</p>'
        # Session shutdown can still retain final logs after the run itself ended.
        awaiting_console = row["launched_as"] and (
            row["final_attempt"] is None or row["console_attempt"] != row["final_attempt"]
        )
        if state in ("queued", "running") or awaiting_console or self.error:
            heading += _reload()
        console = (
            "<p>Launcher observation unavailable.</p>"
            if self._launcher_error and self._launcher_at is None
            else "<p>Console was not recorded.</p>"
        )
        if row["launched_as"]:
            console = "<p>Waiting for retained console.</p>"
            if row["body"] is not None:
                console = f'<pre class="console" tabindex="0" aria-label="Retained console">{escape(row["body"])}</pre>'
                if row["collected"]:
                    console = f"<p>Last collected {_age(row['collected'], now)}</p>" + console
        below = f'<section class="sec"><h2>Console</h2>{console}</section>' + row["details"]
        if row["payload"] is None:
            return _document(address, heading + "<p>No metric curves recorded.</p>" + below)
        return render_run(address, row["payload"], heading, below)

    def experiments(self) -> str:
        with self._connect() as db:
            db.execute("BEGIN")
            observation = db.execute("SELECT * FROM observation WHERE id=1").fetchone()
            rows = db.execute(
                """
                SELECT experiment,address,state,position,launcher,final_attempt,last_write
                FROM runs ORDER BY experiment,address
                """
            ).fetchall()
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
                if _is_open(run, state):
                    address = run["address"]
                    detail = " · ".join(filter(None, (run["position"], run["launcher"] or state)))
                    active.append(
                        f'<div><a href="/runs/{quote(address, safe="/@=")}">{escape(address)}</a>'
                        f" · {escape(detail)}</div>"
                    )
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
            ages = _observation_ages(
                observation, now, launcher=any(row["launcher"] for row in rows)
            )
            body += f'<p class="context">{escape(" · ".join(ages))}</p>'
        else:
            body = "<p>Waiting for the first observation.</p>"
        if self.error:
            body += f'<p role="status">{escape(self.error)}</p>'
        return _live_status(body, active=bool(opened or self.error) or not rows, stale=stale)

    def machines(self) -> str:
        if remote := selected():
            return _live_status(
                f"<p>The launcher database is on {escape(remote)}. "
                "Run <code>etude machines</code> to list its machines.</p>",
                active=False,
                stale=False,
            )
        with self._connect() as db:
            row = db.execute(
                """SELECT observation.*, machine_list.body FROM observation
                   LEFT JOIN machine_list USING(id) WHERE observation.id=1"""
            ).fetchone()
        active = True
        if row is None or row["server"] is not None:
            body = "<p>Waiting for the first local observation.</p>"
        elif row["body"] is not None:
            ages = " · ".join(_observation_ages(row, datetime.now(UTC), launcher=True))
            body = row["body"] + f'<p class="context">{escape(ages)}</p>'
        elif self.error:
            body = "<p>Machine listing unavailable.</p>"
        else:
            body = "<p>No launcher is used here.</p>"
            active = False
        if self.error:
            body += f'<p role="status">{escape(self.error)}</p>'
        return _live_status(body, active=active, stale=False)

    def has_experiment(self, experiment: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM runs WHERE experiment=? LIMIT 1", (experiment,)
                ).fetchone()
                is not None
            )

    def experiment_status(self, experiment: str, *, declared: bool) -> str | None:
        # Read one generation, including its observation when a declared file has no runs.
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT address,job,state,position,launcher,last_write,final_attempt,at,launcher_at,last_loop
                FROM observation LEFT JOIN runs ON runs.experiment=?
                WHERE observation.id=1 ORDER BY address
                """,
                (experiment,),
            ).fetchall()
        if not declared and (not rows or rows[0]["address"] is None):
            return None
        now = datetime.now(UTC)
        active = not rows or bool(self.error)
        stale = False
        body = []
        for row in rows:
            if row["address"] is None:
                continue
            state = _display_state(row, now)
            stale |= state == "no end recorded"
            active |= _is_open(row, state)
            address = row["address"]
            body.append(
                f"<tr><td>{escape(row['job'])}</td>"
                f'<td><a href="/runs/{quote(address, safe="/@=")}">{escape(address)}</a></td>'
                f"<td>{escape(state)}</td><td>{escape(row['position'] or '—')}</td>"
                f"<td>{escape(row['launcher'] or '—')}</td><td>{_age(row['last_write'], now)}</td></tr>"
            )
        if body:
            table = (
                '<div class="scroll"><table><thead><tr><th>Job</th><th>Run</th><th>State</th>'
                "<th>Clock position</th><th>Launcher</th><th>Last write</th></tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
        elif rows:
            table = "<p>No runs recorded.</p>"
        else:
            table = "<p>Waiting for the first observation.</p>"
        ages = ""
        if rows:
            launcher = any(row["launcher"] for row in rows)
            ages = " · ".join(_observation_ages(rows[0], now, launcher=launcher))
        error = f'<p role="status">{escape(self.error)}</p>' if self.error else ""
        return _live_status(
            '<section class="sec"><h2>Run status</h2>'
            f'{error}{table}<p class="context">{escape(ages)}</p>'
            '<p class="lbl">No machine samples recorded.</p></section>',
            active=active,
            stale=stale,
        )


def _machines_table(db: sqlite3.Connection, machines: tuple[MachineStatus, ...]) -> str:
    if not machines:
        return "<p>No machines held.</p>"
    rows = []
    for status in machines:
        machine = status.machine
        runs = []
        for run in status.runs:
            row = db.execute(
                "SELECT address,state,position FROM runs WHERE launched_as=?", (run,)
            ).fetchone()
            address = row["address"] if row else run
            detail = " · ".join(filter(None, (row["state"], row["position"]))) if row else ""
            runs.append(
                f'<div><a href="/runs/{quote(address, safe="/@=")}">{escape(address)}</a>'
                f"{(' · ' + escape(detail)) if detail else ''}</div>"
            )
        description = escape(describe_machine(status))
        if warning := machine_warning(status):
            description += f"<div>{escape(warning)}</div>"
        rows.append(
            f'<tr><th scope="row">{escape(machine.name)}</th><td>{escape(machine.supply)}</td>'
            f"<td>{description}</td><td>{''.join(runs) or 'Idle'}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Machine</th><th>Pool or on-demand</th>'
        "<th>State, window and cost</th><th>Runs</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _is_open(row: sqlite3.Row, state: str) -> bool:
    return state in ("queued", "running") or (
        row["launcher"] is not None and row["final_attempt"] is None
    )


def _live_status(body: str, *, active: bool, stale: bool) -> str:
    if stale:
        body += (
            f"<p>Runs with no launcher record show “no end recorded” after "
            f"{_SILENT // timedelta(minutes=1)} minutes without a write.</p>"
        )
    refresh = f' data-refresh="{_REFRESH * 1000}"' if active else ""
    return f'<div id="experiment-status"{refresh}>{body}</div>'


def _experiments_table(title: str, rows: list[str]) -> str:
    table = (
        '<div class="scroll"><table><thead><tr><th>Experiment</th><th>Runs</th>'
        "<th>Last write</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        if rows
        else f"<p>No {title.lower()} experiments.</p>"
    )
    return f'<section class="sec"><h2>{title}</h2>{table}</section>'


def _status_page(title: str, status: str, *, before: str = "", after: str = "") -> str:
    script = Path(__file__).with_name("experiment_status.js").read_text()
    return _document(
        title,
        f'{before}<header class="head"><h1>{escape(title)}</h1></header>{status}'
        f'<p id="status-error" role="status"></p><script>{script}</script>{after}',
    )


def _display_state(row: sqlite3.Row, now: datetime) -> str:
    state: str = row["state"]
    if state == "running" and row["launcher"] is None and row["last_write"]:
        if now - datetime.fromisoformat(row["last_write"]) >= _SILENT:
            return "no end recorded"
    return state


def _observation_ages(row: sqlite3.Row, now: datetime, *, launcher: bool) -> list[str]:
    ages = [f"indexed {_age(row['at'], now)}"]
    if launcher:
        ages.append(f"last loop {_age(row['last_loop'], now)}")
        if row["launcher_at"]:
            ages.append(f"launcher observed {_age(row['launcher_at'], now)}")
    return ages


def _position(
    item: Standing | None, reached: dict[str, float], preferred: str | None, stored: RunData | None
) -> str:
    clocks = (item.status.clocks if item else {}) | reached
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
        clocks = ", ".join(f"{k} {number(v)}" for k, v in checkpoint.at.items())
        rows.append(
            f"<tr><td>{checkpoint.ordinal}</td><td>{escape(checkpoint.name)}</td><td>{escape(clocks)}</td></tr>"
        )
    checkpoints = (
        '<div class="scroll"><table><thead><tr><th>Ordinal</th><th>Checkpoint</th><th>Clock position</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else "<p>No checkpoints recorded.</p>"
    )
    return f'<section class="sec"><h2>Config</h2>{shown}{source}</section><section class="sec"><h2>Checkpoints</h2>{checkpoints}</section>'


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
        self.files = self._find_files()
        self._building = threading.Lock()
        self._lock = threading.Lock()
        self._html: dict[str, str] = {}
        self._pending: dict[str, Future[str | None]] = {}
        super().__init__(address, _Handler)

    def _find_files(self) -> dict[str, Path]:
        if self.path is None:
            return {}
        files = self.pages.find_files(self.path) if self.path.is_dir() else [self.path]
        return {file.stem: file for file in files}

    def ingest(self) -> None:
        try:
            self.files = self._find_files()
            self.index.refresh(self.files)
        except Exception as error:
            self.index.error = explain(error)

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
            after=f'<iframe title="Experiment report" src="/experiments/{quote(experiment)}/report" style="width:100%;height:80vh;border:0"></iframe>',
        )

    def _report(self, experiment: str, refresh: bool) -> str | None:
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
                    page = (
                        self.pages.build(file)
                        if file
                        else self.pages.build(None, experiment=experiment)
                    )
                    control = (
                        '<button id="refresh-report">Refresh report</button> '
                        '<span id="refresh-status" role="status"></span>'
                        f"<script>{Path(__file__).with_name('view.js').read_text()}</script>"
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
                message = escape(
                    self.server.index.error or "Waiting for the run to appear in the index."
                )
                html = _document("Run", f'<p role="status">{message}</p>{_reload()}')
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
        host = address[0] if address[0] not in ("", "0.0.0.0") else "127.0.0.1"
        origin = f"http://{host}:{server.server_port}"

        def stop_view(signum: int, frame: FrameType | None) -> None:
            raise KeyboardInterrupt

        with advertise(origin):
            previous = signal.signal(signal.SIGTERM, stop_view)
            try:
                print(origin + "/", flush=True)
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGTERM, previous)
