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
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cached_property
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from types import FrameType
from typing import Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import TypeAdapter

from etude.cache import Cache
from etude.errors import explain
from etude.experiment import find_eval_groups, locate_evaluation_members, state_of
from etude.home import home
from etude.identity import SAFE_NAME, run_address
from etude.jobs import EvalRunSummary, RunState, TrainingRunSummary
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
    render_overview,
    render_run,
    run_chart_data,
)
from etude.records import BUILT_IN_NAMES, Continuation, RunRecord
from etude.report import Report
from etude.run import LocatedRun, RunListing, as_storage
from etude.spec import Until, number
from etude.storage import ListedStorage, Storage
from etude.train.read import RunData, RunSources, read_located
from etude.view_link import advertise, run_path

_REFRESH = 5
_REPORT_ROUTE = re.compile(r"/experiments/([^/]+)/report")
_RUN_TABS = ("overview", "charts", "console", "config", "checkpoints", "files")
_DETAIL_TABS = ("console", "curves", "config", "checkpoints")
_REMOTE_STANDING = 30
_SILENT = timedelta(minutes=10)
_CLOCKS = TypeAdapter(dict[str, float])
_METRICS = TypeAdapter(list[str])

# Bump when stored projections change independently of their SQL schema.
_INDEX_VERSION = 2
_MAY_BE_OPEN = "file_state IN ('running','queued') OR phase != 'done'"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, kind TEXT NOT NULL,
    eval_last_write TEXT, file_job TEXT, file_state TEXT, file_position TEXT, file_config TEXT, file_continues TEXT, checkpoints TEXT,
    launched_as TEXT, launcher_job TEXT, launcher_state TEXT, launcher_position TEXT,
    launcher_config TEXT, launcher_continues TEXT, source TEXT, status TEXT, max_attempts INTEGER, machine_rate TEXT,
    phase TEXT GENERATED ALWAYS AS (json_extract(status,'$.phase')) VIRTUAL
);
CREATE INDEX IF NOT EXISTS launcher_address ON runs(launched_as,experiment) WHERE launched_as IS NOT NULL;
CREATE INDEX IF NOT EXISTS experiment_runs ON runs(experiment, address);
CREATE INDEX IF NOT EXISTS open_runs ON runs(experiment) WHERE {_MAY_BE_OPEN};
CREATE TABLE IF NOT EXISTS files (
    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, inputs TEXT NOT NULL, reached TEXT,
    preferred TEXT, last_write TEXT, metadata TEXT NOT NULL, names TEXT, names_version TEXT, pace TEXT, sample_ms INTEGER
);
CREATE INDEX IF NOT EXISTS experiment_files ON files(experiment,address);
CREATE TABLE IF NOT EXISTS held_files (
    address TEXT PRIMARY KEY, observed_at TEXT
);
CREATE TABLE IF NOT EXISTS charts (
    address TEXT PRIMARY KEY REFERENCES files(address) ON DELETE CASCADE,
    panel_order INTEGER GENERATED ALWAYS AS (json_extract(headline,'$.order')) STORED,
    definition TEXT NOT NULL, latest TEXT, headline TEXT, overview TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consoles (
    run TEXT PRIMARY KEY, body TEXT NOT NULL, collected TEXT, gaps INTEGER NOT NULL, final_attempt INTEGER
);
CREATE TABLE IF NOT EXISTS observation (
    id INTEGER PRIMARY KEY, at TEXT, file_error TEXT, launcher_at TEXT, last_loop TEXT, server TEXT, machines TEXT, machine_rail TEXT, standing TEXT
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
           CASE WHEN server IS :remote THEN last_loop END AS last_loop,
           CASE WHEN server IS :remote THEN machine_rail END AS machine_rail
    FROM observation WHERE id=1
)
"""
_VISIBLE = f"""{_OBSERVED}, visible AS (
    SELECT runs.address, runs.experiment, kind, ({_MAY_BE_OPEN}) AS may_be_open,
           CASE WHEN matches THEN phase END AS phase, coalesce(eval_last_write,files.last_write) AS last_write, files.inputs,
           held_files.address IS NULL AS listed, CASE WHEN held_files.address IS NULL THEN at ELSE held_files.observed_at END AS at,
           launcher_at, last_loop, checkpoints, files.reached, files.preferred, files.pace, files.sample_ms, files.names_version,
           CASE WHEN matches THEN machine_rate END AS machine_rate,
           coalesce(CASE WHEN matches THEN launcher_job END, file_job) AS job,
           coalesce(CASE WHEN matches THEN launcher_state END, file_state) AS state,
           coalesce(CASE WHEN matches THEN launcher_position END, file_position) AS position,
           coalesce(file_config, CASE WHEN matches THEN launcher_config END) AS config,
           coalesce(CASE WHEN matches THEN launcher_continues END, file_continues) AS continues,
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
class _ServedReport:
    html: str
    built: datetime
    declaration: str
    headline: str | None
    runs: int | Literal["Eval only"]


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
        self.source_uri = source.uri("run.json").removesuffix("run.json")
        # A view of edited report code and one using the default panels may
        # run side by side. Neither may serve the other's last generation.
        identity = "\0".join(
            (
                self.source_uri,
                str(path.resolve()) if path else "",
                version("etude-sdk"),
                _SCHEMA,
                str(_INDEX_VERSION),
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
        return answer

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
        self._launcher_error = None
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
            """INSERT INTO observation(id,server,launcher_at,last_loop,machine_rail,standing) VALUES(1,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET server=excluded.server,launcher_at=excluded.launcher_at,
                   last_loop=excluded.last_loop,machine_rail=excluded.machine_rail,standing=excluded.standing""",
            (
                remote,
                stamp,
                launcher.last_loop.isoformat() if launcher.last_loop else None,
                _held_machines(launcher.machines) if launcher.machines is not None else None,
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
        db.execute(
            "CREATE TEMP TABLE next_files AS SELECT *, NULL AS definition, NULL AS headline, NULL AS latest, NULL AS overview, NULL AS payload FROM files WHERE 0"
        )
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

        listing = RunListing(storage, objects, missing=missing_declaration)
        previous = dict(db.execute("SELECT address,inputs FROM files"))
        for experiment, members in groupby(listing.declarations, lambda r: r.file.experiment):
            runs = list(members)
            training = [r.file for r in runs if r.file.kind != "evaluation"]
            report = self._report(db, experiment, paths.get(experiment)) if training else None
            # The card uses one clock across the experiment, even when a run lacks that axis.
            clocks = dict.fromkeys(c.name for file in training for c in file.clocks)
            clocks.update(dict.fromkeys(BUILT_IN_NAMES))
            clock = report.clock if report and report.clock in clocks else next(iter(clocks))
            for run in runs:
                evaluation = run.file.kind == "evaluation"
                address = run.folders[0] if evaluation else run_address(run.file.id, experiment)
                try:
                    _stage_run(
                        db,
                        storage,
                        run,
                        address,
                        sorted(listing.keys_in(run.folders)),
                        None if evaluation else report,
                        clock,
                        previous.get(address),
                    )
                except FileNotFoundError as missing:
                    # The writer replaced a listed block. Other runs can still advance.
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
            """UPDATE files SET last_write=n.last_write, names=n.names,
                      names_version=n.names_version
               FROM next_files n WHERE n.address=files.address AND n.metadata IS NULL
               AND (files.last_write IS NOT n.last_write OR files.names_version IS NOT n.names_version)
               RETURNING address,experiment"""
        ).fetchall()
        rebuilt = db.execute(
            """INSERT OR REPLACE INTO files(address,experiment,inputs,reached,preferred,last_write,metadata,names,names_version,pace,sample_ms)
               SELECT address,experiment,inputs,reached,preferred,last_write,metadata,names,names_version,pace,sample_ms
               FROM next_files WHERE metadata IS NOT NULL RETURNING experiment"""
        ).fetchall()
        changed = {row[0] for row in (*removed, *rebuilt)}
        changed.update(row["experiment"] for row in redated if row["address"].endswith("/"))
        if rebuilt:
            # Replacing a file row retires its old chart through the foreign key.
            db.execute(
                """INSERT INTO charts(address,definition,headline,latest,overview,payload)
                   SELECT address,definition,headline,latest,overview,payload FROM next_files WHERE payload IS NOT NULL"""
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
        rates = {
            run: machine.cost.rental.usd_per_hour
            for machine in launcher.machines or ()
            if machine.cost
            for run in machine.runs
        }
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
            file_sources = RunSources(
                (data.address, data.file)
                for data in metadata
                if data.file and data.file.kind != "evaluation"
            )
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
                    file_sources,
                    rates.get(run.status.run) if run.status else None,
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

    @contextmanager
    def snapshot(self) -> Iterator[_ViewSnapshot]:
        with self._connect() as db:
            db.execute("BEGIN")
            remote = selected()
            error = self._launcher_error
            yield _ViewSnapshot(
                db, remote, self, error[1] if error and error[0] == remote else None
            )

    def request_console(self, remote: str | None, run: str) -> None:
        with self._console_lock:
            if remote == self._server:
                self._wanted.add(run)


@dataclass(frozen=True)
class _ViewSnapshot:
    """One index snapshot for the page and its rail, scoped to the selected launcher."""

    db: sqlite3.Connection
    remote: str | None
    index: ChartIndex
    launcher_error: str | None

    def _errors(self, report: str | None = None) -> str:
        row = self._observation
        return "\n".join(
            filter(None, (row["file_error"] if row else None, self.launcher_error, report))
        )

    def _read_run(
        self,
        address: str,
        tab: str,
        *,
        observe: bool = False,
        mounted: str | None = None,
        standalone: bool = False,
    ) -> sqlite3.Row | None:
        # A NULL join key skips the payload lookup on polls and unrelated tabs.
        row: sqlite3.Row | None = self.db.execute(
            _VISIBLE
            + """
            SELECT visible.*,
                   CASE :chart WHEN 'overview' THEN charts.overview ELSE charts.payload END AS payload,
                   json_extract(charts.latest,'$[0]') AS metric, declarations.error AS report_error,
                   consoles.body, consoles.collected, consoles.gaps, consoles.final_attempt AS console_attempt
            FROM visible
            LEFT JOIN charts ON charts.address=CASE
                WHEN :chart IS NOT NULL AND visible.inputs IS NOT :mounted THEN visible.address END
            LEFT JOIN consoles ON consoles.run=CASE
                WHEN :console OR (:standalone AND visible.kind='evaluation') THEN visible.launched_as END
            LEFT JOIN declarations USING(experiment)
            WHERE visible.address=coalesce(
                (SELECT address FROM runs WHERE address=:address),
                (SELECT address FROM runs WHERE launched_as=:address AND (SELECT matches FROM observed))
            )
            """,
            {
                "remote": self.remote,
                "address": address,
                "mounted": mounted,
                "chart": tab if not observe and tab in ("overview", "charts", "curves") else None,
                "console": tab in ("overview", "console"),
                "standalone": standalone,
            },
        ).fetchone()
        return row

    def run(self, address: str, *, tab: str, observe: bool = False) -> str | None:
        if tab not in _RUN_TABS:
            tab = "overview"
        row = self._read_run(address, tab, observe=observe, standalone=True)
        if row is None:
            return self.waiting() if observe else None
        now = datetime.now(UTC)
        state = _display_state(row)
        status = _status(row)
        evaluation = row["kind"] == "evaluation"
        overview = tab == "overview" and not evaluation
        run_line = _run_line(row, state, status, overview=overview)
        error = self._errors(row["report_error"])
        console = evaluation or tab in ("overview", "console")
        collected = self._observe_console(row, status, now) if console else ""

        # inputs identifies the sealed files and their report panels. Hash the displayed
        # launcher facts at their rendered precision so a sub-cent change leaves the chart mounted.
        facts = (
            row["address"],
            row["experiment"],
            row["kind"],
            row["inputs"],
            row["names_version"] if tab == "files" else None,
            run_line,
            row["position"],
            status.formatted_spend if status else None,
            row["machine_rate"] if overview else None,
            row["continues"],
            row["config"] if tab == "config" else None,
            row["source"] if tab == "config" else None,
            row["launched_as"],
            row["body"],
            row["gaps"],
            bool(row["at"]),
            bool(row["launcher_at"]),
            error,
        )
        token = sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
        written = "no files yet" if _no_files(row) else _age(row["last_write"], now)
        ages = _observation_ages(row, now, launcher=status is not None, files="files observed")
        if not overview:
            ages.insert(0, written if _no_files(row) else f"last file write {written}")
        observation = _observation("run", token, " · ".join(ages), collected, written)
        if observe:
            return observation
        experiment = escape(row["experiment"])
        page = f"/experiments/{quote(row['experiment'])}"
        title = f"Evaluation · {escape(address)}" if evaluation else escape(address)
        heading = (
            _breadcrumbs(row["experiment"], run=address)
            + f'<header class="head"><h1>{title}</h1></header>'
            f"<p>{run_line}</p>"
            f"{row['continues'] or ''}"
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
        if evaluation:
            return _document(
                address, heading + observation + (_console(row, collected) if status else "")
            )
        tabs = "".join(
            f'<a href="?tab={name}"'
            + (' aria-current="page"' if name == tab else "")
            + f">{name.title()}</a>"
            for name in _RUN_TABS
        )
        heading += f'<nav class="tabs" aria-label="Run tabs">{tabs}</nav>'
        if tab == "overview":
            heading += _run_numbers(row, status, state, written)
        heading += observation
        if tab not in ("overview", "charts"):
            return _document(address, heading + self._section(tab, row, collected))
        side = _console(row, collected) + _checkpoint_section(row) if tab == "overview" else ""
        if row["payload"] is not None:
            return render_run(address, row["payload"], heading, side)
        curves = _empty_section(row, "Curves", "No metric curves recorded.")
        body = f'<section class="sec" id="charts"><h2>Charts</h2>{curves}</section>'
        if overview:
            body = render_overview(body, side)
        return _document(address, heading + body)

    def _observe_console(self, row: sqlite3.Row, status: Status | None, now: datetime) -> str:
        # Session shutdown can still retain final logs after the run itself ended.
        if status is not None and (not status.done or row["console_attempt"] != status.attempts):
            self.index.request_console(self.remote, row["launched_as"])
        return f"Last collected {_age(row['collected'], now)}" if row["collected"] else ""

    def _section(self, tab: str, row: sqlite3.Row, collected: str) -> str:
        if tab == "console":
            return _console(row, collected)
        if tab == "config":
            return _config(row)
        if tab == "checkpoints":
            return _checkpoint_section(row)
        assert tab == "files"
        files = self.db.execute(
            "SELECT value FROM files,json_each(files.names) WHERE files.address=?",
            (row["address"],),
        )
        names = "".join(f"<li><code>{escape(key)}</code></li>" for (key,) in files)
        body = (
            f'<ul class="file-keys">{names}</ul>'
            if names
            else _empty_section(row, "Files", "No files recorded.")
        )
        return f'<section class="sec"><h2>Files</h2>{body}</section>'

    def waiting(self) -> str:
        message = f"Waiting for the run to appear in {self.index.source_uri}."
        if error := self._errors():
            message += " " + error
        return _observation("run", "waiting", message)

    def _list_runs(self, experiment: str | None = None) -> list[sqlite3.Row]:
        if self._observation is None:
            return []
        # Lists read the panel projection they display, never a full run chart.
        if experiment is None:
            projection, selection = "headline", "ORDER BY experiment,address"
        else:
            projection, selection = (
                "latest,launched_as",
                "WHERE experiment=:experiment ORDER BY address",
            )
        return self.db.execute(
            _VISIBLE
            + f"""
            SELECT experiment,address,job,state,phase,position,status,last_write,at,
                   continues,listed,definition,panel_order,{projection}
            FROM visible LEFT JOIN charts USING(address) {selection}
            """,
            {"experiment": experiment, "remote": self.remote},
        ).fetchall()

    def experiments(self) -> str:
        observation = self._observation
        rows = self._list_runs()
        now = datetime.now(UTC)
        opened: list[str] = []
        completed: list[str] = []
        stale = False
        for name, members in groupby(rows, itemgetter("experiment")):
            runs = list(members)
            panel = _first_panel(runs)
            counts: Counter[str] = Counter()
            active = False
            latest: str | None = None
            dots = []
            costs = []
            panels: list[str] = []
            for run in runs:
                state = _display_state(run)
                counts[state] += 1
                active |= _is_open(run, state)
                if (written := run["last_write"]) and (latest is None or written > latest):
                    latest = written
                status = _status(run)
                if status is not None:
                    costs.append(status)
                address = run["address"]
                facts = (address, state, run["position"], status.machine if status else None)
                label = escape(" · ".join(fact for fact in facts if fact))
                dots.append(
                    f'<a class="run-dot" href="{run_path(address)}" aria-label="{label}" title="{label}">'
                    f'<i class="state-dot" aria-hidden="true" data-state="{escape(state)}"></i></a>'
                )
                if (run["definition"], run["panel_order"]) == panel and run["headline"]:
                    panels.append(run["headline"])
            totals = " · ".join(f"{counts[state]} {escape(state)}" for state in sorted(counts))
            stale |= "no end recorded" in counts
            group = opened if active else completed
            spend = ""
            if costs:
                usd = sum((s.usd for s in costs), Decimal(0))
                spend = f"{'~' if any(s.estimated for s in costs) else ''}${usd:.2f} · "
            dated = (
                "no files yet"
                if all(_no_files(run) for run in runs)
                else "File observation unavailable"
            )
            if latest:
                dated = f"last write <span>{_age(latest, now)}</span>"
            chart = ""
            if panels:
                data = ",".join(panels).replace("<", "\\u003c")
                chart = f'<div class="card-charts"><script type="application/json">[{data}]</script></div>'
            group.append(
                f'<article class="card experiment-card"><h3><a href="/experiments/{quote(name)}">{escape(name)}</a></h3>'
                f'{chart}<div class="run-dots">{"".join(dots)}</div>'
                f"<p>{totals}</p><footer>{spend}{dated}</footer></article>"
            )
        if observation:
            body = (
                _experiment_cards("Open", opened) + _experiment_cards("Completed", completed)
                if rows
                else "<p>Waiting for runs.</p>"
            )
            ages = _observation_ages(observation, now, launcher=any(row["status"] for row in rows))
            body += f'<p class="context">{escape(" · ".join(ages))}</p>'
        else:
            body = "<p>Waiting for the first observation.</p>"
        return _live_status(body, stale=stale, error=self._errors())

    def machines(self) -> str:
        if remote := self.remote:
            return _live_status(
                f"<p>The launcher database is on {escape(remote)}. "
                "Run <code>etude machines</code> to list its machines.</p>",
            )
        row = self._observation
        error = self._errors()
        if row is None or (row["launcher_at"] is None and not error):
            body = "<p>Waiting for the first local observation.</p>"
        elif row["launcher_at"] is None:
            body = "<p>Machine listing unavailable.</p>"
        else:
            # Only the Machines page needs the full table.
            (machines,) = self.db.execute("SELECT machines FROM observation WHERE id=1").fetchone()
            if machines is None:
                body = "<p>No launcher is used here.</p>"
            else:
                ages = " · ".join(_observation_ages(row, datetime.now(UTC), launcher=True))
                body = machines + f'<p class="context">{escape(ages)}</p>'
        return _live_status(body, error=error)

    def reports(self, declared: dict[str, Path], served: dict[str, _ServedReport]) -> str:
        visible = {
            row[0]
            for row in self.db.execute(
                _VISIBLE + "SELECT DISTINCT experiment FROM visible", {"remote": self.remote}
            )
        }
        row = self._observation
        unstarted = "Not started" if row and row["at"] else "Waiting"
        rows = []
        for name in sorted(visible | declared.keys()):
            report = served.get(name)
            state = "Completed" if name in visible else unstarted
            if name in self._open_experiments:
                state = "Open"
            built = (
                f'<time datetime="{report.built.isoformat()}">{report.built:%Y-%m-%d %H:%M:%S UTC}</time>'
                if report
                else "Never"
            )
            declaration = report.declaration if report else ("—" if name in declared else "Default")
            headline = escape(report.headline) if report and report.headline else "—"
            rows.append(
                f'<tr><td><a href="/experiments/{quote(name)}/report">{escape(name)}</a></td>'
                f"<td>{state}</td><td>{built}</td><td>{escape(declaration)}</td>"
                f"<td>{headline}</td><td>{report.runs if report else '—'}</td></tr>"
            )
        columns = (
            "Experiment",
            "State",
            "Built in this view",
            "Declared at",
            "Headline",
            "Runs drawn",
        )
        return _live_status(
            f'<div class="reports-index">{_table(columns, rows)}</div>'
            if rows
            else "<p>No experiments yet.</p>",
            error=self._errors(),
        )

    @cached_property
    def _open_experiments(self) -> dict[str, set[str]]:
        # The partial index skips completed history, and no chart or console is selected.
        rows = self.db.execute(
            _VISIBLE
            + "SELECT experiment,state,phase,last_write,at FROM visible WHERE may_be_open ORDER BY experiment",
            {"remote": self.remote},
        )
        opened = {}
        for name, runs in groupby(rows, itemgetter("experiment")):
            states = {state for row in runs if _is_open(row, state := _display_state(row))}
            if states:
                opened[name] = states
        return opened

    @cached_property
    def _observation(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.db.execute(
            _OBSERVED + "SELECT * FROM observed",
            {"remote": self.remote},
        ).fetchone()
        return row

    def rail(self, section: str) -> str:
        links = []
        for name, states in self._open_experiments.items():
            label = escape(", ".join(sorted(states)))
            links.append(
                f'<a href="/experiments/{quote(name)}"><i class="state-dot" '
                f'role="img" aria-label="{label}" title="{label}"></i>{escape(name)}</a>'
            )
        row = self._observation
        empty = "None" if row and row["at"] else "Awaiting file observation"
        opened = "".join(links) or f"<span>{empty}</span>"
        launcher_at = row["launcher_at"] if row else None
        machines = row["machine_rail"] if row else None
        outage = ""
        if self.launcher_error:
            outage = "Server unavailable" if self.remote is not None else "Launcher unavailable"
        loop = ""
        if self.remote is not None or machines is not None:
            if row and row["last_loop"]:
                age = "last loop " + _age(row["last_loop"], datetime.now(UTC))
                loop = f"{outage} · {age}" if outage else age
            else:
                loop = outage or ("No server loop" if launcher_at else "Awaiting server")
            loop = f'<div class="rail-loop">{loop}</div>'
        if self.remote is not None:
            machines = f'<a href="/machines">On {escape(self.remote)}</a>'
        elif machines is None:
            label = outage or ("No launcher" if launcher_at else "Awaiting launcher")
            machines = f"<span>{label}</span>"
        nav = []
        for path, label in (
            ("/", "Experiments"),
            ("/machines", "Machines"),
            ("/reports", "Reports"),
        ):
            current = ' aria-current="location"' if path == section else ""
            nav.append(f'<a href="{path}"{current}>{label}</a>')
        return (
            f'<nav id="view-rail" aria-label="Live view" data-refresh="{_REFRESH * 1000}">'
            f'<div class="rail-pages">{"".join(nav)}</div>'
            f'<div class="rail-group"><h2>Open experiments</h2>{opened}</div>'
            f'<div class="rail-group"><h2>Held machines</h2>{machines}</div>'
            f"{loop}</nav>"
        )

    def report_ready(self, experiment: str, *, declared: bool) -> bool | None:
        """Whether a known experiment has indexed files. Unknown experiments have no page."""
        row = self.db.execute(
            _VISIBLE
            + """SELECT CASE
                    WHEN EXISTS (SELECT 1 FROM files WHERE experiment=:experiment) THEN 1
                    WHEN :declared OR EXISTS (SELECT 1 FROM visible WHERE experiment=:experiment) THEN 0
                 END""",
            {"experiment": experiment, "declared": declared, "remote": self.remote},
        ).fetchone()
        return bool(row[0]) if row[0] is not None else None

    def experiment_status(
        self, experiment: str, *, file: Path | None, run: str | None, tab: str, curves: str | None
    ) -> str | None:
        observation = self._observation
        rows = self._list_runs(experiment)
        if file is None and not rows:
            return None
        now = datetime.now(UTC)
        base = f"/experiments/{quote(experiment)}"
        addresses = {row["address"] for row in rows}
        selected = rows[0]["address"] if rows else None
        if run:
            qualified = f"{experiment}/{run}"
            # Delivery can match a queued launch to a differently named stored run.
            candidates = (run, qualified)
            selected = next((key for key in candidates if key in addresses), None)
            if selected is None:
                selected = next(
                    (r["address"] for r in rows if r["launched_as"] in candidates), None
                )
        if tab not in _DETAIL_TABS:
            tab = "console"
        panel = _first_panel(rows)
        stale = False
        body = []
        unfinished = []
        detail = ""
        for row in rows:
            address = row["address"]
            short = address.removeprefix(experiment + "/")
            # Historical compact and qualified addresses can coexist.
            compact = short == experiment or short.startswith(experiment + "@")
            key = address if compact or short in addresses else short
            pick = f"{base}?run={quote(key, safe='@')}&amp;tab="
            state = _display_state(row)
            stale |= state == "no end recorded"
            status = _status(row)
            if status and not status.done:
                unfinished.append(status.run)
            metric = (
                " · ".join(_METRICS.validate_json(row["latest"]))
                if row["latest"] and (row["definition"], row["panel_order"]) == panel
                else None
            )
            last_write = (
                "no files yet" if _no_files(row) else f"last write {_age(row['last_write'], now)}"
            )
            if observation and row["at"] != observation["at"]:
                last_write += f" · {_file_age(row['at'], now, 'files observed')}"
            reason = _state_reason(state, status)
            machine = escape(status.machine) if status and status.machine else "—"
            current = ' aria-current="true"' if address == selected else ""
            body.append(
                f'<tr><td><a title="{escape(address)}" href="{pick}{tab}#run-detail"{current}>{escape(short)}</a>'
                f"<small>{escape(row['job'])}</small>{row['continues'] or ''}</td>"
                f'<td><span class="pill">{escape(state)}</span><small>{escape(reason)}</small></td>'
                f"<td>{escape(row['position'] or '—')}<small>{last_write}</small></td>"
                f"<td>{escape(metric or '—')}</td><td>{machine}</td>"
                f"<td>{status.formatted_spend if status else '—'}</td></tr>"
            )
            if address == selected:
                detail = self._detail(address, pick, tab, curves, now)
        if rows:
            table = _table(("Run", "State", "Clock", "Latest metric", "Machine", "Dollars"), body)
            if not detail:
                detail = "<p>The selected run is unavailable in this experiment.</p>"
        elif observation and observation["at"]:
            table = "<p>No runs recorded.</p>"
        else:
            table = "<p>Waiting for the first file observation.</p>"
        ages = ""
        if observation:
            launcher = any(row["status"] for row in rows)
            ages = " · ".join(_observation_ages(observation, now, launcher=launcher))
        declaration = self.db.execute(
            "SELECT error FROM declarations WHERE experiment=?", (experiment,)
        ).fetchone()
        return _live_status(
            _command_bar(file, self.index.source_uri, unfinished)
            + f'<div class="experiment-split"><section class="sec"><h2>Runs</h2>{table}</section>{detail}</div>'
            f'<section><p class="context">{escape(ages)}</p>'
            '<p class="lbl">No machine samples recorded.</p></section>',
            stale=stale,
            error=self._errors(declaration[0] if declaration else None),
        )

    def _detail(self, address: str, pick: str, tab: str, curves: str | None, now: datetime) -> str:
        row = self._read_run(address, tab, mounted=curves)
        assert row is not None  # Selected from this read transaction.
        status = _status(row)
        if tab != "curves":
            collected = self._observe_console(row, status, now) if tab == "console" else ""
            section = self._section(tab, row, collected)
        elif row["kind"] == "evaluation":
            section = '<p>Evaluation scores are in the <a href="#report">Report</a> tab.</p>'
        else:
            # A poll with the mounted generation needs no chart bytes, even from SQLite.
            token = row["inputs"]
            data = (
                f'<script type="application/json">{row["payload"]}</script>'
                if row["payload"]
                else ""
            )
            section = (
                f'<div class="run-curves" data-generation="{token}">{data}</div>'
                if token
                else _empty_section(row, "Curves", "No metric curves recorded.")
            )
        tabs = "".join(
            f'<a id="detail-{name}" href="{pick}{name}#run-detail"'
            + (' aria-current="page"' if name == tab else "")
            + f">{name.title()}</a>"
            for name in _DETAIL_TABS
        )
        return (
            f'<aside id="run-detail" class="run-detail"><h2>{_run_link(address)}</h2>'
            f"<p>{_run_line(row, _display_state(row), status)}</p>"
            f'<nav aria-label="Run detail">{tabs}</nav>{section}</aside>'
        )


def _command_bar(file: Path | None, source: str, unfinished: list[str]) -> str:
    commands: list[tuple[str, ...]] = []
    if file:
        commands.extend(
            ("etude", action, str(file), "--source", source)
            for action in ("launch", "eval", "report")
        )
    if unfinished:
        commands.append(("etude", "cancel", *unfinished))
    if not commands:
        return ""
    buttons = "".join(
        f'<button type="button" id="copy-{cmd[1]}"><code>{escape(shlex.join(cmd))}</code></button>'
        for cmd in commands
    )
    return f'<div class="command-bar"><span class="lbl" aria-live="polite">Copy command</span>{buttons}</div>'


def _first_panel(rows: list[sqlite3.Row]) -> tuple[str, int] | None:
    # Held files from an older report or clock cannot contribute a comparable metric.
    newest = max((r for r in rows if r["definition"]), key=itemgetter("listed", "at"), default=None)
    if newest is None:
        return None
    orders = [
        r["panel_order"]
        for r in rows
        if r["definition"] == newest["definition"] and r["panel_order"] is not None
    ]
    return (newest["definition"], min(orders)) if orders else None


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
            """INSERT INTO next_files(address,experiment,inputs,last_write,names,names_version)
               SELECT address,experiment,inputs,last_write,names,names_version FROM files WHERE address=?""",
            (key,),
        )
    return held


def _stage_run(
    db: sqlite3.Connection,
    storage: ListedStorage,
    run: LocatedRun,
    address: str,
    keys: list[str],
    report: Report | None,
    clock: str,
    previous: str | None,
) -> None:
    digest = sha256()
    definition = None
    if report is not None:
        digest.update(clock.encode() + b"\0")
        digest.update(report.model_dump_json(include=report.model_fields_set).encode())
        definition = digest.hexdigest()
    names = json.dumps(keys) if report is not None else None
    sealed = [key for key in keys if not key.endswith(".open")]
    for key in sealed:
        digest.update(f"{key}\0{storage.objects[key]}\0".encode())
    inputs = digest.hexdigest()
    data = read_located(storage, run, sealed, whole_blocks=True) if previous != inputs else None
    db.execute(
        """INSERT INTO next_files(address,experiment,inputs,last_write,names,names_version)
           VALUES(?,?,?,?,?,?)""",
        (
            address,
            run.file.experiment,
            inputs,
            run.last_write.isoformat() if run.last_write else None,
            names,
            sha256(names.encode()).hexdigest() if names is not None else None,
        ),
    )
    if data is None:
        return
    metadata = data.model_dump_json(exclude={"last_write"}, round_trip=True)
    if report is None:
        db.execute("UPDATE next_files SET metadata=? WHERE address=?", (metadata, address))
        return
    charts = run_chart_data(storage.storage, data, report, clock)
    headline = charts.headline
    reached = data.reached
    preferred = (
        report.clock
        if report.clock in reached
        else next((c.name for c in data.clocks if c.name in reached), next(iter(reached), None))
    )
    db.execute(
        """UPDATE next_files SET metadata=?,reached=?,preferred=?,definition=?,headline=?,latest=?,
           overview=?,payload=?,pace=?,sample_ms=? WHERE address=?""",
        (
            metadata,
            _CLOCKS.dump_json(reached).decode(),
            preferred,
            definition,
            headline.model_dump_json(exclude_defaults=True) if headline else None,
            json.dumps(headline.latest()) if headline else None,
            charts.overview,
            charts.payload,
            _CLOCKS.dump_json(_mean_pace(data, reached)).decode(),
            data.blocks[-1].header.wall_last if data.blocks else None,
            address,
        ),
    )


def _mean_pace(data: RunData, reached: dict[str, float]) -> dict[str, float]:
    """Clock units per second between this run's first and latest sealed rows."""
    if not data.blocks:
        return {}
    first, last = data.blocks[0].header, data.blocks[-1].header
    seconds = (last.wall_last - first.wall_first) / 1000
    if seconds <= 0:
        return {}
    # Subtract the first recorded coordinate so the child never claims its parent's steps.
    start = {c.name: c.first for c in first.clocks if c.first is not None}
    return {
        c.name: (c.last - start[c.name]) / seconds
        for c in last.clocks
        if c.last is not None
        and c.name in start
        and c.last >= start[c.name]
        and c.last == reached.get(c.name)
    }


def _store_run(
    db: sqlite3.Connection,
    address: str,
    experiment: str,
    run: TrainingRunSummary | EvalRunSummary,
    reached: dict[str, float],
    preferred: str | None,
    file_sources: RunSources,
    machine_rate: Decimal | None,
) -> None:
    stored = run.stored
    file = stored.file if stored else None
    record = run.standing.record if run.standing else None
    job = record.launch.spec if record else None
    status = run.status
    training = isinstance(run, TrainingRunSummary)
    file_state: RunState | None
    file_continues = joined_continues = None
    if isinstance(run, TrainingRunSummary):
        file_job = run.job if file else None
        file_state = stored.stored_state if stored else None
        if file and file.continues:
            parent = file_sources.parent(experiment, file.continues)
            file_continues = _handoff(experiment, file.continues, parent)
        if run.continues:
            joined_continues = _handoff(experiment, run.continues, run.parent_address)
    else:
        # A tournament can arrive before the root that names its evaluation launch.
        file_state = run.file_state
        file_job = (file.job_name or file.job) if file else None
        if file_state and not file_job:
            file_job = "Evaluation"

    # Once files exist, even an empty sealed clock belongs to them. The
    # launcher can still supply a target on a different clock.
    clocks = status.clocks if stored is None and status else reached
    file_config = file.config_json if file else None
    fields = {
        "address": address,
        "file_continues": file_continues,
        "launcher_continues": joined_continues if joined_continues != file_continues else None,
        "experiment": experiment,
        "kind": "training" if training else "evaluation",
        "eval_last_write": run.last_write.isoformat()
        if isinstance(run, EvalRunSummary) and run.last_write
        else None,
        "file_job": file_job,
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
        "machine_rate": f"${machine_rate:.2f}/hour" if machine_rate is not None else None,
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


def _handoff(experiment: str, edge: Continuation, parent: str | None) -> str:
    name = edge.parent.source_id
    recorded = run_address(name, experiment) if SAFE_NAME.fullmatch(name) else name
    link = _run_link(parent) if parent else escape(recorded)
    return f'<div class="context">Continued from {link} at {escape(edge.clock)} {number(edge.value)}</div>'


def _held_machines(machines: tuple[MachineStatus, ...]) -> str:
    links = []
    for status in machines:
        name = status.machine.name
        idle = ' <span class="idle">idle</span>' if not status.runs else ""
        links.append(f'<a href="/machines#machine-{quote(name)}">{escape(name)}{idle}</a>')
    rate = sum(status.cost.rental.usd_per_hour for status in machines if status.cost)
    held = "".join(links) or "<span>None</span>"
    return f'{held}<span class="rail-rate">${rate:.2f} an hour</span>'


def _machines_table(db: sqlite3.Connection, machines: tuple[MachineStatus, ...]) -> str:
    if not machines:
        return "<p>No machines held.</p>"
    occupied = [run for status in machines for run in status.runs]
    placements = {}
    if occupied:
        allocated = db.execute(
            """SELECT launched_as,address,launcher_state AS state,launcher_position AS position,
                      status,max_attempts
               FROM runs WHERE launched_as IN (SELECT value FROM json_each(?))""",
            (json.dumps(occupied),),
        )
        placements = {row["launched_as"]: row for row in allocated}
    rows = []
    for status in machines:
        machine = status.machine
        runs = []
        for run in status.runs:
            row = placements.get(run)
            address = row["address"] if row else run
            detail = (
                _run_line(row, row["state"], _status(row), on_machine=machine.name) if row else ""
            )
            runs.append(f"<div>{_run_link(address)}{(' · ' + detail) if detail else ''}</div>")
        description = escape(describe_machine(status, available="ready"))
        if warning := machine_warning(status):
            description += f"<div>{escape(warning)}</div>"
        rows.append(
            f'<tr id="machine-{escape(machine.name)}"><th scope="row">{escape(machine.name)}</th><td>{escape(machine.supply)}</td>'
            f"<td>{description}</td><td>{''.join(runs) or 'Idle'}</td></tr>"
        )
    return _table(
        ("Machine", "Pool or on-demand", "Readiness, window and cost", "Occupied slots"), rows
    )


def _status(row: sqlite3.Row) -> Status | None:
    return Status.model_validate_json(row["status"]) if row["status"] is not None else None


def _state_reason(state: str, status: Status | None) -> str:
    if status is None:
        return ""
    if state == "queued":
        return status.reason or ""
    if not status.done:
        return ""
    outcome = (
        status.outcome
        if status.outcome != state and (state, status.outcome) != ("finished", "succeeded")
        else None
    )
    return " · ".join(filter(None, (outcome, status.cause)))


def _run_line(
    row: sqlite3.Row,
    state: str,
    status: Status | None,
    *,
    on_machine: str | None = None,
    overview: bool = False,
) -> str:
    parts = [f'<span class="pill">{escape(state)}</span>']
    facts = [] if overview else [row["position"]]
    if status:
        facts.append(_state_reason(state, status))
        facts.extend(
            fact
            for fact in launcher_facts(status, row["max_attempts"], on_machine=on_machine)
            if not overview or fact != status.formatted_spend
        )
    parts.extend(f"<span>{escape(fact)}</span>" for fact in facts if fact)
    return " · ".join(parts)


def _run_numbers(row: sqlite3.Row, status: Status | None, state: str, written: str) -> str:
    target = status.until if status else None
    clock = target.clock if target else row["preferred"]
    rates = _CLOCKS.validate_json(row["pace"]) if row["pace"] else {}
    rate = rates.get(clock) if clock else None
    values = [
        ("Clock", row["position"]),
        ("Latest metric", row["metric"]),
        ("Mean pace", f"{rate:.4g} {clock}/s" if rate is not None else None),
    ]
    if state == "running" and target and rate:
        reached = _CLOCKS.validate_json(row["reached"])[target.clock]
        seconds = max(0, target.value - reached) / rate
        try:
            end = datetime.fromtimestamp(row["sample_ms"] / 1000, UTC) + timedelta(seconds=seconds)
        except (OverflowError, ValueError, OSError):
            pass  # The projected date lies outside the calendar.
        else:
            values.append(("Ends about", end.strftime("%Y-%m-%d %H:%M UTC")))
    values.extend(
        (
            ("Dollars", status.formatted_spend if status else None),
            ("Machine rate", row["machine_rate"]),
        )
    )
    return (
        '<dl class="run-numbers">'
        + "".join(
            f"<div><dt>{label}</dt><dd>{escape(value or '—')}</dd></div>" for label, value in values
        )
        + f'<div><dt>Last file write</dt><dd id="last-write">{escape(written)}</dd></div></dl>'
    )


def _is_open(row: sqlite3.Row, state: str) -> bool:
    return row["phase"] != "done" if row["phase"] else state in ("queued", "running")


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


def _experiment_cards(title: str, cards: list[str]) -> str:
    body = (
        f'<div class="grid multiples">{"".join(cards)}</div>'
        if cards
        else f"<p>No {title.lower()} experiments.</p>"
    )
    return f'<section class="sec"><h2>{title}</h2>{body}</section>'


def _status_page(title: str, status: str) -> str:
    return _document(
        title,
        f'<header class="head"><h1>{escape(title)}</h1></header>{status}',
    )


def _display_state(row: sqlite3.Row) -> str:
    state: str = row["state"]
    if state == "running" and row["phase"] is None and row["last_write"]:
        if datetime.fromisoformat(row["at"]) - datetime.fromisoformat(row["last_write"]) >= _SILENT:
            return "no end recorded"
    return state


def _empty_section(row: sqlite3.Row, subject: str, recorded: str) -> str:
    # Only a successful listing can establish that a section is empty.
    message = recorded if row["at"] else f"{subject} appear once the view has read the run files."
    return f"<p>{message}</p>"


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
        marker = (
            f' data-last-checkpoint="{escape(json.dumps(checkpoint.at))}"'
            if checkpoint is stored.checkpoints[-1]
            else ""
        )
        rows.append(
            f"<tr{marker}><td>{checkpoint.ordinal}</td><td>{escape(checkpoint.name)}</td><td>{escape(clocks)}</td></tr>"
        )
    return _table(("Ordinal", "Checkpoint", "Clock position"), rows) if rows else None


def _config(row: sqlite3.Row) -> str:
    config = row["config"]
    if config is not None:
        shown = f'<pre class="scroll">{escape(config)}</pre>'
    else:
        shown = _empty_section(row, "Config details", "No config recorded.")
    source = row["source"] or ""
    return f'<section class="sec" id="config"><h2>Config</h2>{shown}{source}</section>'


def _checkpoint_section(row: sqlite3.Row) -> str:
    checkpoints = row["checkpoints"] or _empty_section(
        row, "Checkpoints", "No checkpoints recorded."
    )
    return f'<section class="sec" id="checkpoints"><h2>Checkpoints</h2>{checkpoints}</section>'


def _age(at: str | None, now: datetime) -> str:
    if at is None:
        return "unknown"
    seconds = max(0, int((now - datetime.fromisoformat(at)).total_seconds()))
    for unit, scale in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= scale:
            return f"{seconds // scale} {unit} ago"
    return f"{seconds} s ago"


def _observation(
    page: str, token: str, message: str, collected: str = "", written: str = ""
) -> str:
    return (
        f'<p class="context" id="{page}-observation" data-refresh="{_REFRESH * 1000}" '
        f'data-version="{token}" data-collected="{escape(collected)}" data-written="{escape(written)}">{escape(message)}</p>'
    )


def _breadcrumbs(experiment: str | None, *, run: str | None = None, report: bool = False) -> str:
    pages = [("Experiments", "/")]
    if experiment is not None:
        path = f"/experiments/{quote(experiment, safe='')}"
        pages.append((experiment, path))
        if report:
            pages.append(("Report", path + "/report"))
    if run is not None:
        label = run.removeprefix(experiment + "/") if experiment else run
        pages.append((label, run_path(run)))
    links = []
    for i, (label, url) in enumerate(pages):
        current = ' aria-current="page"' if i == len(pages) - 1 else ""
        links.append(f'<a href="{escape(url)}"{current}>{escape(label)}</a>')
    separator = '<span aria-hidden="true">›</span>'
    return f'<nav class="breadcrumbs" aria-label="Breadcrumb">{separator.join(links)}</nav>'


def _shell(html: str, rail: str, breadcrumbs: str = "") -> str:
    # Both document templates put body tags outside escaped author content.
    opening = (
        f'<body>{rail}<div class="view-body"><p id="status-error" role="status"></p>{breadcrumbs}'
    )
    closing = f"</div><script>{read_asset('refresh_status.js')}</script></body>"
    return html.replace("<body>", opening, 1).replace("</body>", closing, 1)


def _document(title: str, body: str) -> str:
    css = read_asset("page.css")
    return (
        f'<!doctype html><html lang="en"><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)} · Etude</title><style>{css}</style><body><main class="wrap">{body}</main></body></html>'
    )


def _declaration_commit(file: Path) -> str | None:
    """Only attribute a local declaration to HEAD when its file is clean and tracked."""
    try:
        status = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "--literal-pathspecs",
                "-C",
                str(file.parent),
                "status",
                "--porcelain=v2",
                "--branch",
                "--no-ahead-behind",
                "--ignored",
                "--untracked-files=all",
                "--",
                file.name,
            ],
            capture_output=True,
        )
    except OSError:
        return None
    lines = status.stdout.splitlines()
    if status.returncode or any(not line.startswith(b"# ") for line in lines):
        return None
    return next(
        (
            m[1].decode()
            for line in lines
            if (m := re.fullmatch(rb"# branch\.oid ([0-9a-f]+)", line))
        ),
        None,
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
        self._served: dict[str, _ServedReport] = {}
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
        query = parse_qs(url.query)
        observe = query.get("observe") == ["1"]
        if report := _REPORT_ROUTE.fullmatch(path):
            return self._report_page(
                report[1], observe=observe, embed=query.get("embed") == ["1"], refresh=refresh
            )

        with self.index.snapshot() as view:
            if path.startswith("/runs/"):
                html = view.run(
                    path.removeprefix("/runs/"),
                    tab=query.get("tab", ["overview"])[0],
                    observe=observe,
                )
                if html is None:
                    return None
                rail = view.rail("/")
                return html + rail if observe else _shell(html, rail)

            fragment = path.endswith("/status")
            route = path.removesuffix("/status") or "/"
            section = "/"
            breadcrumbs = ""
            if route == "/":
                title, status = "Experiments", view.experiments()
            elif route == "/machines":
                title, status = "Machines", view.machines()
                section = route
            elif route == "/reports":
                with self._lock:
                    served = self._served.copy()
                title, status = "Reports", view.reports(self.files, served)
                section = route
            elif match := re.fullmatch(r"/experiments/([^/]+)(/status/?|/)?", path):
                fragment = match[2] == "/status"
                title = match[1]
                found = view.experiment_status(
                    title,
                    file=self.files.get(title),
                    run=query.get("run", [""])[0] or None,
                    tab=query.get("tab", ["console"])[0],
                    curves=query.get("curves", [None])[0] if fragment else None,
                )
                if found is None:
                    return None
                status = found
                if not fragment:
                    breadcrumbs = _breadcrumbs(title)
                    status = (
                        '<nav class="tabs" id="experiment-tabs" aria-label="Experiment">'
                        '<a href="#status" aria-current="page">Status</a><a href="#report">Report</a></nav>'
                        f'{status}<iframe hidden id="report" title="Experiment report" '
                        f'data-src="/experiments/{quote(title)}/report?embed=1"></iframe>'
                    )
            else:
                return None
            rail = view.rail(section)
            if fragment:
                return status + rail
            if section == "/":
                status += (
                    f"<style>{read_asset('vendor/uPlot.min.css')}</style>"
                    f"<script>{read_asset('vendor/uPlot.iife.min.js')}</script>"
                    f"<script>{read_asset('charts.js')}</script>"
                )
            return _shell(_status_page(title, status), rail, breadcrumbs)

    def _report_page(
        self, experiment: str, *, observe: bool, embed: bool, refresh: bool
    ) -> str | None:
        with self.index.snapshot() as view:
            ready = view.report_ready(experiment, declared=experiment in self.files)
            if ready is None:
                return None
            rail = "" if embed else view.rail("/reports")
        # Release the index snapshot before building or waiting on an authored report.
        with self._lock:
            waiting = not ready and experiment not in self._served
        breadcrumbs = _breadcrumbs(experiment, report=True) if rail and not observe else ""
        if waiting:
            if refresh:
                raise ValueError("Waiting for run files.")
            notice = _observation("report", "waiting", "Waiting for run files.")
            return (
                notice + rail
                if observe
                else _shell(_document(experiment, notice), rail, breadcrumbs)
            )
        if observe:
            return _observation("report", "ready", "Building report…") + rail
        report_html = self._report(experiment, refresh=refresh)
        return _shell(report_html, rail, breadcrumbs) if rail else report_html

    def _report(self, experiment: str, *, refresh: bool) -> str:
        """Share the first build and explicit refreshes between report readers."""
        with self._lock:
            report = self._served.get(experiment)
            if not refresh and report is not None:
                return report.html
            shared = self._pending.get(experiment)
            if shared is None:
                self._pending[experiment] = Future()
        if shared is not None:
            return shared.result()

        # Use the first request's thread so the server can close its listening
        # socket while an authored report is still building.
        try:
            with self._building:
                report = self._build_report(experiment)
        except BaseException as error:
            with self._lock:
                self._pending.pop(experiment).set_exception(error)
            raise
        with self._lock:
            self._served[experiment] = report
            self._pending.pop(experiment).set_result(report.html)
        return report.html

    def _build_report(self, experiment: str) -> _ServedReport:
        file = self.files.get(experiment)
        commit = _declaration_commit(file) if file else None
        page = self.pages.build(file, experiment=experiment)
        control = (
            '<button id="refresh-report">Refresh report</button> '
            '<span id="refresh-status" role="status"></span>'
            f"<script>{read_asset('refresh_report.js')}</script>"
        )
        html = render(page, heading=control)
        declaration = "Default"
        if page.origin:
            declaration = (
                commit[:12]
                if file and commit and _declaration_commit(file) == commit
                else "Working tree"
            )
        return _ServedReport(
            html=html,
            built=datetime.now(UTC),
            declaration=declaration,
            headline=page.headline(),
            runs="Eval only"
            if page.selections and not page.training.runs
            else len(page.training.runs),
        )


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
        if not self._report_route():
            self.send_error(405)
            return
        self._respond(refresh=True)

    def _report_route(self) -> re.Match[str] | None:
        return _REPORT_ROUTE.fullmatch(unquote(urlsplit(self.path).path))

    def _respond(self, *, refresh: bool) -> None:
        html: str | None = None
        try:
            html = self.server.page(self.path, refresh=refresh)
            status = 200 if html else 404
            if html is None and urlsplit(self.path).path.startswith("/runs/"):
                # A train prints its link before the next listing can discover it.
                with self.server.index.snapshot() as view:
                    address = unquote(urlsplit(self.path).path.removeprefix("/runs/"))
                    experiment, _, _ = address.rpartition("/")
                    html = _shell(
                        _document("Run", view.waiting()),
                        view.rail("/"),
                        _breadcrumbs(experiment or None, run=address),
                    )
            body = (html or "Not found.").encode()
        except (Exception, SystemExit) as error:
            reason = explain(error)
            if not refresh and (route := self._report_route()):
                html = _document(
                    "Experiment report",
                    f'<p role="status">Report build failed: {escape(reason)}</p>'
                    '<button onclick="location.reload()">Retry report</button>',
                )
                if parse_qs(urlsplit(self.path).query).get("embed") != ["1"]:
                    with self.server.index.snapshot() as view:
                        html = _shell(
                            html, view.rail("/reports"), _breadcrumbs(route[1], report=True)
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
