"""File contracts for evaluation: agents, environments, matches and tournaments.

An eval run is a run with kind "evaluation" and no clocks: `run.json`, then
`environment.json`, then for each match the author's tables as Parquet under
`tables/` and Etude's record of the match, `matches/KEY.json` (or
`matches/KEY.failed.json`).

A tournament is stored beside the runs under `tournaments/NAME/`, as files
that are written once and never replaced: `tournament.json`, one file per
agent under `entrants/` and `benchmarks/`, and one per eval run under `runs/`.

An agent is what plays together with how it is configured, identified across
eval runs by a digest of both, never by its name: one checkpoint at two
search budgets is two agents. A match seats agents by position (`player`),
names groups of seats and fixes the conditions it is played under.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, model_validator

from etude.run.records import Digest, Name, Record

type Config = dict[str, JsonValue]
type ColumnType = Literal["BOOLEAN", "BIGINT", "DOUBLE", "VARCHAR"]

# The column type of each Python value a table or config may hold.
COLUMN_TYPES: dict[type, ColumnType] = {
    bool: "BOOLEAN",
    int: "BIGINT",
    float: "DOUBLE",
    str: "VARCHAR",
}

# Every match has this group, holding every seat.
EVERYONE = "all"

# `tables/TABLE.WRITTEN.WRITER.parquet`: WRITTEN is zero-padded nanoseconds, so a
# table's file names order by write time; WRITER is a match key or a merging process.
TABLE_FILE = re.compile(r"^tables/([A-Za-z][A-Za-z0-9_]*)\.(\d{20})\.([0-9a-f]+)\.parquet$")


def table_file(table: str, written_ns: int, writer: str) -> str:
    return f"tables/{table}.{written_ns:020d}.{writer}.parquet"


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
    """What matches are played in. Results from different identities are never pooled."""

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
        """Identifies the match wherever it is played."""
        return match_key(
            [agent.identity for agent in self.agents], self.groups, self.conditions, self.games
        )

    @property
    def seed(self) -> int:
        """Derived from the key, so a replayed match draws the same games."""
        return int(self.key[:15], 16)


def match_key(
    seating: Sequence[Digest],
    groups: Mapping[str, Sequence[int]],
    conditions: Config,
    games: int,
) -> Digest:
    """A match's key from its agents' identities, for readers that hold no `Agent`."""
    named = {name: list(seats) for name, seats in groups.items()}
    return digest([list(seating), named, conditions, games])


class MatchRecord(Record):
    """How one match went, written after its tables: `matches/KEY.json` when done.

    A failed match is `matches/KEY.failed.json`, so a listing alone tells which
    matches are done; a later success sits beside it and wins.
    """

    match: Match
    state: Literal["done", "failed"]
    error: str | None = None
    # Each table's column types in this match's write; a resume holds later writes to them.
    columns: dict[Name, dict[Name, ColumnType]] = {}
    # When it was recorded: of two runs holding one match, a tournament takes the later.
    wall_ms: int = Field(ge=0)

    @property
    def file(self) -> str:
        suffix = "json" if self.state == "done" else "failed.json"
        return f"matches/{self.match.key}.{suffix}"


class Tournament(Record):
    """`tournaments/NAME/tournament.json`: what every match of a tournament shares.

    A benchmark pairs every entrant with every benchmark; a round robin pairs
    every two entrants once. Each pairing is one match per condition. Fixed
    once stored. `benchmarks` are the ones known when it was described; the
    stored ones are files beside it, and `tournament.json` leaves them out.
    """

    format: Literal["benchmark", "round_robin"]
    name: Name
    environment: Environment
    conditions: tuple[Config, ...] = Field(min_length=1)
    games: int = Field(ge=1)
    benchmarks: tuple[Agent, ...] = ()


class TournamentRun(Record):
    """`tournaments/NAME/runs/RUN.json`: an eval run held for the tournament, and what it was given."""

    run: Name
    entrants: tuple[Digest, ...] = ()
    benchmarks: tuple[Digest, ...] = ()
    # (k, n): the run plays the matches whose key is k modulo n.
    share: tuple[int, int] = (0, 1)
