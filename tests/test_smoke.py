"""`etude smoke`: an experiment file's training, evaluation and report run tiny before spending."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from etude.cli import main
from etude.errors import LauncherError
from etude.launch.client import default, queue
from etude.records import Clock
from etude.smoke import smoke_file
from etude.spec import Job, Runtime, SmokeForm
from etude.storage import DirectoryStorage, default_root
from etude.train.tracking import track
from tests.run.storage import MeasuredStorage


@pytest.fixture(autouse=True)
def _command_environment(command_python: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETUDE_TEST_PYTHON", command_python)
    monkeypatch.setattr("tests.launch.tasks.RUNTIME", Runtime(python=command_python))


# Saves one checkpoint at the given number of updates; refuses a real-sized run, so a
# passing smoke shows its smoke form ran rather than the job's own command.
TRAIN = """
import sys
from pathlib import Path

from etude.records import Clock
from etude.train.tracking import track

updates = int(sys.argv[1])
if updates > 10 or "diverge" in sys.argv:
    raise SystemExit(f"{updates} updates diverged")
with track(clocks=[Clock(name="updates", kind="step", meaning="optimizer updates")]) as run:
    if "unsaved" not in sys.argv:
        Path("model.pt").write_bytes(b"weights")
        run.checkpoint(Path("model.pt"), updates=updates)
"""

EXPERIMENT = """
from pathlib import Path

from etude.eval.match import environment
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, Benchmark, Code, Seating
from tests.eval.stored import stored_inference
from etude.jobs import ExperimentState
from etude.jobs import EvalJob, TrainingJob
from tests.launch.tasks import Command, RUNTIME, train_command
from etude.launch.records import Job, SmokeForm
from etude.report import Report
from tests.eval.games import {play}



TRAIN = "{python} " + str(Path(__file__).parent / "train.py")


def training_jobs(seeds: int = 1, tiny: str = "1", bare: bool = False) -> list:
    smoke = Command(command=f"{{TRAIN}} {{tiny}}") if tiny else None
    ppo = TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=TRAIN + " 1000"), repo=None, name="ppo", max_hours=0.1, smoke=smoke).runs(*range(seeds))
    plain = TrainingJob(train=train_command, runtime=RUNTIME, config=Command(command=TRAIN + " 2000"), repo=None, name="plain", max_hours=0.1).runs(0) if bare else []
    return ppo + plain


def eval_jobs(state: ExperimentState) -> Job:
    chosen = [saved for run in state.runs for saved in run.checkpoints]
    anchor = Code(name="heuristic", source=[__file__], config={{"strength": 0}})
    vs = Seating(name="vs heuristic", players=(stored_inference(config={{"search": 0}}), anchor),
                 configs=({{"deal": 0}}, {{"deal": 1}}), games=4)
    ladder = Benchmark(name="ladder", environment=environment("duel"), seatings=(vs,))
    return EvalJob(tournament=ladder, play={play}, checkpoints=chosen, max_hours=0.1,
                      repo=None, runtime=RUNTIME)


def report() -> Report:
    {report}
"""


def experiment(folder: Path, *, play: str = "play", report: str = "return Report()") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "train.py").write_text(TRAIN)
    path = folder / "ppo.py"
    path.write_text(EXPERIMENT.format(python=sys.executable, play=play, report=report))
    return path


def _ids(out: str) -> list[str]:
    """The run ids a submission listed: its lines that are not indented."""
    return [
        line.split()[0]
        for line in out.splitlines()
        if not line.startswith(" ") and "  new," in line
    ]


@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Experiment files put their folder on the import path; each test keeps its own.
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_passing_smoke_queues_and_its_runs_record_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--seeds", "2", "--smoke"]) == 0
    said = capsys.readouterr()
    out, err = said.out, said.err
    # Two seeds of one job are smoked once; the seating plays each deal once.
    assert "smoke train ppo: passed in" in err and "(1 checkpoint)" in err
    assert "smoke eval ladder: passed in" in err and "(2 matches, 1 game each)" in err
    assert "smoke report: passed in" in err
    ids = _ids(out)
    assert len(ids) == 2
    record = default().record(ids[0])
    assert record.launch.smoke.outcome == "passed"
    assert [step.step for step in record.launch.smoke.steps] == ["train", "eval", "report"]
    assert main(["status", ids[0]]) == 0
    assert "  smoke: passed in " in capsys.readouterr().out
    # Nothing the smoke made is in this computer's runs.
    assert not default_root().exists()


@pytest.mark.parametrize(
    ("step", "change", "said"),
    [
        ("train", {}, "smoke train ppo: failed after"),
        # A game that breaks under its second deal only is still caught, and named.
        ("eval", {"play": "play_failing_deal_one"}, "(RuntimeError: deal 1 crashed)"),
        (
            "report",
            {"report": "raise RuntimeError('report broke')"},
            "(RuntimeError: report broke)",
        ),
    ],
)
def test_a_failing_step_submits_nothing_and_is_named(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    step: str,
    change: dict[str, str],
    said: str,
) -> None:
    file = experiment(tmp_path / "lab", **change)
    flags = ["--tiny", "1 diverge"] if step == "train" else []
    assert main(["launch", str(file), *flags, "--smoke"]) == 1
    err = capsys.readouterr().err
    assert said in err and "nothing was submitted" in err
    assert default().runs(done=True) == ()


def test_a_job_without_a_smoke_form_is_reported_and_queues_as_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--tiny", "", "--smoke"]) == 0
    said = capsys.readouterr()
    assert "smoke train ppo: not smoked (no smoke form)" in said.err
    (id,) = _ids(said.out)
    assert default().record(id).launch.smoke.outcome == "not_smoked"


def test_a_job_without_a_smoke_form_beside_one_with_it_is_not_recorded_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--bare", "--smoke"]) == 0
    said = capsys.readouterr()
    assert "smoke train plain: not smoked (no smoke form)" in said.err
    ppo, plain = (default().record(id) for id in _ids(said.out))
    assert (ppo.launch.spec.name, plain.launch.spec.name) == ("ppo", "plain")
    assert ppo.launch.smoke.outcome == "passed"
    assert [(s.step, s.name) for s in ppo.launch.smoke.steps] == [
        ("train", "ppo"),
        ("eval", "ladder"),
        ("report", "report"),
    ]
    assert plain.launch.smoke.outcome == "not_smoked"
    assert [(s.step, s.name) for s in plain.launch.smoke.steps][0] == ("train", "plain")
    assert main(["status", plain.id]) == 0
    assert "passed" not in capsys.readouterr().out
    assert str(plain.launch.smoke) == "not smoked (train: no smoke form)"


def test_a_tiny_job_that_saves_no_checkpoint_leaves_eval_not_smoked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--tiny", "1 unsaved", "--smoke"]) == 0
    said = capsys.readouterr()
    assert "smoke train ppo: passed in" in said.err and "(0 checkpoints)" in said.err
    assert "smoke eval: not smoked (no checkpoints)" in said.err
    (id,) = _ids(said.out)
    smoke = default().record(id).launch.smoke
    assert smoke.outcome == "not_smoked"
    assert str(smoke) == "not smoked (eval: no checkpoints)"


def test_without_smoke_a_broken_tiny_job_queues_unsmoked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--tiny", "1 diverge"]) == 0
    said = capsys.readouterr()
    assert "smoke" not in said.err
    (id,) = _ids(said.out)
    assert default().record(id).launch.smoke.outcome == "not_smoked"
    assert main(["status", id]) == 0
    assert "smoke:" not in capsys.readouterr().out


def _trained(source: Path, tmp_path: Path) -> None:
    """One checkpoint of experiment ppo's run ppo-1, delivered to `source`."""
    clocks = [Clock(name="updates", kind="step", meaning="optimizer updates")]
    with track(experiment="ppo", run="ppo-1", clocks=clocks, directory=source) as run:
        (tmp_path / "model.pt").write_bytes(b"weights 1")
        run.checkpoint(tmp_path / "model.pt", updates=100)


def test_eval_and_report_alone_read_a_source_and_deliver_nothing_to_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "bucket"
    _trained(source, tmp_path)
    before = sorted(source.rglob("*"))
    file = experiment(tmp_path / "lab")
    steps = ["--step", "eval", "--step", "report"]
    assert main(["smoke", str(file), *steps, "--source", str(source)]) == 0
    err = capsys.readouterr().err
    assert "smoke eval ladder: passed" in err and "smoke report: passed" in err
    assert "smoke train" not in err
    assert sorted(source.rglob("*")) == before
    assert main(["smoke", str(file), "--step", "eval"]) == 1
    assert "needs --source" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"play": "play_failing_deal_one"}, "smoke eval ladder: failed"),
        ({"report": "raise RuntimeError('report broke')"}, "smoke report: failed"),
    ],
)
def test_a_launching_eval_that_fails_its_smoke_launches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: dict[str, str], said: str
) -> None:
    source = tmp_path / "bucket"
    _trained(source, tmp_path)
    file = experiment(tmp_path / "lab", **change)
    file.write_text(file.read_text().replace("def training_jobs(", "def fixture_training_jobs("))
    assert main(["launch", str(file), "--source", str(source), "--pool", "local", "--smoke"]) == 1
    assert said in capsys.readouterr().err
    assert default().runs(done=True) == ()


@pytest.mark.parametrize(
    ("flags", "change", "outcome"),
    [
        (["--smoke"], {}, "passed"),
        ([], {"report": "raise RuntimeError('report broke')"}, "not_smoked"),
    ],
)
def test_a_launched_eval_run_records_its_smoke(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    change: dict[str, str],
    outcome: str,
) -> None:
    source = tmp_path / "bucket"
    _trained(source, tmp_path)
    file = experiment(tmp_path / "lab", **change)
    file.write_text(file.read_text().replace("def training_jobs(", "def fixture_training_jobs("))
    assert main(["launch", str(file), "--source", str(source), "--pool", "local", *flags]) == 0
    assert ", running " in capsys.readouterr().out
    (launched,) = default().runs(done=True)
    smoke = default().record(launched.id).launch.smoke
    assert smoke.outcome == outcome
    assert [step.step for step in smoke.steps] == (["eval", "report"] if flags else [])


def test_the_python_client_smokes_when_asked(tmp_path: Path) -> None:
    broken = Job(command="true", name="broken", max_hours=0.1, smoke=SmokeForm(command="exit 3"))
    with pytest.raises(LauncherError, match="smoke train broken: .*nothing was submitted"):
        queue(broken.runs(1, 2), "local", smoke=True)
    assert default().runs(done=True) == ()
    (plain,) = queue(broken.runs(1), "local")
    assert default().record(plain).launch.smoke.outcome == "not_smoked"
    fine = broken.model_copy(update={"smoke": SmokeForm(command="true")})
    (smoked,) = queue(fine.runs(1), "local", smoke=True)
    assert default().record(smoked).launch.smoke.outcome == "passed"


@pytest.mark.parametrize("steps", [[], ["--step", "eval", "--step", "report"]])
@pytest.mark.parametrize("source_given", [False, True])
def test_eval_only_smoke_uses_outside_checkpoints_and_this_computers_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    steps: list[str],
    source_given: bool,
) -> None:
    source = tmp_path / "source"
    with track(experiment="producer", run="base@0", directory=source, clocks=["updates"]) as run:
        path = run.out / "weights.pt"
        path.write_bytes(b"outside weights")
        run.checkpoint(path, updates=1)
    file = tmp_path / "outside_smoke.py"
    monkeypatch.setenv("EVAL_TEST_KEY", "local fixture credential")
    file.write_text(f"""
import os
from etude.experiment import observe
from etude.jobs import EvalJob
from tests.eval.stored import stored_inference, stored_relative
from etude.eval.records import Held, RoundRobin, Code
from etude.eval.match import environment

def play(match):
    assert os.environ["EVAL_TEST_KEY"] == "local fixture credential"
    model = next(p for p in match.players if hasattr(p, "checkpoint"))
    assert model.checkpoint.path.read_bytes() == b"outside weights"
    return {{"games": [{{"seat": 0, "won": 1}}]}}

def eval_jobs(state):
    checkpoint = observe({str(source)!r}, "producer").runs[0].final
    return EvalJob(tournament=RoundRobin(name="outside", environment=environment("game"),
        configs=({{}},), games=3), enter=(checkpoint.player(Held()), Code(name="fixed")),
        play=play, max_hours=0.1)
""")
    before = sorted(source.rglob("*"))
    assert (
        main(["smoke", str(file), *steps, *(["--source", str(source)] if source_given else [])])
        == 0
    )
    error = capsys.readouterr().err
    assert "smoke eval outside: passed" in error
    assert "smoke report: passed" in error
    assert sorted(source.rglob("*")) == before
    assert not default_root().exists()


def test_policy_key_smoke_names_cross_experiment_seat(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    with track(
        experiment="rival", run="small", job="small", directory=source, clocks=["updates"]
    ) as run:
        weights = run.out / "weights.pt"
        weights.write_bytes(b"weights")
        run.checkpoint(weights, updates=1)
    with track(
        experiment="tla-frontier", run="small", job="small", directory=source, clocks=["updates"]
    ) as run:
        weights = run.out / "weights.pt"
        weights.write_bytes(b"frontier weights")
        run.checkpoint(weights, updates=1)
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
    return EvalJob(tournament=Benchmark(name="frontier", environment=environment("duel"),
        seatings=(Seating(name="vs frontier", players=(direct,
            Final(experiment="tla-frontier")), configs=({},), games=8),)),
        checkpoints=state.policy(PolicyKey()).checkpoints, play=play, repo=None, max_hours=1)
""")
    measured = MeasuredStorage(DirectoryStorage(source))
    smoked = smoke_file(file, steps=["eval"], source=measured)
    assert smoked.rest[0].outcome == "not_smoked"
    assert measured.listings == 1
    # Discovery reads every run declaration; skipped opponents need no other records or bytes.
    assert [key for key in measured.got if key.startswith("tla-frontier/")] == [
        "tla-frontier/small/run.json"
    ]
    error = capsys.readouterr().err
    assert "not smoked" in error and "cross-experiment seats: tla-frontier" in error
    assert "no checkpoints" not in error
