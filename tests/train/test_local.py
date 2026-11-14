"""Local recipes keep one identity across planned stops and explicit resumes."""

from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from etude.cli import main
from etude.eval.match import environment, evaluate
from etude.eval.records import Code, Inference, Match
from etude.experiment import observe
from etude.storage import DirectoryStorage
from etude.train.read import read
from etude.train.state import read_state
from tests.eval.games import play_checkpoint_folder


@pytest.mark.parametrize(
    ("location", "failure"),
    [
        ("uri", "missing"),
        ("run", "missing"),
        ("uri", "digest"),
        ("run", "digest"),
        ("uri", "length"),
        ("run", "length"),
        ("bucket", "missing"),
    ],
)
@pytest.mark.parametrize("two_jobs", [False, True])
def test_local_input_refusals_leave_the_address_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
    failure: str,
    two_jobs: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    content = b"initial weights"
    source = tmp_path / "home/track/producer/base@0/out/weights.pt"
    source.parent.mkdir(parents=True)
    if failure != "missing":
        source.write_bytes(b"changed weights" if failure == "digest" else b"short")
    # A bucket key must never be mistaken for an existing local path.
    key = "out/weights.pt"
    if location == "bucket":
        Path(key).parent.mkdir()
        Path(key).write_bytes(content)
    file = tmp_path / f"input_{location}_{failure}_{two_jobs}.py"
    recipe = f"""
from pydantic import BaseModel
from etude.jobs import TrainingJob
from etude.spec import Input
from etude.train.tracking import track

class Config(BaseModel):
    needs_input: bool = True

def train(config: Config) -> None:
    with track(config=config) as run:
        if config.needs_input:
            assert run.input("weights").read_bytes() == {content!r}

def training_jobs() -> list[TrainingJob]:
    first = [TrainingJob(name="first", train=train, config=Config(needs_input=False))] if {two_jobs!r} else []
    return [*first, TrainingJob(name="fit", train=train, config=Config(), inputs=(
        Input(name="weights", key={key!r}, sha256={sha256(content).hexdigest()!r},
              size_bytes={len(content)}, LOCATION),))]
"""
    locator = {
        "uri": f"uri={source.as_uri()!r}",
        "run": 'run="producer/base@0"',
        "bucket": "",
    }[location]
    file.write_text(recipe.replace("LOCATION", locator))
    args = ["train", str(file)]
    assert main(args) == 1
    error = capsys.readouterr().err
    assert "weights" in error
    if two_jobs:
        assert "selection_refused" in error
        assert f"{file.stem}/fit@0" in error
    if location == "bucket":
        assert "input_not_located" in error
        assert key in error
        assert "uri" in error and "run" in error
    else:
        assert "input_undelivered" in error
        assert ("file is missing" if failure == "missing" else "bytes differ") in error
        assert source.as_uri() in error
    root = tmp_path / "home/track" / file.stem / "fit@0"
    assert not (root / "run.json").exists()
    assert not (root.parent / "first@0/run.json").exists()
    assert observe(tmp_path / "home/track", file.stem).runs == ()

    source.write_bytes(content)
    if location == "bucket":
        file.write_text(recipe.replace("LOCATION", f"uri={source.as_uri()!r}"))
    assert main(args) == 0
    state = observe(tmp_path / "home/track", file.stem)
    assert len(state.runs) == (2 if two_jobs else 1)
    assert all(run.state == "finished" for run in state.runs)


RECIPE = """from pathlib import Path
from pydantic import BaseModel
from etude.train.records import Step
from etude.train.tracking import track
from etude.stopping import Stopped

class Config(BaseModel):
    updates: int = 4
    lr: float = 0.1

class Counts(BaseModel):
    games: int = 0

def train(config: Config) -> None:
    with track(clocks=[Step("updates", "updates")], config=config) as run:
        counts = run.resume(extra=Counts())
        for update in run.steps("updates", to=config.updates, save_every=1):
            counts.games += 3
            run.log({"games": counts.games, "seed": run.seed})
            run.save()
            if update == 2 and not (run.out / "stopped").exists():
                (run.out / "stopped").touch()
                raise Stopped()
        (run.out / "phase-two").write_text(str(counts.games))
"""


def test_local_stop_resume_and_finished_refusals_preserve_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(
        RECIPE.replace(
            "def train(config: Config) -> None:\n",
            'def train(config: Config) -> None:\n    print("recipe progress")\n',
        )
    )
    args = ["train", str(file)]
    assert main([*args, "--json"]) == 75
    stopped = capsys.readouterr()
    assert "recipe progress" in stopped.err
    assert json.loads(stopped.out) == {
        "run": "train",
        "experiment": "train",
        "outcome": "stopped",
    }
    base = tmp_path / "home/track/train"
    root = base
    assert not (root / "out/phase-two").exists()
    first = read(DirectoryStorage(base.parent), "train", "train")
    assert first.outcome is not None and first.outcome.state == "stopped"
    assert first.points("games", "updates").y.tolist() == [3, 6]
    capsys.readouterr()
    assert main(["train", str(file), "--resume", "--json"]) == 0
    output = capsys.readouterr()
    assert "recipe progress" in output.err
    assert json.loads(output.out) == {
        "run": "train",
        "experiment": "train",
        "outcome": "finished",
    }
    assert (root / "out/phase-two").read_text() == "12"
    completed = read(DirectoryStorage(base.parent), "train", "train")
    assert completed.points("games", "updates").y.tolist() == [3, 6, 9, 12]
    assert len(completed.attempts) == 2
    outcome = (root / "outcome.json").read_bytes()
    assert main(args) == 1
    assert "run_name_taken" in capsys.readouterr().err
    assert main(["train", str(file), "--resume"]) == 1
    assert "finished at updates=4" in capsys.readouterr().err
    for target in ("updates=3", "updates=4"):
        assert main(["train", str(file), "--resume", "--until", target]) == 1
        assert "finished at updates=4" in capsys.readouterr().err
    assert main(["train", str(file), "--resume", "--config", '{"lr":0.2}']) == 1
    assert "lr was 0.1, is 0.2" in capsys.readouterr().err
    assert main(["train", str(file), "--resume", "--seed", "8"]) == 1
    assert "select the file's sole job and seed 0" in capsys.readouterr().err
    assert (root / "outcome.json").read_bytes() == outcome
    assert len(read(DirectoryStorage(base.parent), "train", "train").attempts) == 2
    assert main(["train", str(file), "--resume", "--until", "updates=6"]) == 0
    extended = read(DirectoryStorage(base.parent), "train", "train")
    assert extended.points("games", "updates").y.tolist() == [3, 6, 9, 12, 15, 18]


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("changes", "problems"),
    [
        ('{"lrr":1,"updatess":2}', ["unknown config fields: lrr, updatess"]),
        ('{"lr":0,"updates":"no"}', ["lr: Input should be greater than 0", "updates: "]),
        ("[]", ["expected a JSON object"]),
        ('{"lr":', ["expected a JSON object"]),
    ],
)
def test_local_config_refusals_name_fields_before_calling_the_recipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resume: bool,
    changes: str,
    problems: list[str],
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(
        RECIPE.replace("import BaseModel", "import BaseModel, Field").replace(
            "lr: float = 0.1", "lr: float = Field(default=0.1, gt=0)"
        )
    )
    root = tmp_path / "home/track/train"
    outcome = None
    args = ["train", str(file)]
    if resume:
        assert main(args) == 75
        outcome = (root / "outcome.json").read_bytes()
        args += ["--resume"]
    capsys.readouterr()
    assert main([*args, "--config", changes]) == 1
    error = capsys.readouterr().err
    assert all(problem in error for problem in problems)
    assert "https://errors.pydantic.dev" not in error
    if resume:
        assert (root / "outcome.json").read_bytes() == outcome
        assert len(read(tmp_path / "home/track", "train", "train").writers) == 1
    else:
        assert not root.exists()


def test_local_config_keeps_explicitly_allowed_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(
        RECIPE.replace("class Config(BaseModel):", 'class Config(BaseModel, extra="allow"):')
    )
    assert main(["train", str(file), "--config", '{"custom":7}']) == 75
    assert main(["train", str(file), "--resume"]) == 0
    data = read(tmp_path / "home/track", "train", "train")
    assert data.file is not None and data.file.config_json is not None
    assert json.loads(data.file.config_json)["custom"] == 7


def test_local_experiment_override_and_resume_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    args = ["train", str(file), "--experiment", "lab"]
    assert main([*args, "--json"]) == 75
    assert json.loads(capsys.readouterr().out)["run"] == "lab"
    assert main(args) == 1
    error = capsys.readouterr().err
    assert "use --resume" in error and "--until" in error
    assert main([*args, "--resume", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["run"] == "lab"
    assert main([*args, "--resume", "--until", "updates=6"]) == 0
    data = read(tmp_path / "home/track", "lab", "lab")
    assert len(data.attempts) == 3
    assert data.points("games", "updates").y.tolist() == [3, 6, 9, 12, 15, 18]
    assert not (tmp_path / "home/track/train").exists()


def test_a_new_recipe_failure_before_any_rows_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "fail.py"
    file.write_text(
        RECIPE.replace(
            "        counts = run.resume(extra=Counts())",
            '        raise RuntimeError("learner failed")\n        counts = run.resume(extra=Counts())',
        )
    )
    with pytest.raises(RuntimeError, match="learner failed"):
        main(["train", str(file)])
    data = read(DirectoryStorage(tmp_path / "home/track"), "fail", "fail")
    assert data.outcome is not None and data.outcome.state == "failed"
    assert data.rows == 0


@pytest.mark.parametrize("filename", ["outcome.json", "state.json"])
def test_outcome_reader_keeps_historical_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["train", str(file)]) == 75
    base = tmp_path / "home/track/train"
    root = base
    (root / "outcome.json").rename(root / filename)
    data = read(DirectoryStorage(base.parent), "train", "train")
    assert data.outcome is not None and data.outcome.state == "stopped"
    with pytest.warns(DeprecationWarning, match=r"RunData.state.*RunData.outcome; removed in 0.5"):
        assert data.state is data.outcome
    dumped = data.model_dump(mode="json")
    assert dumped["outcome"]["state"] == "stopped"
    assert "state" not in dumped


def test_signal_stops_saved_and_restores_the_callers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ETUDE_UNTIL_CLOCK", "unrelated")
    file = tmp_path / "signal_recipe.py"
    recipe = RECIPE.replace("from pathlib import Path", "import signal\nfrom pathlib import Path")
    start = recipe.index("            if update == 2")
    end = recipe.index('        (run.out / "phase-two")', start)
    recipe = (
        recipe[:start]
        + "            if update == 2:\n                signal.raise_signal(signal.SIGINT)\n"
        + recipe[end:]
    )
    file.write_text(recipe)
    assert main(["train", str(file)]) == 75
    root = tmp_path / "home/track/signal_recipe"
    assert not (root / "out/phase-two").exists()
    pointer = json.loads((root / "out/state/current.json").read_text())["save"]
    state = json.loads((root / "out/state" / pointer / "state.json").read_text())
    assert state["at"]["updates"] == 2
    assert state["extra"] == {"games": 6}
    assert os.environ["ETUDE_UNTIL_CLOCK"] == "unrelated"


def test_changed_source_resumes_and_each_attempt_records_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    for args in (
        ["init", "-q"],
        ["add", "train.py"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "First recipe",
        ],
    ):
        _git(tmp_path, args)
    first = _git(tmp_path, ["rev-parse", "HEAD"])
    assert main(["train", str(file)]) == 75
    file.write_text(RECIPE + "\n# A source change does not change run identity.\n")
    for args in (
        ["add", "train.py"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "Changed recipe",
        ],
    ):
        _git(tmp_path, args)
    second = _git(tmp_path, ["rev-parse", "HEAD"])
    assert main(["train", str(file), "--resume"]) == 0
    data = read(DirectoryStorage(tmp_path / "home/track"), "train", "train")
    assert [attempt.commit for attempt in data.attempts] == [first, second]
    assert first != second


def _git(folder: Path, args: list[str]) -> str:
    return subprocess.run(
        ["git", "-C", str(folder), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_resumed_attempt_keeps_its_launch_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["train", str(file), "--until", "updates=3"]) == 75
    assert main(["train", str(file), "--resume"]) == 0
    data = read(DirectoryStorage(tmp_path / "home/track"), "train", "train")
    assert data.reached["updates"] == 3
    assert {attempt.until for attempt in data.attempts} == {"updates=3.0"}


def test_seconds_deadline_stops_before_the_first_step_when_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("etude.local.time", SimpleNamespace(time=lambda: 0.0))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["train", str(file), "--seconds", "1"]) == 75
    data = read(DirectoryStorage(tmp_path / "home/track"), "train", "train")
    assert data.outcome is not None and data.outcome.state == "stopped"
    assert data.rows == 0
    assert not (tmp_path / "home/track/train/out/phase-two").exists()


def test_two_model_cli_recipe_resumes_and_its_checkpoint_can_be_seated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("torch")  # optional serializer; no training workload is induced
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "models.py"
    recipe = RECIPE.replace("from pathlib import Path", "import torch\nfrom pathlib import Path")
    recipe = recipe.replace(
        "        counts = run.resume(extra=Counts())",
        "        policy, value = torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)\n"
        "        counts = run.resume(policy=policy, value=value, extra=Counts())",
    )
    recipe = recipe.replace(
        '        (run.out / "phase-two").write_text(str(counts.games))',
        '        (run.out / "phase-two").write_text(str(counts.games))\n'
        "        run.checkpoint(policy=policy, value=value)",
    )
    file.write_text(recipe)
    assert main(["train", str(file), "--experiment", "tiny"]) == 75
    base = tmp_path / "home/track/tiny"
    root = base
    first = read_state(root / "out/state")
    assert first is not None
    weights = {name: (first.path / f"{name}.pt").read_bytes() for name in ("policy", "value")}
    assert main(["train", str(file), "--experiment", "tiny", "--resume"]) == 0
    checkpoint = observe(base.parent, "tiny").run("tiny").final
    assert checkpoint is not None and checkpoint.at["updates"] == 4
    assert {name: (checkpoint.path / f"{name}.pt").read_bytes() for name in weights} == weights
    seated = Match(players=(checkpoint.player(Inference()), Code(name="opponent")), games=1)
    played = evaluate(
        experiment="tiny",
        environment=environment("tiny-game"),
        matches=[seated],
        play=play_checkpoint_folder,
        workers=1,
        run="eval",
        directory=tmp_path / "eval",
    )
    assert played.played == 1


def test_resume_uses_stored_config_when_defaults_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["train", str(file), "--config", '{"lr":0.2}']) == 75
    file.write_text(RECIPE.replace("lr: float = 0.1", "lr: float = 0.9"))
    assert main(["train", str(file), "--resume"]) == 0
    data = read(tmp_path / "home/track", "train", "train")
    assert data.file is not None and data.file.config_json is not None
    assert json.loads(data.file.config_json)["lr"] == 0.2
    assert (
        main(
            [
                "train",
                str(file),
                "--resume",
                "--config",
                "{}",
                "--until",
                "updates=6",
            ]
        )
        == 1
    )
    assert "lr was 0.2, is 0.9" in capsys.readouterr().err
    assert data.outcome is not None and data.outcome.state == "finished"


def test_second_interrupt_exits_a_loop_that_has_not_observed_its_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    recipe = RECIPE.replace("from pathlib import Path", "import signal\nfrom pathlib import Path")
    start = recipe.index("            if update == 2")
    end = recipe.index('        (run.out / "phase-two")', start)
    file.write_text(
        recipe[:start] + "            if update == 2:\n"
        "                signal.raise_signal(signal.SIGINT)\n"
        "                signal.raise_signal(signal.SIGINT)\n" + recipe[end:]
    )
    assert main(["train", str(file)]) != 0
    data = read(tmp_path / "home/track", "train", "train")
    assert data.outcome is not None and data.outcome.state == "failed"
    assert data.reached["updates"] == 2
    assert not (tmp_path / "home/track/train/out/phase-two").exists()


@pytest.mark.parametrize("selected", [False, True])
def test_declared_training_advances_stages_with_inputs_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    selected: bool,
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECIPE_SETTING", "caller")
    file = tmp_path / "stages.py"
    file.write_text("""
import os
from pydantic import BaseModel
from etude.jobs import TrainingJob
from etude.spec import Variable
from etude.train.tracking import track

class Config(BaseModel):
    second: bool = False

def train(config: Config) -> None:
    with track(config=config, clocks=["updates"]) as run:
        if config.second:
            assert os.environ["RECIPE_SETTING"] == "job"
            assert run.input("start").read_bytes() == b"first stage"
        path = run.out / "weights.pt"
        path.write_bytes(b"second stage" if config.second else b"first stage")
        run.checkpoint(path, updates=1)

def training_jobs(state):
    jobs = [TrainingJob(name="first", train=train, config=Config())]
    finished = [r.final for r in state.runs if r.launch == "first" and r.final]
    if finished:
        jobs.append(TrainingJob(name="second", train=train, config=Config(second=True),
            start=finished[0], environment=(Variable(name="RECIPE_SETTING", value="job"),)))
    return jobs
""")
    args = ["train", str(file)]
    assert main(args) == 0
    assert main([*args, *(["--job", "second"] if selected else [])]) == 0
    output = capsys.readouterr().out
    if not selected:
        assert "stages/first@0  finished" in output
    assert main(args) == 0
    state = observe(tmp_path / "home/track", "stages")
    assert {r.launch for r in state.runs} == {"first", "second"}
    assert all(r.state == "finished" for r in state.runs)
    assert os.environ["RECIPE_SETTING"] == "caller"
    assert main([*args, "--job", "first"]) == 1
    assert "run_name_taken" in capsys.readouterr().err
