"""Two tournaments sharing a checkpoint through bare and composed players.

uv run etude eval examples/agents.py --source /tmp/agents
"""

from agent_game import Shift, build, checkpoint, play
from agent_models import load

from etude.eval.match import environment
from etude.eval.records import RoundRobin, compose
from etude.jobs import Agents, EvalJob, ExperimentState


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    agents = Agents(load=load, build=build, max_delay_seconds=0.05)
    small = checkpoint("small")
    shifted = compose(Shift(over=small))
    return [
        EvalJob(
            tournament=RoundRobin(
                name=name, environment=environment("agents"), configs=({"lane": name},), games=1
            ),
            enter=(small, shifted, compose(Shift(over=shifted, amount=2))),
            play=play,
            agents=agents,
            max_hours=0.1,
        )
        for name in ("ladder", "gauntlet")
    ]
