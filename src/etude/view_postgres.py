"""Postgres owns accepted evidence and independently versioned charts for the hosted view.

Ingest serializes on a run row. Projection reads a committed run snapshot without
holding that lock, then publishes only if its revision still names that evidence.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import Connection
from psycopg_pool import ConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from etude.block_file import parse
from etude.carriage import immutable
from etude.errors import EtudeError, explain
from etude.observations import ViewGrant
from etude.page import DEFAULT_REPORT, experiment_files, read_report, run_chart_data
from etude.records import BUILT_IN_NAMES, RunOutcome, RunRecord, RunWriter
from etude.run import LocatedRun
from etude.train.block import decode
from etude.train.read import read_located
from etude.view import RunChart


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
    """Read one run's relative evidence keys within its projection transaction."""

    name = "Hosted push feed"

    def __init__(self, db: Connection, address: str) -> None:
        self.db, self.address = db, address

    def versions(self, prefix: str) -> dict[str, str]:
        return dict(
            self.db.execute(
                "SELECT key, sha256 FROM run_files WHERE address=%s AND starts_with(key,%s)",
                (self.address, prefix),
            ).fetchall()
        )

    def keys(self, prefix: str) -> list[str]:
        return sorted(self.versions(prefix))

    def get(self, key: str) -> bytes:
        row = self.db.execute(
            "SELECT body FROM run_files WHERE address=%s AND key=%s", (self.address, key)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return bytes(row[0])

    def get_range(self, key: str, start: int, length: int) -> bytes:
        row = self.db.execute(
            "SELECT substring(body FROM %s::int FOR %s::int) FROM run_files WHERE address=%s AND key=%s",
            (start + 1, length, self.address, key),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(key)
        return bytes(row[0])

    def uri(self, key: str) -> str:
        return f"etude-view:/{self.address}/{key}"

    def put(self, key: str, data: bytes) -> None:
        raise TypeError("view evidence is read-only")

    def put_file(self, key: str, path: Path) -> None:
        raise TypeError("view evidence is read-only")

    def delete(self, key: str) -> bool:
        raise TypeError("view evidence is read-only")


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
                pending = db.execute(
                    "SELECT address FROM runs WHERE published_rev < accepted_rev"
                ).fetchall()
            files = experiment_files(path) if path and path.is_dir() else [path] if path else []
            self.reports = {file.stem: read_report(file, None).report for file in files}
            for (address,) in pending:
                self._project(address)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.pool.close()

    def accept_file(
        self, grant: ViewGrant, key: str, digest: str, modified: datetime, body: bytes
    ) -> None:
        block = parse(key)
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
        elif key == "run.json":
            record = RunRecord.model_validate_json(body)
            if record.id != grant.run:
                raise EtudeError("run_address_mismatch", 422)
        elif key == "outcome.json":
            RunOutcome.model_validate_json(body)
        else:
            writer = RunWriter.model_validate_json(body)
            if key != f"metrics/{writer.writer}.json":
                raise EtudeError("writer_record_name_mismatch", 422)

        address = grant.run
        with self.pool.connection() as db:
            db.execute("INSERT INTO runs(address) VALUES (%s) ON CONFLICT DO NOTHING", (address,))
            db.execute("SELECT address FROM runs WHERE address=%s FOR UPDATE", (address,))
            old = db.execute(
                "SELECT sha256, attempt_number FROM run_files WHERE address=%s AND key=%s",
                (address, key),
            ).fetchone()
            if old:
                if old[0] == digest:
                    if not immutable(key) and old[1] < grant.attempt_number:
                        db.execute(
                            "UPDATE run_files SET attempt_number=%s WHERE address=%s AND key=%s",
                            (grant.attempt_number, address, key),
                        )
                    return
                if immutable(key) or old[1] > grant.attempt_number:
                    raise EtudeError("view_evidence_conflict", 409)
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
                                      writer,seq_first,seq_last,level)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                ),
            )
            db.execute(
                "UPDATE runs SET accepted_rev=accepted_rev+1,last_push_at=now() WHERE address=%s",
                (address,),
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
            storage = PostgresStorage(db, address)
            try:
                record = RunRecord.model_validate_json(storage.get("run.json"))
                run = LocatedRun(name=address.rsplit("/", 1)[-1], file=record, folders=("",))
                data = read_located(storage, run, storage.keys(""), whole_blocks=True)
                report = self.reports.get(record.experiment, DEFAULT_REPORT)
                clock = report.clock or next((c.name for c in record.clocks), BUILT_IN_NAMES[0])
                charts = run_chart_data(storage, data, report, clock)
                error = None
            except Exception as exc:
                charts, error = None, explain(exc)
        with self.pool.connection() as db:
            if charts is None:
                db.execute(
                    "UPDATE runs SET projection_error=%s WHERE address=%s AND accepted_rev=%s",
                    (error, address, revision),
                )
                return
            if db.execute(
                """UPDATE runs SET published_rev=%s, projection_error=NULL
                   WHERE address=%s AND accepted_rev=%s RETURNING address""",
                (revision, address, revision),
            ).fetchone():
                db.execute(
                    """
                    INSERT INTO run_charts(address,overview,payload) VALUES (%s,%s,%s)
                    ON CONFLICT(address) DO UPDATE SET overview=excluded.overview,payload=excluded.payload
                    """,
                    (address, charts.overview, charts.payload),
                )

    def run_chart(self, address: str, *, overview: bool, observe: bool) -> RunChart | None:
        with self.pool.connection() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # A status poll does not visit run_charts or run_files.
            payload = "NULL" if observe else "overview" if overview else "payload"
            join = "" if observe else "LEFT JOIN run_charts USING(address)"
            row = db.execute(
                f"SELECT published_rev, last_push_at, projection_error, {payload} FROM runs {join} WHERE address=%s",
                (address,),
            ).fetchone()
            return RunChart(*row) if row else None
