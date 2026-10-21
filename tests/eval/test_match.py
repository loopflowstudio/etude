"""Eval runs end to end: play matches in workers, resume from storage, query with DuckDB."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from hashlib import sha256
from itertools import permutations
from pathlib import Path

import boto3  # type: ignore[import-untyped]
import pytest
from moto import mock_aws

from etude.eval.match import agent, environment, evaluate
from etude.eval.query import compare, connect
from etude.eval.records import Agent, Config, Match
from etude.eval.report import render
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef
from etude.run.storage import DirectoryStorage, S3Storage
from tests.eval.games import play, play_teams, play_text_hits, play_unkeyed
from tests.run.storage import MeasuredStorage


def roster(tmp_path: Path) -> list[Agent]:
    script = tmp_path / "scripted.py"
    script.write_text("def act(state): return 0\n")
    model = ArtifactRef(uri="s3://models/a.pt", sha256=sha256(b"a").hexdigest())
    return [
        agent("deep", checkpoint=model, config={"strength": 3, "search": {"simulations": 64}}),
        agent("shallow", checkpoint=model, config={"strength": 2, "search": {"simulations": 8}}),
        agent("scripted", code=[script], config={"strength": 1}),
        agent("random", code=[script], config={"strength": 0}),
    ]


def seatings(agents: Sequence[Agent], *, conditions: Sequence[Config], games: int) -> list[Match]:
    """Every ordered seating of two distinct agents, under each condition."""
    return [
        Match(agents=seated, conditions=given, games=games)
        for given in conditions
        for seated in permutations(agents, 2)
    ]


def test_matches_in_two_workers_resumes_from_storage_without_replaying(
    tmp_path: Path,
) -> None:
    agents = roster(tmp_path)
    # One checkpoint at two search budgets is two agents; one script at two strengths too.
    assert len({each.identity for each in agents}) == 4
    world = environment("duel", config={"world": "w6"})
    matches = seatings(agents, conditions=[{"deal": 0}, {"deal": 1}], games=4)
    assert len(matches) == 24
    bucket = DirectoryStorage(tmp_path / "bucket")
    first = evaluate(
        experiment="league", environment=world, matches=matches, play=play, workers=2,
        run="league-1", directory=tmp_path / "local", storages=[bucket],
    )  # fmt: skip
    assert (first.played, first.skipped) == (24, 0)
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

    db = connect(bucket)
    assert db.sql("select count(*) from plays").fetchone() == (24 * 4 * 2,)
    assert db.sql("select count(*), count(distinct match) from games").fetchone() == (96, 24)
    assert db.sql("select distinct state from matches").fetchall() == [("done",)]


def test_sql_joins_results_to_agents_by_config_key(tmp_path: Path) -> None:
    agents = roster(tmp_path)
    evaluate(
        experiment="league", environment=environment("duel"), play=play,
        matches=seatings(agents, conditions=[{"deal": 0}], games=6), directory=tmp_path,
    )  # fmt: skip
    db = connect(tmp_path)
    found = db.sql(
        'select a."config.search.simulations", avg(won::int) from plays '
        "join seats using (run, match, player) join agents a using (agent) "
        'where a."config.search.simulations" is not null and not on_play '
        "group by all order by 1"
    ).fetchall()
    # Off the play, an agent loses every third game; otherwise the stronger wins.
    # Shallow beats the two scripts and never the deep search.
    assert found == [(8, pytest.approx(4 / 9)), (64, pytest.approx(2 / 3))]
    names = dict(db.sql("select name, kind from agents").fetchall())
    assert names == {
        "deep": "checkpoint",
        "shallow": "checkpoint",
        "scripted": "code",
        "random": "code",
    }


def test_compare_gives_a_rate_with_an_interval_that_resamples_matches(tmp_path: Path) -> None:
    agents = roster(tmp_path)
    evaluate(
        experiment="league", environment=environment("duel"), play=play,
        matches=seatings(agents[:2], conditions=[{"deal": d} for d in range(5)], games=6),
        directory=tmp_path,
    )  # fmt: skip
    db = connect(tmp_path)
    found = compare(db, table="plays", value="hits", per="attacks", by=["on_play"])
    deep, shallow = agents[0].identity, agents[1].identity
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


def test_a_row_without_player_or_group_is_refused_and_nothing_of_its_match_is_stored(
    tmp_path: Path,
) -> None:
    agents = roster(tmp_path)
    with pytest.raises(EtudeError) as refused:
        evaluate(
            experiment="league", environment=environment("duel"), play=play_unkeyed,
            matches=seatings(agents[:2], conditions=[{}], games=1), directory=tmp_path,
        )  # fmt: skip
    assert refused.value.code == "match_failed"
    cause = refused.value.__cause__
    assert isinstance(cause, EtudeError) and cause.code == "row_needs_player_or_group"
    db = connect(tmp_path)
    assert db.sql("select state, error from matches").fetchall() == [
        ("failed", "EtudeError: row_needs_player_or_group")
    ]
    assert "plays" not in {name for (name,) in db.sql("show tables").fetchall()}


def test_a_column_keeps_its_type_through_an_eval_run(tmp_path: Path) -> None:
    agents = roster(tmp_path)

    matches = seatings(agents[:2], conditions=[{}], games=1)
    evaluate(experiment="league", environment=environment("duel"), play=play, matches=matches[:1],
             run="typed", directory=tmp_path)  # fmt: skip
    with pytest.raises(EtudeError) as refused:
        evaluate(experiment="league", environment=environment("duel"), play=play_text_hits,
                 matches=matches[1:], run="typed", directory=tmp_path)  # fmt: skip
    cause = refused.value.__cause__
    assert isinstance(cause, EtudeError) and cause.code == "column_type_conflict"


def test_two_environments_are_never_pooled(tmp_path: Path) -> None:
    agents = roster(tmp_path)[:2]
    matches = seatings(agents, conditions=[{}], games=6)
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
    found = compare(connect(tmp_path), table="plays", value="won")
    pooled = [c for c in found if c.subject == (agents[0].identity,) and c.opponent is None]
    assert sorted(c.environment for c in pooled) == sorted([six.identity, seven.identity])
    assert all(c.matches == 2 for c in pooled)


def test_group_keyed_rows_compare_teams_in_a_two_on_two(tmp_path: Path) -> None:
    a, b, c, d = roster(tmp_path)
    match = Match(agents=(a, b, c, d), groups={"north": (0, 2), "south": (1, 3)}, games=4)
    assert match.groups["all"] == (0, 1, 2, 3)
    evaluate(experiment="teams", environment=environment("doubles"), play=play_teams,
             matches=[match], directory=tmp_path)  # fmt: skip
    found = compare(connect(tmp_path), table="teams", value="won")
    north, south = tuple(sorted((a.identity, c.identity))), tuple(sorted((b.identity, d.identity)))
    by_pair = {(x.subject, x.opponent): x.value for x in found}
    assert by_pair == {
        (north, south): 0.75,
        (north, None): 0.75,
        (south, north): 0.25,
        (south, None): 0.25,
    }


def test_an_eval_run_delivered_to_a_bucket_is_queried_and_reported_from_it(
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
        db = connect(bucket)
        assert db.sql("select count(*) from plays").fetchone() == (12 * 4 * 2,)
        page = render(db)
    assert "deep" in page and "on_play" in page and "won" in page


def test_an_agent_may_play_itself(tmp_path: Path) -> None:
    deep = roster(tmp_path)[0]
    evaluate(experiment="mirror", environment=environment("duel"), play=play,
             matches=[Match(agents=(deep, deep), games=6)], directory=tmp_path)  # fmt: skip
    found = compare(connect(tmp_path), table="plays", value="won")
    # Equal strength: seat 1 wins the ties, and each seat its every-third games on the play.
    assert {(c.subject, c.opponent): (c.value, c.rows) for c in found} == {
        ((deep.identity,), (deep.identity,)): (0.5, 12),
        ((deep.identity,), None): (0.5, 12),
    }
