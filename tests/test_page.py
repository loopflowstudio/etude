"""`etude report FILE` follows the file's `report()` for the experiment named by the file."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import EtudeError
from etude.page import (
    Drawn,
    Page,
    Pages,
    PanelSelection,
    Point,
    build,
    experiment_files,
    index,
    render,
    target,
)
from etude.records import Clock
from etude.report import (
    Anchors,
    Curve,
    Matches,
    Matrix,
    Ranking,
    Report,
    Score,
    Series,
)
from etude.storage import DirectoryStorage, storage_from_uri
from tests.experiments import experiment_file
from tests.run.storage import MeasuredStorage
from tests.train.tracking import open_run

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
    with open_run(
        experiment=experiment,
        run=run,
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ):
        pass


def test_the_report_is_the_files_and_the_experiment_is_named_for_it(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    trained(runs, "other", "b")
    page = build(runs, experiment_file(tmp_path / "experiments", "lab", LAB))
    assert (page.experiment, page.origin, page.notes) == ("lab", "lab.py:report", ())
    assert page.report.evals == (Matrix(rows="opponent_deck", columns="deck"),)
    assert [run.name for run in page.training.runs] == ["a"]


def test_edit_during_report_build_is_used_on_the_next_refresh(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    # The declaration edits its file after Python has loaded its first version.
    replacement = (
        "from etude.report import Report\ndef report(): return Report(baseline='second')\n"
    )
    file = experiment_file(
        tmp_path / "experiments",
        "lab",
        "from pathlib import Path\nfrom etude.report import Report\n"
        f"def report():\n    Path(__file__).write_text({replacement!r})\n"
        "    return Report(baseline='first')\n",
    )
    pages = Pages(runs)
    assert pages.build(file).report.baseline == "first"
    assert pages.build(file).report.baseline == "second"


def test_refresh_tracks_a_helper_first_imported_by_eval_jobs(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    trained(runs, "lab", "a")
    folder = tmp_path / "experiments"
    helper = experiment_file(folder, "late_report_jobs", "JOBS = []\n")
    file = experiment_file(
        folder,
        "lab",
        "def eval_jobs(state):\n    from late_report_jobs import JOBS\n    return JOBS\n",
    )
    pages = Pages(runs)
    assert pages.build(file).experiment == "lab"
    helper.write_text("raise ValueError('edited evaluation declaration')\n")
    with pytest.raises(ValueError, match="edited evaluation declaration"):
        pages.build(file)


def test_checkpoints_with_no_etude_clock_are_named_with_the_reason(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run = open_run(
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    )
    run.checkpoint("s3://models/10.pt", updates=10)
    run.finish(timeout=30)
    file = experiment_file(tmp_path / "experiments", "lab", LAB)
    assert build(runs, file).notes == ()
    # As an older client saved it, copied without its attempt records.
    for path in (runs / "lab/a/checkpoints").glob("*.json"):
        record = json.loads(path.read_text())
        record["at"] = {"updates": record["at"]["updates"]}
        path.write_text(json.dumps(record))
    for path in (runs / "lab/a/metrics").glob("*.json"):
        path.unlink()
    (note,) = build(runs, file).notes
    assert "No running_hours or hours_since_start for some checkpoints of a" in note
    assert "attempt records (metrics/WRITER.json) of a are not in this source" in note


def test_without_a_report_the_page_is_the_default_and_says_so(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run = open_run(
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    )
    run.log({"custom/score": 1.0}, updates=1)
    run.finish(timeout=30)
    page = build(runs, experiment_file(tmp_path, "lab", "def train(config: object) -> None: ...\n"))
    assert page.report == Report(other_metrics=True, ml=()) and page.origin is None
    assert page.notes == ("lab.py defines no report(), so this is the default report.",)
    assert page.report.evals == (Curve(), Anchors())
    assert "custom/score" in page.training.metrics  # nothing declared, so everything is drawn

    declared = "from etude.report import Report\n\ndef report() -> Report:\n    return Report()\n"
    page = build(runs, experiment_file(tmp_path / "declared", "lab", declared))
    assert "custom/score" not in page.training.metrics


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
    experiment_file(folder / "more", "ladder", "def eval_jobs(state):\n    return []\n")
    # A helper module and an experiment file are no experiments, and are never run to find out.
    experiment_file(folder, "games", "raise SystemExit('run')\n\ndef play(match, agents): ...\n")
    experiment_file(folder, "job", "raise SystemExit('run')\n\ndef train(config): ...\n")
    experiment_file(folder / ".venv", "vendored", REPORTED)
    assert [f.relative_to(folder) for f in experiment_files(folder)] == [
        Path("lab.py"),
        Path("more/ladder.py"),
    ]
    experiment_file(folder / "copy", "lab", REPORTED)
    with pytest.raises(EtudeError, match="experiment_named_twice: copy/lab.py and lab.py"):
        experiment_files(folder)


def test_a_folder_holding_etude_home_ignores_the_sources_it_retains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "lab"
    monkeypatch.setenv("ETUDE_HOME", str(folder / "home"))
    monkeypatch.setenv("ETUDE_TRACK_DIR", str(folder / "runs"))
    experiment_file(folder, "paced", REPORTED)
    # A launch keeps a copy of the source for each attempt under Etude's home.
    experiment_file(folder / "home/sessions/a1/attempts/b2/src", "paced", REPORTED)
    assert experiment_files(folder) == [folder / "paced.py"]


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
    found = index(runs, built)
    rows = {entry.experiment: entry for entry in found.entries}
    assert rows["broken"].page is None
    assert rows["broken"].error is not None and "RuntimeError: typo" in rows["broken"].error
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


def test_a_page_carries_its_charts_and_needs_no_network(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    html = render(build(tmp_path / "runs", experiment_file(tmp_path, "lab", REPORTED)))
    assert "uPlot" in html and "<script src" not in html and "<link" not in html


def test_a_panels_matches_are_a_seating_or_a_tag_of_a_tournament_never_both() -> None:
    assert Curve(matches=Matches(tournament="ladder", tag="anchor")).matches is not None
    with pytest.raises(ValueError, match="matches_name_a_seating_or_a_tag_not_both"):
        Matches(seating="vs heuristic", tag="anchor")


def test_an_absent_experiments_error_names_the_source_as_it_was_given(tmp_path: Path) -> None:
    trained(tmp_path / "runs", "lab", "a")
    given = re.escape(f"no runs of 'fresh' in {tmp_path / 'runs'};")
    with pytest.raises(ValueError, match=given):
        build(tmp_path / "runs", experiment_file(tmp_path, "fresh"))


def test_planned_marker_uses_the_current_continuation_not_an_older_overrun(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    file = experiment_file(tmp_path, "lab", REPORTED)
    with open_run(
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ) as run:
        run.log({"rl/loss": 1.0}, updates=1500)
    with open_run(
        resume=True,
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ) as run:
        run.log({"rl/loss": 0.5}, updates=1200)
    page = build(runs, file)
    assert page.training.runs[0].at["updates"] == 1200
    with open_run(
        resume=True,
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ):
        pass
    assert build(runs, file).training.runs[0].at["updates"] == 1200


def test_report_experiment_override_selects_runs_and_names_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    source = tmp_path / "runs"
    file = tmp_path / "coin.py"
    file.write_text(
        "from etude.report import Report\ndef report():\n    return Report(other_metrics=True)\n"
    )
    for name, score in (("coin", 1), ("wide", 9)):
        with open_run(experiment=name, run=name, directory=source) as run:
            run.log({"score": score}, updates=1)
    page = Pages(source).build(file, experiment="wide")
    assert page.experiment == "wide"
    assert page.training == build(source, file, experiment="wide").training
    assert main(["report", str(file), "--experiment", "wide", "--source", str(source)]) == 0
    assert capsys.readouterr().out.strip() == str(source / "reports/wide.html")
    assert (source / "reports/wide.html").is_file()
    assert not (source / "reports/coin.html").exists()
    assert main(["report", str(tmp_path), "--experiment", "wide"]) == 1
    assert "--experiment needs one Python file" in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [False, True])
def test_default_rl_panels_use_logged_keys_but_explicit_panels_keep_missing_keys(
    tmp_path: Path, explicit: bool
) -> None:
    runs = tmp_path / "runs"
    with open_run(experiment="lab", run="a", directory=runs) as run:
        run.log({"rl/loss": 0.5}, updates=1)
    panels = "ml=(Series(title='Losses', keys=('rl/loss', 'rl/value_loss')),)" if explicit else ""
    code = (
        "from etude.report import Report, Series\n"
        f"def report() -> Report:\n    return Report({panels})\n"
    )
    page = build(runs, experiment_file(tmp_path, "lab", code))
    assert page.report.ml == (
        Series(title="Losses", keys=("rl/loss", "rl/value_loss") if explicit else ("rl/loss",)),
    )
    assert page.training.series["updates"]["rl/loss"][0].mean == [0.5]
    assert page.headline() == "rl/loss 0.5"


def test_selective_report_finds_the_first_ungrouped_metric_drawable_across_runs(
    tmp_path: Path,
) -> None:
    source = MeasuredStorage(DirectoryStorage(tmp_path / "runs"))
    file = experiment_file(tmp_path, "lab", REPORTED)
    for name, loss in (("a", float("nan")), ("b", 0.42), ("c", 0.25)):
        with open_run(
            experiment="lab", run=name, clocks=["updates"], directory=tmp_path / "runs"
        ) as run:
            run.log({"loss": loss, "reward": 7.0}, updates=150)
    pages = Pages(source)
    page = pages.build(file)
    assert page.headline() == "loss 0.25–0.42" and not page.training.metrics
    assert source.listings == 1
    assert not any(key.endswith((".index.json", ".permanent.json")) for key in source.got)
    reads, size = source.requests, source.bytes
    assert pages.build(file).headline() == page.headline()
    assert (source.listings, source.requests, source.bytes) == (2, reads, size)


def test_seated_headline_uses_the_latest_evaluated_checkpoint_of_the_job(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    for seed in (0, 1):
        with open_run(
            experiment="coin",
            run=f"coin@{seed}",
            job="coin",
            seed=seed,
            clocks=["updates"],
            directory=runs,
        ) as run:
            for step in (100, 200):
                weights = run.out / f"weights-{step}"
                weights.write_text(str(step))
                run.checkpoint(weights, updates=step)
    seating = Matches(seating="prediction")
    score = Score(table="scores", value="won")
    points = tuple(
        Point(
            selection=0,
            score=0,
            run=f"coin@{seed}",
            ordinal=ordinal,
            line=None,
            at={"updates": 100 * ordinal},
            value=value,
        )
        for seed, ordinal, value in ((0, 1, 0), (1, 1, 0.5), (0, 2, 1))
    )
    page = build(runs, None, experiment="coin").model_copy(
        update={
            "report": Report(score=score, evals=(Curve(matches=seating),)),
            "selections": (seating,),
            "scores": (score,),
            "panel_selections": (PanelSelection(selection=0),),
            "drawn": (Drawn(seated=True, tournaments=("coin",)),),
            "points": points,
        }
    )
    assert page.headline() == "scores.won 0.25"
    whole = (*points[:-1], points[-1].model_copy(update={"whole": True}))
    assert page.model_copy(update={"points": whole}).headline() == "scores.won 1"


def test_policy_key_report_resolves_cross_experiment_seats_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = DirectoryStorage(tmp_path / "source")
    for experiment, seed in (("frontier", 0), ("frontier", 1), ("rival", 0)):
        with open_run(
            experiment=experiment,
            run=f"small@{seed}",
            job="small",
            seed=seed,
            directory=source.root,
            clocks=["updates"],
            strict=True,
        ) as run:
            weights = run.out / "weights.pt"
            weights.write_bytes(b"weights")
            run.checkpoint(weights, updates=100)
    file = tmp_path / "rival.py"
    file.write_text("""
from etude.checkpoints import PolicyKey
from etude.eval.match import environment
from etude.eval.records import Benchmark, Final, Seating
from tests.eval.stored import stored_inference
from etude.jobs import EvalJob
from tests.eval.games import play

def eval_jobs(state):
    direct = stored_inference()
    return [EvalJob(tournament=Benchmark(name=name, environment=environment("duel"),
        seatings=(Seating(name="vs frontier", players=(direct,
            Final(experiment="frontier", job="small")), configs=({},), games=8),)),
        checkpoints=state.policy(PolicyKey()).checkpoints, play=play, repo=None, max_hours=1)
        for name in ("first", "second")]
""")
    local = tmp_path / "local"
    monkeypatch.setenv("ETUDE_TRACK_DIR", str(local))
    local_listings = 0
    versions = DirectoryStorage.versions

    def count_local(storage: DirectoryStorage, prefix: str) -> dict[str, str]:
        nonlocal local_listings
        if storage.root == local:
            local_listings += 1
        return versions(storage, prefix)

    monkeypatch.setattr(DirectoryStorage, "versions", count_local)
    measured = MeasuredStorage(source)
    page = build(measured, file)
    assert [(c.tournament, c.unplayed) for c in page.coverage] == [("first", 2), ("second", 2)]
    assert measured.requests == 13  # 12 discovery reads and the checkpoint statsdb ingests
    assert (measured.listings, local_listings) == (1, 1)
    for seed in (0, 1):
        # The report, both tournaments and statsdb share the same declarations.
        assert measured.got.count(f"frontier/small@{seed}/run.json") == 1
        assert measured.got.count(f"frontier/small@{seed}/checkpoints/updates-100.json") == 1
        assert measured.got.count(f"frontier/small@{seed}/outcome.json") == 1
    assert not any(key.endswith("weights.pt") for key in measured.got)


def test_page_cache_reuses_unchanged_build_and_invalidates_on_external_data_edit(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    with open_run(
        experiment="lab",
        run="a",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ) as run:
        run.log({"loss": 1.0}, updates=1)
    label = tmp_path / "label.txt"
    label.write_text("First")
    file = experiment_file(
        tmp_path / "experiments",
        "lab",
        f"""
from pathlib import Path
from etude.report import Report

def report() -> Report:
    return Report(labels={{"a": Path({str(label)!r}).read_text().strip()}})
""",
    )
    first_source = MeasuredStorage(DirectoryStorage(runs))
    first = build(first_source, file)
    assert first.report.labels == {"a": "First"}
    assert any(
        key.endswith((".blk", ".tail", ".index.json", ".permanent.json"))
        for key in first_source.got
    )

    # Unchanged inputs: reuses the persisted Page without touching any metric block or index.
    second_source = MeasuredStorage(DirectoryStorage(runs))
    second = build(second_source, file)
    assert second == first
    assert not any(
        key.endswith((".blk", ".tail", ".index.json", ".permanent.json"))
        for key in second_source.got
    )

    # Editing a non-Python file read by report() invalidates the cache on the very next build.
    label.write_text("Edited")
    third = build(MeasuredStorage(DirectoryStorage(runs)), file)
    assert third.report.labels == {"a": "Edited"}

    # Another experiment writing to the same source does not invalidate this experiment's page.
    with open_run(
        experiment="other",
        run="z",
        clocks=[Clock(name="updates", kind="step", meaning="updates")],
        directory=runs,
    ) as other:
        other.log({"loss": 2.0}, updates=1)
    after_other = MeasuredStorage(DirectoryStorage(runs))
    assert build(after_other, file) == third
    assert not any(
        key.endswith((".blk", ".tail", ".index.json", ".permanent.json")) for key in after_other.got
    )
