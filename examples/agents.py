"""Two tournaments sharing loaded agents; no model library or rental required.

uv run etude eval examples/agents.py --source /tmp/agents
"""

from agent_game import play
from agent_models import Loading, load

from etude.eval.match import environment
from etude.eval.records import Code, RoundRobin
from etude.jobs import Agents, EvalJob, ExperimentState


def eval_jobs(state: ExperimentState) -> list[EvalJob]:
    agents = Agents(load=load, config=Loading(), max_delay_seconds=0.05)
    return [
        EvalJob(
            tournament=RoundRobin(
                name=name, environment=environment("agents"), configs=({"lane": name},), games=1
            ),
            enter=(Code(name="left"), Code(name="right")),
            play=play,
            agents=agents,
            max_hours=0.1,
        )
        for name in ("ladder", "gauntlet")
    ]
