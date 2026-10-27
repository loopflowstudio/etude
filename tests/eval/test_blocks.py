"""An eval run's records as sealed blocks: few objects, few reads, and conversion of old runs."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import duckdb
import pytest
from moto import mock_aws

from etude.cache import Cache
from etude.eval import block
from etude.eval.block import Block, Staging, quote
from etude.eval.convert import convert, delete_files
from etude.eval.match import environment, evaluate
from etude.eval.query import read_blobs, statsdb
from etude.eval.records import Code, Match, MatchRecord
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage, S3Storage
from tests.eval.games import play, play_with_evidence
from tests.run.storage import CountedClient, MeasuredStorage

DUEL = environment("duel")


def pair(tmp_path: Path) -> tuple[Code, Code]:
    script = tmp_path / "scripted.py"
    script.write_text("def act(state): return 0\n")
    return (
        Code(name="strong", source=[script], config={"strength": 2}),
        Code(name="weak", source=[script], config={"strength": 1}),
    )


def deals(players: tuple[Code, Code], count: int) -> list[Match]:
    return [Match(players=players, conditions={"deal": deal}, games=2) for deal in range(count)]


def rows(db: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[object, ...]]]:
    """Every row a reader sees, in a fixed order."""
    return {
        "matches": db.sql("select match, state, games from matches order by all").fetchall(),
        "plays": db.sql("select * exclude (run) from plays order by all").fetchall(),
        "games": db.sql("select * exclude (run) from games order by all").fetchall(),
    }


def test_an_eval_run_delivers_a_few_objects_per_block_not_one_per_match_or_blob(
    tmp_path: Path,
) -> None:
    bucket = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    evaluate(experiment="cards", environment=DUEL, play=play_with_evidence,
             matches=deals(pair(tmp_path), 40), run="a", directory=tmp_path / "a",
             storages=[bucket])  # fmt: skip
    # run.json, environment.json and state.json; one block of its pack, two tables and index.
    assert bucket.uploads == 3 + 4
    (index,) = (key for key in bucket.keys("a/blocks/") if key.endswith("/block.json"))
    kept = Block.model_validate_json(bucket.get(index))
    assert len(kept.matches) == 40 and len(kept.blobs) == 80
    # Plain DuckDB reads the delivered tables without Etude.
    games = tmp_path / "bucket" / "a" / "blocks" / "*" / "games.parquet"
    assert duckdb.sql(f"select count(*) from read_parquet({quote(games)})").fetchone() == (80,)

    reading = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    db = statsdb("cards", reading)
    # run.json, environment.json, the index and two tables; not one byte of the pack.
    assert (reading.listings, reading.requests, reading.ranges) == (1, 5, 0)
    assert kept.pack is not None and reading.bytes < kept.pack.size_bytes
    assert db.sql("select count(*), count(distinct match) from blobs").fetchone() == (80, 40)
    # Rows name their blobs, and each blob's bytes come back verified.
    named = [digest for (digest,) in db.sql("select evidence_sha256 from games").fetchall()]
    found = dict(read_blobs(db, reading, named))
    assert sorted(found) == sorted(named)
    assert all(sha256(body).hexdigest() == digest for digest, body in found.items())
    # The pack's blobs lie together: one range read for all of them.
    assert reading.ranges == 1
    with pytest.raises(EtudeError) as unknown:
        list(read_blobs(db, reading, ["0" * 64]))
    assert unknown.value.code == "blob_unknown"
    db.close()


def test_blobs_read_from_a_bucket_are_read_once_per_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="runs")
        cache = Cache(tmp_path / "cache")
        evaluate(experiment="cards", environment=DUEL, play=play_with_evidence,
                 matches=deals(pair(tmp_path), 4), run="a", directory=tmp_path / "a",
                 storages=[S3Storage("runs", client=client, cache=cache)])  # fmt: skip
        bucket = S3Storage("runs", client=client, cache=cache)
        db = statsdb("cards", bucket)
        named = [digest for (digest,) in db.sql("select sha256 from blobs").fetchall()]
        before = client.requests
        assert len(dict(read_blobs(db, bucket, named))) == 8
        assert client.requests == before + 1
        assert len(dict(read_blobs(db, S3Storage("runs", client=client, cache=cache), named))) == 8
        assert client.requests == before + 1
        db.close()


def per_file(
    tmp_path: Path, players: tuple[Code, Code]
) -> tuple[DirectoryStorage, dict[str, list[tuple[object, ...]]]]:
    """An eval run `a` in the per-file layout older Etude wrote, with manabot-style evidence.

    Made from a run of blocks: each match's record, a table file per match and
    one per merge (the later holds the same rows, as a merge does), and each
    blob as an author's file under `artifacts/`.
    """
    made = tmp_path / "made"
    evaluate(experiment="cards", environment=DUEL, play=play_with_evidence,
             matches=deals(players, 6), run="a", directory=made)  # fmt: skip
    root = tmp_path / "old"
    folder = root / "a"
    folder.mkdir(parents=True)
    for name in ("run.json", "environment.json", "state.json"):
        shutil.copy(made / "a" / name, folder / name)
    (blocks,) = (made / "a" / "blocks").glob("[0-9]*")
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
        merged = folder / "tables" / f"{table}.{99:020d}.abcdef.parquet"
        shutil.copy(blocks / f"{table}.parquet", merged)
    pack = blocks / "blobs.zip"
    with pack.open("rb") as stream:
        for digest, span in kept.blobs.items():
            stream.seek(span.offset)
            (folder / "artifacts" / "evidence" / f"{digest}.json.gz").write_bytes(
                stream.read(span.size_bytes)
            )
    db = statsdb("cards", made)
    expected = rows(db)
    db.close()
    return DirectoryStorage(root), expected


def test_a_per_file_run_converts_to_blocks_with_the_same_rows_and_blobs(tmp_path: Path) -> None:
    players = pair(tmp_path)
    old, expected = per_file(tmp_path, players)
    old_keys = old.keys("a/")
    # Readers refuse the old layout, naming the command that converts it.
    with pytest.raises(EtudeError) as refused:
        statsdb("cards", old)
    assert refused.value.code == "eval_run_not_converted"
    assert "etude convert-eval a" in str(refused.value.__cause__)

    local = DirectoryStorage(tmp_path / "local")
    shutil.copytree(old.root, local.root)
    done = convert("a", local)
    assert (done.blocks, done.matches, done.blobs) == (1, 6, 12)
    assert done.replaced == len(old_keys) - 3
    # A second pass finds nothing new.
    assert convert("a", local).blocks == 0
    db = statsdb("cards", local)
    converted = rows(db)
    assert converted == expected
    named = {digest for (digest,) in db.sql("select evidence_sha256 from games").fetchall()}
    held = dict(read_blobs(db, local, named))
    assert set(held) == named
    db.close()

    # The bucket has not been given the blocks: nothing there is deleted.
    with pytest.raises(EtudeError) as unconverted:
        delete_files("a", old)
    assert unconverted.value.code == "eval_files_not_converted"
    assert old.keys("a/") == old_keys
    # Once delivered, exactly the files they replace go.
    shutil.copytree(local.root / "a" / "blocks", old.root / "a" / "blocks")
    assert delete_files("a", old) == len(old_keys) - 3
    assert {key.split("/")[1] for key in old.keys("a/")} == {
        "run.json",
        "environment.json",
        "state.json",
        "blocks",
    }
    db = statsdb("cards", old)
    assert rows(db) == expected
    db.close()


def test_matches_a_dead_writer_staged_are_sealed_by_the_next_writer(tmp_path: Path) -> None:
    players = pair(tmp_path)
    matches = deals(players, 3)
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches[:1], run="a",
             directory=tmp_path)  # fmt: skip
    root = tmp_path / "a"
    # A writer stages a match and dies before sealing it.
    staging = Staging(root, "0123456789ab")
    db = duckdb.connect()
    db.execute(
        f"copy (select 0 as game, 0 as player, true as won, 'a' as run, '{matches[1].key}' as match)"
        f" to {quote(staging.table('plays', matches[1].key))} (format parquet)"
    )
    staging.stage(MatchRecord(match=matches[1], state="done", wall_ms=1))
    staging.close()
    resumed = evaluate(experiment="cards", environment=DUEL, play=play, matches=matches, run="a",
                       directory=tmp_path)  # fmt: skip
    assert (resumed.played, resumed.skipped) == (1, 2)
    found = statsdb("cards", tmp_path)
    assert found.sql("select count(*) from matches").fetchone() == (3,)
    assert not [path for path in (root / "blocks").iterdir() if path.name.startswith(".")]
    found.close()


def test_a_long_eval_run_seals_a_block_whenever_its_segment_is_old_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(block, "SEAL_SECONDS", 0.0)
    bucket = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(pair(tmp_path), 3),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    # Each match is delivered in a block of its own as soon as it is stored.
    indexes = [key for key in bucket.keys("a/blocks/") if key.endswith("/block.json")]
    assert len(indexes) == 3
    assert bucket.uploads == 3 + 3 * 3
