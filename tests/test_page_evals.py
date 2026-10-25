"""An experiment's evals on its page: checkpoints bound by key, anchors drawn once, gaps kept."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from moto import mock_aws

from etude.experiment import advance, read_experiment
from etude.page import Page, build
from etude.run.errors import EtudeError
from etude.run.records import Clock
from etude.run.storage import DirectoryStorage, S3Storage
from etude.train.run import Run, track
from tests.experiments import experiment_file
from tests.run.storage import CountedClient

LAB = """
from etude.eval.match import agent, environment
from etude.eval.records import ArtifactRef, Benchmark, CheckpointKey, CheckpointRef, Matchup, Model
from etude.jobs import evaluation
from etude.report import Anchors, Curve, Matches, Report, Score
from tests.eval.games import play, play_failing_deal_one

DUEL = environment("duel")
DIRECT = Model(config={"search": 0})
DEALS = ({"deal": 0}, {"deal": 1})
WEAK = agent("weak", code=[__file__], config={"strength": 0})
STRONG = agent("strong", code=[__file__], config={"strength": 9})
OUTSIDE = agent(
    "outside",
    checkpoint=CheckpointRef(
        key=CheckpointKey(address="hf://lab/outside.pt"),
        artifact=ArtifactRef(uri="hf://lab/outside.pt", sha256="ab" * 32),
    ),
    config={"strength": 5},
)


def versus(name, first, second):
    return Matchup(name=name, players=(first, second), conditions=DEALS, games=2, tags=("anchor",))


def eval(state):
    rated = [each for run in state.runs for each in run.checkpoints.every(2)]
    ladder = Benchmark(name="ladder", environment=DUEL, matchups=(
        versus("vs weak", DIRECT, WEAK),
        versus("vs outside", DIRECT, OUTSIDE),
        versus("strong vs weak", STRONG, WEAK),
        versus("outside vs weak", OUTSIDE, WEAK),
    ))
    corrupt = Benchmark(name="corrupt", environment=DUEL, matchups=(
        Matchup(name="deal 1", players=(DIRECT, WEAK), conditions=({"deal": 1},), games=3),
    ))
    return [
        evaluation(ladder, play=play, checkpoints=rated, max_hours=0.1),
        evaluation(corrupt, play=play_failing_deal_one, checkpoints=rated[:1], max_hours=0.1),
    ]


def report():
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        evals=(
            Curve(),
            Curve(matches=Matches(matchup="vs outside")),
            Curve(matches=Matches(matchup="vs nobody")),
            Anchors(),
        ),
    )
"""

CLOCKS = [Clock(name="updates", kind="step", meaning="optimizer updates")]


def save(run: Run, tmp_path: Path, updates: range) -> None:
    """Checkpoints with the same bytes in every run, as runs warm-started from one file save."""
    for update in updates:
        weights = tmp_path / "weights.pt"
        weights.write_text(f"weights at {update}")
        run.checkpoint(weights, updates=update)


@pytest.fixture(scope="module")
def lab(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    """Two runs of `lab`, four checkpoints each, evaluated once; then `a` saves two more."""
    tmp_path = tmp_path_factory.mktemp("lab")
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path / "experiments", "lab", LAB)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ETUDE_HOME", str(tmp_path / "home"))
        # Running an experiment file puts its folder on the import path.
        patch.setattr(sys, "path", list(sys.path))
        for name in ("a", "b"):
            run = track(experiment="lab", run=name, job=name, clocks=CLOCKS, directory=runs)
            save(run, tmp_path, range(100, 500, 100))
            run.finish(timeout=30)
        # Deal 1 of the corrupt benchmark fails, and the pass stops.
        with pytest.raises(EtudeError, match="match_failed"):
            advance(read_experiment(file), source=DirectoryStorage(runs), directory=runs)
        resumed = track(experiment="lab", run="a", job="a", clocks=CLOCKS, directory=runs)
        save(resumed, tmp_path, range(500, 700, 100))
        resumed.finish(timeout=30)
        yield runs, file


@pytest.fixture(scope="module")
def page(lab: tuple[Path, Path]) -> Page:
    return build(*lab)


def agent(page: Page, name: str, run: str | None = None) -> int:
    (found,) = [i for i, each in enumerate(page.agents) if each.name == name and each.run == run]
    return found


def selection(page: Page, tournament: str, *, matchup: str | None = None) -> int:
    return next(
        i
        for i, each in enumerate(page.selections)
        if (each.tournament, each.matchup, each.tag) == (tournament, matchup, None)
    )


def test_checkpoints_are_bound_by_key_and_fixed_agents_are_anchors(page: Page) -> None:
    # Identical bytes under two keys are two agents, each where its own run stood.
    bound = {(each.run, each.ordinal) for each in page.agents if each.run is not None}
    assert bound == {("a", 2), ("a", 4), ("b", 2), ("b", 4)}
    marks = {(m.run, m.ordinal): m.at for m in page.training.checkpoints}
    assert marks[("a", 2)] == marks[("b", 2)] == {"updates": 200}
    # Code and an outside checkpoint are fixed: no run, no ordinal.
    assert {each.name for each in page.agents if each.run is None} == {"weak", "strong", "outside"}
    ladder = selection(page, "ladder")
    a2, b2 = agent(page, "updates-200.pt", "a"), agent(page, "updates-200.pt", "b")
    weak, strong = agent(page, "weak"), agent(page, "strong")
    against = {
        (c.subject, c.opponent): c
        for c in page.cells
        if c.selection == ladder and not c.fixed and not c.split
    }
    assert against[(a2, weak)].matches == against[(b2, weak)].matches == 2
    # Anchors meet once, not once per checkpoint, and each has a strength among anchors.
    assert against[(strong, weak)].matches == 2 and against[(strong, weak)].value > 0.5
    (strength,) = [
        c for c in page.cells if c.fixed and c.subject == strong and c.selection == ladder
    ]
    assert strength.opponent is None and strength.matches == 2


def test_a_page_shows_what_was_not_evaluated_not_yet_played_and_failed(page: Page) -> None:
    coverage = {(c.run, c.ordinal, c.tournament): c for c in page.coverage}
    # Every second checkpoint is rated; the others were not evaluated.
    assert not [key for key in coverage if key[1] % 2]
    assert (coverage[("a", 2, "ladder")].played, coverage[("a", 2, "ladder")].unplayed) == (4, 0)
    # Saved after the pass: eval(state) wants it, and nothing has played it yet.
    assert (coverage[("a", 6, "ladder")].played, coverage[("a", 6, "ladder")].unplayed) == (0, 4)
    # The failed match is counted and listed as failed, never as not yet played.
    failed = coverage[("a", 2, "corrupt")]
    assert (failed.played, failed.failed, failed.unplayed) == (0, 1, 0)
    (failure,) = page.failures
    assert [page.agents[i].name for i in failure.agents] == ["updates-200.pt", "weak"]
    assert "deal 1 crashed" in (failure.error or "")


def test_panels_select_matches_by_tournament_matchup_or_tag(page: Page) -> None:
    chosen = {(each.tournament, each.matchup, each.tag) for each in page.selections}
    assert {("ladder", None, "anchor"), ("ladder", "vs outside", None)} <= chosen
    outside = selection(page, "ladder", matchup="vs outside")
    # The checkpoints' opponents in that matchup's matches are the outside anchor alone.
    opponents = {
        page.agents[c.opponent].name
        for c in page.cells
        if c.selection == outside
        and c.opponent is not None
        and page.agents[c.subject].run is not None
    }
    assert opponents == {"outside"}
    assert any("matchup 'vs nobody'" in note for note in page.notes)


def test_a_watched_page_reads_each_bucket_file_once_per_computer(
    lab: tuple[Path, Path], page: Page
) -> None:
    boto3 = pytest.importorskip("boto3")
    runs, file = lab
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="lab")
        local = DirectoryStorage(runs)
        for key in local.keys(""):
            S3Storage("lab", client=client).put(key, local.get(key))
        # `etude report --watch` builds the page every few seconds, eval(state) included.
        first = build(S3Storage("lab", client=client), file)
        assert first.cells == page.cells and first.coverage == page.coverage
        read_once = client.bytes
        for _ in range(3):
            assert build(S3Storage("lab", client=client), file) == first
        assert client.bytes == read_once
