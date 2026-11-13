"""Typed jobs end to end: Etude decides the command, and the function gets its typed config."""

from __future__ import annotations

import hashlib
import importlib
import subprocess
import sys
import sysconfig
from decimal import Decimal
from pathlib import Path

import pytest

from etude.cli import main
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Benchmark, Code, Inference, RoundRobin, Seating
from etude.jobs import JobOptions, evaluation
from etude.launch.errors import LauncherError
from etude.launch.job_file import read_job_file
from etude.launch.records import Input, Job, Runtime, SmokeForm, Until, job_id
from etude.launch.smoke import tiny
from etude.train.read import read
from tests.experiments import authored
from tests.launch.counting import job_text
from tests.launch.test_runpod_machines import cloud
from tests.launch.test_worker_attempts import local

TRAIN = """
from pydantic import BaseModel


class TrainConfig(BaseModel):
    lr: float
    layers: tuple[int, ...] = (64, 64)


class OtherConfig(BaseModel):
    lr: float = 1.0


def train(config: TrainConfig) -> None:
    print(f"{type(config).__name__} lr={config.lr} layers={config.layers}")
"""

JOB = """
from etude.jobs import training
from etude.launch.records import Job, Runtime

from {module} import OtherConfig, TrainConfig, train


def runs(python: str) -> Job:
    return training(train, config={config}, max_hours=0.1, runtime=Runtime(python=python))
"""

PLAY = """
from pathlib import Path
from urllib.parse import urlparse

from etude.eval.match import Tables
from etude.eval.records import Match, CheckpointPlayer


def play(match: Match) -> Tables:
    # A model's checkpoint file is on this machine; the row says how big it is.
    sizes = [
        len(Path(urlparse(seated.checkpoint.artifact.uri).path).read_bytes())
        if isinstance(seated, CheckpointPlayer)
        else 0
        for seated in match.players
    ]
    return {"plays": [{"game": 0, "seat": seat, "bytes": size} for seat, size in enumerate(sizes)]}
"""


@pytest.fixture
def job_python(tmp_path: Path) -> str:
    """A job environment of its own that can import Etude and its test dependencies."""
    directory = tmp_path / "job-env"
    subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable, str(directory)],
        check=True,
        capture_output=True,
    )
    (site,) = directory.glob("lib/python*/site-packages")
    (site / "client.pth").write_text(
        f"import site; site.addsitedir({sysconfig.get_paths()['purelib']!r})\n"
    )
    return str(directory / "bin" / "python")


def checkout(folder: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)
    for command in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "work"],
    ):
        subprocess.run(["git", "-C", str(folder), *command], check=True)
    return folder


@pytest.fixture(autouse=True)
def _restore_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Job files put their folder on the import path; each test keeps its own.
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_a_training_job_calls_its_function_with_its_typed_config(
    tmp_path: Path, job_python: str
) -> None:
    # The job file sits beside its training code, below the checkout's root.
    repo = checkout(
        tmp_path / "lab",
        {
            "experiments/lab_train.py": TRAIN,
            "experiments/job.py": JOB.format(module="lab_train", config="TrainConfig(lr=0.25)"),
        },
    )
    (run,) = read_job_file(repo / "experiments" / "job.py", ["--python", job_python])
    assert run.job.repo == str(repo)
    scheduler = local(tmp_path)
    launched = scheduler.launch(run)
    (status,) = scheduler.wait(launched, every=0.05, timeout=60)
    text = job_text(scheduler.logs(refresh=True, run=launched))
    assert status.outcome == "succeeded", text
    # The tuple crossed as JSON and arrived as the function's own type again.
    assert "TrainConfig lr=0.25 layers=(64, 64)" in text


@pytest.mark.parametrize("experiment", [None, "existing-study"])
def test_a_plain_training_file_supplies_the_recipes_experiment(
    tmp_path: Path,
    job_python: str,
    monkeypatch: pytest.MonkeyPatch,
    experiment: str | None,
) -> None:
    module = "plain_" + (experiment or "unnamed").replace("-", "_")
    repo = checkout(
        tmp_path / "lab",
        {
            f"{module}.py": """
from pydantic import BaseModel
from etude.train.records import Step
from etude.train.run import track

class Config(BaseModel):
    value: int = 7

def train(config: Config) -> None:
    with track(clocks=[Step("updates", "optimizer updates")], strict=True) as run:
        saved = run.out / "final.txt"
        saved.write_text(str(config.value))
        run.log({"value": config.value}, updates=1)
        run.checkpoint(saved, updates=1)
""",
            "plain.py": f"""
from etude.jobs import training
from etude.launch.records import Job, Runtime
from {module} import Config, train

def runs(python: str) -> Job:
    job = training(train, config=Config(), max_hours=0.01, max_attempts=1,
                   runtime=Runtime(python=python))
    return job.model_copy(update={{"experiment": {experiment!r}}})
""",
        },
    )
    scheduler = local(tmp_path)
    monkeypatch.setattr(importlib.import_module("etude.launch.cli"), "default", lambda: scheduler)
    assert main(["launch", str(repo / "plain.py"), "--python", job_python]) == 0
    (record,) = scheduler.runs()
    (status,) = scheduler.wait(record.id, every=0.05, timeout=30)
    assert status.outcome == "succeeded", job_text(scheduler.logs(refresh=True, run=record.id))
    assert record.spec.experiment == (experiment or "plain")
    assert len(scheduler.attempts(record.id)) == 1
    tracked = tmp_path / "track"
    data = read(tracked, experiment or "plain", record.id)
    assert authored(data.reached) == {"updates": 1}
    assert len(data.checkpoints) == 1
    assert (tracked / record.id / "out/final.txt").read_text() == "7"


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
@pytest.mark.parametrize(
    ("config", "refusal"),
    [
        ("OtherConfig()", "train takes a TrainConfig config, not OtherConfig"),
        ('TrainConfig.model_construct(lr="fast")', "would not validate on the machine"),
        ("TrainConfig(lr=1), smoke=OtherConfig()", "train takes a TrainConfig config, not Other"),
    ],
)
def test_a_config_the_function_cannot_take_is_refused_before_anything_launches(
    tmp_path: Path, config: str, refusal: str
) -> None:
    module = f"refused_{len(config)}"
    repo = checkout(
        tmp_path / "lab",
        {f"{module}.py": TRAIN, "job.py": JOB.format(module=module, config=config)},
    )
    with pytest.raises(LauncherError, match=refusal):
        read_job_file(repo / "job.py", ["--python", "python3.12"])


def test_a_training_jobs_smoke_form_is_its_smoke_config(tmp_path: Path) -> None:
    config = "TrainConfig(lr=0.25), smoke=TrainConfig(lr=0.5, layers=(8,))"
    repo = checkout(
        tmp_path / "lab",
        {"tiny_train.py": TRAIN, "job.py": JOB.format(module="tiny_train", config=config)},
    )
    (run,) = read_job_file(repo / "job.py", ["--python", "python3.12"])
    small = tiny(run.job)
    assert small is not None and small.smoke is None
    assert (small.command, small.config) == (run.job.command, '{"lr":0.5,"layers":[8]}')
    # The smoke form is not part of what makes the job itself.
    assert job_id(run.job) == job_id(run.job.model_copy(update={"smoke": None}))


def test_a_target_is_read_once_and_one_job_has_one_id_however_it_is_written() -> None:
    # Written as text, as an experiment file does.
    written = Job.model_validate({"command": "train", "max_hours": 1, "until": "steps=26000.0"})
    assert written.until == Until(clock="steps", value=26000)
    assert str(written.until) == "steps=26000" and written.model_dump()["until"] == "steps=26000"
    whole = Job(command="train", max_hours=1, until=Until.parse("steps=26000"))
    assert job_id(written) == job_id(whole)
    smoked = written.model_copy(update={"smoke": SmokeForm(until=Until.parse("steps=2"))})
    small = tiny(smoked)
    assert small is not None and small.until == Until(clock="steps", value=2)
    with pytest.raises(LauncherError, match="not a target"):
        Until.parse("steps")


def test_a_function_the_job_cannot_import_by_name_is_refused(tmp_path: Path) -> None:
    repo = checkout(
        tmp_path / "lab",
        {
            "job.py": TRAIN
            + "\nfrom etude.jobs import training\n\n\ndef runs() -> object:\n"
            + "    return training(train, config=TrainConfig(lr=1), max_hours=0.1)\n"
        },
    )
    with pytest.raises(LauncherError, match="importable module"):
        read_job_file(repo / "job.py")


UNPACKAGED = '[project]\nname = "lab"\nversion = "0"\ndependencies = []\n'
PACKAGED = (
    UNPACKAGED + '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
)
TYPED = """
from etude.jobs import training

from {module} import TrainConfig, train


def runs() -> object:
    return training(train, config=TrainConfig(lr=1), max_hours=0.1{options})
"""


@pytest.mark.parametrize(
    ("project", "options", "derived"),
    [
        (UNPACKAGED, "", True),
        (PACKAGED, "", False),  # building the project needs more than its lock
        (UNPACKAGED, ", prepare=None", False),  # an explicit None opts out
    ],
)
def test_an_unpackaged_uv_project_prepares_its_environment_once_per_lock(
    tmp_path: Path, project: str, options: str, derived: bool
) -> None:
    module = f"uv_lab_{tmp_path.name}".replace("-", "_")  # each checkout's own import
    repo = checkout(
        tmp_path / "lab",
        {
            "pyproject.toml": project,
            "uv.lock": "version = 1\n",
            ".python-version": "3.12\n",
            f"{module}.py": TRAIN,
            "job.py": TYPED.format(module=module, options=options),
        },
    )
    (run,) = read_job_file(repo / "job.py")
    prepare = run.job.prepare
    if derived:
        assert prepare is not None
        assert prepare.paths == ("pyproject.toml", "uv.lock", ".python-version")
        assert prepare.python == ".venv/bin/python"
        assert run.job.command.startswith('"$ETUDE_PYTHON" -m etude.call')
    else:
        assert prepare is None and run.job.command.startswith("uv run python -m etude.call")


def test_typed_jobs_take_every_job_field_but_what_they_derive() -> None:
    # The experiment is the launched file's, or an evaluation's own `experiment`; a
    # training job's smoke form is its `smoke` config, and an evaluation's is Etude's.
    derived = {"command", "config", "experiment", "smoke"}
    assert set(JobOptions.__annotations__) == set(Job.model_fields) - derived


def test_an_evaluation_job_holds_a_benchmark_for_the_checkpoint_it_is_given(
    tmp_path: Path, job_python: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = checkout(tmp_path / "arena", {"arena_duel.py": PLAY})
    monkeypatch.syspath_prepend(str(repo))
    duel = importlib.import_module("arena_duel")
    progress = Benchmark(
        name="anchors",
        environment=environment("duel"),
        seatings=(
            Seating(
                name="model vs fixed",
                players=(
                    Inference(config={"search": 0}),
                    Code(name="fixed", source=[repo / "arena_duel.py"]),
                ),
                conditions=({"deal": 1}, {"deal": 2}),
                games=2,
            ),
        ),
    )
    c = cloud(tmp_path)
    weights = b"weights at updates 100\n"
    c.storage.objects["etude/t/ppo-1/checkpoints/updates-100"] = weights
    given = Input(
        name="updates-100",
        key="etude/t/ppo-1/checkpoints/updates-100",
        sha256=hashlib.sha256(weights).hexdigest(),
        size_bytes=len(weights),
    )
    job = evaluation(
        progress,
        experiment="ppo",
        play=duel.play,
        checkpoint=given,
        max_hours=0.1,
        max_usd=Decimal(1),
        max_attempts=1,
        runtime=Runtime(python=job_python),
    )
    run = c.scheduler.launch(job, rent="l4")
    (status,) = c.scheduler.wait(run, every=0.05, timeout=60)
    assert status.outcome == "succeeded", job_text(c.scheduler.logs(refresh=True, run=run))
    assert c.provider.creates == 1
    reads = [k for m, k in c.storage.asked if m == "GET" and k.startswith(given.key)]
    assert len(reads) == 1  # the checkpoint reached the machine once, as the job's input

    # The eval run is the launched run, delivered to the bucket like any other.
    folder = f"etude/t/{run}/"
    delivered = tmp_path / "delivered"
    for key, body in c.storage.objects.items():
        if key.startswith(folder):
            (delivered / run / key.removeprefix(folder)).parent.mkdir(parents=True, exist_ok=True)
            (delivered / run / key.removeprefix(folder)).write_bytes(body)
    db = statsdb("ppo", delivered)
    assert (
        db.sql(
            "select a.name, p.bytes from plays p join seats s using (run, match, seat)"
            " join players a using (player) where p.seat = 0"
        ).fetchall()
        == [("updates-100", len(weights))] * 2
    )


def test_an_evaluation_holds_what_its_tournament_takes() -> None:
    progress = Benchmark(name="anchors", environment=environment("duel"))
    entrant = Code(name="fixed", source=[Path(__file__)])
    with pytest.raises(LauncherError, match="give it checkpoints, no entrants"):
        evaluation(
            progress,
            experiment="ppo",
            play=lambda match: {},
            enter=[entrant],
            max_hours=0.1,
            repo=None,
        )
    weights = Input(name="w", key="w", sha256="0" * 64, size_bytes=1)
    with pytest.raises(LauncherError, match="name the experiment"):
        evaluation(progress, play=lambda match: {}, checkpoint=weights, max_hours=0.1, repo=None)
    with pytest.raises(
        LauncherError, match="needs max_hours: the most hours it may hold a machine"
    ):
        # The omission under test.
        evaluation(progress, experiment="ppo", play=lambda match: {}, checkpoint=weights, repo=None)  # type: ignore[call-arg]
    league = RoundRobin(name="league", environment=environment("duel"), conditions=({},), games=1)
    with pytest.raises(LauncherError, match="give it entrants"):
        evaluation(league, experiment="ppo", play=lambda match: {}, max_hours=0.1, repo=None)


def test_the_job_command_starts_by_importing_no_other_part_of_etude() -> None:
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, etude.call; print(sorted(m for m in sys.modules if m.startswith('etude')))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert loaded.strip() == "['etude', 'etude.call', 'etude.stopping']"


def test_job_seeds_expand_to_named_requests_without_changing_job_identity(tmp_path: Path) -> None:
    file = tmp_path / "job.py"
    file.write_text(
        'from etude.launch.records import Job\ndef runs():\n    return Job(command="true", max_hours=1, name="baseline", seeds=(0, 7))\n'
    )
    requests = read_job_file(file, experiment="chosen")
    assert [(run.id, run.seed, run.job.experiment) for run in requests] == [
        ("baseline-seed-0", 0, "chosen"),
        ("baseline-seed-7", 7, "chosen"),
    ]
    first = requests[0].job
    assert job_id(first) == job_id(first.model_copy(update={"seeds": (42,)}))
    file.write_text(
        'from etude.launch.records import Job\ndef runs():\n    return [Job(command="true", max_hours=1), Job(command="true", max_hours=1, name="other")]\n'
    )
    assert [run.id for run in read_job_file(file)] == ["job-seed-0", "other-seed-0"]
