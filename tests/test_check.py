"""The same declaration failures reach offline checks and launch refusals."""

import json
from hashlib import sha256
from pathlib import Path
from textwrap import dedent

import pytest

from etude.checkpoints import CheckpointRecord
from etude.cli import main
from etude.controller import check
from etude.launch import client, server
from etude.launch.database import launcher_file
from etude.records import ArtifactRef, Clock, RunRecord
from etude.storage import DirectoryStorage
from tests.run.storage import MeasuredStorage


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
    assert f"next: etude launch {file} --quote --experiment lab" in output


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
    measured = MeasuredStorage(source)
    monkeypatch.setattr(check, "DirectoryStorage", lambda root: measured)

    assert main(["check", str(file), "--source", str(directory), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == str(directory)
    assert [run["address"] for run in result["training"]] == ["plan/base@2", "plan/compare@3"]
    _, panel = result["evals"]
    # The three games are divided across two held seeds. Only the first has saved.
    assert (panel["checkpoints"], panel["matches"], panel["games"]) == (1, 1, 2)
    assert (measured.listings, measured.requests, measured.ranges) == (1, 4, 0)
    assert not launcher_file(home).exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--source", "r2://test/runs"], "--quote for bucket storage"),
        (["--source", "check-directory-does-not-exist"], "--source needs an existing directory"),
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
    assert message in capsys.readouterr().err
