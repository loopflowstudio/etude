"""Eval runs end to end: record episodes, read them back joined by identity, compare."""

from __future__ import annotations

import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from etude.eval.read import agent_names, compare, conflicts, episodes
from etude.eval.records import Count, Episode, Group, Measure, Player
from etude.eval.report import render
from etude.eval.run import Evaluation, checkpoint_agent, code_agent, record
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef
from etude.run.storage import DirectoryStorage
from etude.train.records import Step
from etude.train.run import track

WIN = Measure(name="win", meaning="won the game", kind="rate", of="player")
MARGIN = Measure(name="margin", meaning="life lead at the end", kind="mean", of="player")
TURNS = Measure(name="turns", meaning="turns until the game ended", kind="mean", of="episode")


def checkpoint(name: str) -> ArtifactRef:
    return ArtifactRef(uri=f"s3://models/{name}.pt", sha256=sha256(name.encode()).hexdigest())


def duel(key: str, block: str, first: str, second: str, first_wins: bool, margin: float) -> Episode:
    """A two-player game; `margin` is the first player's."""
    return Episode(
        key=key,
        block=block,
        labels={"cell": "mirror" if block.endswith("m") else "cross"},
        players=(
            Player(agent=first, measures={"win": Count(hits=first_wins, opportunities=1), "margin": margin}),
            Player(agent=second, measures={"win": Count(hits=not first_wins, opportunities=1), "margin": -margin}),
        ),
        measures={"turns": 10.0},
    )  # fmt: skip


def test_two_eval_runs_join_by_identity_and_compare_with_block_counts(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    script = tmp_path / "heuristic.py"
    script.write_text("def act(state): return 0\n")
    # The same scripted agent under a different name in each run.
    first = record(
        experiment="arena-a",
        run="eval-a",
        agents=[code_agent("heuristic", [script]), checkpoint_agent("ckpt", checkpoint("c1"))],
        measures=[WIN, MARGIN, TURNS],
        directory=source,
    )
    # Two 8-episode deals; the heuristic wins 1 of the first and 3 of the second.
    first.add(
        duel(f"d{deal}-{game}", f"d{deal}m", "heuristic", "ckpt", game < (1, 3)[deal], 1.0)
        for deal in range(2)
        for game in range(8)
    )
    first.finish()
    second = record(
        experiment="arena-b",
        run="eval-b",
        agents=[code_agent("baseline", [script]), checkpoint_agent("late", checkpoint("c2"))],
        measures=[WIN, MARGIN, TURNS],
        directory=source,
    )
    second.add(duel(f"g{game}", "x0", "late", "baseline", game >= 6, -4.0) for game in range(8))
    second.finish()

    views = episodes(source)
    assert len(views) == 24
    names = agent_names(views)
    heuristic = code_agent("any", [script]).identity
    assert names[heuristic] == "heuristic/baseline"
    c1, c2 = (checkpoint(name).sha256 for name in ("c1", "c2"))
    assert c1 is not None and c2 is not None

    wins = {(c.subject, c.opponent): c for c in compare(views, "win")}
    overall = wins[(heuristic,), None]
    assert overall.value == pytest.approx(10 / 24)  # rates divide totals
    assert (overall.episodes, overall.blocks) == (24, 3)
    assert overall.interval is not None
    assert overall.interval[0] <= overall.value <= overall.interval[1]
    against_c1 = wins[(heuristic,), (c1,)]
    assert (against_c1.value, against_c1.blocks) == (0.25, 2)
    against_c2 = wins[(heuristic,), (c2,)]
    assert (against_c2.value, against_c2.blocks, against_c2.interval) == (0.75, 1, None)
    assert wins[(c2,), (heuristic,)].value == 0.25

    margins = {(c.subject, c.opponent): c.value for c in compare(views, "margin")}
    assert margins[(heuristic,), None] == pytest.approx((16 * 1.0 + 8 * 4.0) / 24)

    by_cell = {
        (c.subject, c.opponent, c.labels["cell"]): c.value
        for c in compare(views, "win", by=["cell"])
    }
    assert by_cell[(heuristic,), None, "mirror"] == pytest.approx(4 / 16)
    assert by_cell[(heuristic,), None, "cross"] == 0.75
    (turns,) = compare(views, "turns")
    assert (turns.subject, turns.opponent, turns.value, turns.episodes) == ((), None, 10.0, 24)
    # The bootstrap has a fixed seed: the same files give the same page.
    assert render(views) == render(episodes(source))


def test_groups_of_players_compare_on_a_group_measure(tmp_path: Path) -> None:
    team_win = Measure(name="team_win", meaning="the team won", kind="rate", of="group")
    agents = [checkpoint_agent(name, checkpoint(name)) for name in "abcd"]
    run = record(experiment="teams", agents=agents, measures=[team_win, WIN], directory=tmp_path)

    def game(key: str, seats: str, first_team_wins: bool) -> Episode:
        return Episode(
            key=key,
            players=tuple(Player(agent=seat) for seat in seats),
            groups=(
                Group(name="north", players=(0, 1), measures={"team_win": Count(hits=first_team_wins, opportunities=1)}),
                Group(name="south", players=(2, 3), measures={"team_win": Count(hits=not first_team_wins, opportunities=1)}),
            ),
        )  # fmt: skip

    run.add([game(f"ab-{i}", "abcd", i < 3) for i in range(4)])
    run.add([game(f"ac-{i}", "acbd", True) for i in range(2)])
    run.finish()

    found = {(c.subject, c.opponent): c for c in compare(episodes(tmp_path), "team_win")}
    a, b, c, d = (agent.identity for agent in agents)
    ab, cd, ac, bd = (tuple(sorted(pair)) for pair in ((a, b), (c, d), (a, c), (b, d)))
    assert (found[ab, cd].value, found[ab, cd].episodes) == (0.75, 4)
    assert found[cd, ab].value == 0.25
    assert found[ac, bd].value == 1.0
    assert (ab, ac) not in found  # these teams never met


def test_a_measure_declared_differently_is_a_conflict_not_combined(tmp_path: Path) -> None:
    agents = [checkpoint_agent("a", checkpoint("a")), checkpoint_agent("b", checkpoint("b"))]
    win_as_mean = Measure(name="win", meaning="won the game", kind="mean", of="player")
    for name, measure, value in (
        ("rated", WIN, Count(hits=1, opportunities=1)),
        ("averaged", win_as_mean, 1.0),
    ):
        with record(
            experiment="mixed", run=name, agents=agents, measures=[measure], directory=tmp_path
        ) as run:
            run.add([Episode(key="g", players=(Player(agent="a", measures={"win": value}), Player(agent="b")))])  # fmt: skip

    views = episodes(tmp_path)
    (conflict,) = conflicts(views)
    assert (conflict.measure, conflict.runs) == ("win", ("averaged", "rated"))
    with pytest.raises(EtudeError, match="measure_declaration_conflict"):
        compare(views, "win")
    page = render(views)
    assert "Conflicting declarations" in page and "<h2>win</h2>" not in page


def test_recording_is_strict_and_stores_nothing_from_a_refused_call(tmp_path: Path) -> None:
    agents = [checkpoint_agent("a", checkpoint("a")), checkpoint_agent("b", checkpoint("b"))]
    run = record(
        experiment="strict", run="s", agents=agents, measures=[WIN, TURNS], directory=tmp_path
    )
    good = Episode(key="ok", players=(Player(agent="a"), Player(agent="b")))
    refused = {
        "undeclared_agent": Player(agent="z"),
        "undeclared_measure": Player(agent="a", measures={"elo": 1.0}),
        "measure_at_wrong_level": Player(agent="a", measures={"turns": 3.0}),
        "measure_kind_mismatch": Player(agent="a", measures={"win": 1.0}),
    }  # fmt: skip
    for code, player in refused.items():
        with pytest.raises(EtudeError, match=code):
            run.add([good, Episode(key="bad", players=(player, Player(agent="b")))])
    with pytest.raises(EtudeError, match="duplicate_episode_key"):
        run.add([good, good])
    assert run.keys() == frozenset()
    assert episodes(tmp_path) == []


def test_resume_keeps_keys_and_a_later_write_replaces_an_episode(tmp_path: Path) -> None:
    bucket = DirectoryStorage(tmp_path / "bucket")
    agents = [checkpoint_agent("a", checkpoint("a")), checkpoint_agent("b", checkpoint("b"))]

    def resume(measures: list[Measure]) -> Evaluation:
        return record(
            experiment="resume",
            run="r",
            agents=agents,
            measures=measures,
            directory=tmp_path / "local",
            storages=[bucket],
        )

    def game(key: str, a_wins: bool) -> Episode:
        return Episode(
            key=key,
            players=(
                Player(agent="a", measures={"win": Count(hits=a_wins, opportunities=1)}),
                Player(agent="b", measures={"win": Count(hits=not a_wins, opportunities=1)}),
            ),
        )

    first = resume([WIN])
    first.add([game("g0", False), game("g1", False)])
    # The process dies without finishing; a relaunch resumes the same run.
    again = resume([WIN])
    assert again.keys() == {"g0", "g1"}
    again.add([game("g1", True), game("g2", True)])
    again.finish()

    # Delivered to the bucket, which reads back on its own.
    found = {(c.subject, c.opponent): c for c in compare(episodes(bucket), "win")}
    a = agents[0].identity
    assert found[(a,), None].value == pytest.approx(2 / 3)
    assert found[(a,), None].episodes == 3
    with pytest.raises(EtudeError, match="evaluation_declaration_conflict"):
        resume([WIN, TURNS])


def test_a_training_run_is_not_resumed_as_an_eval_run(tmp_path: Path) -> None:
    track(experiment="train", run="t", clocks=[Step("updates", "optimizer updates")], directory=tmp_path).finish()  # fmt: skip
    with pytest.raises(EtudeError):
        record(experiment="train", run="t", agents=[checkpoint_agent("a", checkpoint("a"))], measures=[WIN], directory=tmp_path)  # fmt: skip


def test_report_command_writes_the_eval_page(tmp_path: Path) -> None:
    agents = [checkpoint_agent("alpha", checkpoint("a")), checkpoint_agent("beta", checkpoint("b"))]
    with record(
        experiment="cli", agents=agents, measures=[WIN, MARGIN, TURNS], directory=tmp_path
    ) as run:
        run.add(duel(f"g{i}", f"d{i // 2}", "alpha", "beta", i % 2 == 0, 0.0) for i in range(4))
    done = subprocess.run(
        [sys.executable, "-m", "etude", "reports", "eval", "--source", str(tmp_path)],
        env={**os.environ, "ETUDE_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    page = Path(done.stdout.strip()).read_text()
    assert page == (tmp_path / "reports" / "eval" / "index.html").read_text()
    assert "<h2>win</h2>" in page and "alpha" in page and "beta" in page
