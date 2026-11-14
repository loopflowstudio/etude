"""Experiment identity survives shared sources and historical copies of run folders."""

import json
from hashlib import sha256
from pathlib import Path
from shutil import copytree

import pytest
from pydantic import ValidationError

from etude.experiment import observe, state_of
from etude.page import Pages
from etude.records import Continuation, RunRecord, SourceKey
from etude.run import runs, sync
from etude.storage import DirectoryStorage
from etude.train.read import read, read_all, read_batches
from etude.train.records import Step
from etude.train.tracking import track
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
    copytree(source / "one/r", source / "r")
    copytree(source / "r", source / "machines/host/r")
    every = read_all(source)
    assert len(every) == 1
    assert every[0].points("score", "updates").y.tolist() == [10]
    assert len(every[0].attempts) == 1
    assert runs(source) == [("one", "r")]


def test_report_refresh_tracks_same_named_runs_in_each_experiment(tmp_path: Path) -> None:
    source = tmp_path / "runs"
    files = tmp_path / "experiments"
    for experiment in ("one", "two"):
        with track(
            experiment=experiment,
            run="r",
            clocks=[Step("updates", "updates")],
            directory=source / experiment,
        ):
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
    with track(
        resume=True,
        experiment="two",
        run="r",
        clocks=[Step("updates", "updates")],
        directory=source / "two",
    ) as run:
        run.log({"score": 7}, updates=1)
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
    with track(
        experiment="one", run="r", clocks=[Step("updates", "updates")], directory=tmp_path
    ) as run:
        run.log({"score": 10}, updates=1)
    data = read(tmp_path, "two", "r")
    assert data.file is None and data.rows == 0 and data.outcome is None


def test_run_record_reads_historical_identity_and_round_trips_the_exact_config() -> None:
    config = '{ "lr" : 0.1 }'
    historical = json.dumps(
        {
            "schema_version": 1,
            "experiment": "pilot",
            "namespace": "local",
            "declaration": {
                "schema_version": 1,
                "source_key": {"namespace": "local", "kind": "run", "source_id": "r"},
                "clocks": [],
                "job": "digest",
                "seed": 7,
                "config_json": config,
                "config_sha256": sha256(config.encode()).hexdigest(),
            },
        }
    )
    record = RunRecord.model_validate_json(historical)
    assert (record.id, record.source_key.source_id, record.job) == ("pilot/r", "r", "digest")
    assert record.config_json == config
    assert record.config_sha256 == sha256(config.encode()).hexdigest()
    assert RunRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValueError, match="no launcher specification"):
        _ = record.launch.spec


def test_a_run_json_in_out_is_carried_as_data_and_never_discovered_as_a_run(tmp_path: Path) -> None:
    from_json = RunRecord(id="pretend/nested", experiment="pretend").model_dump_json()
    source = tmp_path / "source"
    with track(
        experiment="real", run="r", clocks=[Step("updates", "updates")], directory=source
    ) as live:
        nested = live.out / "model"
        nested.mkdir()
        (nested / "run.json").write_text(from_json)
    target = MeasuredStorage(DirectoryStorage(tmp_path / "target"))
    assert sync(source, storages=[target]) == {}
    assert runs(source) == runs(target) == [("real", "r")]
    assert target.storage.get("real/r/out/model/run.json").decode() == from_json


@pytest.mark.parametrize("launched", [False, True])
def test_new_run_json_contains_only_declared_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launched: bool
) -> None:
    if launched:
        for key, value in {
            "ETUDE_RUN": "baseline-seed-7",
            "ETUDE_EXPERIMENT": "pilot",
            "ETUDE_ATTEMPT": "allocation-1",
            "ETUDE_OUT": str(tmp_path / "pilot/baseline-seed-7/out"),
            "ETUDE_JOB": "digest",
            "ETUDE_SEED": "7",
        }.items():
            monkeypatch.setenv(key, value)
    with track(
        experiment="pilot",
        run="baseline-seed-7",
        job="digest",
        seed=7,
        clocks=[Step("updates", "optimizer updates")],
        config='{ "lr": 0.1 }',
        directory=tmp_path,
    ) as run:
        document = json.loads((run.out.parent / "run.json").read_text())
    assert set(document) == {
        "schema_version",
        "id",
        "experiment",
        "namespace",
        "source_kind",
        "clocks",
        "job",
        "job_name",
        "seed",
        "cohort",
        "kind",
        "config",
        "continues",
        "start",
    }
    assert document["id"] == "pilot/baseline-seed-7"
    assert document["config"] == '{ "lr": 0.1 }'
    record = RunRecord.model_validate(document)
    assert record.submission is None
    assert record.name == "baseline-seed-7"


def test_default_filled_stored_run_json_still_decodes() -> None:
    record = RunRecord.model_validate_json(
        json.dumps(
            {
                "schema_version": 2,
                "id": "pilot/r",
                "experiment": "pilot",
                "config": '{ "lr": 0.1 }',
                "spec": None,
                "pool": None,
                "rent": None,
                "created_at": None,
                "cancel_requested_at": None,
                "version": 1,
                "autorun": False,
                "queued": False,
                "resume_after": 0,
                "params": {},
                "source": None,
                "smoke": {},
            }
        )
    )
    assert record.config_json == '{ "lr": 0.1 }'
    assert record.submission is None
    assert record.name == "r"


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": 1, "declaration": {}},
        {"schema_version": 1, "declaration": {"experiment": "old"}},
        {
            "schema_version": 1,
            "declaration": {},
            "source_key": {"namespace": "local", "kind": "run", "source_id": "r"},
        },
    ],
)
def test_malformed_historical_run_declarations_are_validation_errors(
    body: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="historical run declaration requires"):
        RunRecord.model_validate(body)
