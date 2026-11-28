"""Play variable-length matches with a pre-collated forward per step.

ETUDE_TRACK_DIR=/tmp/vectorized uv run etude eval examples/vectorized.py
"""

from agent_game import checkpoint
from vector_game import play_many
from vector_models import load

from etude.eval.match import environment
from etude.eval.records import Benchmark, Seating
from etude.jobs import Agents, EvalJob, ExperimentState


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    model = checkpoint("small")
    return [
        EvalJob(
            tournament=Benchmark(
                name="vector",
                environment=environment("vectorized"),
                seatings=(Seating(name="self play", players=(model, model), games=8),),
            ),
            play_many=play_many,
            agents=Agents(load=load, devices=("cpu",), max_batch=4),
            max_hours=0.1,
        )
    ]
