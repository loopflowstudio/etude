"""`etude smoke`: an experiment file's training, evaluation and report run tiny before spending."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from etude.cli import main
from etude.launch.client import default, queue_cohort
from etude.launch.errors import LauncherError
from etude.launch.records import Job, SmokeForm
from etude.tracking.records import Clock
from etude.tracking.storage import default_root
from etude.train.run import track

# Saves one checkpoint at the given number of updates; refuses a real-sized run, so a
# passing smoke shows its smoke form ran rather than the job's own command.
TRAIN = """
import sys
from pathlib import Path

from etude.tracking.records import Clock
from etude.train.run import track

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
from etude.eval.records import Benchmark, Code, Inference, Seating
from etude.experiment import ExperimentState
from etude.jobs import evaluation
from etude.launch.records import Job, SmokeForm
from etude.report import Report
from tests.eval.games import {play}

TRAIN = "{python} " + str(Path(__file__).parent / "train.py")


def runs(seeds: int = 1, tiny: str = "1", bare: bool = False) -> list:
    smoke = SmokeForm(command=f"{{TRAIN}} {{tiny}}") if tiny else None
    ppo = Job(command=TRAIN + " 1000", name="ppo", max_hours=0.1, smoke=smoke).runs(*range(seeds))
    plain = Job(command=TRAIN + " 2000", name="plain", max_hours=0.1).runs(0) if bare else []
    return ppo + plain


def eval(state: ExperimentState) -> Job:
    chosen = [saved for run in state.runs for saved in run.checkpoints]
    anchor = Code(name="heuristic", source=[__file__], config={{"strength": 0}})
    vs = Seating(name="vs heuristic", players=(Inference(config={{"search": 0}}), anchor),
                 conditions=({{"deal": 0}}, {{"deal": 1}}), games=4)
    ladder = Benchmark(name="ladder", environment=environment("duel"), seatings=(vs,))
    return evaluation(ladder, play={play}, checkpoints=chosen, max_hours=0.1,
                      repo=None)


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
    return [line.split()[0] for line in out.splitlines() if not line.startswith(" ")]


@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Experiment files put their folder on the import path; each test keeps its own.
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_passing_smoke_queues_and_its_runs_record_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--seeds", "2", "--cohort", "c", "--smoke"]) == 0
    said = capsys.readouterr()
    out, err = said.out, said.err
    # Two seeds of one job are smoked once; the seating plays each deal once.
    assert "smoke train ppo: passed in" in err and "(1 checkpoint)" in err
    assert "smoke eval ladder: passed in" in err and "(2 matches, 1 game each)" in err
    assert "smoke report: passed in" in err
    ids = _ids(out)
    assert len(ids) == 2
    record = default().record(ids[0])
    assert record.smoke.outcome == "passed"
    assert [step.step for step in record.smoke.steps] == ["train", "eval", "report"]
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
    assert default().record(id).smoke.outcome == "not_smoked"


def test_a_job_without_a_smoke_form_beside_one_with_it_is_not_recorded_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--bare", "--cohort", "c", "--smoke"]) == 0
    said = capsys.readouterr()
    assert "smoke train plain: not smoked (no smoke form)" in said.err
    ppo, plain = (default().record(id) for id in _ids(said.out))
    assert (ppo.spec.name, plain.spec.name) == ("ppo", "plain")
    assert ppo.smoke.outcome == "passed"
    assert [(s.step, s.name) for s in ppo.smoke.steps] == [
        ("train", "ppo"),
        ("eval", "ladder"),
        ("report", "report"),
    ]
    assert plain.smoke.outcome == "not_smoked"
    assert [(s.step, s.name) for s in plain.smoke.steps][0] == ("train", "plain")
    assert main(["status", plain.id]) == 0
    assert "passed" not in capsys.readouterr().out
    assert str(plain.smoke) == "not smoked (train: no smoke form)"


def test_a_tiny_job_that_saves_no_checkpoint_leaves_eval_not_smoked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    file = experiment(tmp_path / "lab")
    assert main(["queue", str(file), "--tiny", "1 unsaved", "--smoke"]) == 0
    said = capsys.readouterr()
    assert "smoke train ppo: passed in" in said.err and "(0 checkpoints)" in said.err
    assert "smoke eval: not smoked (no checkpoints)" in said.err
    (id,) = _ids(said.out)
    smoke = default().record(id).smoke
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
    assert default().record(id).smoke.outcome == "not_smoked"
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
    assert main(["eval", str(file), "--source", str(source), "--pool", "local", "--smoke"]) == 1
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
    assert main(["eval", str(file), "--source", str(source), "--pool", "local", *flags]) == 0
    assert ", launched " in capsys.readouterr().out
    (launched,) = default().runs(done=True)
    smoke = default().record(launched.id).smoke
    assert smoke.outcome == outcome
    assert [step.step for step in smoke.steps] == (["eval", "report"] if flags else [])


def test_the_python_client_smokes_when_asked(tmp_path: Path) -> None:
    broken = Job(command="true", name="broken", max_hours=0.1, smoke=SmokeForm(command="exit 3"))
    with pytest.raises(LauncherError, match="smoke train broken: .*nothing was submitted"):
        queue_cohort("refused", broken.runs(1, 2), "local", smoke=True)
    assert default().runs(done=True) == ()
    (plain,) = queue_cohort("plain", broken.runs(1), "local")
    assert default().record(plain).smoke.outcome == "not_smoked"
    fine = broken.model_copy(update={"smoke": SmokeForm(command="true")})
    (smoked,) = queue_cohort("kept", fine.runs(1), "local", smoke=True)
    assert default().record(smoked).smoke.outcome == "passed"
