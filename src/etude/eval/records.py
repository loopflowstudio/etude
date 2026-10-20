"""File contracts for tournaments: agents, the environment they play in, and matches.

A tournament is an eval run: `run.json` with kind "evaluation" and no clocks,
then `environment.json`, then for each match the author's tables as Parquet
under `tables/` and Etude's record of the match, `matches/KEY.json`.

An agent is what plays together with how it is configured, identified across
tournaments by a digest of both, never by its name: one checkpoint at two
search budgets is two agents. A match seats agents by position (`player`),
names groups of seats and fixes the conditions it is played under.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, model_validator

from etude.run.records import Digest, Name, Record

type Config = dict[str, JsonValue]
type ColumnType = Literal["BOOLEAN", "BIGINT", "DOUBLE", "VARCHAR"]

# Every match has this group, holding every seat.
EVERYONE = "all"


def digest(value: object) -> Digest:
    """The digest of a value's canonical JSON, so equal values agree on every machine."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(text.encode()).hexdigest()


class Agent(Record):
    """A saved model or code, and the configuration it plays with."""

    # What a report calls it; several names may share an identity.
    name: Name
    kind: Literal["checkpoint", "code"]
    # A checkpoint's artifact digest, or the digest of the code's source files.
    artifact: Digest
    uri: str | None = None
    config: Config = {}

    @property
    def identity(self) -> Digest:
        return digest([self.kind, self.artifact, self.config])


class Environment(Record):
    """What a tournament is played in. Results from different identities are never pooled."""

    name: Name
    # The digest of the files that define it.
    files: Digest
    config: Config = {}

    @property
    def identity(self) -> Digest:
        return digest([self.name, self.files, self.config])


class Match(Record):
    """A seating of agents under fixed conditions, played for some games.

    The unit Etude schedules, resumes and resamples. Group `all` holds every
    seat and is added when not given.
    """

    # By seat: `player` in a row is a position here.
    agents: tuple[Agent, ...] = Field(min_length=1)
    groups: dict[Name, tuple[int, ...]] = {}
    conditions: Config = {}
    games: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def everyone(cls, data: Any) -> Any:
        if isinstance(data, dict) and EVERYONE not in data.get("groups", {}):
            seats = tuple(range(len(data.get("agents", ()))))
            data = {**data, "groups": {EVERYONE: seats, **data.get("groups", {})}}
        return data

    @model_validator(mode="after")
    def seated(self) -> Self:
        if self.groups[EVERYONE] != tuple(range(len(self.agents))):
            raise ValueError("group_all_is_every_seat")
        for seats in self.groups.values():
            if not seats or len(set(seats)) != len(seats):
                raise ValueError("group_seats_empty_or_repeated")
            if not all(0 <= seat < len(self.agents) for seat in seats):
                raise ValueError("group_seat_out_of_range")
        return self

    @property
    def key(self) -> Digest:
        """Identifies the match within its tournament."""
        seating = [agent.identity for agent in self.agents]
        groups = {name: list(seats) for name, seats in self.groups.items()}
        return digest([seating, groups, self.conditions, self.games])

    @property
    def seed(self) -> int:
        """Derived from the key, so a replayed match draws the same games."""
        return int(self.key[:15], 16)


class MatchRecord(Record):
    """`matches/KEY.json`: how one match went. Written after its tables."""

    match: Match
    state: Literal["done", "failed"]
    error: str | None = None
    # Each table's column types in this match's write; a resume holds later writes to them.
    columns: dict[Name, dict[Name, ColumnType]] = {}
