"""Experiment identity survives shared sources and historical copies of run folders."""

from pathlib import Path
from shutil import copytree

from etude.experiment import observe, state_of
from etude.page import Pages
from etude.records import Continuation, SourceKey
from etude.storage import DirectoryStorage
from etude.tracking.listing import runs
from etude.train.read import read, read_all, read_batches
from etude.train.records import Step
from etude.train.run import track
from tests.experiments import experiment_file
from tests.run.storage import MeasuredStorage


def test_same_run_name_in_two_experiments_has_distinct_rows_and_lineage(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    for experiment, score in (("one", 10), ("two", 20)):
        with track(
            experiment=experiment,
            run="baseline-seed-0",
            clocks=[Step("updates", "updates")],
            directory=source / experiment,
        ) as run:
            run.log({"score": score}, updates=1)
    measured = MeasuredStorage(DirectoryStorage(source))
    every = read_all(measured)
    assert [(data.experiment, data.name) for data in every] == [
        ("one", "baseline-seed-0"),
        ("two", "baseline-seed-0"),
    ]
    # Listing and declaration reads grow with identities, not metric history.
    assert (measured.listings, measured.requests) == (1, 6)
    assert runs(source) == [(data.experiment, data.name) for data in every]
    for experiment, score in (("one", 10), ("two", 20)):
        assert read(source, experiment, "baseline-seed-0").points(
            "score", "updates"
        ).y.tolist() == [score]
        batches = list(
            read_batches(source, experiment, "baseline-seed-0", clock="updates", metrics=["score"])
        )
        assert [value for batch in batches for value in batch.points["score"].y] == [score]
        state = state_of(every, experiment)
        assert state == observe(source, experiment)
        assert len(state.runs) == 1 and state.runs[0].experiment == experiment


def test_copies_of_a_historical_flat_run_still_join_once(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    with track(
        experiment="one", run="r", clocks=[Step("updates", "updates")], directory=source
    ) as run:
        run.log({"score": 10}, updates=1)
    copytree(source / "r", source / "machines/host/r")
    copytree(source / "r", source / "one/r")
    every = read_all(source)
    assert len(every) == 1
    assert every[0].points("score", "updates").y.tolist() == [10]
    assert len(every[0].attempts) == 1
    assert runs(source) == [("one", "r")]


def test_report_refresh_tracks_same_named_runs_in_each_experiment(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    files = tmp_path / "experiments"
    for experiment in ("one", "two"):
        with track(experiment=experiment, run="r", clocks=[], directory=source / experiment):
            pass
        experiment_file(
            files,
            experiment,
            "def report():\n    from etude.report import Report\n    return Report()\n",
        )
    pages = Pages(source)

    def rows() -> dict[str, list[int]]:
        """Each experiment's page: the rows of every run it draws."""
        built = pages.build_each(files)
        return {
            name.stem: [run.rows for run in page.training.runs]
            for name, page in built.items()
            if not isinstance(page, str)
        }

    assert rows() == {"one": [0], "two": [0]}
    with track(resume=True, experiment="two", run="r", clocks=[], directory=source / "two") as run:
        run.log({"score": 7})
    assert rows() == {"one": [0], "two": [1]}


def test_continuation_in_another_experiment_cannot_assign_a_runs_lineage(tmp_path: Path) -> None:
    for experiment in ("one", "two"):
        with track(
            experiment=experiment,
            run="r",
            clocks=[Step("updates", "updates")],
            directory=tmp_path / experiment,
            continues=Continuation(
                parent=SourceKey(namespace="local", kind="run", source_id="parent"),
                clock="updates",
                value=1,
            )
            if experiment == "one"
            else None,
        ):
            pass
    every = read_all(tmp_path)
    assert state_of(every, "one").run("r").line == "parent"
    assert state_of(every, "two").run("r").line == "r"


def test_reading_another_experiment_does_not_return_a_flat_runs_rows(tmp_path: Path) -> None:
    with track(experiment="one", run="r", clocks=[], directory=tmp_path) as run:
        run.log({"score": 10})
    data = read(tmp_path, "two", "r")
    assert data.file is None and data.rows == 0 and data.state is None
