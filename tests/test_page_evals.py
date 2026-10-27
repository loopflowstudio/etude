"""An experiment's evals on its page: checkpoints placed by seat or key, anchors drawn once, gaps kept."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from moto import mock_aws

from etude.carriage import checkpoint_file
from etude.eval.match import environment, evaluate
from etude.eval.query import compare, statsdb
from etude.eval.records import CheckpointKey, CheckpointRef, Code, Inference, Match, Model, Player
from etude.experiment import advance, observe, read_experiment
from etude.page import Page, build
from etude.report import Matches
from etude.run.errors import EtudeError
from etude.run.records import ArtifactRef, Clock
from etude.run.storage import DirectoryStorage, S3Storage
from etude.train.run import Run, track
from tests.eval.games import play, play_failing_deal_one
from tests.experiments import experiment_file
from tests.run.storage import CountedClient

LAB = """
from etude.eval.match import environment
from etude.eval.records import (
    ArtifactRef, Benchmark, CheckpointKey, CheckpointRef, Code, Inference, Matchup, Model,
)
from etude.jobs import evaluation
from etude.report import Anchors, Curve, Matches, Report, Score
from tests.eval.games import play, play_failing_deal_one

DUEL = environment("duel")
DIRECT = Inference(config={"search": 0})
DEALS = ({"deal": 0}, {"deal": 1})
WEAK = Code(name="weak", source=[__file__], config={"strength": 0})
STRONG = Code(name="strong", source=[__file__], config={"strength": 9})
OUTSIDE = Model(
    name="outside",
    checkpoint=CheckpointRef(
        key=CheckpointKey(address="hf://lab/outside.pt"),
        artifact=ArtifactRef(uri="hf://lab/outside.pt", sha256="ab" * 32),
    ),
    inference=Inference(config={"strength": 5}),
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


def player(page: Page, name: str, run: str | None = None) -> int:
    (found,) = [i for i, each in enumerate(page.players) if each.name == name and each.run == run]
    return found


def selection(page: Page, tournament: str | None, *, matchup: str | None = None) -> int:
    return next(
        i
        for i, each in enumerate(page.selections)
        if (each.tournament, each.matchup, each.tag) == (tournament, matchup, None)
    )


def test_checkpoints_are_bound_by_key_and_fixed_players_are_anchors(page: Page) -> None:
    # Identical bytes under two keys are two models, each where its own run stood.
    bound = {(each.run, each.ordinal) for each in page.players if each.run is not None}
    assert bound == {("a", 2), ("a", 4), ("b", 2), ("b", 4)}
    marks = {(m.run, m.ordinal): m.at for m in page.training.checkpoints}
    assert marks[("a", 2)] == marks[("b", 2)] == {"updates": 200}
    # Code and an outside checkpoint are fixed: no run, no ordinal.
    assert {each.name for each in page.players if each.run is None} == {"weak", "strong", "outside"}
    ladder = selection(page, "ladder")
    a2, b2 = player(page, "updates-200.pt", "a"), player(page, "updates-200.pt", "b")
    weak, strong = player(page, "weak"), player(page, "strong")
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
    assert [page.players[i].name for i in failure.players] == ["updates-200.pt", "weak"]
    assert "deal 1 crashed" in (failure.error or "")


def test_panels_select_matches_by_tournament_matchup_or_tag(page: Page) -> None:
    chosen = {(each.tournament, each.matchup, each.tag) for each in page.selections}
    assert {("ladder", None, "anchor"), ("ladder", "vs outside", None)} <= chosen
    outside = selection(page, "ladder", matchup="vs outside")
    # The checkpoints' opponents in that matchup's matches are the outside anchor alone.
    opponents = {
        page.players[c.opponent].name
        for c in page.cells
        if c.selection == outside
        and c.opponent is not None
        and page.players[c.subject].run is not None
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


def test_a_report_reads_no_checkpoint_bytes(lab: tuple[Path, Path], page: Page) -> None:
    boto3 = pytest.importorskip("boto3")
    runs, file = lab
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="lab")
        local = DirectoryStorage(runs)
        stored = [key for key in local.keys("") if checkpoint_file(key)]
        for key in local.keys(""):
            S3Storage("lab", client=client).put(key, local.get(key))
        assert stored
        assert build(S3Storage("lab", client=client), file).cells == page.cells
        assert client.read and not [key for key in client.read if checkpoint_file(key)]


VERSIONED = """
from etude.eval.match import environment
from etude.eval.records import Benchmark, Code, Inference, Matchup
from etude.jobs import evaluation
from tests.eval.games import play

DIRECT = Inference(config={"search": 0})
WEAK = Code(name="weak", source=[__file__], config={"strength": 0})


def eval(state):
    rated = [each for run in state.runs for each in run.checkpoints]
    versus = Matchup(name="vs weak", players=(DIRECT, WEAK), conditions=({"deal": 0},), games=2)
    ladder = Benchmark(name="ladder", environment=environment("duel"), matchups=(versus,))
    return [evaluation(ladder, play=play, checkpoints=rated, max_hours=0.1)]
"""


def test_a_page_offers_each_commit_its_matches_were_played_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path / "experiments", "versioned", VERSIONED)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    # As in a launched job: the player's code is a source extracted at a commit.
    monkeypatch.setenv("ETUDE_SOURCE_DIR", str(file.parent))
    for commit, updates in (("a" * 40, range(100, 300, 100)), ("b" * 40, range(300, 400, 100))):
        # Each pass sees exactly the checkpoints delivered before it: the run
        # finishes, as a resumed job's attempt does, before the eval advances.
        run = track(experiment="versioned", run="a", job="a", clocks=CLOCKS, directory=runs)
        save(run, tmp_path, updates)
        run.finish(timeout=30)
        monkeypatch.setenv("ETUDE_COMMIT", commit)
        advance(read_experiment(file), source=DirectoryStorage(runs), directory=runs)
    page = build(runs, file)
    assert [(each.commit, each.matches) for each in page.commits] == [("a" * 40, 2), ("b" * 40, 1)]
    weak = player(page, "weak")
    played = {
        commit: {page.players[c.subject].name for c in page.cells if c.commit == commit
                 and c.opponent == weak and not c.split}
        for commit in (None, "a" * 40, "b" * 40)
    }  # fmt: skip
    # Each commit's view holds the matches played at it; the latest view holds them all.
    assert played == {
        None: {"updates-100.pt", "updates-200.pt", "updates-300.pt"},
        "a" * 40: {"updates-100.pt", "updates-200.pt"},
        "b" * 40: {"updates-300.pt"},
    }


EVALUATION_ONLY = """
from etude.report import At, Crosstable, Curve, Earlier, Report, Score


def report():
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        clock="updates",
        evals=(
            Curve(title="Score against the heuristic", opponent="heuristic"),
            Curve(title="Against 400 updates earlier", opponent=Earlier(clock="updates", by=400)),
            Curve(title="Against update 200", opponent=At(clock="updates", value=200)),
            Crosstable(),
        ),
    )
"""

DUEL = environment("duel")
HEURISTIC = Code(name="heuristic", source=[__file__], config={"strength": 5})


def outside(name: str, at: dict[str, float], **config: int) -> Model:
    """A checkpoint no Etude run recorded, keyed by its address, seated where `at` says."""
    address = f"hf://lab/{name}.pt"
    ref = CheckpointRef(
        key=CheckpointKey(address=address),
        artifact=ArtifactRef(uri=address, sha256="cd" * 32),
        at=at,
    )
    return Model(name=name, checkpoint=ref, inference=Inference(config=config))


def duel(first: Player, second: Player, deal: int = 0) -> Match:
    return Match(players=(first, second), conditions={"deal": deal}, games=2)


@pytest.fixture(scope="module")
def evaluation_only(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    """Two plain eval runs of `lab`, and no training run: checkpoints placed by their seats."""
    tmp_path = tmp_path_factory.mktemp("evaluation_only")
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path / "experiments", "lab", EVALUATION_ONLY)
    u = {n: outside(f"u{n}", {"updates": n}) for n in (0, 200, 400, 600)}
    loose = outside("loose", {}, strength=4)
    grids = [duel(each, HEURISTIC, deal) for each in u.values() for deal in (0, 1)]
    pairs = [duel(u[600], u[200]), duel(u[400], u[0]), duel(u[600], u[400])]
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ETUDE_HOME", str(tmp_path / "home"))
        patch.setattr(sys, "path", list(sys.path))
        # (600, 400) is delivered by both runs; "drift" is seated at two positions.
        for run, matches in (
            ("plain-a", [*grids[:4], *pairs, duel(loose, HEURISTIC)]),
            ("plain-b", [*grids[4:], pairs[2]]),
        ):
            drift = outside("drift", {"updates": 300 if run == "plain-a" else 350}, strength=6)
            evaluate(
                experiment="lab",
                environment=DUEL,
                matches=[*matches, duel(drift, HEURISTIC, deal=int(run == "plain-b"))],
                play=play,
                run=run,
                directory=runs,
            )
        yield runs, file


def test_an_evaluation_only_experiment_draws_checkpoints_where_their_seats_say(
    evaluation_only: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    runs, file = evaluation_only
    monkeypatch.setenv("ETUDE_HOME", str(runs.parent / "home"))
    page = build(runs, file)
    assert page.training.runs == ()
    outside_tournaments = selection(page, None)
    assert page.selections[outside_tournaments] == Matches()
    at = {each.name: each.at for each in page.players}
    assert {name: at[name] for name in ("u0", "u200", "u400", "u600")} == {
        f"u{n}": {"updates": n} for n in (0, 200, 400, 600)
    }
    # Unplaced: code, and a checkpoint seated without `at`.
    assert at["heuristic"] == at["loose"] == {}
    assert all(each.run is None for each in page.players)
    assert any("loose" in note and "anchors" in note for note in page.notes)
    # Seats that disagree: the latest is drawn, and the earlier is named.
    assert at["drift"] == {"updates": 350}
    assert (
        "drift is drawn at updates 350, as its latest seat says; earlier seats said updates 300."
        in page.notes
    )
    pair = {
        (page.players[c.subject].name, page.players[c.opponent].name): c
        for c in page.cells
        if c.selection == outside_tournaments and c.opponent is not None and not c.split
    }
    # The match both runs delivered counts once, where reading every row counts it twice.
    assert pair[("u600", "u400")].matches == pair[("u400", "u600")].matches == 1
    db = statsdb("lab", runs)
    try:
        direct = {
            (each.subject, each.opponent): each for each in compare(db, table="plays", value="won")
        }
    finally:
        db.close()
    keys = {each.name: each.key for each in page.players}
    for (subject, opponent), cell in pair.items():
        found = direct[((keys[subject],), (keys[opponent],))]
        if {subject, opponent} == {"u600", "u400"}:
            assert found.matches == 2
            continue
        assert (cell.value, cell.interval, cell.matches) == (
            found.value,
            found.interval,
            found.matches,
        )
    assert {"u0", "u200", "u400", "u600"} <= {s for s, o in pair if o == "heuristic"}


def test_an_evaluation_only_page_reads_each_bucket_object_once(
    evaluation_only: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boto3 = pytest.importorskip("boto3")
    runs, file = evaluation_only
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    with mock_aws():
        client = CountedClient(boto3.client("s3", region_name="us-east-1"))
        client.create_bucket(Bucket="lab")
        local = DirectoryStorage(runs)
        for key in local.keys(""):
            S3Storage("lab", client=client).put(key, local.get(key))
        first = build(S3Storage("lab", client=client), file)
        assert first.training.runs == () and first.cells
        sizes = {key: len(local.get(key)) for key in local.keys("")}
        assert all(read <= sizes[key] for key, read in client.read.items())
        read_once = client.bytes
        assert build(S3Storage("lab", client=client), file) == first
        assert client.bytes == read_once


SEATED = """
from etude.report import Report, Score


def report():
    return Report(score=Score(table="plays", value="won", reference=0.5))
"""


def test_a_tracked_checkpoint_is_placed_by_its_seat_first_and_its_training_record_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path / "experiments", "seated", SEATED)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    run = track(experiment="seated", run="a", job="a", clocks=CLOCKS, directory=runs)
    save(run, tmp_path, range(100, 300, 100))
    run.finish(timeout=30)
    first, second = observe(DirectoryStorage(runs), "seated").runs[0].checkpoints
    # Seated as stored matches were: without `at`, and once where the seat says otherwise.
    bare = first.ref.model_copy(update={"at": {}})
    moved = second.ref.model_copy(update={"at": {"updates": 250}})
    evaluate(
        experiment="seated",
        environment=DUEL,
        matches=[
            duel(Inference().model(bare), HEURISTIC),
            duel(Inference().model(moved), HEURISTIC),
        ],
        play=play,
        run="plain",
        directory=runs,
    )
    page = build(runs, file)
    placed = {each.name: (each.run, each.at) for each in page.players if each.run is not None}
    assert placed == {
        "updates-100.pt": ("a", {"updates": 100}),
        "updates-200.pt": ("a", {"updates": 250}),
    }
    assert "updates-200.pt: seated at updates 250; its training record says 200." in page.notes


def test_a_failure_outside_a_tournament_is_kept_until_its_environment_plays_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path / "experiments", "lab", SEATED)
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    crashing = duel(outside("u0", {"updates": 0}), HEURISTIC, deal=1)
    with pytest.raises(EtudeError, match="match_failed"):
        evaluate(
            experiment="lab",
            environment=DUEL,
            matches=[crashing],
            play=play_failing_deal_one,
            run="failed-in-duel",
            directory=runs,
        )
    evaluate(
        experiment="lab",
        environment=environment("other"),
        matches=[crashing],
        play=play,
        run="done-in-other",
        directory=runs,
    )
    page = build(runs, file)
    (failure,) = page.failures
    assert failure.tournament is None
    assert [page.players[i].name for i in failure.players] == ["u0", "heuristic"]
    assert failure.error is not None and "deal 1 crashed" in failure.error
