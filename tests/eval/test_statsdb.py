"""An experiment's kept database: scoped to the experiment, caught up by reading only what arrived."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from etude.checkpoints import CheckpointRef, checkpoint_id
from etude.errors import EtudeError
from etude.eval import query
from etude.eval.convert import convert
from etude.eval.match import Tables, environment, evaluate
from etude.eval.query import compare, statsdb
from etude.eval.records import CheckpointPlayer, Code, Inference, Match
from etude.records import ArtifactRef
from etude.storage import DirectoryStorage
from tests.eval.games import play, play_game_seats
from tests.eval.per_file import write_per_file
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

    # One match more, in a run of its own: its declaration, environment, record, two
    # tables and how it ended.
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(players, 3),
             run="b", directory=tmp_path / "b", storages=[bucket])  # fmt: skip
    grown = MeasuredStorage(bucket)
    db = statsdb("cards", grown)
    assert (grown.listings, grown.requests) == (1, 1 + 1 + 1 + 2 + 1)
    assert db.sql("select count(*), count(distinct run) from matches").fetchone() == (4, 2)
    assert db.sql("select count(*) from plays").fetchone() == (4 * 2 * 2,)
    db.close()


class Asked(MeasuredStorage):
    """Storage that also counts reads asked for, a missing object's included."""

    def __init__(self, storage: DirectoryStorage) -> None:
        super().__init__(storage)
        self.asked: list[str] = []

    def get(self, key: str) -> bytes:
        self.asked.append(key)
        return super().get(key)


def test_a_folder_listed_without_a_declaration_is_not_asked_for_one(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(pair(tmp_path), 0),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    # A launcher's journal beside the run can hold folders shaped like a run's.
    bucket.put("a/attempts/a1/share/blocks/block.json", b"{}")
    statsdb("cards", bucket).close()
    again = Asked(bucket)
    statsdb("cards", again).close()
    assert (again.listings, again.asked) == (1, [])


def test_a_run_that_has_not_ended_is_partial(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    players = pair(tmp_path)
    for run, numbers in (("a", (0,)), ("b", (1, 2))):
        evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(players, *numbers),
                 run=run, directory=tmp_path / run, storages=[bucket])  # fmt: skip
    # Killed mid-pass, `b` delivered its blocks but never wrote how it ended.
    (bucket.root / "b" / "outcome.json").unlink()
    db = statsdb("cards", bucket)
    assert db.sql("select run, state from runs order by run").fetchall() == [
        ("a", "finished"),
        ("b", None),
    ]
    assert db.sql("select count(*) from matches where run = 'b'").fetchone() == (2,)
    db.close()


def test_a_match_counts_once_from_the_latest_block_holding_it(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    evaluate(experiment="cards", environment=DUEL, play=play, matches=deals(pair(tmp_path), 0, 1),
             run="a", directory=tmp_path / "a", storages=[bucket])  # fmt: skip
    assert held("cards", bucket) == (1, 2, 2 * 2 * 2)
    # The same matches arrive again, in a later block and in an earlier one, as a
    # replayed run's may: each match's rows come from one block.
    (index,) = (key for key in bucket.keys("a/blocks/") if key.endswith("/block.json"))
    folder = bucket.root / index.removesuffix("/block.json")
    written, writer = folder.name.split(".")
    for at in (int(written) + 1, int(written) - 1):
        shutil.copytree(folder, folder.with_name(f"{at:020d}.{writer}"))
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


def test_a_match_two_runs_played_counts_from_the_later_under_its_latest_name(
    tmp_path: Path,
) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    ref = CheckpointRef(
        key=checkpoint_id(address="hf://lab/a.pt"),
        artifact=ArtifactRef(uri="hf://lab/a.pt", sha256="ab" * 32),
    )
    _, rival = pair(tmp_path)
    # One model, renamed between runs: a name is part of no key.
    for run, name in (("first", "early"), ("second", "late")):
        model = CheckpointPlayer(
            name=name, checkpoint=ref, inference=Inference(config={"strength": 3})
        )
        evaluate(experiment="cards", environment=DUEL, play=play, run=run,
                 matches=[Match(players=(model, rival), conditions={"deal": 0}, games=2)],
                 directory=tmp_path / run, storages=[bucket])  # fmt: skip
    db = statsdb("cards", bucket)
    try:
        assert db.sql("select run, newest from matches order by run").fetchall() == [
            ("first", 2),
            ("second", 1),
        ]
        found = db.sql("select name, names from players where kind = 'model'").fetchone()
        assert found is not None
        assert (found[0], json.loads(found[1])) == ("late", ["late", "early"])
    finally:
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
        # Its rows named the game's own seat, if any, `seat`.
        columns = [name for name, *_ in duckdb.sql(f"describe from '{file}'").fetchall()]
        renamed = "seat as player" + (", game_seat as seat" if "game_seat" in columns else "")
        duckdb.sql(
            f"copy (select * rename ({renamed}) from read_parquet('{file}'))"
            f" to '{file}' (format parquet)"
        )


def _published(tmp_path: Path, play: Callable[[Match], Tables]) -> tuple[Path, Path]:
    """Run `a` played as blocks (`now`), and the same run as published etude-sdk stored it, converted."""
    players = pair(tmp_path)
    now = tmp_path / "now"
    evaluate(experiment="cards", environment=DUEL, play=play, run="a",
             matches=deals(players, 0, 1, 2), directory=now)  # fmt: skip
    published = tmp_path / "published"
    write_per_file(now / "a", published / "a")
    _as_published(published / "a")
    assert all(
        '"agents"' in file.read_text() for file in (published / "a" / "matches").glob("*.json")
    )
    convert("a", published)
    return now, published


def test_eval_runs_published_etude_sdk_stored_read_as_players_and_seats(tmp_path: Path) -> None:
    now, published = (statsdb("cards", folder) for folder in _published(tmp_path, play))
    assert published.sql("select count(*) from plays where seat is not null").fetchone() == (
        3 * 2 * 2,
    )
    assert published.sql("select name, kind from players order by name").fetchall() == [
        ("strong", "code"),
        ("weak", "code"),
    ]
    assert compare(published, table="plays", value="won") == compare(
        now, table="plays", value="won"
    )


def test_a_published_table_with_the_games_own_seat_keeps_each_players_rows(tmp_path: Path) -> None:
    now, published = (statsdb("cards", folder) for folder in _published(tmp_path, play_game_seats))
    rows = (
        "select p.name, x.game, x.game_seat, x.won from plays x join seats s"
        " using (run, match, seat) join players p using (player) order by all"
    )
    assert published.sql(rows).fetchall() == now.sql(rows).fetchall()
    assert {seat for (seat,) in now.sql("select distinct game_seat from plays").fetchall()} == {
        0,
        1,
    }


class _PandasLookups:
    """Counts every search of the import path for pandas (it is not installed here)."""

    def __init__(self) -> None:
        self.count = 0

    def find_spec(self, name: str, path: object, target: object = None) -> None:
        if name == "pandas":
            self.count += 1


def test_pandas_lookups_do_not_grow_with_the_matches_played_and_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # DuckDB looks for pandas for each Python value a statement takes as a parameter;
    # without pandas installed, each look searches the import path. Rows reach DuckDB
    # as files instead, so playing looks never and holding a fixed few times.
    lookups = _PandasLookups()
    monkeypatch.setattr(sys, "meta_path", [lookups, *sys.meta_path])

    def counted(matches: int) -> tuple[int, int]:
        bucket = DirectoryStorage(tmp_path / f"bucket-{matches}")
        players = pair(tmp_path)
        lookups.count = 0
        evaluate(experiment="cards", environment=DUEL, play=play,
                 matches=deals(players, *range(matches)), run="a",
                 directory=tmp_path / f"run-{matches}", storages=[bucket])  # fmt: skip
        played = lookups.count
        assert held("cards", bucket) == (1, matches, matches * 2 * 2)
        return played, lookups.count - played

    few, many = counted(5), counted(50)
    assert few[0] == many[0] == 0
    assert few[1] == many[1]


def test_same_named_eval_runs_keep_each_experiments_results(tmp_path: Path) -> None:
    source = DirectoryStorage(tmp_path / "runs")
    players = pair(tmp_path)
    for experiment, numbers in (("one", (0,)), ("two", (1, 2))):
        evaluate(
            experiment=experiment,
            environment=DUEL,
            play=play,
            matches=deals(players, *numbers),
            run="a",
            directory=tmp_path / "runs" / experiment,
        )
    assert held("one", source) == (1, 1, 4)
    assert held("two", source) == (1, 2, 8)
    # Each database remembers folder declarations independently. Reopening
    # either experiment reads no source objects, even with colliding names.
    for experiment in ("one", "two"):
        measured = MeasuredStorage(source)
        statsdb(experiment, measured).close()
        assert (measured.listings, measured.requests) == (1, 0)
