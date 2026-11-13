"""Local recipes keep one identity across planned stops and explicit resumes."""

from __future__ import annotations

import json
import os
import subprocess
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

RECIPE = """from pathlib import Path
from pydantic import BaseModel
from etude.train.records import Step
from etude.train.run import track
from etude.stopping import Stopped

class Config(BaseModel):
    updates: int = 4
    lr: float = 0.1

class Counts(BaseModel):
    games: int = 0

def train(config: Config) -> None:
    with track(clocks=[Step("updates", "updates")], config=config) as run:
        counts = run.resume(extra=Counts())
        for update in run.steps("updates", to=config.updates, every=1):
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
    args = ["run", str(file), "--seed", "7"]
    assert main(args) == 75
    base = tmp_path / "home/track/train"
    root = base / "train-seed-7"
    assert not (root / "out/phase-two").exists()
    first = read(DirectoryStorage(base), "train", "train-seed-7")
    assert first.state is not None and first.state.state == "stopped"
    assert first.points("games", "updates").y.tolist() == [3, 6]
    capsys.readouterr()
    assert main(["run", str(file), "--resume", "train-seed-7", "--json"]) == 0
    output = capsys.readouterr()
    assert "recipe progress" in output.err
    assert json.loads(output.out) == {
        "run": "train-seed-7",
        "experiment": "train",
        "state": "finished",
    }
    assert (root / "out/phase-two").read_text() == "12"
    completed = read(DirectoryStorage(base), "train", "train-seed-7")
    assert completed.points("games", "updates").y.tolist() == [3, 6, 9, 12]
    assert len(completed.attempts) == 2
    outcome = (root / "outcome.json").read_bytes()
    assert main(args) == 1
    assert "run_name_taken" in capsys.readouterr().err
    assert main(["run", str(file), "--resume", "train-seed-7"]) == 1
    assert "run_already_finished" in capsys.readouterr().err
    assert main(["run", str(file), "--resume", "train-seed-7", "--config", '{"lr":0.2}']) == 1
    assert "lr was 0.1, is 0.2" in capsys.readouterr().err
    assert main(["run", str(file), "--resume", "train-seed-7", "--seed", "8"]) == 1
    assert "seed was 7, is 8" in capsys.readouterr().err
    assert (root / "outcome.json").read_bytes() == outcome
    assert len(read(DirectoryStorage(base), "train", "train-seed-7").attempts) == 2
    assert main(["run", str(file), "--resume", "train-seed-7", "--until", "updates=6"]) == 0
    extended = read(DirectoryStorage(base), "train", "train-seed-7")
    assert extended.points("games", "updates").y.tolist() == [3, 6, 9, 12, 15, 18]


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
        main(["run", str(file)])
    data = read(DirectoryStorage(tmp_path / "home/track/fail"), "fail", "fail-seed-0")
    assert data.state is not None and data.state.state == "failed"
    assert data.rows == 0


def test_outcome_reader_keeps_historical_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["run", str(file)]) == 75
    base = tmp_path / "home/track/train"
    root = base / "train-seed-0"
    (root / "outcome.json").rename(root / "state.json")
    data = read(DirectoryStorage(base), "train", "train-seed-0")
    assert data.state is not None and data.state.state == "stopped"


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
    assert main(["run", str(file)]) == 75
    root = tmp_path / "home/track/signal_recipe/signal_recipe-seed-0"
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
    assert main(["run", str(file)]) == 75
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
    assert main(["run", str(file), "--resume", "train-seed-0"]) == 0
    data = read(DirectoryStorage(tmp_path / "home/track/train"), "train", "train-seed-0")
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
    assert main(["run", str(file), "--until", "updates=3"]) == 75
    assert main(["run", str(file), "--resume", "train-seed-0"]) == 0
    data = read(DirectoryStorage(tmp_path / "home/track/train"), "train", "train-seed-0")
    assert data.reached["updates"] == 3
    assert {attempt.until for attempt in data.attempts} == {"updates=3.0"}


def test_seconds_deadline_stops_before_the_first_step_when_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("etude.local.time", SimpleNamespace(time=lambda: 0.0))
    file = tmp_path / "train.py"
    file.write_text(RECIPE)
    assert main(["run", str(file), "--seconds", "1"]) == 75
    data = read(DirectoryStorage(tmp_path / "home/track/train"), "train", "train-seed-0")
    assert data.state is not None and data.state.state == "stopped"
    assert data.rows == 0
    assert not (tmp_path / "home/track/train/train-seed-0/out/phase-two").exists()


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
    assert main(["run", str(file), "--experiment", "tiny"]) == 75
    base = tmp_path / "home/track/tiny"
    root = base / "models-seed-0"
    first = read_state(root / "out/state")
    assert first is not None
    weights = {name: (first.path / f"{name}.pt").read_bytes() for name in ("policy", "value")}
    assert main(["run", str(file), "--experiment", "tiny", "--resume", "models-seed-0"]) == 0
    checkpoint = observe(base, "tiny").run("models-seed-0").final
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
