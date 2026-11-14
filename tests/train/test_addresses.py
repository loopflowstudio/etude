"""Compact run addresses survive selection, storage, delivery and historical reads."""

import json
from pathlib import Path

import pytest

from etude.cli import main
from etude.jobs import checkpoint_inputs_for
from etude.launch.job_file import read_job_file, select_runs
from etude.records import RunRecord
from etude.run import RunFiles, declare, run_directories, runs, sync
from etude.spec import Job, expand_runs
from etude.storage import DirectoryStorage
from etude.train.read import read
from etude.train.records import Step
from etude.train.tracking import track
from tests.run.storage import MeasuredStorage


@pytest.mark.parametrize(
    ("names", "seeds", "expected"),
    [
        (("wide",), (7,), ["sweep"]),
        (("wide",), (0, 7), ["sweep@0", "sweep@7"]),
        (("small", "wide"), (7,), ["sweep/small", "sweep/wide"]),
        (
            ("sweep", "wide"),
            (0, 7),
            ["sweep/sweep@0", "sweep/sweep@7", "sweep/wide@0", "sweep/wide@7"],
        ),
    ],
)
def test_file_and_python_addresses_use_the_full_declaration_before_selection(
    tmp_path: Path, names: tuple[str, ...], seeds: tuple[int, ...], expected: list[str]
) -> None:
    jobs = [
        Job(command="true", max_hours=1, name=name, experiment="sweep", seeds=seeds)
        for name in names
    ]
    file = tmp_path / "sweep.py"
    file.write_text(
        "from etude.spec import Job\ndef runs():\n    return [\n"
        + ",\n".join(f"        Job.model_validate_json({job.model_dump_json()!r})" for job in jobs)
        + "\n    ]\n"
    )
    requests = read_job_file(file)
    assert [run.id for run in requests] == expected
    assert [run.id for run in expand_runs(jobs)] == expected
    assert [run.id for run in select_runs(requests, job=names[-1], seed=seeds[-1])] == [
        expected[-1]
    ]


def test_local_selection_resumes_current_source_and_refuses_changed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "selection.py"
    recipe = """from pathlib import Path
from pydantic import BaseModel
from etude.jobs import training
from etude.train.tracking import track
from etude.train.records import Step
class Config(BaseModel):
    rate: float = 0.1

def train(config: Config) -> None:
    with track(clocks=[Step("updates", "updates")], config=config) as run:
        run.resume(extra=Config())
        for update in run.steps("updates", to=1, save_every=1):
            run.log({"revision": 1})
            run.save()

def runs():
    return [training(train, config=Config(), name=name, seeds=(0, 7),
                     repo=str(Path(__file__).parent), max_hours=1) for name in ("small", "wide")]
"""
    file.write_text(recipe)
    args = ["train", str(file), "--job", "wide", "--seed", "7"]
    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"] == "selection/wide@7"
    source = tmp_path / "home/track"
    assert runs(source) == [("selection", "wide@7")]
    outcome = (source / "selection/wide@7/outcome.json").read_bytes()
    assert main([*args, "--config", '{"rate":0.2}']) == 1
    assert "rate was 0.1, is 0.2; choose --experiment" in capsys.readouterr().err
    assert (source / "selection/wide@7/outcome.json").read_bytes() == outcome
    file.write_text(recipe.replace('"revision": 1', '"revision": 2'))
    assert main([*args, "--resume", "--until", "updates=2"]) == 0
    data = read(source, "selection", "wide@7")
    assert data.points("revision", "updates").y.tolist() == [1, 2]
    assert {writer.run for writer in data.writers} == {"selection/wide@7"}
    assert main(["train", str(file), "--job", "absent"]) == 1
    assert "no runs match" in capsys.readouterr().err


def test_compact_parent_and_nested_runs_are_discovered_and_delivered_once(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    with track(
        experiment="league", run="league", clocks=[Step("updates", "updates")], directory=source
    ) as active:
        active.log({"score": 3}, updates=1)
    declare(experiment="league", run="eval", clocks=[], kind="evaluation", directory=source)
    # A state file resembling a declaration remains the training run's data.
    decoy = source / "league/out/embedded/run.json"
    decoy.parent.mkdir(parents=True)
    decoy.write_text(RunRecord(id="decoy", experiment="league").model_dump_json())
    measured = MeasuredStorage(DirectoryStorage(source))
    assert runs(measured) == [("league", "eval"), ("league", "league")]
    assert (measured.listings, measured.requests) == (1, 2)
    assert set(run_directories(source)) == {source / "league", source / "league/eval"}
    assert sync(source, storages=[DirectoryStorage(target)], stopped=True) == {}
    assert runs(target) == [("league", "eval"), ("league", "league")]
    assert read(target, "league", "league").points("score", "updates").y.tolist() == [3]


def test_delivery_keeps_historical_qualified_addresses(tmp_path: Path) -> None:
    source = tmp_path / "source"
    historical = source / "old/old"
    historical.mkdir(parents=True)
    record = RunRecord(id="old/old", experiment="old")
    (historical / "run.json").write_text(record.model_dump_json())
    target = tmp_path / "target"
    assert sync(source, storages=[DirectoryStorage(target)], stopped=True) == {}
    assert RunFiles(target / "old/old").run_file() == record
    assert runs(target) == [("old", "old")]


def test_queue_status_and_logs_use_the_selected_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "sweep.py"
    file.write_text(
        "from etude.spec import Job\ndef runs():\n"
        "    return [Job(command='true', name=name, seeds=(0, 7), max_hours=1) "
        "for name in ('small', 'wide')]\n"
    )
    assert main(["queue", str(file), "--job", "wide", "--seed", "7"]) == 0
    assert capsys.readouterr().out.startswith("sweep/wide@7 ")
    assert main(["status", "sweep/wide@7"]) == 0
    assert "queued" in capsys.readouterr().out
    assert main(["logs", "--run", "sweep/wide@7"]) == 0
    assert main(["queue", str(file), "--job", "wide", "--seed", "7"]) == 1
    assert "--resume --until" in capsys.readouterr().err
    assert main(["status", "sweep@7"]) == 1
    assert "no run 'sweep@7'" in capsys.readouterr().err


def test_a_job_named_for_its_experiment_keeps_its_qualified_address(tmp_path: Path) -> None:
    with track(
        experiment="league", run="league/league", clocks=["updates"], directory=tmp_path
    ) as active:
        active.log({"score": 1}, updates=1)
        assert active.address == "league/league"
        weights = tmp_path / "weights.pt"
        weights.write_bytes(b"weights")
        active.checkpoint(weights, updates=1)
    checkpoint = read(tmp_path, "league", "league").checkpoints[0]
    assert {item.run for item in checkpoint_inputs_for(checkpoint, "start")} == {"league/league"}
    stored = RunFiles(tmp_path / "league/league").run_file()
    assert stored is not None and stored.id == "league/league"
    assert read(tmp_path, "league", "league").writers[0].run == "league/league"
