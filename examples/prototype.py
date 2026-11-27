"""Fit a coin's bias, compare predictions and render a report without a model library.

uv run etude train examples/prototype.py
uv run etude eval examples/prototype.py
uv run etude report examples/prototype.py
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from random import Random

from pydantic import BaseModel, Field

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward
from etude.eval.match import environment
from etude.eval.records import Benchmark, CheckpointPlayer, CheckpointSlot, Code, Match, Seating
from etude.eval.result import Tables
from etude.jobs import Agents, EvalJob, ExperimentState, TrainingJob
from etude.report import Curve, Report, Score
from etude.train.tracking import track


class Config(BaseModel):
    updates: int = Field(default=20, ge=1)
    heads: float = Field(default=0.7, ge=0, le=1)


class Rules(BaseModel, frozen=True):
    heads: float = Field(default=0.7, ge=0, le=1)


class Counts(BaseModel):
    heads: int = 0
    tosses: int = 0


def train(config: Config) -> None:
    with track(config=config) as run:
        counts = run.resume(extra=Counts())
        for update in run.steps("updates", to=config.updates, save_every=1):
            counts.heads += Random(f"{run.seed}:{update}").random() < config.heads
            counts.tosses += 1
            run.log({"heads": counts.heads / counts.tosses})
            run.save()
        path = run.out / "coin.json"
        path.write_text(counts.model_dump_json())
        run.checkpoint(path)


def load(checkpoint: CheckpointRef) -> Forward:
    counts = Counts.model_validate_json(checkpoint.path.read_text())
    probability = counts.heads / counts.tosses

    def forward(inputs: list[object]) -> list[object]:
        return [probability for _ in inputs]

    return forward


def build(player: object, leaves: Mapping[str, Forward]) -> Forward:
    if isinstance(player, CheckpointPlayer):
        return leaves[player.checkpoint.key]
    if isinstance(player, Code):
        return lambda inputs: [0.5 for _ in inputs]
    raise TypeError(f"unknown player {type(player).__name__}")


def play(match: Match[Rules], agents: Sequence[Forward]) -> Tables:
    # Keep game randomness separate from every player's action stream.
    target = Random(f"{match.seed}:game").random() < match.config.heads
    scores: list[dict[str, bool | int | float | str | None]] = []
    for seat, agent in enumerate(agents):
        [probability] = agent([None])
        action = Random(f"{match.seed}:player:{seat}").random() < float(str(probability))
        scores.append({"seat": seat, "won": action == target})
    return {"scores": scores}


PANEL = Benchmark(
    name="prediction",
    environment=environment("coin-prediction"),
    seatings=(
        Seating(
            name="fair baseline",
            players=(CheckpointSlot(), Code(name="fair")),
            config=Rules(),
            base_seed=42,
            games=8,
        ),
    ),
)


def training_jobs() -> list[TrainingJob[Config]]:
    return [
        TrainingJob(
            name="coin",
            train=train,
            config=Config(),
            smoke=Config(updates=1),
            seeds=(0,),
            max_hours=0.01,
            max_usd=Decimal("0.10"),
        )
    ]


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    return [
        EvalJob(
            tournament=PANEL,
            play=play,
            agents=Agents(load=load, build=build),
            checkpoints=[c for run in state.runs for c in run.checkpoints],
            max_hours=0.01,
            max_usd=Decimal("0.10"),
        )
    ]


def report() -> Report:
    return Report(score=Score(table="scores", value="won", reference=0.5), evals=(Curve(),))
