"""File contracts for eval runs: agents, measures and the episodes they play.

An eval run declares its agents and measures once, in `evaluation.json` beside
`run.json`, then records episodes. A player is a position in one episode; what
takes it is an agent, named within the run and identified across runs by a
digest. A group is a set of positions measured together, such as a team.

No measure is privileged: win/loss is declared like any other. Each measure is
a mean or a rate and belongs to one level, player, group or episode.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from etude.run.records import Digest, Name, Record


class Agent(Record):
    """What takes a position: a saved model, or code such as a scripted player."""

    # Local to the run; players refer to it. Runs are joined by `identity`.
    name: Name
    kind: Literal["checkpoint", "code"]
    # A checkpoint's artifact digest, or the digest of the code's source files.
    identity: Digest
    uri: str | None = None
    # The author's: the training run it came from, a seed, "anchor".
    labels: dict[Name, str] = {}


class Measure(Record):
    """One declared quantity. A rate divides total hits by total opportunities."""

    name: Name
    meaning: str = Field(min_length=1, max_length=200)
    kind: Literal["mean", "rate"]
    of: Literal["player", "group", "episode"]


class Count(Record):
    """A rate measure's value in one episode: hits out of opportunities."""

    hits: float = Field(ge=0)
    opportunities: int = Field(ge=0)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.hits > self.opportunities:
            raise ValueError("hits_exceed_opportunities")
        return self


type Values = dict[Name, float | Count]


class Player(Record):
    """A position in one episode and the agent that took it."""

    agent: Name
    labels: dict[Name, str] = {}
    measures: Values = {}


class Group(Record):
    """Positions measured together: a team, a partnership, a coalition."""

    # Unique within its episode.
    name: Name
    # Positions in `Episode.players`; a player may be in several groups.
    players: tuple[int, ...] = Field(min_length=1)
    labels: dict[Name, str] = {}
    measures: Values = {}


class Episode(Record):
    """One finished game or task. Recording a key again replaces it."""

    key: Name
    # In position order.
    players: tuple[Player, ...] = Field(min_length=1)
    groups: tuple[Group, ...] = ()
    # Episodes sharing a block, such as one deal played both ways, are
    # resampled together; with no block an episode is its own.
    block: Name | None = None
    labels: dict[Name, str] = {}
    measures: Values = {}
    # The gameplay log, relative to the run. Etude never reads it.
    log: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len({group.name for group in self.groups}) != len(self.groups):
            raise ValueError("duplicate_group")
        for group in self.groups:
            if len(set(group.players)) != len(group.players):
                raise ValueError("duplicate_group_player")
            if not all(0 <= position < len(self.players) for position in group.players):
                raise ValueError("group_player_out_of_range")
        return self


class EvaluationFile(Record):
    """`evaluation.json`: an eval run's agents and measures. Re-declaring must match."""

    schema_version: Literal[1] = 1
    agents: tuple[Agent, ...] = Field(min_length=1)
    measures: tuple[Measure, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("duplicate_agent_name")
        if len({agent.identity for agent in self.agents}) != len(self.agents):
            raise ValueError("duplicate_agent_identity")
        if len({measure.name for measure in self.measures}) != len(self.measures):
            raise ValueError("duplicate_measure")
        return self
