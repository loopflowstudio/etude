"""Eval runs in the per-file layout older Etude wrote, made from a run of blocks, to test conversion."""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from etude.eval.block import Block, quote


def write_per_file(made: Path, folder: Path) -> Block:
    """Write the one block of run folder `made` as run folder `folder` in the per-file layout.

    Each match gets its record and a table file, and each table one merged
    file holding the same rows (as a finished writer's merge did). Each blob
    becomes an author's file under `artifacts/evidence/`. Returns the block.
    """
    folder.mkdir(parents=True)
    for name in ("run.json", "environment.json", "state.json"):
        shutil.copy(made / name, folder / name)
    (blocks,) = (made / "blocks").glob("[0-9]*")
    kept = Block.model_validate_json((blocks / "block.json").read_bytes())
    (folder / "matches").mkdir()
    (folder / "tables").mkdir()
    (folder / "artifacts" / "evidence").mkdir(parents=True)
    for written, record in enumerate(kept.matches, 1):
        old = record.model_copy(update={"blobs": ()})
        (folder / "matches" / f"{record.match.key}.json").write_text(old.model_dump_json())
        for table in kept.tables:
            target = folder / "tables" / f"{table}.{written:020d}.{record.match.key}.parquet"
            duckdb.execute(
                f"copy (select * from read_parquet({quote(blocks / f'{table}.parquet')})"
                f" where match = '{record.match.key}') to {quote(target)} (format parquet)"
            )
    for table in kept.tables:
        shutil.copy(
            blocks / f"{table}.parquet", folder / "tables" / f"{table}.{99:020d}.abcdef.parquet"
        )
    if kept.blobs:
        with (blocks / "blobs.zip").open("rb") as stream:
            for digest, span in kept.blobs.items():
                stream.seek(span.offset)
                (folder / "artifacts" / "evidence" / f"{digest}.json.gz").write_bytes(
                    stream.read(span.size_bytes)
                )
    return kept
