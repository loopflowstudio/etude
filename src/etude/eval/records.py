"""File contracts for evaluation: agents, environments, matches and tournaments.

An eval run is a run with kind "evaluation" and no clocks: `run.json`, then
`environment.json`, then for each match the author's tables as Parquet under
`tables/` and Etude's record of the match, `matches/KEY.json` (or
`matches/KEY.failed.json`).

A tournament (`Benchmark` or `RoundRobin`) is stored beside the runs under
`tournaments/NAME/`, as files that are written once and never replaced. Its
schedule is derived from those files, by `Matchup.matches` and
`RoundRobin.matches`, wherever it is read.

An agent is what plays together with how it is configured, identified across
eval runs by a digest of both, never by its name: one checkpoint at two
search budgets is two agents. A match seats agents by position (`player`),
names groups of seats and fixes the conditions it is played under.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from itertools import combinations
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from etude.run.records import ArtifactRef, Digest, Name, Record
from etude.run.storage import SAFE_NAME

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
        seating = [agent.identity for agent in self.agents]
        groups = {name: list(seats) for name, seats in self.groups.items()}
        return digest([seating, groups, self.conditions, self.games])

    @property
    def seed(self) -> int:
        """Derived from the key, so a replayed match draws the same games."""
        return int(self.key[:15], 16)


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


class Model(Record):
    """The checkpoint a benchmark is held for, playing with `config`.

    `config` is the author's typed model or JSON, such as whether search runs
    on top of the network and how. Etude stores it and splits on it only.
    """

    config: Config = {}

    @field_validator("config", mode="before")
    @classmethod
    def dumped(cls, value: object) -> object:
        return value.model_dump(mode="json") if isinstance(value, BaseModel) else value

    def agent(self, checkpoint: ArtifactRef) -> Agent:
        """The agent this side is once `checkpoint`, which must carry its digest, fills it."""
        if checkpoint.sha256 is None:
            raise ValueError("checkpoint_agent_requires_digest")
        # Named for reading only; its last path part, as a training run names its files.
        name = re.sub(r"\s", "_", checkpoint.uri.rstrip("/").rsplit("/", 1)[-1])
        return Agent(
            name=name or checkpoint.sha256[:12],
            kind="checkpoint",
            artifact=checkpoint.sha256,
            uri=checkpoint.uri,
            config=self.config,
        )


class Matchup(Record):
    """A named line of a benchmark: `players` in seat order, each a fixed agent or the model.

    Its value is the first player's against the second. Both may be the model,
    such as search against no search.
    """

    name: str = Field(min_length=1, max_length=200)
    players: tuple[Agent | Model, Agent | Model]
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)

    @property
    def file(self) -> str:
        """`matchups/NAME.DEFINITION.json`, digests both: a listing alone shows a redefined name."""
        return f"matchups/{digest(self.name)}.{digest(self.model_dump(mode='json'))}.json"

    def matches(self, checkpoint: ArtifactRef) -> list[Match]:
        """One match per condition, with `checkpoint` in every model's seat."""
        seated = tuple(
            each.agent(checkpoint) if isinstance(each, Model) else each for each in self.players
        )
        return [
            Match(agents=seated, conditions=dict(given), games=self.games)
            for given in self.conditions
        ]


class Benchmark(Record):
    """Matchups a model's checkpoints are each held for, to follow it through training.

    Stored under `tournaments/NAME/`: `benchmark.json` (without matchups),
    `matchups/` (every matchup any hold defined), `models/SHA256.json` (each
    checkpoint held) and `runs/`.
    """

    name: Name
    environment: Environment
    matchups: tuple[Matchup, ...] = ()

    @model_validator(mode="after")
    def named(self) -> Self:
        if not SAFE_NAME.match(self.name):
            raise ValueError("unsafe_tournament_name")
        if len({each.name for each in self.matchups}) != len(self.matchups):
            raise ValueError("duplicate_matchup_name")
        return self


class RoundRobin(Record):
    """Hand-picked agents, every two of them once per condition.

    Stored under `tournaments/NAME/`: `round_robin.json`, `entrants/IDENTITY.json`
    and `runs/`. Seat swaps belong inside the author's match.
    """

    name: Name
    environment: Environment
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)

    @model_validator(mode="after")
    def named(self) -> Self:
        if not SAFE_NAME.match(self.name):
            raise ValueError("unsafe_tournament_name")
        return self

    def matches(self, entrants: Iterable[Agent]) -> list[Match]:
        """Each pair once per condition, in identity order."""
        ordered = sorted({each.identity: each for each in entrants}.items())
        return [
            Match(agents=(a, b), conditions=dict(given), games=self.games)
            for (_, a), (_, b) in combinations(ordered, 2)
            for given in self.conditions
        ]


class TournamentRun(Record):
    """`tournaments/NAME/runs/RUN.json`: an eval run held for the tournament, and what it added."""

    run: Name
    checkpoint: Digest | None = None
    entrants: tuple[Digest, ...] = ()
    # (k, n): the run plays the matches whose key is k modulo n.
    share: tuple[int, int] = (0, 1)
