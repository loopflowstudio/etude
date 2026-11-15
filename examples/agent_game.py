"""A light game module: model loading belongs in agent_models."""

from collections.abc import Sequence

from etude.eval.agents import Agent
from etude.eval.records import Match
from etude.eval.result import Tables


def play(match: Match, agents: Sequence[Agent]) -> Tables:
    return {
        "scores": [
            {"seat": seat, "score": int(str(agents[seat].act([match.games])[0]))}
            for seat in range(len(match.players))
        ]
    }
