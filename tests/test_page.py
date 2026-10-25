"""`etude report FILE` follows the file's `report()` for the experiment named by the file."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from etude.cli import main
from etude.page import build, target
from etude.report import Anchors, Curve, Matrix, Ranking, Report
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
            Curve(split="seat", tag="anchor"),
            Matrix(rows="opponent_deck", columns="deck"),
            Ranking(by="seat"),
        )
    )
    assert report.columns() == ("seat", "opponent_deck", "deck")
    assert "rl/entropy" in Report().keys()
