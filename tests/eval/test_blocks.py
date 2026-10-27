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
from etude.eval.records import Code, Match, MatchRecord, Rollup
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage, S3Storage
from tests.eval.games import play, play_cards, play_with_evidence
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
    # Sorted by match and game, so a filtered read skips the rest.
    order = duckdb.sql(f"select match, game from read_parquet({quote(games)})").fetchall()
    assert order == sorted(order)

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
    done = convert("a", local, rollups=[WINS])
    assert (done.blocks, done.matches, done.blobs) == (1, 6, 12)
    assert done.replaced == len(old_keys) - 3
    # A second pass finds nothing new.
    assert convert("a", local).blocks == 0
    db = statsdb("cards", local)
    converted = rows(db)
    assert converted == expected
    assert totals(db) == from_rows(db)
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


WINS = Rollup(
    name="wins", table="plays", by=("seat", "on_play"), sums={"won": "won", "hits": "hits"}
)


class ReadStorage(MeasuredStorage):
    """A store that remembers which keys were read."""

    def __init__(self, storage: DirectoryStorage) -> None:
        super().__init__(storage)
        self.read: list[str] = []

    def get(self, key: str) -> bytes:
        self.read.append(key)
        return super().get(key)


def totals(db: duckdb.DuckDBPyConnection) -> list[tuple[object, ...]]:
    return db.sql("select run, seat, on_play, rows, won, hits from wins order by all").fetchall()


def from_rows(db: duckdb.DuckDBPyConnection) -> list[tuple[object, ...]]:
    return db.sql(
        "select run, seat, on_play, count(*), sum(won::BIGINT), sum(hits) from plays"
        " group by all order by all"
    ).fetchall()


def test_a_rollup_is_summed_once_per_block_and_a_new_block_adds_only_its_own_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(block, "SEAL_SECONDS", 0.0)
    bucket = MeasuredStorage(DirectoryStorage(tmp_path / "bucket"))
    matches = deals(pair(tmp_path), 5)
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches[:3], run="a",
             directory=tmp_path / "a", storages=[bucket], rollups=[WINS])  # fmt: skip
    # Each block: two tables, one rollup and its index.
    assert bucket.uploads == 3 + 3 * 4
    db = statsdb("cards", DirectoryStorage(tmp_path / "bucket"))
    assert totals(db) == from_rows(db) and len(totals(db)) == 4
    db.close()

    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches, run="a",
             directory=tmp_path / "a", storages=[bucket], rollups=[WINS])  # fmt: skip
    reading = ReadStorage(DirectoryStorage(tmp_path / "bucket"))
    db = statsdb("cards", reading)
    # Two new blocks, each read once: its index, two tables and its partial totals.
    assert (reading.listings, reading.requests) == (1, 2 * 4)
    assert sum(key.endswith("/wins.parquet") for key in reading.read) == 2
    assert totals(db) == from_rows(db)
    assert db.sql("select sum(rows) from wins").fetchone() == (5 * 2 * 2,)
    db.close()


def test_a_match_two_blocks_hold_counts_once_in_its_rollup(tmp_path: Path) -> None:
    matches = deals(pair(tmp_path), 3)
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches[:2], run="a",
             directory=tmp_path, rollups=[WINS])  # fmt: skip
    root = tmp_path / "a"
    # A seal wrote its block and died before emptying its staging: the next writer
    # seals the same match again, in a later block.
    (sealed,) = (path for path in (root / "blocks").iterdir() if not path.name.startswith("."))
    held = Block.model_validate_json((sealed / "block.json").read_bytes())
    staging = Staging(root, "0123456789ab", [WINS])
    shutil.copy(sealed / "plays.parquet", staging.table("plays", held.matches[0].match.key))
    shutil.copy(sealed / "wins.parquet", staging.partial("wins", held.matches[0].match.key))
    staging.stage(held.matches[0])
    staging.close()
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches, run="a",
             directory=tmp_path, rollups=[WINS])  # fmt: skip
    db = statsdb("cards", tmp_path)
    assert db.sql("select sum(rows) from wins").fetchone() == (3 * 2 * 2,)
    assert totals(db) == from_rows(db)
    db.close()


def test_a_resume_with_other_rollups_is_refused(tmp_path: Path) -> None:
    matches = deals(pair(tmp_path), 2)
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches[:1], run="a",
             directory=tmp_path, rollups=[WINS])  # fmt: skip
    with pytest.raises(EtudeError) as refused:
        evaluate(experiment="cards", environment=DUEL, play=play, matches=matches, run="a",
                 directory=tmp_path)  # fmt: skip
    assert refused.value.code == "eval_run_rollups_conflict"


# manabot's card page (`manabot.eval.card_query.card_data`): each card's counts and
# winning counts, by deal and the filters the page offers, over eligible games.
CARDS = Rollup(
    name="card_deals",
    table="card_counts",
    join=("card_plays",),
    by=("card", "deal", "on_play", "deck", "opponent_deck", "player", "opponents"),
    sums={
        "gp": "deck_copies > 0",
        "gp_wins": "if(deck_copies > 0, win, 0)",
        "oh": "opening_copies",
        "oh_wins": "opening_copies * win",
        "gd": "drawn_copies",
        "gd_wins": "drawn_copies * win",
        "gih": "gih_copies",
        "gih_wins": "gih_copies * win",
        "gns": "deck_copies > 0 and gih_copies = 0",
        "gns_wins": "if(deck_copies > 0 and gih_copies = 0, win, 0)",
    },
    where="eligible",
)


def card_page(db: duckdb.DuckDBPyConnection, where: str = "true") -> list[tuple[object, ...]]:
    """`card_data`'s per-card-and-deal sums, from raw rows."""
    return db.sql(
        "select c.card, p.deal,"
        " count(*) filter (where c.deck_copies > 0),"
        " coalesce(sum(p.win) filter (where c.deck_copies > 0), 0),"
        " sum(c.opening_copies), sum(c.opening_copies * p.win),"
        " sum(c.drawn_copies), sum(c.drawn_copies * p.win),"
        " sum(c.gih_copies), sum(c.gih_copies * p.win),"
        " count(*) filter (where c.deck_copies > 0 and c.gih_copies = 0),"
        " coalesce(sum(p.win) filter (where c.deck_copies > 0 and c.gih_copies = 0), 0)"
        " from card_counts c join card_plays p using (run, match, seat, game)"
        " join seats s on s.run = p.run and s.match = p.match and s.seat = p.seat"
        " join seats o on o.run = p.run and o.match = p.match and o.seat = 1 - p.seat"
        f" where p.eligible and {where} group by all order by all"
    ).fetchall()


def card_rollup(db: duckdb.DuckDBPyConnection, where: str = "true") -> list[tuple[object, ...]]:
    sums = ", ".join(f"sum({column})" for column in CARDS.sums)
    return db.sql(
        f"select card, deal, {sums} from card_deals where {where} group by all order by all"
    ).fetchall()


def test_a_rollup_holds_the_card_page_and_a_catch_up_reads_only_the_new_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(block, "SEAL_SECONDS", 0.0)
    strong, weak = pair(tmp_path)
    matches = deals((strong, weak), 4)
    evaluate(experiment="cards", environment=DUEL, play=play_cards, matches=matches[:3],
             run="a", directory=tmp_path / "a", storages=[DirectoryStorage(tmp_path / "bucket")],
             rollups=[CARDS])  # fmt: skip
    db = statsdb("cards", tmp_path / "bucket")
    assert card_rollup(db) == card_page(db) and len(card_page(db)) == 3 * 2
    # The page's filters select the same games from the rollup alone.
    assert card_rollup(db, f"player = '{strong.key}' and on_play and deck = 'red'") == card_page(
        db, f"s.player = '{strong.key}' and p.on_play and p.deck = 'red'"
    )
    assert card_rollup(db, f"opponents = '{weak.key}'") == card_page(db, f"o.player = '{weak.key}'")
    db.close()

    evaluate(experiment="cards", environment=DUEL, play=play_cards, matches=matches,
             run="a", directory=tmp_path / "a", storages=[DirectoryStorage(tmp_path / "bucket")],
             rollups=[CARDS])  # fmt: skip
    reading = ReadStorage(DirectoryStorage(tmp_path / "bucket"))
    db = statsdb("cards", reading)
    # The new block's index, two tables and partial totals: nothing of an earlier block.
    assert (reading.listings, reading.requests) == (1, 4)
    assert card_rollup(db) == card_page(db)
    db.close()


def test_a_rollup_that_cannot_sum_a_match_refuses_it(tmp_path: Path) -> None:
    text = Rollup(name="named", table="card_counts", by=("card",), sums={"cards": "card"})
    with pytest.raises(EtudeError) as refused:
        evaluate(experiment="cards", environment=DUEL, play=play_cards,
                 matches=deals(pair(tmp_path), 1), run="a", directory=tmp_path,
                 rollups=[text])  # fmt: skip
    assert refused.value.code == "match_failed"
    assert "rollup_sum_not_numeric" in repr(refused.value.__cause__)
    # The failed match's block keeps its record and none of its rows.
    (sealed,) = (tmp_path / "a" / "blocks").glob("[0-9]*")
    kept = Block.model_validate_json((sealed / "block.json").read_bytes())
    assert [record.state for record in kept.matches] == ["failed"]
    assert (kept.tables, kept.totals) == ((), ())


class CountedConnection:
    """A DuckDB connection counting the statements it runs; each row of a batch is one."""

    def __init__(self, db: duckdb.DuckDBPyConnection) -> None:
        self.db, self.statements = db, 0

    def execute(self, query: str, parameters: object = None) -> duckdb.DuckDBPyConnection:
        self.statements += 1
        return self.db.execute(query, parameters)

    def executemany(self, query: str, rows: list[object]) -> duckdb.DuckDBPyConnection:
        self.statements += len(rows)
        return self.db.executemany(query, rows)

    def __getattr__(self, name: str) -> object:
        return getattr(self.db, name)


def test_taking_in_a_block_runs_the_same_statements_however_many_matches_and_blobs_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[CountedConnection] = []
    connect = duckdb.connect

    def counted(path: Path) -> CountedConnection:
        opened.append(CountedConnection(connect(path)))
        return opened[-1]

    players = pair(tmp_path)
    statements = []
    for count in (2, 12):
        bucket = tmp_path / f"bucket-{count}"
        evaluate(experiment="cards", environment=DUEL, play=play_with_evidence,
                 matches=deals(players, count), run="a", directory=tmp_path / str(count),
                 storages=[DirectoryStorage(bucket)])  # fmt: skip
        with monkeypatch.context() as patched:
            patched.setattr(duckdb, "connect", counted)
            db = statsdb("cards", bucket)
        assert db.sql("select count(*) from blobs").fetchone() == (count * 2,)
        db.close()
        statements.append(opened[-1].statements)
    assert statements[0] == statements[1]


def test_a_rollup_given_to_statsdb_sums_each_block_once_as_sealing_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(block, "SEAL_SECONDS", 0.0)
    matches = deals(pair(tmp_path), 4)
    for run, rollups in (("sealed", [CARDS]), ("later", [])):
        evaluate(experiment="cards", environment=DUEL, play=play_cards, matches=matches[:3],
                 run=run, directory=tmp_path / run,
                 storages=[DirectoryStorage(tmp_path / f"{run}-bucket")],
                 rollups=rollups)  # fmt: skip
    db = statsdb("cards", tmp_path / "sealed-bucket")
    sealed = db.sql("select * exclude (run) from card_deals order by all").fetchall()
    db.close()

    reading = ReadStorage(DirectoryStorage(tmp_path / "later-bucket"))
    db = statsdb("cards", reading, rollups=[CARDS])
    assert db.sql("select * exclude (run) from card_deals order by all").fetchall() == sealed
    # Each block's two tables are read once, for its rows and its partial totals alike.
    assert sum(key.endswith(".parquet") for key in reading.read) == 3 * 2
    assert not list((tmp_path / "later-bucket").rglob("card_deals*"))
    db.close()

    # Held from now on: reopening reads nothing, and a new block is summed from its tables.
    reading.read.clear()
    db = statsdb("cards", reading)
    assert reading.read == []
    db.close()
    evaluate(experiment="cards", environment=DUEL, play=play_cards, matches=matches,
             run="later", directory=tmp_path / "later",
             storages=[DirectoryStorage(tmp_path / "later-bucket")])  # fmt: skip
    db = statsdb("cards", reading)
    assert sum(key.endswith(".parquet") for key in reading.read) == 2
    assert card_rollup(db) == card_page(db)
    db.close()

    changed = CARDS.model_copy(update={"where": None})
    with pytest.raises(EtudeError) as refused:
        statsdb("cards", reading, rollups=[changed])
    assert refused.value.code == "rollup_conflict"
