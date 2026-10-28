"""`etude report FILE` follows the file's `report()` for the experiment named by the file."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from etude.cli import main
from etude.page import Page, Pages, Server, build, experiment_files, index, render, target
from etude.report import Anchors, Curve, Fraction, Matches, Matrix, Ranking, Report
from etude.run.errors import EtudeError
from etude.run.storage import DirectoryStorage, storage_from_uri
from etude.train.records import Step
from etude.train.run import track
from tests.experiments import experiment_file

LAB = """
from etude.report import Matrix, Report, Score


def train(config: object) -> None: ...


def report() -> Report:
    return Report(
        score=Score(table="plays", value="won", reference=0.5),
        evals=(Matrix(rows="opponent_deck", columns="deck"),),
        baseline="uniform",
    )
"""


@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Running an experiment file puts its folder on the import path.
    monkeypatch.setattr(sys, "path", list(sys.path))


def trained(runs: Path, experiment: str, run: str) -> None:
    with track(experiment=experiment, run=run, clocks=[Step("updates", "updates")], directory=runs):
        pass


def test_the_report_is_the_files_and_the_experiment_is_named_for_it(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    trained(runs, "other", "b")
    page = build(runs, experiment_file(tmp_path / "experiments", "lab", LAB))
    assert (page.experiment, page.origin, page.notes) == ("lab", "lab.py:report", ())
    assert page.report.evals == (Matrix(rows="opponent_deck", columns="deck"),)
    assert [run.name for run in page.training.runs] == ["a"]


def test_without_a_report_the_page_is_the_default_and_says_so(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    page = build(runs, experiment_file(tmp_path, "lab", "def train(config: object) -> None: ...\n"))
    assert page.report == Report() and page.origin is None
    assert page.notes == ("lab.py defines no report(), so this is the default report.",)
    assert page.report.evals == (Curve(), Anchors())


def test_a_file_that_is_not_an_experiments_is_refused(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    with pytest.raises(EtudeError, match="experiment_report_returns_a_report"):
        build(runs, experiment_file(tmp_path, "lab", "def report():\n    return {}\n"))
    with pytest.raises(EtudeError, match="experiment_file_not_found"):
        build(runs, tmp_path / "missing.py")


def test_an_absent_experiment_names_the_ones_the_source_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trained(tmp_path / "runs", "lab", "a")
    elsewhere = experiment_file(tmp_path, "elsewhere")
    assert main(["report", str(elsewhere), "--source", str(tmp_path / "runs")]) == 1
    assert "it holds lab" in capsys.readouterr().err


def test_a_bucket_report_is_written_here_and_a_directorys_beside_its_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("boto3")
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    # Making a client reaches no network: only reading the bucket would.
    assert target(storage_from_uri("r2://bucket/runs"), "lab") == Path("reports/lab.html")
    assert target(storage_from_uri("s3://bucket/runs"), "lab") == Path("reports/lab.html")
    assert target(DirectoryStorage(tmp_path), "lab") == tmp_path / "reports/lab.html"
    assert target(DirectoryStorage(tmp_path), "lab", tmp_path / "out") == tmp_path / "out/lab.html"


def test_a_report_names_the_eval_columns_its_panels_split_by() -> None:
    report = Report(
        evals=(
            Curve(split="seat", matches=Matches(tag="anchor")),
            Matrix(rows="opponent_deck", columns="deck"),
            Ranking(by="seat"),
        )
    )
    assert report.columns() == ("seat", "opponent_deck", "deck")
    assert "rl/entropy" in Report().keys()


REPORTED = "from etude.report import Report\n\n\ndef report() -> Report:\n    return Report()\n"


def test_a_folder_holds_the_files_that_define_eval_or_report_and_none_is_run(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "experiments"
    experiment_file(folder, "lab", REPORTED)
    experiment_file(folder / "more", "ladder", "def eval(state):\n    return []\n")
    # A helper module and a job file are no experiments, and are never run to find out.
    experiment_file(folder, "games", "raise SystemExit('run')\n\ndef play(match): ...\n")
    experiment_file(folder, "job", "raise SystemExit('run')\n\ndef train(config): ...\n")
    experiment_file(folder / ".venv", "vendored", REPORTED)
    assert [f.relative_to(folder) for f in experiment_files(folder)] == [
        Path("lab.py"),
        Path("more/ladder.py"),
    ]
    experiment_file(folder / "copy", "lab", REPORTED)
    with pytest.raises(EtudeError, match="experiment_named_twice"):
        experiment_files(folder)


def test_a_folders_report_is_an_index_of_its_experiments_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runs, folder = tmp_path / "runs", tmp_path / "experiments"
    trained(runs, "lab", "a")
    experiment_file(folder, "lab", REPORTED)
    # One file fails as it runs, and one experiment has no runs yet: each says so on its row.
    experiment_file(folder, "broken", "def report(): ...\n\nraise RuntimeError('typo')\n")
    experiment_file(folder, "fresh", REPORTED)
    built = Pages(runs).build_each(folder)
    found = index(runs, built, href=lambda stem: f"{stem}.html")
    rows = {entry.experiment: entry for entry in found.entries}
    assert rows["broken"].page is None and rows["broken"].error == "RuntimeError: typo"
    assert rows["fresh"].error is not None and "no runs of 'fresh'" in rows["fresh"].error
    lab, whole = rows["lab"].page, built[folder / "lab.py"]
    assert lab is not None and isinstance(whole, Page)
    # The index keeps the page but its line data, which it never draws.
    assert lab.training.runs == whole.training.runs and lab.training.series == {}
    assert rows["lab"].href == "lab.html"
    # Written, it is an index beside a page for each experiment that has one.
    assert main(["report", str(folder), "--source", str(runs)]) == 0
    assert capsys.readouterr().out.strip() == str(runs / "reports/index.html")
    assert sorted(p.name for p in (runs / "reports").iterdir()) == ["index.html", "lab.html"]
    # Served, it is the index at / and each experiment's page at /STEM.
    server = Server(runs, folder, ("127.0.0.1", 0))
    try:
        assert '"href": "/lab"' in (server.page("/") or "")
        assert "lab.py:report" in (server.page("/lab") or "")
        assert server.page("/elsewhere") is None
    finally:
        server.server_close()


COUNTED = """
from pathlib import Path

from etude.report import Report

with Path(__file__).with_suffix(".built").open("a") as built:
    built.write("built\\n")


def report() -> Report:
    return Report()
"""


def test_a_served_page_is_built_again_only_once_its_files_change(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    trained(runs, "other", "c")
    file = experiment_file(tmp_path / "experiments", "lab", COUNTED)
    builds = file.with_suffix(".built")  # the file runs once for every page built
    server = Server(runs, file, ("127.0.0.1", 0))
    try:
        assert '"name": "a"' in (server.page("/") or "")
        server.page("/")
        assert builds.read_text().count("built") == 1
        # Another experiment's run is not this page's input; a run of its own is.
        trained(runs, "other", "d")
        server.page("/")
        assert builds.read_text().count("built") == 1
        trained(runs, "lab", "b")
        assert '"name": "b"' in (server.page("/") or "")
        assert builds.read_text().count("built") == 2
        file.write_text(COUNTED + "# edited\n")
        server.page("/")
        assert builds.read_text().count("built") == 3
    finally:
        server.server_close()


def test_a_page_carries_its_charts_and_needs_no_network(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    html = render(build(tmp_path / "runs", experiment_file(tmp_path, "lab", REPORTED)))
    assert "uPlot" in html and "<script src" not in html and "<link" not in html


def test_a_panels_matches_are_a_matchup_or_a_tag_of_a_tournament_never_both() -> None:
    assert Curve(matches=Matches(tournament="ladder", tag="anchor")).matches is not None
    with pytest.raises(ValueError, match="matches_name_a_matchup_or_a_tag_not_both"):
        Matches(matchup="vs heuristic", tag="anchor")


def test_a_curve_against_a_fraction_of_its_position_keeps_that_opponent_through_json() -> None:
    report = Report(evals=(Curve(opponent=Fraction(clock="updates", value=0.5)),))
    (curve,) = Report.model_validate_json(report.model_dump_json()).evals
    assert isinstance(curve, Curve)
    assert curve.opponent == Fraction(clock="updates", value=0.5)
    for outside in (0.0, 1.0, 2.0):
        with pytest.raises(ValueError):
            Fraction(clock="updates", value=outside)


def test_an_absent_experiments_error_names_the_source_as_it_was_given(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    given = re.escape(f"no runs of 'fresh' in {tmp_path / 'runs'};")
    with pytest.raises(ValueError, match=given):
        build(tmp_path / "runs", experiment_file(tmp_path, "fresh"))
