"""A four-update training and evaluation prototype for the getting-started guide."""

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

from pydantic import BaseModel

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward
from etude.eval.match import environment
from etude.eval.records import Benchmark, Held, Match, Seating
from etude.eval.result import Tables
from etude.jobs import Agents, EvalJob, ExperimentState, TrainingJob
from etude.report import Curve, Report, Score
from etude.train.tracking import track


class Config(BaseModel):
    updates: int = 4
    target: float = 0.7


class Estimate(BaseModel):
    probability: float = 0.5


def train(config: Config) -> None:
    with track(config=config) as run:
        estimate = run.resume(extra=Estimate())
        for update in run.steps("updates", to=config.updates, save_every=1):
            estimate.probability += (config.target - estimate.probability) / 2
            run.log({"error": abs(config.target - estimate.probability)})
            run.save()
            path = run.out / f"estimate-{update}.txt"
            path.write_text(str(estimate.probability))
            run.checkpoint(path)


def load(checkpoint: CheckpointRef) -> Forward:
    probability = float(checkpoint.path.read_text())

    def forward(inputs: list[object]) -> list[object]:
        return [probability for _ in inputs]

    return forward


def play(match: Match, agents: Sequence[Forward]) -> Tables:
    return {
        "scores": [
            {"seat": seat, "score": 1 - abs(cast(float, agent([None])[0]) - 0.7)}
            for seat, agent in enumerate(agents)
        ]
    }


def training_jobs() -> list[TrainingJob]:
    return [
        TrainingJob(
            name="estimate",
            train=train,
            config=Config(),
            smoke=Config(updates=1),
            max_hours=0.1,
            max_usd=Decimal("0.25"),
            extras=("eval",),
        )
    ]


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    return [
        EvalJob(
            tournament=Benchmark(
                name="accuracy",
                environment=environment("estimate"),
                seatings=(Seating(name="self", players=(Held(), Held()), configs=({},), games=1),),
            ),
            checkpoints=[checkpoint for run in state.runs for checkpoint in run.checkpoints],
            play=play,
            agents=Agents(load=load),
            max_hours=0.1,
            max_usd=Decimal("0.25"),
            extras=("eval",),
        )
    ]


def report() -> Report:
    return Report(score=Score(table="scores", value="score"), evals=(Curve(),))
