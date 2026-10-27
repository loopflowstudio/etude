"""An experiment's kept database: scoped to the experiment, caught up by reading only what arrived."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pytest

from etude.eval import query
from etude.eval.match import Tables, environment, evaluate
from etude.eval.query import compare, statsdb
from etude.eval.records import Code, Match
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage
from tests.eval.games import play
from tests.run.storage import MeasuredStorage

DUEL = environment("duel")


def pair(tmp_path: Path) -> tuple[Code, Code]:
    script = tmp_path / "scripted.py"
    script.write_text("def act(state): return 0\n")
    return (
        Code(name="strong", source=[script], config={"strength": 2}),
        Code(name="weak", source=[script], config={"strength": 1}),
    )


def deals(players: tuple[Code, Code], *numbers: int) -> list[Match]:
    return [Match(players=players, conditions={"deal": deal}, games=2) for deal in numbers]


def held(experiment: str, bucket: DirectoryStorage) -> tuple[int, int, int]:
    """Runs, matches and plays rows in the experiment's database."""
    db = statsdb(experiment, bucket)
    try:
        found = db.sql(
            "select (select count(*) from runs), (select count(*) from matches),"
            " (select count(*) from plays)"
        ).fetchone()
        assert found is not None
        return found
    finally:
        db.close()


def test_an_experiment_holds_its_own_eval_runs_only(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    players = pair(tmp_path)
    for experiment, run, numbers in (
        ("cards", "a", (0, 1)),
        ("cards", "b", (2,)),
        ("other", "c", (3,)),
    ):
        evaluate(experiment=experiment, environment=DUEL, play=play, matches=deals(players, *numbers),
                 run=run, directory=tmp_path / run, storages=[bucket])  # fmt: skip
    # Every run of the experiment, joined; another experiment's run is not.
    assert held("cards", bucket) == (2, 3, 3 * 2 * 2)
    assert held("other", bucket) == (1, 1, 2 * 2)
    with pytest.raises(EtudeError) as refused:
        statsdb("../cards", bucket)
    assert refused.value.code == "unsafe_run_or_experiment_name"


def test_opening_again_reads_only_what_arrived(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    players = pair(tmp_path)
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(players, 0, 1, 2),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    first = MeasuredStorage(bucket)
    statsdb("cards", first).close()
    assert first.listings == 1 and first.requests > 0

    # Nothing new: one listing, and no file read again.
    unchanged = MeasuredStorage(bucket)
    db = statsdb("cards", unchanged)
    assert (unchanged.listings, unchanged.requests) == (1, 0)
    assert db.sql("select count(*) from matches").fetchone() == (3,)
    db.close()

    # One match more, in a run of its own: its declaration, environment, record and two tables.
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(players, 3),
             run="b", directory=tmp_path / "b", storages=[bucket])  # fmt: skip
    grown = MeasuredStorage(bucket)
    db = statsdb("cards", grown)
    assert (grown.listings, grown.requests) == (1, 1 + 1 + 1 + 2)
    assert db.sql("select count(*), count(distinct run) from matches").fetchone() == (4, 2)
    assert db.sql("select count(*) from plays").fetchone() == (4 * 2 * 2,)
    db.close()


def test_a_match_counts_once_from_the_latest_file_holding_it(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(pair(tmp_path), 0, 1),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    assert held("cards", bucket) == (1, 2, 2 * 2 * 2)
    # The same rows arrive again, in a later file and in an earlier one, as a merged
    # file and those it replaced may: the match's rows come from the latest.
    (merged,) = (key for key in bucket.keys("a/tables/") if key.startswith("a/tables/plays."))
    table, written, writer, suffix = merged.rsplit("/", 1)[-1].split(".")
    for at in (int(written) + 1, int(written) - 1):
        shutil.copy(
            bucket.root / merged, bucket.root / f"a/tables/{table}.{at:020d}.{writer}.{suffix}"
        )
        assert held("cards", bucket) == (1, 2, 2 * 2 * 2)


def test_a_failed_match_retried_counts_once_as_done(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    matches = deals(pair(tmp_path), 0)

    def broken(match: Match) -> Tables:
        raise RuntimeError("engine crashed")

    with pytest.raises(EtudeError):
        evaluate(experiment="cards", environment=DUEL, play=broken, matches=matches, run="a",
                 directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    db = statsdb("cards", bucket)
    assert db.sql("select state, error from matches").fetchall() == [
        ("failed", "RuntimeError: engine crashed")
    ]
    db.close()
    evaluate(experiment="cards", environment=DUEL, play=play, matches=matches, run="a",
             directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    db = statsdb("cards", bucket)
    assert db.sql("select state from matches").fetchall() == [("done",)]
    assert db.sql("select count(*) from seats").fetchone() == (2,)
    db.close()


def test_a_kept_database_is_rebuilt_on_request_or_for_another_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(pair(tmp_path), 0),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    statsdb("cards", bucket).close()
    for rebuild in (True, False):
        if not rebuild:
            monkeypatch.setattr(query, "SCHEMA", query.SCHEMA + 1)
        measured = MeasuredStorage(bucket)
        db = statsdb("cards", measured, rebuild=rebuild)
        # Everything is read again from the store, which stays the authority.
        assert measured.requests > 0
        assert db.sql("select count(*) from plays").fetchone() == (2 * 2,)
        db.close()


def _as_published(folder: Path) -> None:
    """Rewrite an eval run as published etude-sdk stored it: `agents`, and seats as `player`."""
    for file in folder.glob("matches/*.json"):
        record = json.loads(file.read_text())
        players = record["match"].pop("players")
        record["match"]["agents"] = [
            {"key": {"checkpoint": None, "code": each["name"], "recorded": None,
                     "config": each["config"]},
             "name": each["name"], "artifact": None, "version": each["version"]}
            for each in players
        ]  # fmt: skip
        file.write_text(json.dumps(record))
    for file in folder.glob("tables/*.parquet"):
        duckdb.sql(
            f"copy (select * rename (seat as player) from read_parquet('{file}'))"
            f" to '{file}' (format parquet)"
        )


def test_eval_runs_published_etude_sdk_stored_read_as_players_and_seats(tmp_path: Path) -> None:
    players = pair(tmp_path)
    for name in ("now", "mixed"):
        bucket = DirectoryStorage(tmp_path / name / "bucket")
        for run, numbers in (("a", (0, 1)), ("b", (2,))):
            evaluate(experiment="cards", environment=DUEL, play=play, run=run,
                     matches=deals(players, *numbers), directory=tmp_path / name / run,
                     storages=[bucket])  # fmt: skip
    _as_published(tmp_path / "mixed" / "bucket" / "a")
    stored = (tmp_path / "mixed" / "bucket" / "a" / "matches").glob("*.json")
    assert all('"agents"' in file.read_text() for file in stored)
    now = statsdb("cards", DirectoryStorage(tmp_path / "now" / "bucket"))
    mixed = statsdb("cards", DirectoryStorage(tmp_path / "mixed" / "bucket"))
    assert mixed.sql("select count(*) from plays where seat is not null").fetchone() == (3 * 2 * 2,)
    assert mixed.sql("select name, kind from players order by name").fetchall() == [
        ("strong", "code"),
        ("weak", "code"),
    ]
    assert compare(mixed, table="plays", value="won") == compare(now, table="plays", value="won")
