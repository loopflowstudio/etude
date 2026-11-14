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
    error = capsys.readouterr().err
    assert "rate was 0.1, is 0.2" in error
    assert "--resume --until CLOCK=TARGET" in error and "--experiment NAME" in error
    assert main([*args, "--resume", "--config", '{"rate":0.2}']) == 1
    resumed_error = capsys.readouterr().err
    assert "rate was 0.1, is 0.2" in resumed_error
    assert "--resume --until CLOCK=TARGET" in resumed_error and "--experiment NAME" in resumed_error
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


SELECTION = """from pydantic import BaseModel
from etude.jobs import training
from etude.train.tracking import track
from etude.stopping import Stopped
class Config(BaseModel):
    updates: int = 4
    rate: float = 0.1

def train(config: Config) -> None:
    with track(config=config) as run:
        run.resume(extra=Config())
        for update in run.steps("updates", to=config.updates, save_every=1):
            run.log({"score": update})
            run.save()
            if run.seed == 1 and update == 2 and not (run.out / "stopped").exists():
                (run.out / "stopped").touch()
                raise Stopped()

def runs():
    return [training(train, config=Config(), name=name, seeds=(0, 1, 2))
            for name in ("small", "wide")]
"""


def test_selection_preflight_refuses_every_conflict_without_training_any_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "sweep.py"
    file.write_text(SELECTION)
    args = ["train", str(file), "--job", "small"]
    source = tmp_path / "home/track"
    assert main(args) == 75
    capsys.readouterr()
    before = {path: path.read_bytes() for path in source.rglob("*") if path.is_file()}
    # A later missing member refuses before extending either earlier member.
    assert main([*args, "--resume", "--until", "updates=6"]) == 1
    assert "sweep/small@2: run_not_found" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
    # New work preceding a taken address must not run either.
    file.write_text(SELECTION.replace('("small", "wide")', '("wide", "small")'))
    assert main(["train", str(file)]) == 1
    error = capsys.readouterr().err
    assert "sweep/small@0: run_name_taken" in error
    assert "sweep/small@1: run_name_taken" in error
    assert runs(source) == [("sweep", "small@0"), ("sweep", "small@1")]
    assert main([*args, "--seed", "2"]) == 0
    capsys.readouterr()
    # Finished members emit their outcome without running; the stopped member carries on.
    assert main([*args, "--resume", "--json"]) == 0
    output = capsys.readouterr()
    assert "sweep/small@0 finished at updates=4" in output.err
    assert "sweep/small@2 finished at updates=4" in output.err
    assert [json.loads(line) for line in output.out.splitlines()] == [
        {"run": f"sweep/small@{seed}", "experiment": "sweep", "outcome": "finished"}
        for seed in range(3)
    ]
    assert (source / "sweep/small@0/outcome.json").read_bytes() == before[
        source / "sweep/small@0/outcome.json"
    ]
    assert len(read(source, "sweep", "small@0").writers) == 1
    assert read(source, "sweep", "small@1").points("score", "updates").y.tolist() == [1, 2, 3, 4]
    assert main([*args, "--resume", "--until", "updates=6", "--json"]) == 0
    output = capsys.readouterr()
    assert [json.loads(line)["run"] for line in output.out.splitlines()] == [
        "sweep/small@0",
        "sweep/small@1",
        "sweep/small@2",
    ]
    for seed in range(3):
        assert read(source, "sweep", f"small@{seed}").reached["updates"] == 6


@pytest.mark.parametrize("packaged", [False, True])
def test_local_declarations_need_no_hours_or_git_but_submission_and_smoke_do(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    packaged: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "sweep.py"
    file.write_text(SELECTION)
    if packaged:
        package = tmp_path / "local_recipe"
        package.mkdir()
        (package / "__init__.py").touch()
        (package / "loop.py").write_text(SELECTION.split("def runs():")[0])
        file.write_text(
            "from local_recipe.loop import train, Config\nfrom etude.jobs import training\n\n"
            + "def runs():"
            + SELECTION.split("def runs():")[1]
        )
    args = [str(file), "--job", "wide", "--seed", "0"]
    assert main(["train", *args]) == 0
    data = read(tmp_path / "home/track", "sweep", "wide@0")
    assert data.rows == 4
    assert all(writer.commit is None for writer in data.writers)
    for command in ("launch", "queue", "smoke"):
        assert main([command, str(file)]) == 1
        assert "needs max_hours" in capsys.readouterr().err
    file.write_text(SELECTION.replace("name=name, seeds=", "max_hours=1, name=name, seeds="))
    for command in ("launch", "queue", "smoke"):
        assert main([command, str(file)]) == 1
        assert "no git checkout" in capsys.readouterr().err


def test_checkpoint_listing_preserves_compact_and_historical_stored_addresses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"weights")
    for address in ("coin", "old/old", "league/league"):
        experiment = address.split("/")[0]
        with track(experiment=experiment, run=address, directory=tmp_path) as run:
            run.log({"score": 1}, updates=1)
            run.checkpoint(weights, updates=1)
    assert main(["checkpoints", "--source", str(tmp_path)]) == 0
    assert {line.split("\t")[0] for line in capsys.readouterr().out.splitlines()} == {
        "coin",
        "old/old",
        "league/league",
    }
