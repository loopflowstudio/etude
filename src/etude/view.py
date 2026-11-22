"""Live run pages. One ingestion loop publishes the chart index for all readers.

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
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from types import FrameType
from urllib.parse import quote, unquote, urlsplit

from etude.cache import Cache
from etude.errors import explain
from etude.experiment import state_of
from etude.identity import run_address
from etude.jobs import EvalRunSummary, ExperimentState, TrainingRunSummary
from etude.launch.client import standing
from etude.launch.records import Standing
from etude.launch.server import selected
from etude.launch.source import home
from etude.page import Pages, read_report, render, render_run, report_sources, run_chart_data
from etude.report import Report
from etude.run import LocatedRun, as_storage, located_runs, owning_folder
from etude.storage import ListedStorage, Storage
from etude.train.read import RunData, read_located
from etude.view_link import advertise

_REFRESH = 5
_REMOTE_STANDING = 30
_SILENT = timedelta(minutes=10)


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
            (source.uri("run.json"), str(path.resolve()) if path else "", version("etude-sdk"))
        )
        directory = home() / "views"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = directory / (sha256(identity.encode()).hexdigest() + ".sqlite")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    address TEXT PRIMARY KEY, experiment TEXT NOT NULL, state TEXT NOT NULL,
                    last_write TEXT, launcher TEXT, launched_as TEXT
                );
                CREATE INDEX IF NOT EXISTS launcher_address ON runs(launched_as);
                CREATE INDEX IF NOT EXISTS experiment_runs ON runs(experiment, address);
                CREATE TABLE IF NOT EXISTS charts (
                    address TEXT PRIMARY KEY, inputs TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation (id INTEGER PRIMARY KEY, at TEXT NOT NULL, launcher_at TEXT);
            """)
        self._cache = Cache()
        self._files: dict[str, _RunFiles] = {}
        self._reports: dict[Path | None, tuple[tuple[tuple[str, int, int], ...], Report]] = {}
        self._standing: tuple[Standing, ...] = ()
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
                self._standing = standing()
                self._launcher_at = datetime.now(UTC)
                self._launcher_error = None
            except Exception as error:
                self._launcher_error = explain(error)
            self._next_standing = time.monotonic() + (_REMOTE_STANDING if remote else 0)
        return self._standing

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
        indexed = dict(db.execute("SELECT address, inputs FROM charts"))
        # Stage large payloads outside the main database, one run at a time.
        db.execute("PRAGMA temp_store=FILE")
        db.execute("CREATE TEMP TABLE changed(address TEXT, inputs TEXT, payload TEXT)")
        current: dict[str, _RunFiles] = {}
        present: set[str] = set()
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
            present.add(address)
            if indexed.get(address) != fingerprint:
                # Rendering may materialize block arrays. Only metadata lives
                # through to the next poll; raw bytes belong to the shared cache.
                payload = run_chart_data(self.source, inputs.data.model_copy(), report)
                db.execute(
                    "INSERT INTO changed(address,inputs,payload) VALUES (?,?,?)",
                    (address, fingerprint, payload),
                )
        states = [
            state_of(by_experiment[name], name, retained=tuple(retained[name]))
            for name in sorted(by_experiment.keys() | retained.keys())
        ]
        db.execute(
            "INSERT OR REPLACE INTO charts(address,inputs,payload) SELECT address,inputs,payload FROM changed"
        )
        db.executemany(
            "DELETE FROM charts WHERE address=?", ((a,) for a in indexed.keys() - present)
        )
        self._publish_states(db, states, by_experiment)
        db.execute(
            "INSERT OR REPLACE INTO observation(id,at,launcher_at) VALUES(1,?,?)",
            (observed.isoformat(), self._launcher_at.isoformat() if self._launcher_at else None),
        )
        self._files = current
        self.error = self._launcher_error

    @staticmethod
    def _publish_states(
        db: sqlite3.Connection, states: list[ExperimentState], data: dict[str, list[RunData]]
    ) -> None:
        removed = {row[0] for row in db.execute("SELECT address FROM runs")}
        for state in states:
            addresses = {
                r.name: run_address(r.file.id, state.experiment)
                for r in data[state.experiment]
                if r.file
            }
            runs: tuple[TrainingRunSummary | EvalRunSummary, ...] = (*state.runs, *state.eval_runs)
            for run in runs:
                status = run.status
                address = addresses.get(run.name) or (
                    status.run if status else run_address(run.name, state.experiment)
                )
                detail = None if status is None else str(status)
                if status and status.machine:
                    detail = f"{detail} · {status.machine}"
                removed.discard(address)
                db.execute(
                    """
                    INSERT INTO runs(address,experiment,state,last_write,launcher,launched_as) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(address) DO UPDATE SET experiment=excluded.experiment,
                      state=excluded.state,last_write=excluded.last_write,launcher=excluded.launcher,launched_as=excluded.launched_as
                    WHERE (experiment,state,last_write,launcher,launched_as) IS NOT
                          (excluded.experiment,excluded.state,excluded.last_write,excluded.launcher,excluded.launched_as)
                    """,
                    (
                        address,
                        state.experiment,
                        run.state,
                        run.last_write.isoformat() if run.last_write else None,
                        detail,
                        status.run if status else None,
                    ),
                )
        db.executemany("DELETE FROM runs WHERE address=?", ((a,) for a in removed))

    def run(self, address: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT runs.*, charts.payload, observation.at, observation.launcher_at FROM runs
                LEFT JOIN charts USING(address) JOIN observation ON observation.id=1
                WHERE runs.address=? OR runs.launched_as=?
                """,
                (address, address),
            ).fetchone()
        if row is None:
            return None
        now = datetime.now(UTC)
        last = datetime.fromisoformat(row["last_write"]) if row["last_write"] else None
        state = row["state"]
        if (
            state == "running"
            and row["launcher"] is None
            and last is not None
            and now - last >= _SILENT
        ):
            state = "no end recorded"
        details = [row["launcher"]] if row["launcher"] else []
        details.extend(
            [
                f"last write {_age(last, now)}",
                f"indexed {_age(datetime.fromisoformat(row['at']), now)}",
            ]
        )
        if row["launcher"] and row["launcher_at"]:
            details.append(
                f"launcher observed {_age(datetime.fromisoformat(row['launcher_at']), now)}"
            )
        heading = (
            f'<a href="/experiments/{quote(row["experiment"])}">{escape(row["experiment"])}</a>'
            f'<header class="head"><h1>{escape(address)}</h1><span class="pill">{escape(state)}</span></header>'
            f'<p class="context">{escape(" · ".join(details))}</p>'
            '<p id="run-position"></p>'
        )
        if state == "no end recorded":
            heading += f"<p>No new file for at least {_SILENT // timedelta(minutes=1)} minutes.</p>"
        if self.error:
            heading += f'<p role="status">{escape(self.error)}</p>'
        if state in ("queued", "running") or self.error:
            heading += _reload()
        if row["payload"] is None:
            return _document(address, heading + "<p>No metric curves recorded.</p>")
        return render_run(address, row["payload"], heading)

    def experiments(self) -> list[str]:
        with self._connect() as db:
            return [
                row[0]
                for row in db.execute("SELECT DISTINCT experiment FROM runs ORDER BY experiment")
            ]

    def addresses(self, experiment: str) -> list[str]:
        with self._connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT address FROM runs WHERE experiment=? ORDER BY address", (experiment,)
                )
            ]


def _age(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "unknown"
    seconds = max(0, int((now - at).total_seconds()))
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
        if path == "/":
            names = sorted(set(self.index.experiments()) | self.files.keys())
            links = "".join(
                f'<li><a href="/experiments/{quote(n)}">{escape(n)}</a></li>' for n in names
            )
            message = escape(self.index.error or "Waiting for runs.")
            return _document(
                "Experiments",
                "<h1>Experiments</h1>"
                + (f"<ul>{links}</ul>" if names else f"<p>{message}</p>{_reload()}"),
            )
        if not path.startswith("/experiments/"):
            return None
        experiment, _, component = path.removeprefix("/experiments/").partition("/")
        runs = self.index.addresses(experiment)
        if not runs and experiment not in self.files:
            return None
        if component == "report":
            return self._report(experiment, refresh)
        if component:
            return None
        links = "".join(
            f'<li><a href="/runs/{quote(r, safe="/@=")}">{escape(r)}</a></li>' for r in runs
        )
        return _document(
            experiment,
            f"<h1>{escape(experiment)}</h1><ul>{links}</ul>"
            f'<iframe title="Experiment report" src="/experiments/{quote(experiment)}/report" style="width:100%;height:80vh;border:0"></iframe>',
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
