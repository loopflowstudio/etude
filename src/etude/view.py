"""Live run and experiment pages backed by a shared, rebuildable chart index.

File and launcher observations publish independently. A single file reader stages
changed charts in a private SQLite temp table. Each publisher joins its observation
to the latest committed facts from the other source through state_of. Reports
build on demand in separate request threads.
"""

from __future__ import annotations

import fcntl
import json
import re
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
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import TypeAdapter

from etude.cache import Cache
from etude.errors import explain
from etude.experiment import find_eval_groups, locate_evaluation_members, state_of
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
    read_asset,
    read_report,
    render,
    render_run,
    run_chart_data,
)
from etude.records import RunRecord
from etude.report import Report
from etude.run import LocatedRun, RunListing, as_storage
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
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, kind TEXT NOT NULL,
    eval_last_write TEXT, file_job TEXT, file_state TEXT, file_position TEXT, file_config TEXT, checkpoints TEXT,
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
CREATE TABLE IF NOT EXISTS held_files (
    address TEXT PRIMARY KEY, observed_at TEXT
);
CREATE TABLE IF NOT EXISTS charts (
    address TEXT PRIMARY KEY REFERENCES files(address) ON DELETE CASCADE, payload TEXT NOT NULL
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
    SELECT runs.address, runs.experiment, kind, coalesce(eval_last_write,files.last_write) AS last_write, files.inputs,
           CASE WHEN held_files.address IS NULL THEN at ELSE held_files.observed_at END AS at,
           file_error, launcher_at, last_loop, checkpoints,
           coalesce(CASE WHEN matches THEN launcher_job END, file_job) AS job,
           coalesce(CASE WHEN matches THEN launcher_state END, file_state) AS state,
           coalesce(CASE WHEN matches THEN launcher_position END, file_position) AS position,
           coalesce(file_config, CASE WHEN matches THEN launcher_config END) AS config,
           CASE WHEN matches THEN source END AS source,
           CASE WHEN matches THEN launched_as END AS launched_as,
           CASE WHEN matches THEN status END AS status,
           CASE WHEN matches THEN max_attempts END AS max_attempts
    FROM observed CROSS JOIN runs LEFT JOIN files USING(address) LEFT JOIN held_files USING(address)
    WHERE file_state IS NOT NULL OR matches
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
            db.execute("PRAGMA foreign_keys=ON")
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
            self._launcher_error = (remote, f"Launcher: {explain(error)}")
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
            self._launcher_error = (remote, f"Launcher: {explain(error)}")
            return
        if self._remote_read:
            self._remote_read = None
            self._next_standing = time.monotonic() + _REMOTE_STANDING

    def _publish_launcher(
        self, db: sqlite3.Connection, remote: str | None, answer: _LauncherRead
    ) -> None:
        launcher = answer.observation
        stamp = answer.at.isoformat()
        previous = db.execute("SELECT server,launcher_at FROM observation WHERE id=1").fetchone()
        if previous and previous["server"] != remote:
            # Different launchers can reuse a run address. Its console belongs to the old one.
            db.execute("DELETE FROM consoles")
        elif previous and previous["launcher_at"] and previous["launcher_at"] >= stamp:
            return  # Another view has already published a newer answer.

        db.execute(
            """INSERT INTO observation(id,server,launcher_at,last_loop,standing) VALUES(1,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET server=excluded.server,launcher_at=excluded.launcher_at,
                   last_loop=excluded.last_loop,standing=excluded.standing""",
            (
                remote,
                stamp,
                launcher.last_loop.isoformat() if launcher.last_loop else None,
                launcher.model_dump_json(exclude={"consoles"}, round_trip=True),
            ),
        )
        db.executemany(
            """INSERT OR REPLACE INTO consoles(run,body,collected,gaps,final_attempt)
               VALUES(:run,:body,:collected,:gaps,:final_attempt)""",
            (
                tail.model_dump(mode="json") | {"run": run}
                for run, tail in launcher.consoles.items()
            ),
        )
        previous_experiments = db.execute(
            "SELECT DISTINCT experiment FROM runs WHERE launched_as IS NOT NULL"
        )
        self._publish_runs(
            db,
            launcher,
            {row[0] for row in previous_experiments} | {s.record.experiment for s in launcher.runs},
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
                    observed_at, vanished = self._prepare_files(db, files)
                    # Release the preparation snapshot before reading the latest launcher.
                    db.commit()
                    db.execute("BEGIN IMMEDIATE")
                    self._publish_files(db, observed_at, vanished, discovery_error)
            except Exception as failure:
                with self._connect() as db:
                    db.execute(
                        """INSERT INTO observation(id,file_error) VALUES(1,?)
                           ON CONFLICT(id) DO UPDATE SET file_error=excluded.file_error""",
                        (f"Run files: {explain(failure)}",),
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

    def _prepare_files(
        self, db: sqlite3.Connection, paths: dict[str, Path]
    ) -> tuple[datetime, dict[str, str]]:
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
        vanished: dict[str, str] = {}

        def missing_declaration(folder: str, error: FileNotFoundError) -> RunRecord | None:
            vanished[folder] = explain(error)
            row = db.execute("SELECT metadata FROM files WHERE address=?", (folder,)).fetchone()
            return RunData.model_validate_json(row[0]).file if row else None

        listing = RunListing.read(storage, objects=objects, missing=missing_declaration)
        previous = dict(db.execute("SELECT address,inputs FROM files"))
        reports: dict[str, Report] = {}
        for run in listing.declarations:
            experiment = run.file.experiment
            address = (
                run.folders[0]
                if run.file.kind == "evaluation"
                else run_address(run.file.id, experiment)
            )
            report = None
            if run.file.kind != "evaluation":
                if experiment not in reports:
                    reports[experiment] = self._report(db, experiment, paths.get(experiment))
                report = reports[experiment]
            sealed = sorted(
                key for key in listing.keys_in(run.folders) if not key.endswith(".open")
            )
            try:
                _stage_run(db, storage, run, address, sealed, report, previous.get(address))
            except FileNotFoundError as missing:
                # A live writer replaced a listed block after this listing began.
                # The run keeps its published facts; its other runs still advance.
                vanished[address] = explain(missing)

        return observed_at, _hold_files(db, listing, vanished) if vanished else {}

    def _publish_files(
        self,
        db: sqlite3.Connection,
        observed_at: datetime,
        vanished: dict[str, str],
        discovery_error: str | None,
    ) -> None:
        # Only missed runs need a date of their own. Freeze the old listing date
        # on the first miss, or NULL if this run has never supplied file facts.
        held = {row[0] for row in db.execute("SELECT address FROM held_files")}
        db.executemany(
            """INSERT INTO held_files(address,observed_at) VALUES(:address,
               (SELECT at FROM observation, runs WHERE observation.id=1 AND runs.address=:address AND file_state IS NOT NULL))""",
            ({"address": address} for address in vanished.keys() - held),
        )
        db.executemany(
            "DELETE FROM held_files WHERE address=?",
            ((address,) for address in held - vanished.keys()),
        )
        gaps = (
            f"Run files: {address}: {reason}"
            for address, reason in vanished.items()
            if address in held
        )
        error = "\n".join(filter(None, (discovery_error, *gaps))) or None
        removed = db.execute(
            """DELETE FROM files WHERE NOT EXISTS
               (SELECT 1 FROM next_files n WHERE n.address=files.address AND n.experiment=files.experiment)
               RETURNING experiment"""
        ).fetchall()
        # An open block can advance the last write without changing a sealed curve.
        redated = db.execute(
            """UPDATE files SET last_write=n.last_write FROM next_files n
               WHERE n.address=files.address AND n.metadata IS NULL AND files.last_write IS NOT n.last_write RETURNING address,experiment"""
        ).fetchall()
        rebuilt = db.execute(
            """INSERT OR REPLACE INTO files(address,experiment,inputs,reached,preferred,last_write,metadata)
               SELECT address,experiment,inputs,reached,preferred,last_write,metadata
               FROM next_files WHERE metadata IS NOT NULL RETURNING experiment"""
        ).fetchall()
        changed = {row[0] for row in (*removed, *rebuilt)}
        changed.update(row["experiment"] for row in redated if row["address"].endswith("/"))
        if rebuilt:
            # Replacing a file row retires its old chart through the foreign key.
            db.execute(
                """INSERT INTO charts(address,payload)
                   SELECT address,payload FROM next_files WHERE payload IS NOT NULL"""
            )
        db.execute(
            """DELETE FROM declarations WHERE experiment NOT IN (SELECT experiment FROM next_declarations)"""
        )
        db.execute("""INSERT OR REPLACE INTO declarations(experiment,file,accepted,error)
                      SELECT * FROM next_declarations EXCEPT SELECT * FROM declarations""")
        # File scans use the retained standing. Only a launcher reply replaces its endpoint.
        row = db.execute(
            """INSERT INTO observation(id,at,file_error) VALUES(1,?,?)
               ON CONFLICT(id) DO UPDATE SET at=excluded.at,file_error=excluded.file_error
               RETURNING standing""",
            (observed_at.isoformat(), error),
        ).fetchone()
        if changed:
            launcher = (
                LauncherObservation.model_validate_json(row[0]) if row[0] else LauncherObservation()
            )
            self._publish_runs(db, launcher, changed)

    def _publish_runs(
        self, db: sqlite3.Connection, launcher: LauncherObservation, experiments: set[str]
    ) -> None:
        standing: dict[str, list[Standing]] = defaultdict(list)
        for held in launcher.runs:
            standing[held.record.experiment].append(held)
        for experiment in experiments:
            rows = db.execute(
                """SELECT address, metadata, reached, preferred, last_write
                   FROM files WHERE experiment=? ORDER BY address""",
                (experiment,),
            )
            files = {row["address"]: row for row in rows}
            metadata = [
                RunData.model_validate_json(row["metadata"]).model_copy(
                    update={
                        "last_write": datetime.fromisoformat(row["last_write"])
                        if row["last_write"]
                        else None
                    }
                )
                for row in files.values()
            ]
            removed = {
                row[0]
                for row in db.execute("SELECT address FROM runs WHERE experiment=?", (experiment,))
            }
            state = state_of(metadata, experiment, retained=tuple(standing[experiment]))
            runs: tuple[TrainingRunSummary | EvalRunSummary, ...] = (*state.runs, *state.eval_runs)
            for run in runs:
                # Stored charts keep their compact address through a launcher alias.
                address = run.stored_address or run.address
                chart = files.get(address)
                _store_run(
                    db,
                    address,
                    experiment,
                    run,
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

    def run(self, address: str, *, observe: bool = False) -> str | None:
        remote = selected()
        # The document reads its chart in the same snapshot as its change token.
        payload = (
            "NULL" if observe else "(SELECT payload FROM charts WHERE address=visible.address)"
        )
        with self._connect() as db:
            row = db.execute(
                _VISIBLE
                + f"""
                SELECT visible.*, {payload} AS payload, declarations.error AS report_error,
                       consoles.body, consoles.collected, consoles.gaps, consoles.final_attempt AS console_attempt
                FROM visible
                LEFT JOIN consoles ON consoles.run=visible.launched_as
                LEFT JOIN declarations USING(experiment)
                WHERE address=coalesce(
                    (SELECT address FROM runs WHERE address=:address),
                    (SELECT address FROM runs WHERE launched_as=:address AND (SELECT matches FROM observed))
                )
                """,
                {"remote": remote, "address": address},
            ).fetchone()
        if row is None:
            return self.waiting() if observe else None
        now = datetime.now(UTC)
        state = _display_state(row)
        status = _status(row)
        run_line = _run_line(row, state, status)
        error = self.read_error(remote, row["file_error"], report=row["report_error"])
        # Session shutdown can still retain final logs after the run itself ended.
        if status is not None and (not status.done or row["console_attempt"] != status.attempts):
            with self._console_lock:
                if remote == self._server:
                    self._wanted.add(row["launched_as"])

        # inputs identifies the sealed files and their report panels. Hash the displayed
        # launcher facts at their rendered precision so a sub-cent change leaves the chart mounted.
        facts = (
            row["address"],
            row["experiment"],
            row["kind"],
            row["inputs"],
            run_line,
            row["config"],
            row["source"],
            row["launched_as"],
            row["body"],
            row["gaps"],
            bool(row["at"]),
            bool(row["launcher_at"]),
            error,
        )
        token = sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
        ages = [
            "no files yet" if _no_files(row) else f"last file write {_age(row['last_write'], now)}"
        ]
        ages.extend(
            _observation_ages(row, now, launcher=status is not None, files="files observed")
        )
        collected = f"Last collected {_age(row['collected'], now)}" if row["collected"] else ""
        observation = _observation("run", token, " · ".join(ages), collected)
        if observe:
            return observation
        evaluation = row["kind"] == "evaluation"
        experiment = escape(row["experiment"])
        page = f"/experiments/{quote(row['experiment'])}"
        title = f"Evaluation · {escape(address)}" if evaluation else escape(address)
        breadcrumb = "Experiment overview" if address == row["experiment"] else experiment
        heading = (
            f'<a href="{page}">{breadcrumb}</a>'
            f'<header class="head"><h1>{title}</h1></header>'
            f"<p>{run_line}</p>{observation}{_refresh_script()}"
        )
        if evaluation:
            heading += f'<p>Scores are in the <a href="{page}#report">{experiment} report</a>.</p>'
        if state == "no end recorded":
            heading += (
                f"<p>The last file observation found at least {_SILENT // timedelta(minutes=1)} "
                "minutes without a write.</p>"
            )
        if error:
            heading += f'<p role="status">{escape(error)}</p>'
        if evaluation and status is None:
            return _document(address, heading)
        below = _console(row, collected)
        if evaluation:
            return _document(address, heading + below)
        below += _run_details(row)
        if row["payload"] is None:
            tabs = f'<nav class="tabs" aria-label="Run sections"><a href="#charts">Charts</a>{_TRAINING_TABS}</nav>'
            curves = "No metric curves recorded." if row["at"] else _UNREAD.format("Curves")
            return _document(
                address,
                heading
                + tabs
                + f'<section class="sec" id="charts"><h2>Charts</h2><p>{curves}</p></section>'
                + below,
            )
        return render_run(address, row["payload"], heading, below, _TRAINING_TABS)

    def waiting(self) -> str:
        source = self.source.uri("run.json").removesuffix("run.json")
        message = f"Waiting for the run to appear in {source}."
        if error := self.read_error(selected(), self.error):
            message += " " + error
        return _observation("run", "waiting", message)

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
            stale=stale,
            error=self.read_error(remote, observation["file_error"] if observation else None),
        )

    def machines(self) -> str:
        if remote := selected():
            return _live_status(
                f"<p>The launcher database is on {escape(remote)}. "
                "Run <code>etude machines</code> to list its machines.</p>",
            )
        with self._connect() as db:
            row = db.execute(
                "SELECT at,file_error,launcher_at,last_loop,machines FROM observation WHERE id=1 AND server IS NULL"
            ).fetchone()
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
        return _live_status(body, error=error)

    def report_ready(self, experiment: str, *, declared: bool) -> bool | None:
        """Whether a known experiment has indexed files. Unknown experiments have no page."""
        with self._connect() as db:
            row = db.execute(
                _VISIBLE
                + """SELECT CASE
                        WHEN EXISTS (SELECT 1 FROM files WHERE experiment=:experiment) THEN 1
                        WHEN :declared OR EXISTS (SELECT 1 FROM visible WHERE experiment=:experiment) THEN 0
                     END""",
                {"experiment": experiment, "declared": declared, "remote": selected()},
            ).fetchone()
        return bool(row[0]) if row[0] is not None else None

    def experiment_status(self, experiment: str, *, declared: bool) -> str | None:
        remote = selected()
        observation, rows = self._list_runs(remote, experiment)
        if not declared and not rows:
            return None
        now = datetime.now(UTC)
        stale = False
        body = []
        for row in rows:
            state = _display_state(row)
            stale |= state == "no end recorded"
            status = _status(row)
            address = row["address"]
            last_write = "no files yet" if _no_files(row) else _age(row["last_write"], now)
            if observation and row["at"] != observation["at"]:
                last_write += f"<div>{_file_age(row['at'], now, 'files observed')}</div>"
            body.append(
                f"<tr><td>{escape(row['job'])}</td>"
                f"<td>{_run_link(address)}</td>"
                f"<td>{_run_line(row, state, status)}</td><td>{last_write}</td></tr>"
            )
        if body:
            table = _table(("Job", "Run", "Status", "Last file write"), body)
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
            stale=stale,
            error=self.read_error(
                remote,
                observation["file_error"] if observation else None,
                report=observation["report_error"] if observation else None,
            ),
        )


def _hold_files(
    db: sqlite3.Connection, listing: RunListing, vanished: dict[str, str]
) -> dict[str, str]:
    # A group is observed as one run. Keep its previous generation in full
    # if a member or its root changed again during the read.
    addresses: dict[str, str] = {}
    if any(key.endswith("/") for key in vanished):
        folders = {
            run.folders[0]: run.file
            for run in listing.declarations
            if run.file.kind == "evaluation"
        }
        # Only removed or missed copies lack a staged row. Each contains metadata
        # for one physical folder, independent of how many matches it delivered.
        old = db.execute("""SELECT address,metadata FROM files
                WHERE address LIKE '%/' AND NOT EXISTS
                    (SELECT 1 FROM next_files n WHERE n.address=files.address)""")
        for row in old:
            if row["address"] not in folders:
                data = RunData.model_validate_json(row["metadata"])
                if data.file:
                    folders[row["address"]] = data.file
        previous_launcher = db.execute("SELECT standing FROM observation WHERE id=1").fetchone()
        standing = (
            LauncherObservation.model_validate_json(previous_launcher[0]).runs
            if previous_launcher and previous_launcher[0]
            else ()
        )
        owners = locate_evaluation_members(folders, find_eval_groups(standing))
        addresses = {
            key: run_address(file.id, file.experiment) for key, file in folders.items()
        } | owners
    for key, reason in vanished.items():
        if key.endswith("/") and key not in addresses:
            # The discovery itself is incomplete. Until its declaration can
            # be read, there is no experiment to which we can assign this loss.
            raise FileNotFoundError(reason)
    held = {addresses.get(key, key): reason for key, reason in vanished.items()}
    keep = vanished.keys() | {key for key, address in addresses.items() if address in held}
    for key in keep:
        db.execute("DELETE FROM next_files WHERE address=?", (key,))
        db.execute(
            """INSERT INTO next_files(address,experiment,inputs,last_write)
               SELECT address,experiment,inputs,last_write FROM files WHERE address=?""",
            (key,),
        )
    return held


def _stage_run(
    db: sqlite3.Connection,
    storage: ListedStorage,
    run: LocatedRun,
    address: str,
    sealed: list[str],
    report: Report | None,
    previous: str | None,
) -> None:
    digest = sha256()
    for key in sealed:
        digest.update(f"{key}\0{storage.objects[key]}\0".encode())
    if report is not None:
        digest.update(report.model_dump_json(include=report.model_fields_set).encode())
    inputs = digest.hexdigest()
    metadata = reached = payload = None
    if previous != inputs:
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


def _store_run(
    db: sqlite3.Connection,
    address: str,
    experiment: str,
    run: TrainingRunSummary | EvalRunSummary,
    reached: dict[str, float],
    preferred: str | None,
) -> None:
    stored = run.stored
    file = stored.file if stored else None
    record = run.standing.record if run.standing else None
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
    file_state = (
        run.file_state
        if isinstance(run, EvalRunSummary)
        else stored.stored_state
        if stored
        else None
    )
    fields = {
        "address": address,
        "experiment": experiment,
        "kind": "training" if training else "evaluation",
        "eval_last_write": run.last_write.isoformat()
        if isinstance(run, EvalRunSummary) and run.last_write
        else None,
        "file_job": file_job or ("Evaluation" if file_state else None),
        "file_state": file_state,
        "file_position": _position(clocks, preferred) if stored else None,
        "file_config": file_config,
        "checkpoints": _checkpoints(stored) if stored and training else None,
        "launched_as": status.run if status else None,
        "launcher_job": (job.name or "Evaluation") if job and not (file and training) else None,
        "launcher_state": (run.allocation_phase or run.state).replace("_", " ") if status else None,
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
                """SELECT address,launcher_state AS state,launcher_position AS position,status,
                          max_attempts
                   FROM runs WHERE launched_as=?""",
                (run,),
            ).fetchone()
            address = row["address"] if row else run
            detail = (
                _run_line(row, row["state"], _status(row), on_machine=machine.name) if row else ""
            )
            runs.append(f"<div>{_run_link(address)}{(' · ' + detail) if detail else ''}</div>")
        description = escape(describe_machine(status, available="ready"))
        if warning := machine_warning(status):
            description += f"<div>{escape(warning)}</div>"
        rows.append(
            f'<tr><th scope="row">{escape(machine.name)}</th><td>{escape(machine.supply)}</td>'
            f"<td>{description}</td><td>{''.join(runs) or 'Idle'}</td></tr>"
        )
    return _table(
        ("Machine", "Pool or on-demand", "Readiness, window and cost", "Occupied slots"), rows
    )


def _status(row: sqlite3.Row) -> Status | None:
    return Status.model_validate_json(row["status"]) if row["status"] is not None else None


def _run_line(
    row: sqlite3.Row, state: str, status: Status | None, *, on_machine: str | None = None
) -> str:
    parts = [f'<span class="pill">{escape(state)}</span>']
    facts = [row["position"]]
    if status:
        if state == "queued":
            facts.append(status.reason)
        elif status.done:
            if status.outcome != state and (state, status.outcome) != ("finished", "succeeded"):
                facts.append(status.outcome)
            facts.append(status.cause)
        facts.extend(launcher_facts(status, row["max_attempts"], on_machine=on_machine))
    parts.extend(f"<span>{escape(fact)}</span>" for fact in facts if fact)
    return " · ".join(parts)


def _is_open(status: Status | None, state: str) -> bool:
    return not status.done if status else state in ("queued", "running")


def _live_status(body: str, *, stale: bool = False, error: str | None = None) -> str:
    if error:
        body = f'<p role="status">{escape(error)}</p>' + body
    if stale:
        body += (
            f"<p>Runs with no launcher record show “no end recorded” after "
            f"a file observation confirms {_SILENT // timedelta(minutes=1)} minutes without a write.</p>"
        )
    return f'<div id="experiment-status" data-refresh="{_REFRESH * 1000}">{body}</div>'


def _table(columns: tuple[str, ...], rows: list[str]) -> str:
    headers = "".join(f"<th>{escape(column)}</th>" for column in columns)
    return (
        f'<div class="scroll"><table><thead><tr>{headers}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _experiments_table(title: str, rows: list[str]) -> str:
    table = (
        _table(("Experiment", "Runs", "Last file write"), rows)
        if rows
        else f"<p>No {title.lower()} experiments.</p>"
    )
    return f'<section class="sec"><h2>{title}</h2>{table}</section>'


def _status_page(title: str, status: str, *, before: str = "", after: str = "") -> str:
    return _document(
        title,
        f'{before}<header class="head"><h1>{escape(title)}</h1></header>{status}'
        f"{_refresh_script()}{after}",
    )


def _display_state(row: sqlite3.Row) -> str:
    state: str = row["state"]
    if state == "running" and row["status"] is None and row["last_write"]:
        if datetime.fromisoformat(row["at"]) - datetime.fromisoformat(row["last_write"]) >= _SILENT:
            return "no end recorded"
    return state


def _no_files(row: sqlite3.Row) -> bool:
    """An accepted listing found no file for the run; without one, its last write is unknown."""
    return row["last_write"] is None and bool(row["at"])


def _file_age(at: str | None, now: datetime, label: str) -> str:
    return f"{label} {_age(at, now)}" if at else "File observation unavailable"


def _observation_ages(
    row: sqlite3.Row, now: datetime, *, launcher: bool, files: str = "Run files listed"
) -> list[str]:
    ages = [_file_age(row["at"], now, files)]
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


def _console(row: sqlite3.Row, collected: str) -> str:
    if row["body"] is not None:
        console = (
            '<pre id="retained-output" class="console" tabindex="0" aria-label="Retained console">'
            f"{escape(row['body'])}</pre>"
            '<script>{const output = document.getElementById("retained-output");'
            "output.scrollTop = output.scrollHeight;}</script>"
        )
        if row["gaps"]:
            console += "<p>Some console output is missing from retention.</p>"
        hidden = "" if collected else " hidden"
        console = f'<p id="console-collected"{hidden}>{collected}</p>' + console
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


def _observation(page: str, token: str, message: str, collected: str = "") -> str:
    return (
        f'<p class="context" id="{page}-observation" data-refresh="{_REFRESH * 1000}" '
        f'data-version="{token}" data-collected="{escape(collected)}">{escape(message)}</p>'
    )


def _refresh_script() -> str:
    script = read_asset("refresh_status.js")
    return f'<p id="status-error" role="status"></p><script>{script}</script>'


def _document(title: str, body: str) -> str:
    css = read_asset("page.css")
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
        self.pages = Pages(self.index.source)
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
            return f"Experiment files: {explain(error)}"
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
        url = urlsplit(route)
        path = unquote(url.path)
        observe = parse_qs(url.query).get("observe") == ["1"]
        if path.startswith("/runs/"):
            return self.index.run(path.removeprefix("/runs/"), observe=observe)
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
        if component == "report":
            return self._report(experiment, refresh=refresh, observe=observe)
        if component not in ("", "status"):
            return None
        status = self.index.experiment_status(experiment, declared=experiment in self.files)
        if component or status is None:
            return status
        return _status_page(
            experiment,
            status,
            before='<a href="/">Experiments</a>',
            after=f'<iframe id="report" title="Experiment report" src="/experiments/{quote(experiment)}/report" style="width:100%;height:80vh;border:0"></iframe>',
        )

    def _report(self, experiment: str, *, refresh: bool, observe: bool) -> str | None:
        """Wait for indexed files, then share the first build and explicit refreshes."""
        ready = self.index.report_ready(experiment, declared=experiment in self.files)
        if ready is None:
            return None
        with self._lock:
            html = self._html.get(experiment)
            if not ready and html is None:
                if refresh:
                    raise ValueError("Waiting for run files.")
                notice = _observation("report", "waiting", "Waiting for run files.")
                return notice if observe else _document(experiment, notice + _refresh_script())
            if observe:
                return _observation("report", "ready", "Building report…")
            if not refresh and html is not None:
                return html
            shared = self._pending.get(experiment)
            if shared is None:
                self._pending[experiment] = Future()
        if shared is not None:
            return shared.result()

        # Use the first request's thread so the server can close its listening
        # socket while an authored report is still building.
        try:
            with self._building:
                html = self._build_report(experiment)
        except BaseException as error:
            with self._lock:
                self._pending.pop(experiment).set_exception(error)
            raise
        with self._lock:
            self._html[experiment] = html
            self._pending.pop(experiment).set_result(html)
        return html

    def _build_report(self, experiment: str) -> str:
        page = self.pages.build(self.files.get(experiment), experiment=experiment)
        control = (
            '<button id="refresh-report">Refresh report</button> '
            '<span id="refresh-status" role="status"></span>'
            f"<script>{read_asset('refresh_report.js')}</script>"
        )
        return render(page, heading=control)


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
        if not self._is_report():
            self.send_error(405)
            return
        self._respond(refresh=True)

    def _is_report(self) -> bool:
        return (
            re.fullmatch(r"/experiments/[^/]+/report", unquote(urlsplit(self.path).path))
            is not None
        )

    def _respond(self, *, refresh: bool) -> None:
        html: str | None = None
        try:
            html = self.server.page(self.path, refresh=refresh)
            status = 200 if html else 404
            if html is None and urlsplit(self.path).path.startswith("/runs/"):
                # A train prints its link before the next listing can discover it.
                html = _document("Run", self.server.index.waiting() + _refresh_script())
            body = (html or "Not found.").encode()
        except (Exception, SystemExit) as error:
            reason = explain(error)
            if not refresh and self._is_report():
                html = _document(
                    "Experiment report",
                    f'<p role="status">Report build failed: {escape(reason)}</p>'
                    '<form method="get"><button>Retry report</button></form>',
                )
                body, status = html.encode(), 500
            else:
                body, status = reason.encode(), 404 if isinstance(error, ValueError) else 500
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
