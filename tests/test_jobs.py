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

from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Benchmark, Code, Inference, Matchup, RoundRobin
from etude.jobs import JobOptions, evaluation
from etude.launch.errors import LauncherError
from etude.launch.job_file import read_job_file
from etude.launch.records import Input, Job, Runtime
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
from etude.eval.records import Match, Model


def play(match: Match) -> Tables:
    # A model's checkpoint file is on this machine; the row says how big it is.
    sizes = [
        len(Path(urlparse(seated.checkpoint.artifact.uri).path).read_bytes())
        if isinstance(seated, Model)
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
    text = job_text(scheduler.logs(run=launched))
    assert status.outcome == "succeeded", text
    # The tuple crossed as JSON and arrived as the function's own type again.
    assert "TrainConfig lr=0.25 layers=(64, 64)" in text


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
@pytest.mark.parametrize(
    ("config", "refusal"),
    [
        ("OtherConfig()", "train takes a TrainConfig config, not OtherConfig"),
        ('TrainConfig.model_construct(lr="fast")', "would not validate on the machine"),
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
    # The experiment is the launched file's, or an evaluation's own `experiment`.
    derived = {"command", "config", "experiment"}
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
        matchups=(
            Matchup(
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
    assert status.outcome == "succeeded", job_text(c.scheduler.logs(run=run))
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
    assert loaded.strip() == "['etude', 'etude.call']"
