"""Eval runs end to end: play matches in workers, resume from storage, query with DuckDB."""

from __future__ import annotations

import random
import shutil
import subprocess
from collections.abc import Sequence
from hashlib import sha256
from itertools import permutations
from pathlib import Path
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws
from pydantic import BaseModel

from etude.eval.match import environment, evaluate
from etude.eval.query import compare, estimate, statsdb
from etude.eval.records import (
    CheckpointKey,
    CheckpointRef,
    Code,
    Config,
    Inference,
    Match,
    Model,
    Player,
)
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef
from etude.run.storage import DirectoryStorage, S3Storage
from tests.eval.games import play, play_teams, play_text_hits, play_unkeyed
from tests.run.storage import CountedClient, MeasuredStorage


def roster(tmp_path: Path) -> list[Player]:
    script = tmp_path / "scripted.py"
    script.write_text("def act(state): return 0\n")
    file = ArtifactRef(uri="s3://models/a.pt", sha256=sha256(b"a").hexdigest())
    model = CheckpointRef(key=CheckpointKey(address=file.uri), artifact=file)
    return [
        Model(
            name="deep",
            checkpoint=model,
            inference=Inference(config={"strength": 3, "search": {"simulations": 64}}),
        ),
        Model(
            name="shallow",
            checkpoint=model,
            inference=Inference(config={"strength": 2, "search": {"simulations": 8}}),
        ),
        Code(name="scripted", source=[script], config={"strength": 1}),
        Code(name="random", source=[script], config={"strength": 0}),
    ]


def seatings(players: Sequence[Player], *, conditions: Sequence[Config], games: int) -> list[Match]:
    """Every ordered seating of two distinct players, under each condition."""
    return [
        Match(players=seated, conditions=given, games=games)
        for given in conditions
        for seated in permutations(players, 2)
    ]


def test_matches_in_two_workers_resumes_from_storage_without_replaying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    players = roster(tmp_path)
    # One checkpoint at two search budgets is two models; one script at two strengths too.
    assert len({each.key for each in players}) == 4
    world = environment("duel", config={"world": "w6"})
    matches = seatings(players, conditions=[{"deal": 0}, {"deal": 1}], games=4)
    assert len(matches) == 24
    bucket = DirectoryStorage(tmp_path / "bucket")
    first = evaluate(
        experiment="league", environment=world, matches=matches, play=play, workers=2,
        run="league-1", directory=tmp_path / "local", storages=[bucket],
    )  # fmt: skip
    assert (first.played, first.skipped) == (24, 0)
    # One plain line per stored match, so a long eval run is never silent.
    progress = [line for line in capsys.readouterr().err.splitlines() if "matches played" in line]
    assert len(progress) == 24
    assert progress[-1].startswith("etude: league-1: 24/24 matches played, ")
    # Each table's per-match files were merged into one, and the merged file replaced them.
    tables = bucket.keys("league-1/tables/")
    assert sorted(
        key.split("/")[-1].split(".")[0] for key in tables if key.endswith(".parquet")
    ) == [
        "games",
        "plays",
    ]

    shutil.rmtree(tmp_path / "local")
    measured = MeasuredStorage(bucket)
    again = evaluate(
        experiment="league", environment=world, matches=matches, play=play, workers=2,
        run="league-1", directory=tmp_path / "local", storages=[measured],
    )  # fmt: skip
    assert (again.played, again.skipped) == (0, 24)
    # One listing of the delivered match records and one read of each.
    assert (measured.listings, measured.requests) == (1, 24)

    db = statsdb("league", bucket)
    assert db.sql("select count(*) from plays").fetchone() == (24 * 4 * 2,)
    assert db.sql("select count(*), count(distinct match) from games").fetchone() == (96, 24)
    assert db.sql("select distinct state from matches").fetchall() == [("done",)]


def test_sql_joins_results_to_players_by_config_key(tmp_path: Path) -> None:
    players = roster(tmp_path)
    evaluate(
        experiment="league", environment=environment("duel"), play=play,
        matches=seatings(players, conditions=[{"deal": 0}], games=6), directory=tmp_path,
    )  # fmt: skip
    db = statsdb("league", tmp_path)
    found = db.sql(
        'select a."config.search.simulations", avg(won::int) from plays '
        "join seats using (run, match, seat) join players a using (player) "
        'where a."config.search.simulations" is not null and not on_play '
        "group by all order by 1"
    ).fetchall()
    # Off the play, a player loses every third game; otherwise the stronger wins.
    # Shallow beats the two scripts and never the deep search.
    assert found == [(8, pytest.approx(4 / 9)), (64, pytest.approx(2 / 3))]
    names = dict(db.sql("select name, kind from players").fetchall())
    assert names == {
        "deep": "model",
        "shallow": "model",
        "scripted": "code",
        "random": "code",
    }


def test_compare_gives_a_rate_with_an_interval_that_resamples_matches(tmp_path: Path) -> None:
    players = roster(tmp_path)
    evaluate(
        experiment="league", environment=environment("duel"), play=play,
        matches=seatings(players[:2], conditions=[{"deal": d} for d in range(5)], games=6),
        directory=tmp_path,
    )  # fmt: skip
    db = statsdb("league", tmp_path)
    found = compare(db, table="plays", value="hits", per="attacks", by=["on_play"])
    deep, shallow = players[0].key, players[1].key
    cell = next(
        c
        for c in found
        if c.subject == (deep,) and c.opponent == (shallow,) and c.split == {"on_play": False}
    )
    # Off the play in 3 games a match, deep wins 2: 3 + 3 + 1 hits of 12 attacks.
    assert (cell.matches, cell.rows) == (10, 30)
    assert cell.value == pytest.approx(7 / 12)
    assert cell.interval == pytest.approx((7 / 12, 7 / 12))
    everyone = next(
        c
        for c in found
        if c.subject == (deep,) and c.opponent is None and c.split == {"on_play": False}
    )
    assert everyone.value == cell.value
    on_play = compare(db, table="plays", value="won", where="on_play")
    assert {(c.subject, c.opponent): c.value for c in on_play} == {
        ((deep,), (shallow,)): 1.0,
        ((deep,), None): 1.0,
        # Shallow wins only when on the play in every third game.
        ((shallow,), (deep,)): pytest.approx(1 / 3),
        ((shallow,), None): pytest.approx(1 / 3),
    }


def test_a_row_without_seat_or_group_is_refused_and_nothing_of_its_match_is_stored(
    tmp_path: Path,
) -> None:
    players = roster(tmp_path)
    with pytest.raises(EtudeError) as refused:
        evaluate(
            experiment="league", environment=environment("duel"), play=play_unkeyed,
            matches=seatings(players[:2], conditions=[{}], games=1), directory=tmp_path,
        )  # fmt: skip
    assert refused.value.code == "match_failed"
    cause = refused.value.__cause__
    assert isinstance(cause, EtudeError) and cause.code == "row_needs_seat_or_group"
    db = statsdb("league", tmp_path)
    assert db.sql("select state, error from matches").fetchall() == [
        ("failed", "EtudeError: row_needs_seat_or_group")
    ]
    assert "plays" not in {name for (name,) in db.sql("show tables").fetchall()}


def test_a_column_keeps_its_type_through_an_eval_run(tmp_path: Path) -> None:
    players = roster(tmp_path)

    matches = seatings(players[:2], conditions=[{}], games=1)
    evaluate(experiment="league", environment=environment("duel"), play=play, matches=matches[:1],
             run="typed", directory=tmp_path)  # fmt: skip
    with pytest.raises(EtudeError) as refused:
        evaluate(experiment="league", environment=environment("duel"), play=play_text_hits,
                 matches=matches[1:], run="typed", directory=tmp_path)  # fmt: skip
    cause = refused.value.__cause__
    assert isinstance(cause, EtudeError) and cause.code == "column_type_conflict"


def test_two_environments_are_never_pooled(tmp_path: Path) -> None:
    players = roster(tmp_path)[:2]
    matches = seatings(players, conditions=[{}], games=6)
    six, seven = (
        environment("duel", config={"world": "w6"}),
        environment("duel", config={"world": "w7"}),
    )
    for name, world in (("on-w6", six), ("on-w7", seven)):
        evaluate(experiment="league", environment=world, play=play, matches=matches, run=name,
                 directory=tmp_path)  # fmt: skip
    with pytest.raises(EtudeError) as conflict:
        evaluate(experiment="league", environment=seven, play=play, matches=matches, run="on-w6",
                 directory=tmp_path)  # fmt: skip
    assert conflict.value.code == "eval_run_environment_conflict"
    found = compare(statsdb("league", tmp_path), table="plays", value="won")
    pooled = [c for c in found if c.subject == (players[0].key,) and c.opponent is None]
    assert sorted(c.environment for c in pooled) == sorted([six.identity, seven.identity])
    assert all(c.matches == 2 for c in pooled)


def test_group_keyed_rows_compare_teams_in_a_two_on_two(tmp_path: Path) -> None:
    a, b, c, d = roster(tmp_path)
    match = Match(players=(a, b, c, d), groups={"north": (0, 2), "south": (1, 3)}, games=4)
    assert match.groups["all"] == (0, 1, 2, 3)
    evaluate(experiment="teams", environment=environment("doubles"), play=play_teams,
             matches=[match], directory=tmp_path)  # fmt: skip
    found = compare(statsdb("teams", tmp_path), table="teams", value="won")
    north, south = (
        tuple(sorted((a.key, c.key))),
        tuple(sorted((b.key, d.key))),
    )
    by_pair = {(x.subject, x.opponent): x.value for x in found}
    assert by_pair == {
        (north, south): 0.75,
        (north, None): 0.75,
        (south, north): 0.25,
        (south, None): 0.25,
    }


def test_an_eval_run_delivered_to_a_bucket_is_queried_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket="runs")
        bucket = S3Storage("runs", "evals", client=client)
        evaluate(
            experiment="league", environment=environment("duel"), play=play,
            matches=seatings(roster(tmp_path), conditions=[{}], games=4),
            directory=tmp_path / "local", storages=[bucket],
        )  # fmt: skip
        db = statsdb("league", bucket)
        assert db.sql("select count(*) from plays").fetchone() == (12 * 4 * 2,)
        split = compare(db, table="plays", value="won", by=["on_play"])
        names = dict(db.sql("select player, name from players").fetchall())
    assert {c.split["on_play"] for c in split} == {True, False}
    assert "deep" in {names[c.subject[0]] for c in split}


def test_a_player_may_play_itself(tmp_path: Path) -> None:
    deep = roster(tmp_path)[0]
    evaluate(experiment="mirror", environment=environment("duel"), play=play,
             matches=[Match(players=(deep, deep), games=6)], directory=tmp_path)  # fmt: skip
    found = compare(statsdb("mirror", tmp_path), table="plays", value="won")
    # Equal strength: seat 1 wins the ties, and each seat its every-third games on the play.
    assert {(c.subject, c.opponent): (c.value, c.rows) for c in found} == {
        ((deep.key,), (deep.key,)): (0.5, 12),
        ((deep.key,), None): (0.5, 12),
    }


class Direct(BaseModel):
    search: int = 0


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_models_inference_code_is_its_version_not_its_key(tmp_path: Path) -> None:
    file = ArtifactRef(uri="s3://models/updates-100.pt", sha256=sha256(b"a").hexdigest())
    checkpoint = CheckpointRef(key=CheckpointKey(run="ppo-1", name="updates-100.pt"), artifact=file)
    inference = tmp_path / "inference.py"
    inference.write_text("def act(state): return 0\n")
    before = Inference(source=[inference], config=Direct()).model(checkpoint)
    assert before.key == 'checkpoint:ppo-1/checkpoints/updates-100.pt {"search":0}'
    assert before.name == "updates-100.pt"
    inference.write_text("def act(state): return 1\n")
    changed = Inference(source=[inference], config=Direct()).model(checkpoint)
    assert changed.key == before.key
    assert changed.version.source != before.version.source
    # The same bytes under another key are another model.
    copied = CheckpointRef(key=CheckpointKey(run="ppo-2", name="updates-100.pt"), artifact=file)
    assert Inference(config=Direct()).model(copied).key != before.key


def test_where_a_checkpoint_stood_is_carried_by_its_seat_and_is_not_its_identity() -> None:
    file = ArtifactRef(uri="hf://lab/u600.pt", sha256=sha256(b"a").hexdigest())
    key = CheckpointKey(address=file.uri)
    seated = Inference().model(CheckpointRef(key=key, artifact=file, at={"updates": 600}))
    corrected = Inference().model(
        CheckpointRef(key=key, artifact=file, at={"updates": 612}, line="seed-2")
    )
    rival = Code(name="rival", source=[__file__])
    assert (seated.checkpoint.at, corrected.checkpoint.at) == ({"updates": 600}, {"updates": 612})
    assert (seated.checkpoint.line, corrected.checkpoint.line) == (None, "seed-2")
    # Correcting a position or a line neither forks the model nor replays its match.
    assert seated.key == corrected.key
    assert Match(players=(seated, rival)).key == Match(players=(corrected, rival)).key


def test_a_code_players_version_is_its_commit_and_tracked_files_in_a_checkout_or_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "players").mkdir(parents=True)
    (repo / "players" / "greedy.py").write_text("STRENGTH = 4\n")
    (repo / "game.py").write_text("RULES = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")
    # As the launcher extracts a commit: its files without `.git`, somewhere else.
    source = tmp_path / "session" / "src"
    shutil.copytree(repo, source, ignore=shutil.ignore_patterns(".git"))
    # Caches and scratch beside the checked-out code are untracked.
    (repo / "players" / "__pycache__").mkdir()
    (repo / "players" / "__pycache__" / "greedy.pyc").write_bytes(b"\0")
    (repo / "players" / "notes.txt").write_text("scratch\n")
    calls = 0
    run = subprocess.run

    def counted(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counted)
    checked_out = Code(name="greedy", source=[repo / "players", repo / "game.py"])
    # One `git ls-files` for the checkout, whatever number of paths are in it, and its commit.
    assert calls == 2
    assert checked_out.key == "code:greedy"
    assert checked_out.version.commit is not None
    monkeypatch.chdir(source)
    monkeypatch.setenv("ETUDE_SOURCE_DIR", str(source))
    monkeypatch.setenv("ETUDE_COMMIT", checked_out.version.commit)
    launched = Code(name="greedy", source=["players", "game.py"])
    assert (launched.key, launched.version) == (checked_out.key, checked_out.version)
    with pytest.raises(EtudeError) as refused:
        Code(name="cache", source=[repo / "players" / "__pycache__"])
    assert refused.value.code == "identity_path_without_files"
    # An uncommitted edit is the same player, at another version of the same commit.
    (repo / "players" / "greedy.py").write_text("STRENGTH = 5\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ETUDE_SOURCE_DIR")
    edited = Code(name="greedy", source=[repo / "players", repo / "game.py"])
    assert edited.key == checked_out.key
    assert edited.version.commit == checked_out.version.commit
    assert edited.version.source != checked_out.version.source


def test_an_interval_does_not_depend_on_the_order_matches_are_read_in() -> None:
    hits = (0, 1, 1, 2, 3, 3, 4, 4, 2, 0, 1, 3)
    # Matches of unequal length, so the order of resampled blocks would show.
    blocks = [(float(each), float(4 + each % 3)) for each in hits]
    shuffled = blocks[:]
    random.Random(7).shuffle(shuffled)
    assert shuffled != blocks
    assert estimate(shuffled) == estimate(blocks)


def test_queries_reports_and_resumes_read_each_bucket_file_once_per_computer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="runs")
        matches = seatings(roster(tmp_path), conditions=[{}], games=4)
        evaluate(
            experiment="league", environment=environment("duel"), play=play, matches=matches,
            run="league-1", directory=tmp_path / "local", storages=[S3Storage("runs", client=client)],
        )  # fmt: skip
        written = client.stored("runs")
        # Each use opens the bucket afresh, as separate commands on one computer do.
        db = statsdb("league", S3Storage("runs", client=client))
        assert db.sql("select count(*) from plays").fetchone() == (12 * 4 * 2,)
        first = client.bytes
        assert 0 < first <= written
        for _ in range(3):
            # A watched report opens the database again every few seconds.
            statsdb("league", S3Storage("runs", client=client)).close()
        shutil.rmtree(tmp_path / "local")
        again = evaluate(
            experiment="league", environment=environment("duel"), play=play, matches=matches,
            run="league-1", directory=tmp_path / "local", storages=[S3Storage("runs", client=client)],
        )  # fmt: skip
        assert again.skipped == 12
        # Later queries and the resume ask only by ETag, and storage sends no body.
        assert client.bytes == first
