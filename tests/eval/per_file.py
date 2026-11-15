"""Eval runs in the per-file layout older Etude wrote, made from a run of blocks, to test conversion."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from etude.eval.block import Block, quote


def write_per_file(made: Path, folder: Path) -> None:
    """Write run `made` as run folder `folder` in the per-file layout.

    Each match gets its record and a table file, and each table one merged
    file holding the same rows (as a finished writer's merge did). Each blob
    becomes an author's file under `artifacts/evidence/`.
    """
    folder.mkdir(parents=True)
    for name in ("run.json", "environment.json"):
        shutil.copy(made / name, folder / name)
    shutil.copy(made / "outcome.json", folder / "state.json")
    (folder / "matches").mkdir()
    (folder / "tables").mkdir()
    (folder / "artifacts" / "evidence").mkdir(parents=True)
    tables: dict[str, list[Path]] = {}
    written = 0
    for blocks in sorted((made / "blocks").glob("[0-9]*")):
        kept = Block.model_validate_json((blocks / "block.json").read_bytes())
        for record in kept.matches:
            written += 1
            old = record.model_copy(update={"blobs": ()})
            (folder / "matches" / f"{record.match.key}.json").write_text(old.model_dump_json())
            for table in kept.tables:
                target = folder / "tables" / f"{table}.{written:020d}.{record.match.key}.parquet"
                duckdb.execute(
                    f"copy (select * from read_parquet({quote(blocks / f'{table}.parquet')})"
                    f" where match = '{record.match.key}') to {quote(target)} (format parquet)"
                )
        for table in kept.tables:
            tables.setdefault(table, []).append(blocks / f"{table}.parquet")
        if kept.blobs:
            with (blocks / "blobs.zip").open("rb") as stream:
                for digest, span in kept.blobs.items():
                    stream.seek(span.offset)
                    (folder / "artifacts" / "evidence" / f"{digest}.json.gz").write_bytes(
                        stream.read(span.size_bytes)
                    )
    for table, paths in tables.items():
        target = folder / "tables" / f"{table}.{99:020d}.abcdef.parquet"
        duckdb.execute(
            f"copy (select * from read_parquet([{', '.join(quote(path) for path in paths)}]))"
            f" to {quote(target)} (format parquet)"
        )
