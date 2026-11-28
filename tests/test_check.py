"""Exercise offline checks and the guide through the CLI."""

import json
import os
import re
from hashlib import sha256
from pathlib import Path
from textwrap import dedent

import pytest
from typer.main import get_command_name

from etude.checkpoints import CheckpointRecord
from etude.cli import app, main
from etude.controller import check
from etude.eval.match import environment
from etude.eval.records import Code, RoundRobin
from etude.eval.tournament import evaluating
from etude.launch import client, server
from etude.launch.database import launcher_file
from etude.records import ArtifactRef, Clock, RunRecord
from etude.storage import DirectoryStorage
from tests.run.storage import MeasuredStorage


def test_guide_lists_registered_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["guide"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    guide, commands = captured.out.split("\n## Commands\n\n")
    rows = re.findall(r"(?m)^- `etude (.*?)` \((read|ask|wait)\) \S.*$", commands)
    assert len(rows) == len(commands.splitlines())
    assert len(rows) == len(dict(rows))

    groups = {"": app}
    for group in app.registered_groups:
        assert isinstance(group.name, str) and group.typer_instance is not None
        groups[f"{group.name} "] = group.typer_instance
    expected: dict[str, object] = {}
    for prefix, group_app in groups.items():
        for command in group_app.registered_commands:
            assert command.callback is not None
            if not command.hidden:
                name = command.name or get_command_name(command.callback.__name__)
                expected[prefix + name] = getattr(command.callback, "command_kind", None)
        if (callback := group_app.registered_callback) is not None:
            assert callback.callback is not None
            expected[prefix.strip()] = getattr(callback.callback, "command_kind", None)
    assert dict(rows) == expected

    references = re.findall(
        r"(?m)(?:^|`)(?:uv run )?etude ([a-z][a-z-]*(?: [a-z][a-z-]*(?=[ `]|$))*)",
        guide,
    )
    assert set(references) <= set(expected)
    assert "`etude server` (read) Show which machine holds the database" in commands


def test_guide_example_checks_at_first_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["guide"]) == 0
    (example,) = re.findall(r"```python\n(.*?)```", capsys.readouterr().out, re.DOTALL)
    (tmp_path / ".git").mkdir()
    file = tmp_path / "experiment.py"
    file.write_text(example)

    assert main(["check", str(file), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["issues"] == []
    assert [run["address"] for run in result["training"]] == [
        "experiment/coin@0",
        "experiment/coin@1",
    ]
    (panel,) = result["evals"]
    assert (panel["name"], panel["checkpoints"], panel["matches"]) == ("prediction", 0, 0)


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
    assert main(["check", str(file), "--format", "json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["issues"][0] == {
        "severity": "error",
        "file": str(file),
        "line": 8,
        "message": "seeds must be nonempty and distinct",
        "fix": "seeds=(1, 2, 3)",
    }
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
    assert f"{file}:8: error: a job's max_hours must be finite and more than zero" in issues
    assert "  fix: max_hours=1" in issues
    assert f"{tmp_path / 'check_loader.py'}:12: error: load must take one checkpoint" in issues
    assert "  fix: def load(checkpoint):" in issues
    assert f"{file}:15: error: experiment_report_returns_a_report: report() returned dict" in issues
    assert "  fix: return Report()" in issues
    assert "3 errors" in issues
    assert main(["eval", str(file), "--source", str(tmp_path / "stored")]) == 1
    assert "  fix: def load(checkpoint):" in capsys.readouterr().err
    assert main(["launch", str(file), "--source", str(tmp_path / "stored"), "--quote"]) == 1
    assert "  fix: max_hours=1" in capsys.readouterr().err


def test_check_distinguishes_unchecked_input_syntax_and_local_recipes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "recipe.py"
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
    assert main(["check", str(file), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "local_recipe"


def test_check_and_launch_place_a_file_without_declarations_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "notes.py"
    file.write_text("x = 1\n")
    fix = "  fix: def training_jobs():\n           return TrainingJob(...)\n"
    assert main(["check", str(file)]) == 1
    assert f"{file}:1: error: define training_jobs() or eval_jobs(state)\n{fix}" in (
        capsys.readouterr().out
    )
    assert main(["launch", str(file), "--pool", "local"]) == 1
    assert f"etude: {file}:1: define training_jobs() or eval_jobs(state)\n{fix}" in (
        capsys.readouterr().err
    )


def test_check_reports_a_report_only_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "notes.py"
    file.write_text(
        'from etude.report import Report\ntrain = "base@0"\ndef report(): return Report()\n'
    )
    assert main(["check", str(file)]) == 0
    assert f"ok, report only: etude report {file}; nothing to launch" in capsys.readouterr().out
    assert main(["check", str(file), "--format", "json"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["kind"] == "report_only"
    assert checked["training"] == checked["evals"] == []


def test_report_refusals_describe_the_authored_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "notes.py"
    for source, diagnostic, fix in (
        ("report = 42", "experiment_report_is_not_a_function: report has type int", "def report()"),
        (
            "def report(): return {}",
            "experiment_report_returns_a_report: report() returned dict",
            "return Report()",
        ),
    ):
        file.write_text(source + "\n")
        assert main(["check", str(file)]) == 1
        assert f"error: {diagnostic}\n  fix: {fix}" in capsys.readouterr().out
        assert main(["report", str(file), "--source", str(tmp_path / "runs")]) == 1
        assert f"{diagnostic}\n  fix: {fix}" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["check", "launch", "queue", "smoke"])
def test_experiment_commands_refuse_unused_file_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    file = tmp_path / "eval_only.py"
    assert main([command]) == 2
    assert "a Python experiment FILE is required" in capsys.readouterr().err
    file.write_text("def eval_jobs(state): raise AssertionError('must refuse the flag first')\n")
    assert main([command, str(file), "--typo"]) == 2
    captured = capsys.readouterr()
    assert "--typo" in captured.err
    assert "file has no training_jobs() options" in captured.err
    assert captured.out == ""
    assert not launcher_file(home).exists()


@pytest.mark.parametrize("command", ["check", "launch", "queue", "smoke", "train", "eval"])
def test_experiment_commands_refuse_unusable_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    non_python = tmp_path / "experiment.txt"
    non_python.touch()
    for file in (tmp_path / "missing.py", non_python):
        assert main([command, str(file)]) == 2
        captured = capsys.readouterr()
        assert captured.err.startswith("etude: ")
        assert f"{file}: expected an existing Python" in captured.err
        assert captured.out == ""


def test_report_names_the_unusable_experiment_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    non_python = tmp_path / "experiment.txt"
    non_python.touch()
    for file, code in (
        (tmp_path / "missing.py", "experiment_file_not_found"),
        (non_python, "experiment_file_must_be_python"),
    ):
        assert main(["report", str(file), "--source", str(tmp_path / "runs")]) == 1
        assert f"etude: {code}: {file}" in capsys.readouterr().err


def test_smoke_refuses_training_flags_without_the_train_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "exp.py"
    file.write_text("def training_jobs(width: int = 64): raise AssertionError('must not train')\n")
    assert main(["smoke", str(file), "--step", "report", "--width", "32"]) == 2
    assert "this invocation skips training_jobs()" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["training_jobs", "train"])
def test_commands_refuse_non_callable_training_declarations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    file = tmp_path / "exp.py"
    file.write_text(f"{name} = 42\n")
    for command in ("check", "train", "smoke", "launch"):
        assert main([command, str(file)]) == 1
        captured = capsys.readouterr()
        output = captured.out if command == "check" else captured.err
        assert f"{file.name}: {name} must be a function" in output
        assert f"fix: def {name}(" in output


@pytest.mark.parametrize(
    ("module", "binding"),
    [
        ("check_train_helper", "def train(step, model): pass"),
        (
            "check_train_record",
            'train = TrainingJob(name="base", train=run, config=Config(), repo=None)',
        ),
    ],
)
def test_training_jobs_owns_training_even_when_train_is_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], module: str, binding: str
) -> None:
    (tmp_path / f"{module}.py").write_text(
        dedent("""\
        from pydantic import BaseModel

        class Config(BaseModel):
            pass

        def run(config: Config) -> None:
            raise AssertionError("planning must not run training")
        """)
    )
    file = tmp_path / "experiment.py"
    file.write_text(
        dedent(f"""\
        from {module} import Config, run
        from etude.jobs import TrainingJob

        def training_jobs():
            return TrainingJob(name="base", train=run, config=Config(), max_hours=1, repo=None)
        """)
        + binding
        + "\n"
    )
    assert main(["check", str(file)]) == 0
    assert "experiment/base@0" in capsys.readouterr().out
    assert main(["launch", str(file), "--quote", "--source", str(tmp_path / "runs")]) == 0
    assert "experiment/base@0  new, would" in capsys.readouterr().out


def test_eval_only_file_can_bind_train_as_a_run_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "eval_only.py"
    file.write_text('train = "base@0"\ndef eval_jobs(state): print(train); return []\n')
    assert main(["check", str(file)]) == 0
    assert "base@0" in capsys.readouterr().out.splitlines()
    assert main(["eval", str(file), "--source", str(tmp_path / "runs")]) == 0
    assert "base@0" in capsys.readouterr().out.splitlines()


def test_check_and_smoke_point_into_the_experiments_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").touch()
    (tmp_path / "experiments").mkdir()
    modules = tmp_path / "src"
    modules.mkdir()
    monkeypatch.syspath_prepend(str(modules))
    (modules / "check_remote_declaration.py").write_text(
        "def raise_game_error():\n"
        "    error = RuntimeError('bad game settings')\n"
        "    raise error from error\n"
    )
    file = tmp_path / "experiments" / "exp.py"
    file.write_text(
        "from check_remote_declaration import raise_game_error\n"
        "def eval_jobs(state): raise_game_error()\n"
    )
    assert main(["check", str(file)]) == 1
    assert (
        "src/check_remote_declaration.py:3: error: RuntimeError: bad game settings"
        in capsys.readouterr().out
    )
    assert main(["smoke", str(file), "--step", "eval", "--source", str(tmp_path / "runs")]) == 1
    error = capsys.readouterr().err
    assert error.startswith("etude: src/check_remote_declaration.py:3: smoke eval: failed")
    assert "(RuntimeError: bad game settings)" in error


def test_check_and_launch_require_a_checkout_for_eval_only_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    file = tmp_path / "code_only.py"
    file.write_text(
        dedent("""\
        from etude.eval.match import environment
        from etude.eval.records import Code, RoundRobin
        from etude.jobs import EvalJob

        async def play(match, agents):
            raise AssertionError("must not play")

        def eval_jobs(state):
            tournament = RoundRobin(
                name="panel", environment=environment("check"), config={}, games=1,
            )
            return EvalJob(tournament=tournament, play=play, max_hours=1,
                           enter=(Code(name="a"), Code(name="b")))
        """)
    )
    location = f"{file}:8:"
    message = f"{tmp_path} is in no git checkout"
    fix = "fix: put the experiment in a Git checkout or set repo= to its checkout"
    assert main(["check", str(file)]) == 1
    assert f"{location} error: {message}\n  {fix}" in capsys.readouterr().out
    assert main(["launch", str(file), "--source", str(tmp_path / "runs"), "--quote"]) == 1
    assert capsys.readouterr().err == f"etude: {location} {message}\n  {fix}\n"


def test_smoke_locates_errors_when_lowering_an_eval_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "unlabelled.py"
    file.write_text(
        dedent(f"""\
        from etude.checkpoints import CheckpointRecord
        from etude.eval.match import environment
        from etude.eval.records import Benchmark
        from etude.jobs import EvalJob
        from etude.records import ArtifactRef

        async def play(match, agents):
            raise AssertionError("must not play")

        def eval_jobs(state):
            checkpoint = CheckpointRecord(
                run="unlabelled", ordinal=1, at={{}},
                artifact=ArtifactRef(uri={(tmp_path / "weights").as_uri()!r},
                                     sha256="a" * 64, size_bytes=1),
            )
            return EvalJob(tournament=Benchmark(name="panel", environment=environment("check")),
                           checkpoints=(checkpoint,), play=play, max_hours=1, repo=None)
        """)
    )
    assert main(["smoke", str(file), "--step", "eval"]) == 1
    assert capsys.readouterr().err == (
        f"etude: {file}:10: checkpoint_names_no_experiment: Use checkpoints from a named experiment\n"
        '  fix: state.run("base@0").checkpoints\n'
    )


def test_check_formats_a_constructor_type_error_without_the_raw_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    (tmp_path / ".git").touch()
    site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "check_constructor_dependency.py").write_text(
        "from etude.jobs import TrainingJob\n"
        "def make_job(**kwargs):\n"
        "    return TrainingJob(**kwargs)\n"
    )
    monkeypatch.syspath_prepend(str(site))
    file = _experiment(tmp_path, module="check_constructor")
    source = "from check_constructor_dependency import make_job\n" + file.read_text().replace(
        "return TrainingJob(", "return make_job("
    )
    for text, message in (
        (source, "seeds must be nonempty and distinct"),
        (
            source.replace("seeds=(1, 1)", "seeds=(1,)").replace(
                "max_hours=1", 'max_hours="wrong"', 1
            ),
            "TrainingJob.max_hours: Input should be a valid number",
        ),
    ):
        file.write_text(text)
        assert main(["check", str(file)]) == 1
        issues = capsys.readouterr().out
        assert f"{file}:9: error: {message}" in issues
        assert "wrong" not in issues and "pydantic.dev" not in issues
        assert main(["launch", str(file), "--source", str(tmp_path / "runs"), "--quote"]) == 1
        assert f"{file}:9: {message}" in capsys.readouterr().err


def test_check_offers_named_fields_only_for_positional_record_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path / "home"))
    (tmp_path / ".git").touch()
    file = tmp_path / "positional.py"
    for call, fix in (
        (
            'TrainingJob("base")',
            "  fix: pass fields by name, such as TrainingJob(name='base', ...)",
        ),
        ("helper(1, 2)", None),
    ):
        file.write_text(
            "from etude.jobs import TrainingJob\n"
            "def helper(x):\n"
            "    return x\n"
            "def training_jobs():\n"
            f"    return {call}\n"
        )
        assert main(["check", str(file)]) == 1
        output = capsys.readouterr().out
        assert f"{file}:5: error: TypeError: " in output
        assert (fix in output) if fix else ("fix:" not in output)


def test_check_validates_train_alongside_eval_and_accepts_required_config_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").touch()
    file = tmp_path / "hybrid.py"
    source = dedent("""\
        from pydantic import BaseModel
        from etude.eval.match import environment
        from etude.eval.records import Benchmark
        from etude.jobs import EvalJob

        class Config(BaseModel):
            width: int

        def train(config: Config) -> None:
            raise AssertionError("must not train")

        async def play(match, agents):
            raise AssertionError("must not play")

        def eval_jobs(state):
            return EvalJob(tournament=Benchmark(name="panel", environment=environment("check")),
                           play=play, max_hours=1)
        """)
    file.write_text(source.replace("config: Config", "config: int"))
    assert main(["check", str(file)]) == 1
    assert (
        f"{file}:9: error: train's parameter 'config' must be annotated" in capsys.readouterr().out
    )
    file.write_text(source)
    assert main(["check", str(file)]) == 0
    output = capsys.readouterr().out
    assert "panel  benchmark  0 checkpoints  0 matches" in output
    assert "nothing to launch" in output.splitlines()
    assert "next:" not in output
    runs = tmp_path / "runs"
    runs.mkdir()
    assert main(["check", str(file), "--source", str(runs)]) == 0
    assert "nothing left to launch" in capsys.readouterr().out.splitlines()


def test_check_formats_config_round_trip_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _experiment(tmp_path, module="check_excluded_config")
    module = tmp_path / "check_excluded_config.py"
    module.write_text(
        module.read_text()
        .replace("import BaseModel", "import BaseModel, Field")
        .replace("    pass", "    width: int = Field(exclude=True)")
        .replace("def play", "async def play")
    )
    file.write_text(
        file.read_text()
        .replace("seeds=(1, 1)", "seeds=(1,)")
        .replace("Config()", "Config(width=8)")
    )
    assert main(["check", str(file)]) == 1
    output = capsys.readouterr().out
    assert (
        "the Config config would not validate on the machine: Config.width: Field required"
        in output
    )
    assert "pydantic.dev" not in output


def _import_experiment(
    folder: Path, *, module: str, agents: str, build_import: str
) -> tuple[Path, Path, Path]:
    observations = folder / "game-imports"
    build = folder / f"{module}_build.py"
    build.write_text(
        dedent(f"""\
        import os
        if os.getpid() != {os.getpid()}:
            {build_import}

        def build(player, leaves):
            raise AssertionError("check must not build agents")
        """)
    )
    (folder / f"{module}.py").write_text(
        dedent(f"""\
        import os
        from pathlib import Path

        if os.getpid() != {os.getpid()}:
            print("output from importing a game")
            with Path({str(observations)!r}).open("a") as log:
                log.write(os.environ["OMP_NUM_THREADS"] + "\\n")

        async def play(match, agents):
            raise AssertionError("check must not play")
        """)
    )
    (folder / f"{module}_load.py").write_text(
        dedent(f"""\
        import os
        if os.getpid() != {os.getpid()}:
            raise AssertionError("check must not import the loader in the game process")

        def load(checkpoint):
            raise AssertionError("check must not load checkpoints")
        """)
    )
    file = folder / "import_experiment.py"
    file.write_text(
        dedent(f"""\
        from {module} import play
        from {module}_build import build
        from {module}_load import load
        from etude.eval.match import environment
        from etude.eval.records import Benchmark
        from etude.jobs import Agents, EvalJob

        def eval_jobs(state):
            return [
                EvalJob(
                    tournament=Benchmark(name=name, environment=environment("check")),
                    play=play, agents=Agents({agents}), max_hours=1, repo=None,
                )
                for name in ("first", "second")
            ]
        """)
    )
    return file, build, observations


def test_check_warns_once_at_the_import_in_shared_game_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "torch.py").write_text("# Local stand-in for a model library.\n")
    file, build, observations = _import_experiment(
        tmp_path,
        module="check_warning_game",
        agents="load=load, build=build",
        build_import="import torch",
    )
    build.write_text(
        "import sys\n"
        + build.read_text().replace(
            "    import torch",
            '    assert "typer" not in sys.modules\n'
            '    assert "etude.controller.diagnostics" not in sys.modules\n'
            "    import torch",
        )
    )
    message = "game module check_warning_game: imported torch via check_warning_game_build"
    fix = "define your game function in a lightweight module and move the import to load's module"

    assert main(["check", str(file), "--format", "json"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["issues"] == [
        {"severity": "warning", "file": str(build), "line": 6, "message": message, "fix": fix}
    ]
    assert checked["ok"] is True
    assert observations.read_text() == "1\n"

    assert main(["check", str(file)]) == 0
    output = capsys.readouterr().out
    assert f"{build}:6: warning: {message}\n  fix: {fix}\n" in output
    assert f"{file}: 1 warning\n" in output


@pytest.mark.parametrize(
    ("module", "agents"),
    [
        ("check_code_only_game", "build=build"),
        ("check_cpu_only_game", 'load=load, build=build, devices=("cpu",)'),
    ],
)
def test_check_skips_import_probes_for_direct_games(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], module: str, agents: str
) -> None:
    file, _, observations = _import_experiment(
        tmp_path, module=module, agents=agents, build_import='raise RuntimeError("probe ran")'
    )
    assert main(["check", str(file), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == []
    assert not observations.exists()


def test_check_locates_import_failures_in_the_game_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    modules = tmp_path / "src"
    modules.mkdir()
    monkeypatch.syspath_prepend(str(modules))
    accidental = tmp_path / "external"
    accidental.mkdir()
    (accidental / "check_accidental_dependency.py").touch()
    monkeypatch.syspath_prepend(str(accidental))
    file, build, _ = _import_experiment(
        modules,
        module="check_broken_game_import",
        agents="load=load, build=build",
        build_import="import check_accidental_dependency",
    )
    experiments = tmp_path / "experiments"
    experiments.mkdir()
    file.write_text(file.read_text().replace(", repo=None", ""))
    file = file.rename(experiments / file.name)
    assert main(["check", str(file), "--format", "json"]) == 1
    checked = json.loads(capsys.readouterr().out)
    assert checked["ok"] is False
    assert checked["issues"] == [
        {
            "severity": "error",
            "file": str(build),
            "line": 3,
            "message": "ModuleNotFoundError: No module named 'check_accidental_dependency'",
            "fix": None,
        }
    ]


def _planned_experiment(tmp_path: Path) -> Path:
    (tmp_path / "check_planned_game.py").write_text(
        dedent("""\
        from pydantic import BaseModel

        class Config(BaseModel):
            pass

        def train(config: Config) -> None:
            raise AssertionError("check must not train")

        async def play(match, agents):
            raise AssertionError("check must not play")
        """)
    )
    file = tmp_path / "plan.py"
    file.write_text(
        dedent("""\
        from decimal import Decimal

        from check_planned_game import Config, play, train
        from etude.eval.match import environment
        from etude.eval.records import Aligned, Benchmark, CheckpointSlot, Code, RoundRobin, Seating
        from etude.jobs import EvalJob, TrainingJob

        print("reading declarations")

        def training_jobs(state, hours: float = 2):
            return [
                TrainingJob(name="base", train=train, config=Config(), seeds=(1, 2),
                            max_hours=hours, max_usd=Decimal("5.00"), repo=None),
                TrainingJob(name="compare", train=train, config=Config(), seeds=(3,),
                            max_hours=1, repo=None),
            ]

        def eval_jobs(state):
            assert sorted(run.seed for run in state.runs) == [1, 2, 3]
            assert state.run("base@2").state == "queued"
            held = (CheckpointSlot(), Aligned(experiment="baseline", job="bot", clock="steps"))
            return [
                EvalJob(
                    tournament=RoundRobin(name="ladder", environment=environment("check"),
                                          games=2, config={}),
                    enter=tuple(Code(name=name) for name in ("a", "b", "c", "b")),
                    play=play, max_hours=1, repo=None,
                ),
                EvalJob(
                    tournament=Benchmark(
                        name="panel", environment=environment("check"),
                        seatings=(Seating(name="baseline", players=held, configs=({},), games=3),),
                    ),
                    checkpoints=[c for run in state.runs for c in run.checkpoints],
                    play=play, max_hours=1, max_usd=Decimal("2.00"), repo=None,
                ),
            ]
        """)
    )
    return file


def test_check_summarizes_a_first_launch_with_job_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _planned_experiment(tmp_path)
    args = ["check", str(file), "--experiment", "lab", "--hours", "1.5"]
    assert main([*args, "--format", "json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "reading declarations\n"
    result = json.loads(captured.out)
    assert (result["file"], result["experiment"], result["source"]) == (str(file), "lab", None)
    assert result["ok"] is True and result["issues"] == []
    assert result["training"] == [
        {"address": "lab/base@1", "max_hours": 1.5, "max_usd": "5.00"},
        {"address": "lab/base@2", "max_hours": 1.5, "max_usd": "5.00"},
        {"address": "lab/compare@3", "max_hours": 1.0, "max_usd": None},
    ]
    ladder, panel = result["evals"]
    assert (ladder["kind"], ladder["entrants"], ladder["matches"], ladder["games"]) == (
        "round_robin",
        3,
        6,
        6,
    )
    assert (panel["kind"], panel["checkpoints"], panel["matches"], panel["max_usd"]) == (
        "benchmark",
        0,
        0,
        "2.00",
    )
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "training  3 runs, up to 4 machine-hours, $10.00 across 2 runs with max_usd" in output
    assert "lab/compare@3  max_hours 1  no max_usd: owned pools only" in output
    assert "ladder  round robin  3 entrants  6 matches  6 games" in output
    assert "panel  benchmark  0 checkpoints  0 matches  (none stored yet)" in output
    assert f"next: etude launch {file} --quote --hours 1.5 --experiment lab" in output


def test_check_and_launch_name_the_job_missing_max_hours(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = _planned_experiment(tmp_path)
    file.write_text(file.read_text().replace("max_hours=1, repo=None),", "repo=None),", 1))
    message = "job 'compare' needs max_hours"
    assert main(["check", str(file)]) == 1
    assert f"error: {message}" in capsys.readouterr().out
    assert main(["launch", str(file), "--quote", "--source", str(tmp_path / "runs")]) == 1
    assert message in capsys.readouterr().err


def test_check_help_shows_declaration_flags_without_running_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "exp.py"
    file.write_text(
        "def training_jobs(width: int = 64):\n    raise AssertionError('help ran the declaration')\n"
    )
    assert main(["check", "--help"]) == 0
    assert "--source" in capsys.readouterr().out
    for command in ("check", "launch", "queue", "smoke"):
        assert main([command, str(file), "--help"]) == 0
        captured = capsys.readouterr()
        assert "--width" in captured.out
        assert captured.err == ""
        missing = tmp_path / "missing.py"
        assert main([command, str(missing), "--help"]) == 2
        captured = capsys.readouterr()
        assert f"{missing}: expected an existing Python" in captured.err
        assert captured.out == ""


def _save_checkpoint(source: DirectoryStorage, experiment: str, job: str, seed: int) -> None:
    address = f"{experiment}@{seed}"
    record = RunRecord(
        id=address,
        experiment=experiment,
        job_name=job,
        seed=seed,
        clocks=(Clock(name="steps", kind="step", meaning="training steps"),),
    )
    source.put(f"{address}/run.json", record.model_dump_json().encode())
    model = b"saved"
    key = f"{address}/checkpoints/model.bin"
    saved = CheckpointRecord(
        ordinal=1,
        at={"steps": 50},
        artifact=ArtifactRef(
            uri=source.uri(key), sha256=sha256(model).hexdigest(), size_bytes=len(model)
        ),
    )
    source.put(key, model)
    source.put(f"{address}/checkpoints/model.json", saved.model_dump_json().encode())


def test_check_source_resolves_stored_aliases_and_cross_experiment_seats_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ETUDE_HOME", str(home))
    monkeypatch.delenv("ETUDE_SERVER_LOCAL", raising=False)
    home.mkdir()
    (home / "server").write_text("unreachable.invalid")

    def unreachable(*args: object, **kwargs: object) -> None:
        pytest.fail("offline check reached the launcher")

    monkeypatch.setattr(server, "forward", unreachable)
    monkeypatch.setattr(client, "standing", unreachable)
    file = _planned_experiment(tmp_path)
    directory = tmp_path / "runs"
    source = DirectoryStorage(directory)
    _save_checkpoint(source, "plan", "base", 1)
    _save_checkpoint(source, "baseline", "bot", 9)
    ladder: RoundRobin = RoundRobin(
        name="ladder", environment=environment("check"), games=1, config={}
    )
    with evaluating(
        ladder, experiment="plan", enter=(Code(name="a"), Code(name="b")), directory=directory
    ) as active:
        (match,) = active.matches
        active.store(match, {"scores": [{"seat": 0, "score": 1}]})
    measured = MeasuredStorage(source)
    monkeypatch.setattr(check, "DirectoryStorage", lambda root: measured)

    assert main(["check", str(file), "--source", str(directory), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == str(directory)
    assert [run["address"] for run in result["training"]] == ["plan/base@2", "plan/compare@3"]
    ladder_result, panel = result["evals"]
    assert (ladder_result["entrants"], ladder_result["matches"], ladder_result["games"]) == (
        3,
        5,
        5,
    )
    # The three games are divided across two held seeds. Only the first has saved.
    assert (panel["checkpoints"], panel["matches"], panel["games"]) == (1, 1, 2)
    assert (measured.listings, measured.requests, measured.ranges) == (1, 8, 0)
    assert not launcher_file(home).exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--source", "s3://test/runs"], "--quote for bucket storage"),
        (
            ["--source", "check-directory-does-not-exist"],
            "check-directory-does-not-exist: expected an existing directory",
        ),
        (["--format", "yaml"], "Invalid value for"),
        (["--format"], "requires an argument"),
        (["--typo"], "No such option"),
    ],
)
def test_check_rejects_unusable_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
    message: str,
) -> None:
    file = _planned_experiment(tmp_path)
    assert main(["check", str(file), *options]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("etude: ")
    assert message in captured.err
    if options[0] == "--source":
        assert "--source" in captured.err
        assert captured.out == ""
