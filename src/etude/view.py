"""Live run and experiment pages backed by a shared, rebuildable chart index.

File and launcher observations publish independently. A single file reader stages
changed charts in a private SQLite temp table. Each publisher joins its observation
to the latest committed facts from the other source through state_of. Reports
build on demand in separate request threads.
"""

from __future__ import annotations

import fcntl
import shlex
import signal
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
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
from etude.page import (
    DEFAULT_REPORT,
    Pages,
    ReportDeclaration,
    experiment_files,
    read_report,
    render,
    render_run,
    run_chart_data,
)
from etude.records import RunRecord
from etude.report import Report
from etude.run import LocatedRun, as_storage, located_runs, owning_folder
from etude.spec import Until, number
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
# Before the first successful file pass, absence in the index is not absence in storage.
_UNREAD = "{} appear once the view has read the run files."
_CLOCKS = TypeAdapter(dict[str, float])

# A changed schema starts a fresh rebuildable index.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, kind TEXT NOT NULL, last_write TEXT,
    file_job TEXT, file_state TEXT, file_position TEXT, file_config TEXT, checkpoints TEXT,
    launched_as TEXT, launcher_job TEXT, launcher_state TEXT, launcher_position TEXT,
    launcher_config TEXT, source TEXT, status TEXT, max_attempts INTEGER
);
CREATE INDEX IF NOT EXISTS launcher_address ON runs(launched_as,experiment) WHERE launched_as IS NOT NULL;
CREATE INDEX IF NOT EXISTS experiment_runs ON runs(experiment, address);
CREATE TABLE IF NOT EXISTS files (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, inputs TEXT NOT NULL, reached TEXT,
    preferred TEXT, last_write TEXT, metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS experiment_files ON files(experiment,address);
CREATE TABLE IF NOT EXISTS charts (
    address TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consoles (
    run TEXT PRIMARY KEY, body TEXT NOT NULL, collected TEXT, gaps INTEGER NOT NULL, final_attempt INTEGER
);
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY, at TEXT, file_error TEXT, launcher_at TEXT, last_loop TEXT, server TEXT, machines TEXT, standing TEXT
);
CREATE TABLE IF NOT EXISTS declarations (
    experiment TEXT PRIMARY KEY, file TEXT NOT NULL, accepted TEXT, error TEXT
);
"""


# A file generation survives a change of launcher. Its optional launcher columns
# all belong to the endpoint in the observation committed in the same transaction.
_OBSERVED = """
WITH observed AS (
    SELECT at, file_error, server IS :remote AS matches,
           CASE WHEN server IS :remote THEN launcher_at END AS launcher_at,
           CASE WHEN server IS :remote THEN last_loop END AS last_loop
    FROM observation WHERE id=1
)
"""
_VISIBLE = f"""{_OBSERVED}, visible AS (
    SELECT address, experiment, kind, last_write, at, file_error, launcher_at, last_loop, checkpoints,
           coalesce(CASE WHEN matches THEN launcher_job END, file_job) AS job,
           coalesce(CASE WHEN matches THEN launcher_state END, file_state) AS state,
           coalesce(CASE WHEN matches THEN launcher_position END, file_position) AS position,
           coalesce(file_config, CASE WHEN matches THEN launcher_config END) AS config,
           CASE WHEN matches THEN source END AS source,
           CASE WHEN matches THEN launched_as END AS launched_as,
           CASE WHEN matches THEN status END AS status,
           CASE WHEN matches THEN max_attempts END AS max_attempts
    FROM observed CROSS JOIN runs WHERE file_state IS NOT NULL OR matches
)
"""


@dataclass(frozen=True)
class _PendingReport:
    previous: str | None
    future: Future[ReportDeclaration]


@dataclass(frozen=True)
class _LauncherRead:
    at: datetime
    observation: LauncherObservation

    @classmethod
    def read(cls, remote: str | None, consoles: tuple[str, ...]) -> _LauncherRead:
        observation = (
            PythonClient(remote).observation(consoles)
            if remote
            else LauncherObservation.read(consoles, machines=True)
        )
        return cls(datetime.now(UTC), observation)


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
        self._declarations = ThreadPoolExecutor(1, thread_name_prefix="view-report")
        self._reports: dict[Path, _PendingReport] = {}
        self._ssh = ThreadPoolExecutor(1, thread_name_prefix="view-launcher")
        self._remote_read: Future[_LauncherRead] | None = None
        self._wanted: set[str] = set()
        self._console_lock = threading.Lock()
        self._server = selected()
        self._next_standing = 0.0
        self._launcher_error: tuple[str | None, str] | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def close(self) -> None:
        self._declarations.shutdown(cancel_futures=True)
        self._ssh.shutdown(cancel_futures=True)

    def declared_files(self) -> dict[str, Path]:
        with self._connect() as db:
            return {
                row[0]: Path(row[1])
                for row in db.execute("SELECT experiment,file FROM declarations")
            }

    def _poll_launcher(self, remote: str | None) -> _LauncherRead | None:
        if remote != self._server:
            if self._remote_read:
                self._remote_read.cancel()
            self._remote_read = None
            self._next_standing = 0
            self._launcher_error = None
            with self._console_lock:
                self._wanted.clear()
                self._server = remote
        if remote and self._remote_read is None and time.monotonic() < self._next_standing:
            return None
        try:
            if self._remote_read is None:
                with self._console_lock:
                    consoles = tuple(sorted(self._wanted))
                    self._wanted.clear()
                if remote is None:
                    self._launcher_error = None
                    return _LauncherRead.read(None, consoles)
                self._remote_read = self._ssh.submit(_LauncherRead.read, remote, consoles)
            if not self._remote_read.done():
                return None
            answer = self._remote_read.result()
        except Exception as error:
            self._launcher_error = (remote, explain(error))
            self._remote_read = None
            self._next_standing = time.monotonic() + _REMOTE_STANDING
            return None
        self._launcher_error = None
        return answer

    def read_error(
        self, remote: str | None, file_error: str | None, *, report: str | None = None
    ) -> str:
        launcher = self._launcher_error
        selected_error = launcher[1] if launcher and launcher[0] == remote else None
        return "\n".join(filter(None, (file_error, selected_error, report)))

    @property
    def error(self) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT file_error FROM observation WHERE id=1").fetchone()
            return row[0] if row else None

    def refresh_launcher(self) -> None:
        remote = selected()
        answer = self._poll_launcher(remote)
        if answer is None:
            return
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if remote == selected():
                    self._publish_launcher(db, remote, answer)
        except Exception as error:
            self._launcher_error = (remote, explain(error))
            return
        if self._remote_read:
            self._remote_read = None
            self._next_standing = time.monotonic() + _REMOTE_STANDING

    def _publish_launcher(
        self, db: sqlite3.Connection, remote: str | None, answer: _LauncherRead
    ) -> None:
        _select_launcher(db, remote)
        launcher = answer.observation
        stamp = answer.at.isoformat()
        # Another view can publish a newer answer before this poll acquires the writer.
        updated = db.execute(
            """UPDATE observation SET launcher_at=?, last_loop=?, standing=?
               WHERE id=1 AND (launcher_at IS NULL OR launcher_at<?)""",
            (
                stamp,
                launcher.last_loop.isoformat() if launcher.last_loop else None,
                launcher.model_dump_json(exclude={"consoles"}, round_trip=True),
                stamp,
            ),
        )
        if updated.rowcount:
            db.executemany(
                """INSERT OR REPLACE INTO consoles(run,body,collected,gaps,final_attempt)
                   VALUES(:run,:body,:collected,:gaps,:final_attempt)""",
                (
                    tail.model_dump(mode="json") | {"run": run}
                    for run, tail in launcher.consoles.items()
                ),
            )
            self._publish_runs(
                db,
                launcher,
                _launched_experiments(db) | {s.record.experiment for s in launcher.runs},
            )

    def refresh_files(self, files: dict[str, Path], discovery_error: str | None) -> None:
        # File ownership spans storage I/O. A competing view can still publish its launcher.
        with self.database.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                with self._connect() as db:
                    observed_at = self._prepare_files(db, files)
                    # Release the preparation snapshot before reading the latest launcher.
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
                    self._publish_files(db, observed_at, discovery_error)
            except Exception as failure:
                with self._connect() as db:
                    db.execute(
                        """INSERT INTO observation(id,file_error) VALUES(1,?)
                           ON CONFLICT(id) DO UPDATE SET file_error=excluded.file_error""",
                        (explain(failure),),
                    )

    def _report(self, db: sqlite3.Connection, experiment: str, file: Path | None) -> Report:
        if file is None:
            return DEFAULT_REPORT
        row = db.execute(
            "SELECT accepted,error FROM next_declarations WHERE experiment=?", (experiment,)
        ).fetchone()
        encoded = row["accepted"] if row else None
        accepted = ReportDeclaration.model_validate_json(encoded) if encoded else None
        pending = self._reports.get(file)
        if pending is None or pending.future.done():
            error = row["error"] if row else None
            # Another view may have accepted newer panels during this read.
            if pending and pending.previous == encoded:
                try:
                    accepted = pending.future.result()
                    encoded = accepted.encoded()
                    error = None
                except (Exception, SystemExit) as failure:
                    error = f"Report declaration: {explain(failure)}"
            if row is None or (encoded, error) != (row["accepted"], row["error"]):
                db.execute(
                    """INSERT OR REPLACE INTO next_declarations(experiment,file,accepted,error)
                       VALUES(?,?,?,?)""",
                    (experiment, str(file), encoded, error),
                )
            self._reports[file] = _PendingReport(
                encoded, self._declarations.submit(read_report, file, accepted)
            )
        return accepted.report if accepted else DEFAULT_REPORT

    def _prepare_files(self, db: sqlite3.Connection, paths: dict[str, Path]) -> datetime:
        # Every listed run supplies its address and write date. Only changed
        # inputs need metadata and a new chart. Stage one payload at a time on disk.
        db.execute("PRAGMA temp_store=FILE")
        db.execute("CREATE TEMP TABLE next_files AS SELECT *, NULL AS payload FROM files WHERE 0")
        db.execute("CREATE UNIQUE INDEX temp.file_address ON next_files(address)")
        db.execute("CREATE TEMP TABLE next_declarations AS SELECT * FROM declarations")
        db.execute("CREATE UNIQUE INDEX temp.declared_experiment ON next_declarations(experiment)")
        for row in db.execute("SELECT experiment,file FROM declarations"):
            path = Path(row["file"])
            if paths.get(row["experiment"]) != path:
                db.execute("DELETE FROM next_declarations WHERE experiment=?", (row["experiment"],))
                if pending := self._reports.pop(path, None):
                    pending.future.cancel()

        # A slow pass can only confirm silence as of the start of its listing.
        observed_at = datetime.now(UTC)
        objects = self.source.versions("")
        storage = ListedStorage(self.source, objects, self._cache)
        keys = sorted(objects)
        runs = located_runs(storage, keys, objects=objects)
        roots = {folder for run in runs for folder in run.folders}
        members: dict[str, list[str]] = defaultdict(list)
        for key in keys:
            if not key.endswith(".open") and (folder := owning_folder(key, roots)):
                members[folder].append(key)
        previous = dict(db.execute("SELECT address,inputs FROM files"))
        reports: dict[str, Report] = {}
        for run in runs:
            experiment = run.file.experiment
            report = None
            if run.file.kind != "evaluation":
                if experiment not in reports:
                    reports[experiment] = self._report(db, experiment, paths.get(experiment))
                report = reports[experiment]
            sealed = sorted(key for folder in run.folders for key in members[folder])
            _stage_run(db, storage, run, sealed, report, previous)

        return observed_at

    def _publish_files(
        self, db: sqlite3.Connection, observed_at: datetime, discovery_error: str | None
    ) -> None:
        removed = db.execute(
            """DELETE FROM files WHERE NOT EXISTS
               (SELECT 1 FROM next_files n WHERE n.address=files.address AND n.experiment=files.experiment)
               RETURNING experiment"""
        ).fetchall()
        # An open block can advance the last write without changing a sealed curve.
        redated = db.execute(
            """UPDATE files SET last_write=n.last_write FROM next_files n
               WHERE n.address=files.address AND n.metadata IS NULL AND files.last_write IS NOT n.last_write
               RETURNING experiment"""
        ).fetchall()
        rebuilt = db.execute(
            """INSERT OR REPLACE INTO files(address,experiment,inputs,reached,preferred,last_write,metadata)
               SELECT address,experiment,inputs,reached,preferred,last_write,metadata
               FROM next_files WHERE metadata IS NOT NULL RETURNING experiment"""
        ).fetchall()
        changed = {row[0] for rows in (removed, redated, rebuilt) for row in rows}
        if _select_launcher(db, selected()):
            changed.update(_launched_experiments(db))
        if removed or rebuilt:
            db.execute(
                "DELETE FROM charts WHERE address NOT IN (SELECT address FROM files WHERE reached IS NOT NULL)"
            )
            db.execute(
                """INSERT OR REPLACE INTO charts(address,payload)
                   SELECT address,payload FROM next_files WHERE payload IS NOT NULL"""
            )
        db.execute(
            """DELETE FROM declarations WHERE experiment NOT IN (SELECT experiment FROM next_declarations)"""
        )
        db.execute("""INSERT OR REPLACE INTO declarations(experiment,file,accepted,error)
                      SELECT * FROM next_declarations EXCEPT SELECT * FROM declarations""")
        row = db.execute(
            "UPDATE observation SET at=?,file_error=? WHERE id=1 RETURNING standing",
            (observed_at.isoformat(), discovery_error),
        ).fetchone()
        if changed:
            launcher = (
                LauncherObservation.model_validate_json(row[0]) if row[0] else LauncherObservation()
            )
            self._publish_runs(db, launcher, changed)

    def _publish_runs(
        self, db: sqlite3.Connection, launcher: LauncherObservation, experiments: set[str]
    ) -> None:
        standing: dict[str, dict[str, Standing]] = defaultdict(dict)
        for held in launcher.runs:
            standing[held.record.experiment][held.record.id] = held
        for experiment in experiments:
            rows = db.execute(
                """SELECT address, json_set(metadata, '$.last_write', last_write) AS metadata,
                          reached, preferred FROM files WHERE experiment=? ORDER BY address""",
                (experiment,),
            )
            files = {row["address"]: row for row in rows}
            metadata = [RunData.model_validate_json(row["metadata"]) for row in files.values()]
            stored = {data.name: data for data in metadata}
            removed = {
                row[0]
                for row in db.execute("SELECT address FROM runs WHERE experiment=?", (experiment,))
            }
            retained = standing[experiment]
            state = state_of(metadata, experiment, retained=tuple(retained.values()))
            runs: tuple[TrainingRunSummary | EvalRunSummary, ...] = (*state.runs, *state.eval_runs)
            for run in runs:
                data = stored.get(run.name)
                record = retained[run.status.run].record if run.status else None
                if data:
                    address = data.address
                else:
                    assert record is not None  # Every summary has files or a launcher record.
                    address = record.id
                chart = files.get(address)
                _store_run(
                    db,
                    address,
                    experiment,
                    run,
                    data,
                    record,
                    _CLOCKS.validate_json(chart["reached"]) if chart and chart["reached"] else {},
                    chart["preferred"] if chart else None,
                )
                removed.discard(address)
            db.executemany("DELETE FROM runs WHERE address=?", ((a,) for a in removed))
        machines = launcher.machines
        db.execute(
            "UPDATE observation SET machines=? WHERE id=1",
            (_machines_table(db, machines) if machines is not None else None,),
        )
        db.execute(
            "DELETE FROM consoles WHERE NOT EXISTS (SELECT 1 FROM runs WHERE launched_as=consoles.run)"
        )

    def run(self, address: str) -> str | None:
        remote = selected()
        with self._connect() as db:
            row = db.execute(
                _VISIBLE
                + """
                SELECT visible.*, charts.payload, declarations.error AS report_error,
                       declarations.accepted IS NULL AND declarations.file IS NOT NULL AS pending_report,
                       consoles.body, consoles.collected, consoles.gaps, consoles.final_attempt AS console_attempt
                FROM visible
                LEFT JOIN consoles ON consoles.run=visible.launched_as
                LEFT JOIN declarations USING(experiment)
                LEFT JOIN charts USING(address)
                WHERE address=coalesce(
                    (SELECT address FROM runs WHERE address=:address),
                    (SELECT address FROM runs WHERE launched_as=:address AND (SELECT matches FROM observed))
                )
                """,
                {"remote": remote, "address": address},
            ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC)
        state = _display_state(row)
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
            heading += (
                f"<p>The last file observation found at least {_SILENT // timedelta(minutes=1)} "
                "minutes without a write.</p>"
            )
        error = self.read_error(remote, row["file_error"], report=row["report_error"])
        if error:
            heading += f'<p role="status">{escape(error)}</p>'
        # Session shutdown can still retain final logs after the run itself ended.
        awaiting_console = status is not None and (
            not status.done or row["console_attempt"] != status.attempts
        )
        if awaiting_console:
            with self._console_lock:
                if remote == self._server:
                    self._wanted.add(row["launched_as"])
        if (
            _is_open(status, state)
            or awaiting_console
            or error
            or row["at"] is None
            or row["pending_report"]
            or row["launcher_at"] is None
        ):
            heading += _reload()
        if evaluation and status is None:
            return _document(address, heading)
        below = _console(row, now)
        if evaluation:
            return _document(address, heading + below)
        below += _run_details(row)
        if row["payload"] is None:
            tabs = f'<nav class="tabs" aria-label="Run sections">{_TRAINING_TABS}</nav>'
            curves = "No metric curves recorded." if row["at"] else _UNREAD.format("Curves")
            return _document(address, heading + tabs + f"<p>{curves}</p>" + below)
        return render_run(address, row["payload"], heading, below, _TRAINING_TABS)

    def _list_runs(
        self, remote: str | None, experiment: str | None = None
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        # Both tables and their ages come from one generation. Leave large chart,
        # console and machine payloads out of these reads.
        query = _VISIBLE + (
            "SELECT experiment,address,job,state,position,status,max_attempts,last_write,at FROM visible"
        )
        params = {"experiment": experiment, "remote": remote}
        with self._connect() as db:
            db.execute("BEGIN")
            observation = db.execute(
                _OBSERVED
                + """SELECT at,file_error,last_loop,launcher_at,
                          (SELECT error FROM declarations WHERE experiment=:experiment) AS report_error
                   FROM observed""",
                params,
            ).fetchone()
            if observation is None:
                return None, []
            rows = (
                db.execute(query + " ORDER BY experiment,address", params)
                if experiment is None
                else db.execute(query + " WHERE experiment=:experiment ORDER BY address", params)
            )
            return observation, rows.fetchall()

    def experiments(self) -> str:
        remote = selected()
        observation, rows = self._list_runs(remote)
        now = datetime.now(UTC)
        opened: list[str] = []
        completed: list[str] = []
        stale = False
        for name, runs in groupby(rows, itemgetter("experiment")):
            counts: Counter[str] = Counter()
            active = []
            latest: str | None = None
            for run in runs:
                state = _display_state(run)
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
        return _live_status(
            body,
            active=bool(opened)
            or not rows
            or observation is None
            or observation["at"] is None
            or observation["launcher_at"] is None,
            stale=stale,
            error=self.read_error(remote, observation["file_error"] if observation else None),
        )

    def machines(self) -> str:
        if remote := selected():
            return _live_status(
                f"<p>The launcher database is on {escape(remote)}. "
                "Run <code>etude machines</code> to list its machines.</p>",
                active=False,
            )
        with self._connect() as db:
            row = db.execute(
                "SELECT at,file_error,launcher_at,last_loop,machines FROM observation WHERE id=1 AND server IS NULL"
            ).fetchone()
        active = True
        error = self.read_error(None, row["file_error"] if row else None)
        if row is None or (row["launcher_at"] is None and not error):
            body = "<p>Waiting for the first local observation.</p>"
        elif row["machines"] is not None:
            ages = " · ".join(_observation_ages(row, datetime.now(UTC), launcher=True))
            body = row["machines"] + f'<p class="context">{escape(ages)}</p>'
        elif row["launcher_at"] is None:
            body = "<p>Machine listing unavailable.</p>"
        else:
            body = "<p>No launcher is used here.</p>"
            active = False
        return _live_status(body, active=active, error=error)

    def has_experiment(self, experiment: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    _VISIBLE + "SELECT 1 FROM visible WHERE experiment=:experiment LIMIT 1",
                    {"experiment": experiment, "remote": selected()},
                ).fetchone()
                is not None
            )

    def experiment_status(self, experiment: str, *, declared: bool) -> str | None:
        remote = selected()
        observation, rows = self._list_runs(remote, experiment)
        if not declared and not rows:
            return None
        now = datetime.now(UTC)
        active = (
            observation is None or observation["at"] is None or observation["launcher_at"] is None
        )
        stale = False
        body = []
        for row in rows:
            state = _display_state(row)
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
        elif observation and observation["at"]:
            table = "<p>No runs recorded.</p>"
        else:
            table = "<p>Waiting for the first file observation.</p>"
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
            error=self.read_error(
                remote,
                observation["file_error"] if observation else None,
                report=observation["report_error"] if observation else None,
            ),
        )


def _stage_run(
    db: sqlite3.Connection,
    storage: ListedStorage,
    run: LocatedRun,
    sealed: list[str],
    report: Report | None,
    previous: dict[str, str],
) -> None:
    address = run_address(run.file.id, run.file.experiment)
    digest = sha256()
    for key in sealed:
        digest.update(f"{key}\0{storage.objects[key]}\0".encode())
    if report is not None:
        digest.update(report.model_dump_json(include=report.model_fields_set).encode())
    inputs = digest.hexdigest()
    metadata = reached = payload = None
    if previous.get(address) != inputs:
        data = read_located(storage, run, sealed, whole_blocks=True)
        metadata = data.model_dump_json(exclude={"last_write"}, round_trip=True)
        if report is not None:
            payload = run_chart_data(storage.storage, data, report)
            reached = _CLOCKS.dump_json(data.reached).decode()
    db.execute(
        """INSERT INTO next_files(address,experiment,inputs,reached,preferred,last_write,metadata,payload)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            address,
            run.file.experiment,
            inputs,
            reached,
            report.clock if report else None,
            run.last_write.isoformat() if run.last_write else None,
            metadata,
            payload,
        ),
    )


def _launched_experiments(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute("SELECT experiment FROM runs WHERE launched_as IS NOT NULL")
    }


def _select_launcher(db: sqlite3.Connection, remote: str | None) -> bool:
    """Keep the file observation while retiring a different endpoint's standing and tails."""
    db.execute("INSERT OR IGNORE INTO observation(id,server) VALUES(1,?)", (remote,))
    if db.execute(
        """UPDATE observation SET server=?,launcher_at=NULL,last_loop=NULL,standing=NULL,machines=NULL
           WHERE id=1 AND server IS NOT ?""",
        (remote, remote),
    ).rowcount:
        db.execute("DELETE FROM consoles")
        return True
    return False


def _store_run(
    db: sqlite3.Connection,
    address: str,
    experiment: str,
    run: TrainingRunSummary | EvalRunSummary,
    stored: RunData | None,
    record: RunRecord | None,
    reached: dict[str, float],
    preferred: str | None,
) -> None:
    file = stored.file if stored else None
    job = record.launch.spec if record else None
    status = run.status
    training = isinstance(run, TrainingRunSummary)
    file_job = None
    if file:
        file_job = (
            run.job
            if isinstance(run, TrainingRunSummary)
            else file.job_name or file.job or "Evaluation"
        )

    # Once files exist, even an empty sealed clock belongs to them. The
    # launcher can still supply a target on a different clock.
    clocks = status.clocks if stored is None and status else reached
    if stored and preferred not in clocks:
        preferred = next((c.name for c in stored.clocks if c.name in clocks), None)
    file_config = file.config_json if file else None
    fields = {
        "address": address,
        "experiment": experiment,
        "kind": "training" if training else "evaluation",
        "last_write": run.last_write.isoformat() if run.last_write else None,
        "file_job": file_job,
        "file_state": stored.stored_state if stored else None,
        "file_position": _position(clocks, preferred) if stored else None,
        "file_config": file_config,
        "checkpoints": _checkpoints(stored) if stored and training else None,
        "launched_as": status.run if status else None,
        "launcher_job": (job.name or "Evaluation") if job and not (file and training) else None,
        "launcher_state": run.state if status else None,
        "launcher_position": _position(clocks, preferred, status.until) if status else None,
        "launcher_config": record.config_json if record and file_config is None else None,
        "source": _source(record) if record and training else None,
        "status": status.model_dump_json() if status else None,
        "max_attempts": job.max_attempts if job else None,
    }
    # Keep unchanged rows out of the WAL. Only the fixed column names form SQL.
    columns = ",".join(fields)
    values = ",".join(":" + key for key in fields)
    db.execute(
        f"INSERT OR REPLACE INTO runs({columns}) SELECT {values} "
        f"EXCEPT SELECT {columns} FROM runs WHERE address=:address",
        fields,
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
                """SELECT address,launcher_state AS state,launcher_position AS position,status,max_attempts
                   FROM runs WHERE launched_as=?""",
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
            f"a file observation confirms {_SILENT // timedelta(minutes=1)} minutes without a write.</p>"
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


def _display_state(row: sqlite3.Row) -> str:
    state: str = row["state"]
    if state == "running" and row["status"] is None and row["last_write"]:
        if datetime.fromisoformat(row["at"]) - datetime.fromisoformat(row["last_write"]) >= _SILENT:
            return "no end recorded"
    return state


def _observation_ages(row: sqlite3.Row, now: datetime, *, launcher: bool) -> list[str]:
    ages = [f"indexed {_age(row['at'], now)}" if row["at"] else "File observation unavailable"]
    if row["launcher_at"] is None:
        ages.append("Launcher observation unavailable")
    elif launcher:
        if row["last_loop"]:
            ages.append(f"last loop {_age(row['last_loop'], now)}")
        if not row["at"] or abs(
            datetime.fromisoformat(row["at"]) - datetime.fromisoformat(row["launcher_at"])
        ) > timedelta(seconds=_REFRESH):
            ages.append(f"launcher observed {_age(row['launcher_at'], now)}")
    return ages


def _position(clocks: dict[str, float], preferred: str | None, target: Until | None = None) -> str:
    if target:
        limit = number(target.value)
        at = clocks.get(target.clock)
        if at is None:
            return f"Target {target.clock} {limit}"
        return f"{target.clock} {number(at)} / {limit}"
    clock = preferred if preferred in clocks else next(iter(clocks), None)
    return f"{clock} {number(clocks[clock])}" if clock else ""


def _console(row: sqlite3.Row, now: datetime) -> str:
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
    elif row["launcher_at"] is None:
        console = "<p>Waiting for the selected launcher.</p>"
    else:
        console = "<p>Console was not recorded.</p>"
    if row["launched_as"]:
        console += (
            f"<p>Showing up to {ConsoleTail.max_lines} lines. Read the full retained console with "
            f"<code>etude logs --run {escape(shlex.quote(row['launched_as']))}</code></p>"
        )
    return f'<section class="sec" id="console"><h2>Console</h2>{console}</section>'


def _source(record: RunRecord) -> str:
    job = record.launch.spec
    if not job.repo or not job.revision:
        return ""
    source = f"<p>Source {escape(line(record.launch.source, job.revision))}</p>"
    if record.launch.source:
        source += f"<p>Commit <code>{escape(job.revision)}</code></p>"
    return source


def _checkpoints(stored: RunData) -> str | None:
    rows = []
    for checkpoint in stored.checkpoints:
        # Wall clocks are fractional hours; four significant digits read as a position.
        clocks = ", ".join(
            f"{k} {number(v) if float(v).is_integer() else f'{v:.4g}'}"
            for k, v in checkpoint.at.items()
        )
        rows.append(
            f"<tr><td>{checkpoint.ordinal}</td><td>{escape(checkpoint.name)}</td><td>{escape(clocks)}</td></tr>"
        )
    return _table(("Ordinal", "Checkpoint", "Clock position"), rows) if rows else None


def _run_details(row: sqlite3.Row) -> str:
    config = row["config"]
    if config is not None:
        shown = f'<pre class="scroll">{escape(config)}</pre>'
    else:
        shown = (
            "<p>No config recorded.</p>"
            if row["at"]
            else f"<p>{_UNREAD.format('Config details')}</p>"
        )
    source = row["source"] or ""
    checkpoints = row["checkpoints"] or (
        "<p>No checkpoints recorded.</p>"
        if row["at"]
        else f"<p>{_UNREAD.format('Checkpoints')}</p>"
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
        self.path = path.absolute() if path else None
        self.index = ChartIndex(as_storage(source), path)
        self.files = self.index.declared_files()
        self.pages = Pages(source)
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
            self.index.close()
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
            files = (
                experiment_files(self.path, known=self.files.values())
                if self.path.is_dir()
                else [self.path]
            )
        except Exception as error:
            return explain(error)
        self.files = {file.stem: file for file in files}
        return None

    def refresh_files(self) -> None:
        error = self._find_files()
        self.index.refresh_files(self.files, error)

    def refresh_launcher(self) -> None:
        self._reclaim()
        self.index.refresh_launcher()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        stop = threading.Event()

        def poll(refresh: Callable[[], None]) -> None:
            while not stop.is_set():
                try:
                    refresh()
                except Exception as error:
                    # Publication itself can fail (for example, a full index disk).
                    print(explain(error), file=sys.stderr, flush=True)
                stop.wait(_REFRESH)

        with ThreadPoolExecutor(2, thread_name_prefix="etude-view") as readers:
            readers.submit(poll, self.refresh_launcher)
            readers.submit(poll, self.refresh_files)
            try:
                super().serve_forever(poll_interval)
            finally:
                stop.set()

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
                if error := index.read_error(selected(), index.error):
                    message += " " + error
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
