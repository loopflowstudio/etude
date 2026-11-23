"""Postgres owns accepted evidence and independently versioned charts for the hosted view.

Ingest serializes on a run row. Projection reads a committed run snapshot without
holding that lock, then publishes only if its revision still names that evidence.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from etude.block_file import BlockFile, parse
from etude.carriage import immutable
from etude.checkpoints import CheckpointRecord
from etude.errors import EtudeError, explain
from etude.eval.block import Block
from etude.eval.records import Environment, TournamentRun
from etude.identity import run_address
from etude.observations import AttemptObservation, ViewGrant, evidence_member, member_folder
from etude.page import (
    DEFAULT_REPORT,
    Pages,
    ReportDeclaration,
    experiment_files,
    read_report,
    run_chart_data,
)
from etude.records import BUILT_IN_NAMES, RunOutcome, RunRecord, RunWriter
from etude.report import Report
from etude.run import LocatedRun, RunListing
from etude.storage import ObjectVersion
from etude.train.block import decode
from etude.train.read import read_located
from etude.train.records import RowTable
from etude.view import (
    ReportSummary,
    RunDetail,
    RunRow,
    attempt_position,
    checkpoint_table,
    declaration_commit,
    format_position,
    panel_definition,
    render_report,
)


def _migrations() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).with_name("view_migrations")))
    return cfg


def upgrade(url: str) -> None:
    engine = create_engine(make_url(url).set(drivername="postgresql+psycopg"))
    try:
        with engine.begin() as connection:
            cfg = _migrations()
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
    finally:
        engine.dispose()


class PostgresStorage:
    """Visible evidence in one snapshot, relative to a run or grouped by physical run for a report."""

    name = "Hosted push feed"

    def __init__(
        self, db: Connection, *, address: str | None = None, experiment: str | None = None
    ) -> None:
        self.db, self.address, self.experiment = db, address, experiment

    def versions(self, prefix: str) -> dict[str, str]:
        if self.address is not None:
            rows = self.db.execute(
                "SELECT key,sha256,modified_at FROM run_files WHERE address=%s AND visible AND starts_with(key,%s)",
                (self.address, prefix),
            )
        else:
            # Prefix each physical root with its own namespace. A compact run
            # and a tournament inside a different group may have the same name.
            rows = self.db.execute(
                """SELECT replace(address,'/','!') || '/' || address || '/' || key,sha256,modified_at
                   FROM runs JOIN run_files USING(address) WHERE experiment=%s AND visible""",
                (self.experiment,),
            )
        return {
            key: ObjectVersion(digest, at) for key, digest, at in rows if key.startswith(prefix)
        }

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.versions(prefix))

    def _location(self, key: str) -> tuple[str, str]:
        if self.address is not None:
            return self.address, key
        scope, _, rest = key.partition("/")
        address = scope.replace("!", "/")
        if not rest.startswith(address + "/"):
            raise FileNotFoundError(key)
        return address, rest[len(address) + 1 :]

    def get(self, key: str) -> bytes:
        row = self.db.execute(
            "SELECT body FROM run_files WHERE address=%s AND key=%s AND visible",
            self._location(key),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return bytes(row[0])

    def get_range(self, key: str, start: int, length: int) -> bytes:
        row = self.db.execute(
            "SELECT substring(body FROM %s::int FOR %s::int) FROM run_files WHERE address=%s AND key=%s AND visible",
            (start + 1, length, *self._location(key)),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return bytes(row[0])

    def uri(self, key: str) -> str:
        info = self.db.info
        location = sha256(f"{info.host}/{info.port}/{info.dbname}".encode()).hexdigest()[:20]
        return f"etude-view://{location}/{self.address or self.experiment}/{key}"

    def put(self, key: str, data: bytes) -> None:
        raise TypeError("view evidence is read-only")

    def put_file(self, key: str, path: Path) -> None:
        raise TypeError("view evidence is read-only")

    def delete(self, key: str) -> bool:
        raise TypeError("view evidence is read-only")


def _lock_run(db: Connection, grant: ViewGrant) -> int:
    """All evidence for an address is ordered by the same row lock."""
    db.execute(
        """INSERT INTO runs(address,experiment,latest_attempt_number)
           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
        (grant.run, grant.experiment, grant.attempt_number),
    )
    row = db.execute(
        "SELECT latest_attempt_number FROM runs WHERE address=%s FOR UPDATE", (grant.run,)
    ).fetchone()
    assert row is not None
    if grant.attempt_number > row[0]:
        db.execute(
            """UPDATE runs SET latest_attempt_number=%s,last_push_at=now(),
               attempt_state=NULL,machine=NULL,attempt_position=NULL WHERE address=%s""",
            (grant.attempt_number, grant.run),
        )
    return max(int(row[0]), grant.attempt_number)


@dataclass(frozen=True)
class _RunFile:
    """Facts established by the portable parser before taking the run's write lock."""

    member: str
    record: RunRecord | None
    block: BlockFile | None
    tables: list[str]
    staged: bool

    @classmethod
    def read(cls, grant: ViewGrant, key: str, digest: str, body: bytes) -> _RunFile:
        member, name = evidence_member(key)
        block = parse(name)
        tables: list[str] = []
        staged = name.endswith(".parquet")
        record = None
        if block:
            try:
                columns = decode(body)
            except (KeyError, OverflowError, IndexError, ValueError):
                raise EtudeError("invalid_metric_block", 422) from None
            if (
                (columns.writer, columns.first, columns.last)
                != (block.writer, block.first, block.last)
                or block.first > block.last
                or block.level > 2**31 - 1
            ):
                raise EtudeError("metric_block_name_mismatch", 422)
        elif name == "run.json":
            record = RunRecord.model_validate_json(body)
            if (
                run_address(record.id, record.experiment) != (member or grant.run)
                or any(not member_folder(m) for m in record.members or ())
                or (member and (record.members is not None or record.kind != "evaluation"))
            ):
                raise EtudeError("run_address_mismatch", 422)
        elif name.startswith("checkpoints/"):
            CheckpointRecord.model_validate_json(body)
        elif name == "outcome.json":
            RunOutcome.model_validate_json(body)
        elif name.startswith("artifacts/"):
            if name != f"artifacts/{digest}.json":
                raise EtudeError("view_artifact_digest_mismatch", 422)
            RowTable.model_validate_json(body)
        elif name == "environment.json":
            Environment.model_validate_json(body)
        elif name == "tournament.json":
            TournamentRun.model_validate_json(body, context={"experiment": grant.experiment})
        elif staged:
            _validate_parquet(body)
        elif name.endswith("/block.json"):
            index = Block.model_validate_json(body, context={"experiment": grant.experiment})
            if name != f"blocks/{index.name}/block.json":
                raise EtudeError("view_block_name_mismatch", 422)
            folder = key.rsplit("/", 1)[0]
            tables = [folder + "/" + f for f in index.files if f.endswith(".parquet")]
            if any(evidence_member(t)[0] != member for t in tables):
                raise EtudeError("invalid_view_table", 422)
        else:
            writer = RunWriter.model_validate_json(body)
            if name != f"metrics/{writer.writer}.json":
                raise EtudeError("writer_record_name_mismatch", 422)
        return cls(member, record, block, tables, staged)


class PostgresView:
    def __init__(self, url: str, path: Path | None) -> None:
        self.pool: ConnectionPool[Connection] = ConnectionPool(url, min_size=0, max_size=8)
        try:
            with self.pool.connection() as db:
                revision = db.execute("SELECT to_regclass('alembic_version')").fetchone()
                current = (
                    revision
                    and revision[0]
                    and db.execute("SELECT version_num FROM alembic_version").fetchone()
                )
                if current != (ScriptDirectory.from_config(_migrations()).get_current_head(),):
                    raise ValueError("hosted view schema needs etude view-upgrade")
            files = experiment_files(path) if path and path.is_dir() else [path] if path else []
            self.files = {file.stem: file for file in files}
            self._building = Lock()
            self._declarations: dict[Path, ReportDeclaration] = {}
            self._reading_declarations = Lock()
            self._refresh_projections()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.pool.close()

    def accept_observation(self, grant: ViewGrant, observation: AttemptObservation) -> None:
        number = grant.attempt_number
        position = observation.console.position if observation.console else -1
        finished = observation.phase == "done"
        body = observation.model_dump_json()
        with self.pool.connection() as db:
            if number < _lock_run(db, grant):
                raise EtudeError("superseded_attempt", 409)
            prior = db.execute(
                """SELECT attempt_id,producer_seq,console_position,finished FROM attempts
                   WHERE address=%s AND number=%s""",
                (grant.run, number),
            ).fetchone()
            if prior:
                identity, seq, cursor, done = prior
                if (
                    identity != grant.attempt
                    or seq > observation.producer_seq
                    or cursor > position
                    or done
                    and not finished
                ):
                    raise EtudeError("view_attempt_conflict", 409)
                if seq == observation.producer_seq:
                    if not db.execute(
                        "SELECT 1 FROM attempts WHERE address=%s AND number=%s AND observation=%s",
                        (grant.run, number, body),
                    ).fetchone():
                        raise EtudeError("view_attempt_conflict", 409)
                    return
            db.execute(
                """INSERT INTO attempts(address,number,attempt_id,producer_seq,console_position,finished,observation)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(address,number) DO UPDATE SET producer_seq=excluded.producer_seq,
                   console_position=excluded.console_position,finished=excluded.finished,observation=excluded.observation""",
                (
                    grant.run,
                    number,
                    grant.attempt,
                    observation.producer_seq,
                    position,
                    finished,
                    body,
                ),
            )
            db.execute(
                """UPDATE runs SET last_push_at=now(),attempt_state=%s,machine=%s,attempt_position=%s
                   WHERE address=%s""",
                (
                    observation.outcome or observation.phase,
                    observation.context.machine,
                    attempt_position(observation),
                    grant.run,
                ),
            )

    def accept_file(
        self, grant: ViewGrant, key: str, digest: str, modified: datetime, body: bytes
    ) -> None:
        file = _RunFile.read(grant, key, digest, body)
        block = file.block
        address = grant.run
        with self.pool.connection() as db:
            latest = _lock_run(db, grant)
            if not immutable(key) and grant.attempt_number < latest:
                raise EtudeError("superseded_attempt", 409)
            if file.member:
                root = db.execute(
                    """SELECT convert_from(body,'UTF8')::jsonb FROM run_files
                       WHERE address=%s AND key='run.json'""",
                    (address,),
                ).fetchone()
                if not root or file.member not in (root[0].get("members") or ()):
                    raise EtudeError("undeclared_view_member", 409)
                if file.record and file.record.experiment != root[0]["experiment"]:
                    raise EtudeError("view_member_experiment_mismatch", 422)
            old = db.execute(
                "SELECT sha256 FROM run_files WHERE address=%s AND key=%s",
                (address, key),
            ).fetchone()
            if old:
                if old[0] == digest:
                    return
                if immutable(key):
                    raise EtudeError("view_evidence_conflict", 409)
            if file.tables:
                visible = db.execute(
                    "UPDATE run_files SET visible=true WHERE address=%s AND key=ANY(%s)",
                    (address, file.tables),
                )
                # A missing table rolls this transaction back, keeping the entire block staged.
                if visible.rowcount != len(set(file.tables)):
                    raise EtudeError("view_block_tables_missing", 409)
            if block:
                cover = (
                    address,
                    block.writer,
                    block.first,
                    block.last,
                    block.first,
                    block.last,
                    block.level,
                )
                # Same ordering as block_file.select: wider coverage, then higher level.
                if db.execute(
                    """SELECT 1 FROM run_files WHERE address=%s AND writer=%s
                       AND seq_first<=%s AND seq_last>=%s
                       AND (seq_first<>%s OR seq_last<>%s OR level>=%s) LIMIT 1""",
                    cover,
                ).fetchone():
                    return
                db.execute(
                    """DELETE FROM run_files WHERE address=%s AND writer=%s
                       AND seq_first>=%s AND seq_last<=%s""",
                    cover[:4],
                )
            db.execute(
                """
                INSERT INTO run_files(address,key,sha256,body,attempt_number,modified_at,
                                      writer,seq_first,seq_last,level,visible)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(address,key) DO UPDATE SET sha256=excluded.sha256,body=excluded.body,
                    attempt_number=excluded.attempt_number,modified_at=excluded.modified_at
                """,
                (
                    address,
                    key,
                    digest,
                    body,
                    grant.attempt_number,
                    modified,
                    block.writer if block else None,
                    block.first if block else None,
                    block.last if block else None,
                    block.level if block else None,
                    not file.staged,
                ),
            )
            if (record := file.record) and not file.member:
                db.execute(
                    "UPDATE runs SET experiment=%s,job=%s,kind=%s WHERE address=%s",
                    (
                        record.experiment or address,
                        record.job_name or record.name,
                        record.kind,
                        address,
                    ),
                )
            if file.staged:
                return
            db.execute(
                """UPDATE runs SET accepted_rev=accepted_rev+1,
                       last_push_at=CASE WHEN latest_attempt_number=%s THEN now() ELSE last_push_at END
                   WHERE address=%s""",
                (grant.attempt_number, address),
            )
        self._project(address)

    def _panels(self, experiment: str) -> Report:
        file = self.files.get(experiment)
        if file is None:
            return DEFAULT_REPORT
        with self._reading_declarations:
            accepted = read_report(file, self._declarations.get(file))
            self._declarations[file] = accepted
            return accepted.report

    def _refresh_projections(self, experiment: str | None = None) -> None:
        # Find changed definitions from small summaries before reading any metric blocks.
        where = "AND experiment=%s" if experiment is not None else ""
        with self.pool.connection() as db:
            rows = db.execute(
                f"""SELECT address,experiment,run_clock,panel_definition,accepted_rev,
                           published_rev<accepted_rev OR projection_error IS NOT NULL
                    FROM runs WHERE accepted_rev>0 {where} ORDER BY experiment""",
                (experiment,) if experiment is not None else (),
            ).fetchall()
        for address, name, default_clock, definition, revision, pending in rows:
            if not pending:
                try:
                    report = self._panels(name)
                    if panel_definition(report, report.clock or default_clock) == definition:
                        continue
                except (Exception, SystemExit):
                    pass  # Projection records the sanitized failure and leaves the chart intact.
            # Fence any projection in flight with an older declaration, including across web processes.
            with self.pool.connection() as db:
                db.execute(
                    "UPDATE runs SET accepted_rev=accepted_rev+1 WHERE address=%s AND accepted_rev=%s",
                    (address, revision),
                )
            self._project(address)

    def _project(self, address: str) -> None:
        with self.pool.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = db.execute(
                "SELECT accepted_rev FROM runs WHERE address=%s", (address,)
            ).fetchone()
            assert row is not None
            revision = row[0]
            storage = PostgresStorage(db, address=address)
            try:
                record = RunRecord.model_validate_json(storage.get("run.json"))
                run = LocatedRun(name=address.rsplit("/", 1)[-1], file=record, folders=("",))
                # Hosted eval curves come from the experiment report. Only the group root owns its lifecycle.
                evaluation = record.kind == "evaluation"
                keys = (
                    sorted(storage.keys("metrics/") + storage.keys("outcome.json"))
                    if evaluation
                    else storage.keys("")
                )
                data = read_located(storage, run, keys, whole_blocks=True)
                report = self._panels(record.experiment)
                default_clock = next((c.name for c in record.clocks), BUILT_IN_NAMES[0])
                clock = report.clock or default_clock
                charts = None if evaluation else run_chart_data(storage, data, report, clock)
                checkpoints = checkpoint_table(data)
                definition = panel_definition(report, clock)
                headline = charts.headline if charts else None
                error = None
            except (Exception, SystemExit) as exc:
                charts, error = None, explain(exc)
        with self.pool.connection() as db:
            if error is not None:
                db.execute(
                    "UPDATE runs SET projection_error=%s WHERE address=%s AND accepted_rev=%s",
                    (error, address, revision),
                )
                return
            if (
                db.execute(
                    """UPDATE runs SET published_rev=%s, projection_error=NULL,experiment=%s,kind=%s,job=%s,
                       file_state=%s,position=%s,headline=%s,latest=%s,panel_order=%s,panel_definition=%s,run_clock=%s
                   WHERE address=%s AND accepted_rev=%s RETURNING address""",
                    (
                        revision,
                        record.experiment or address,
                        record.kind,
                        record.job_name or record.name,
                        data.stored_state,
                        format_position(data.reached, clock),
                        headline.model_dump_json(exclude_defaults=True) if headline else None,
                        " · ".join(headline.latest()) if headline else None,
                        headline.order if headline else None,
                        definition,
                        default_clock,
                        address,
                        revision,
                    ),
                ).fetchone()
                and charts is not None
            ):
                db.execute(
                    """
                    INSERT INTO run_charts(address,overview,payload,checkpoints) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(address) DO UPDATE SET overview=excluded.overview,payload=excluded.payload, checkpoints=excluded.checkpoints
                    """,
                    (address, charts.overview, charts.payload, checkpoints),
                )

    @contextmanager
    def snapshot(self) -> Iterator[PostgresSnapshot]:
        with self.pool.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield PostgresSnapshot(db)

    def report(self, experiment: str, *, refresh: bool) -> str:
        with self._building:
            with self.snapshot() as view:
                if not refresh and (saved := view.report_html(experiment)) is not None:
                    return saved
                file = self.files.get(experiment)
                commit = declaration_commit(file) if file else None

                def listing(name: str) -> RunListing:
                    return RunListing.read(PostgresStorage(view.db, experiment=name))

                pages = Pages(
                    PostgresStorage(view.db, experiment=experiment),
                    local=False,
                    line_source=listing,
                )
                # Use the snapshot time to fence builds finishing out of order on separate web hosts.
                started = view.db.execute("SELECT transaction_timestamp()").fetchone()
                assert started
                report = render_report(pages.build(file, experiment=experiment), file, commit)
            with self.pool.connection() as db:
                db.execute(
                    """INSERT INTO reports(experiment,html,built_at,declaration,headline,runs_label)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(experiment) DO UPDATE SET html=excluded.html,built_at=excluded.built_at,
                       declaration=excluded.declaration,headline=excluded.headline,runs_label=excluded.runs_label
                       WHERE reports.built_at<=excluded.built_at""",
                    (
                        experiment,
                        report.html,
                        started[0],
                        report.declaration,
                        report.headline,
                        str(report.runs),
                    ),
                )
            if refresh:
                self._refresh_projections(experiment)
            return report.html


def _validate_parquet(body: bytes) -> None:
    # DuckDB is the optional evaluation dependency. Check the footer before retaining an immutable key.
    import duckdb  # noqa: PLC0415

    with NamedTemporaryFile(suffix=".parquet") as file:
        file.write(body)
        file.flush()
        try:
            with duckdb.connect(":memory:") as reader:
                reader.execute("SELECT * FROM parquet_schema(?) LIMIT 1", [file.name]).fetchone()
        except duckdb.Error as error:
            raise EtudeError("invalid_view_parquet", 422) from error


class PostgresSnapshot:
    def __init__(self, db: Connection) -> None:
        self.db = db

    def open_experiments(self) -> tuple[str, ...]:
        rows = self.db.execute(
            """SELECT DISTINCT experiment FROM runs
               WHERE last_push_at > now()-interval '10 minutes'
               AND coalesce(attempt_state,file_state,'running') IN ('running','setting_up') ORDER BY experiment"""
        )
        return tuple(row[0] for row in rows)

    def rows(self, experiment: str | None) -> list[RunRow]:
        # Cards need downsampled headlines; the experiment table only needs their latest values.
        panel = "headline,NULL" if experiment is None else "NULL,latest"
        where = "" if experiment is None else "WHERE experiment=%s"
        rows = self.db.execute(
            f"""SELECT experiment,address,job,coalesce(attempt_state,file_state,'running'),
                   last_push_at,coalesce(nullif(attempt_position,''),position),machine,panel_order,panel_definition,{panel}
                FROM runs {where} ORDER BY experiment,address""",
            () if experiment is None else (experiment,),
        )
        return [RunRow(*row) for row in rows]

    def report_html(self, experiment: str) -> str | None:
        row = self.db.execute(
            "SELECT html FROM reports WHERE experiment=%s", (experiment,)
        ).fetchone()
        return str(row[0]) if row else None

    def report_index(self) -> dict[str, ReportSummary]:
        return {
            name: ReportSummary(
                at, declaration, headline, "Eval only" if label == "Eval only" else int(label)
            )
            for name, at, declaration, headline, label in self.db.execute(
                "SELECT experiment,built_at,declaration,headline,runs_label FROM reports"
            )
        }

    def experiments(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT experiment FROM runs")}

    def report_ready(self, experiment: str, *, declared: bool) -> bool | None:
        row = self.db.execute(
            """SELECT CASE
                 WHEN EXISTS(SELECT 1 FROM runs WHERE experiment=%s AND job IS NOT NULL) THEN true
                 WHEN %s OR EXISTS(SELECT 1 FROM runs WHERE experiment=%s) THEN false END""",
            (experiment, declared, experiment),
        ).fetchone()
        assert row
        return None if row[0] is None else bool(row[0])

    def detail(
        self, address: str, *, tab: str, observe: bool, mounted: str | None = None
    ) -> RunDetail | None:
        payload = "NULL"
        if not observe and tab in ("overview", "charts", "curves"):
            payload = "overview" if tab == "overview" else "payload"
        marks = "checkpoints" if not observe and tab in ("overview", "checkpoints") else "NULL"
        join = "" if marks == payload == "NULL" else "LEFT JOIN run_charts USING(address)"
        args: tuple[str, ...] = (address,)
        if mounted and payload != "NULL":
            # Publication advances this revision for both evidence and changed panel definitions.
            args = (mounted, address)
            payload = f"CASE WHEN published_rev::text<>%s THEN {payload} END"
        attempt = (
            "observation" if not observe and tab in ("console", "overview", "config") else "NULL"
        )
        files = (
            "ARRAY(SELECT key FROM run_files WHERE address=runs.address AND visible ORDER BY key)"
            if tab == "files" and not observe
            else "ARRAY[]::text[]"
        )
        with self.db.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                f"""SELECT published_rev AS revision,last_push_at AS received,projection_error AS error,
                           {payload} AS payload,latest_attempt_number AS attempt_number,producer_seq AS sequence,
                           coalesce(attempt_state,file_state,'running') AS phase,
                           {attempt} AS attempt,{marks} AS checkpoints,experiment,kind,{files} AS files
                    FROM runs {join} LEFT JOIN attempts ON attempts.address=runs.address
                        AND attempts.number=runs.latest_attempt_number WHERE runs.address=%s""",
                args,
            ).fetchone()
        if row is None:
            return None
        if row["attempt"] is not None:
            row["attempt"] = AttemptObservation.model_validate_json(row["attempt"])
        return RunDetail.model_validate(row)
