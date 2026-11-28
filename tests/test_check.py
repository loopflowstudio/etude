"""The same declaration failures reach offline checks and launch refusals."""

from pathlib import Path
from textwrap import dedent

import pytest

from etude.cli import main
from etude.launch.database import launcher_file


def _experiment(folder: Path, *, module: str, load: str = "checkpoint") -> Path:
    (folder / f"{module}.py").write_text(
        dedent(f"""\
        from pydantic import BaseModel

        class Config(BaseModel):
            pass

        def train(config: Config) -> None:
            raise AssertionError("validation must not train")

        def play(match, agents):
            raise AssertionError("validation must not play")

        def load({load}):
            raise AssertionError("validation must not load")
        """)
    )
    file = folder / "experiment.py"
    file.write_text(
        dedent(f"""\
        from {module} import Config, train, play, load
        from etude.eval.match import environment
        from etude.eval.records import Benchmark
        from etude.jobs import Agents, EvalJob, TrainingJob
        from etude.report import Report

        def training_jobs():
            return TrainingJob(name="base", train=train, config=Config(), seeds=(1, 1),
                               max_hours=1, repo=None)

        def eval_jobs(state):
            return EvalJob(tournament=Benchmark(name="panel", environment=environment("check")),
                           play=play, agents=Agents(load=load), max_hours=1, repo=None)

        def report():
            return Report()
        """)
    )
    return file


def test_check_reports_independent_errors_and_the_runtime_uses_the_same_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    file = _experiment(tmp_path, module="check_game")
    game = tmp_path / "check_game.py"
    launch = ["launch", str(file), "--source", str(tmp_path / "stored"), "--quote"]

    assert main(["check", str(file)]) == 1
    issues = capsys.readouterr().out
    assert f"{file}:8: error: seeds must be nonempty and distinct" in issues
    assert "  fix: seeds=(1, 2, 3)" in issues
    assert f"{game}:9: error: play must be an async def function" in issues
    assert "  fix: async def play(" in issues
    assert "2 errors" in issues
    assert main(launch) == 1
    assert "  fix: seeds=(1, 2, 3)" in capsys.readouterr().err

    file.write_text(file.read_text().replace("seeds=(1, 1)", "seeds=(1, 2, 3)"))
    assert main(launch) == 1
    assert f"{game}:9: play must be an async def function" in capsys.readouterr().err
    # A fresh module matches running the command in a new interpreter.
    fixed = tmp_path / "check_game_fixed.py"
    fixed.write_text(game.read_text().replace("def play(", "async def play("))
    file.write_text(file.read_text().replace("from check_game ", "from check_game_fixed "))
    assert main(["check", str(file)]) == 0
    assert "no stored runs" in capsys.readouterr().out
    assert not launcher_file(home).exists()


def test_check_and_eval_locate_a_bad_loader_and_check_reports_bad_budget_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = _experiment(tmp_path, module="check_loader", load="checkpoint, unused")
    file.write_text(
        file.read_text()
        .replace("seeds=(1, 1)", "seeds=(0,)")
        .replace("max_hours=1, repo=None)", "max_hours=0, repo=None)", 1)
        .replace("return Report()", "return {}")
    )
    assert main(["check", str(file)]) == 1
    issues = capsys.readouterr().out
    assert f"{file}:8: error: a job needs a command and hours" in issues
    assert "  fix: max_hours=1" in issues
    assert f"{tmp_path / 'check_loader.py'}:12: error: load must take one checkpoint" in issues
    assert "  fix: def load(checkpoint):" in issues
    assert f"{file}:15: error: experiment_report_returns_a_report" in issues
    assert "3 errors" in issues
    assert main(["eval", str(file), "--source", str(tmp_path / "stored")]) == 1
    assert "  fix: def load(checkpoint):" in capsys.readouterr().err
    assert main(["launch", str(file), "--source", str(tmp_path / "stored"), "--quote"]) == 1
    assert "  fix: max_hours=1" in capsys.readouterr().err


def test_check_distinguishes_unchecked_input_syntax_and_local_recipes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "recipe.py"
    assert main(["check", str(file)]) == 2
    assert "expected an existing Python" in capsys.readouterr().err
    file.write_text("# experiment\ndef train(\n")
    assert main(["check", str(file)]) == 1
    assert f"{file}:2: error: SyntaxError:" in capsys.readouterr().out
    file.write_text(
        "from pydantic import BaseModel\n"
        "class Config(BaseModel):\n"
        "    pass\n"
        "def train(config: Config) -> None:\n"
        "    raise AssertionError('cannot execute training in check')\n"
    )
    assert main(["check", str(file), "--typo"]) == 2
    assert "no training_jobs() options" in capsys.readouterr().err
    assert main(["check", str(file)]) == 0
    assert "local recipe" in capsys.readouterr().out


def test_check_formats_a_constructor_type_error_without_the_raw_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _experiment(tmp_path, module="check_constructor")
    file.write_text(
        file.read_text()
        .replace("seeds=(1, 1)", "seeds=(1,)")
        .replace("max_hours=1", 'max_hours="wrong"', 1)
    )
    assert main(["check", str(file)]) == 1
    issues = capsys.readouterr().out
    assert f"{file}:8: error: TrainingJob.max_hours: Input should be a valid number" in issues
    assert "wrong" not in issues
    assert "pydantic.dev" not in issues
