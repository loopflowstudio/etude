"""File contracts for evaluation: agents, environments, matches and tournaments.

An eval run is a run with kind "evaluation" and no clocks: `run.json`, then
`environment.json`, then for each match the author's tables as Parquet under
`tables/` and Etude's record of the match, `matches/KEY.json` (or
`matches/KEY.failed.json`).

An eval run held for a tournament (`Benchmark` or `RoundRobin`) also stores
`tournament.json` (`TournamentRun`): the definition it was held with and what
it added. A tournament is nothing more than the eval runs naming it;
`assemble` makes one from them, and its schedule follows by
`Matchup.matches` and `RoundRobin.matches` wherever it is read.

An agent is what plays together with how it is configured, identified across
eval runs by a digest of both, never by its name: one checkpoint at two
search budgets is two agents, and so is one checkpoint run by two versions of
its inference code when the agent names that code. A match seats agents by
position (`player`), names groups of seats and fixes the conditions it is
played under.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from etude.eval.source import digest_files
from etude.run.errors import EtudeError
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

# An eval run held for a tournament keeps this file in its folder (`TournamentRun`).
TOURNAMENT_FILE = "tournament.json"

# Every match has this group, holding every seat.
EVERYONE = "all"

# `tables/TABLE.WRITTEN.WRITER.parquet`: WRITTEN is zero-padded nanoseconds, so a
# table's file names order by write time; WRITER is a match key or a merging process.
TABLE_FILE = re.compile(r"^tables/([A-Za-z][A-Za-z0-9_]*)\.(\d{20})\.([0-9a-f]+)\.parquet$")


_CONFIG: TypeAdapter[Config] = TypeAdapter(dict[str, JsonValue])


def as_config(config: Mapping[str, object] | BaseModel | None) -> Config:
    """An author's config, a Pydantic model or a mapping, as the JSON Etude stores."""
    if config is None:
        return {}
    data = config.model_dump(mode="json") if isinstance(config, BaseModel) else dict(config)
    try:
        return _CONFIG.validate_python(data)
    except ValidationError as error:
        raise EtudeError("config_is_not_json", 422) from error


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
    # A checkpoint's: the digest of the source files that run it, when named.
    code: Digest | None = None
    config: Config = {}

    @property
    def identity(self) -> Digest:
        if self.code is None:
            return digest([self.kind, self.artifact, self.config])
        return digest([self.kind, self.artifact, self.code, self.config])


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
    """The checkpoint a benchmark is held for, run by `code` and playing with `config`.

    `config` is the author's typed model or JSON, such as whether search runs
    on top of the network and how. Etude stores it and splits on it only.
    `code` is the inference code's source files, hashed like a code agent's,
    so a checkpoint run by changed code is another agent.
    """

    # The digest of the inference code's source files, when named.
    code: Digest | None = None
    config: Config = {}

    if TYPE_CHECKING:
        # What a person writes; the validators below make the stored fields of it.
        def __init__(
            self,
            *,
            code: Sequence[Path | str] = (),
            config: Mapping[str, object] | BaseModel | None = None,
        ) -> None: ...

    @field_validator("code", mode="before")
    @classmethod
    def hashed(cls, value: object) -> object:
        # Source files given as a list or tuple of paths; a stored record holds their digest.
        if isinstance(value, list | tuple):
            return digest_files(value) if value else None
        return value

    @field_validator("config", mode="before")
    @classmethod
    def dumped(cls, value: object) -> object:
        return as_config(value) if value is None or isinstance(value, BaseModel) else value

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
            code=self.code,
            config=self.config,
        )


class Matchup(Record):
    """A named line of a benchmark: `players` in seat order, each a fixed agent or the model.

    Its value is the first player's against the second. Both may be the model,
    such as search against no search. With no model seat it is played once,
    not for each checkpoint, so two chosen checkpoints can meet:
    `Model.agent(checkpoint)` seats one as the benchmark's model would be.
    """

    name: str = Field(min_length=1, max_length=200)
    players: tuple[Agent | Model, Agent | Model]
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)
    # The author's labels, such as "consecutive" or "held-out", for choosing
    # which matchups a reading counts; `schedule` carries them.
    tags: tuple[Name, ...] = ()

    @property
    def fixed(self) -> bool:
        """Whether every seat is a fixed agent, so no checkpoint changes its matches."""
        return not any(isinstance(each, Model) for each in self.players)

    def matches(self, checkpoint: ArtifactRef | None = None) -> list[Match]:
        """One match per condition, with `checkpoint` in every model's seat."""
        seated: list[Agent] = []
        for each in self.players:
            if isinstance(each, Agent):
                seated.append(each)
            elif checkpoint is None:
                raise ValueError("model_seat_needs_a_checkpoint")
            else:
                seated.append(each.agent(checkpoint))
        return [
            Match(agents=tuple(seated), conditions=dict(given), games=self.games)
            for given in self.conditions
        ]


class Benchmark(Record):
    """Matchups a model's checkpoints are each held for, to follow it through training.

    Its matchups are every matchup any of its eval runs was held with. A
    matchup of fixed agents is held once, such as one checkpoint against an
    earlier one, so an experiment can name the pairs it wants as checkpoints
    appear and a batch hold plays those not yet played.
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

    Its entrants are every agent any of its eval runs entered. Seat swaps
    belong inside the author's match.
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
    """`RUN/tournament.json`: the tournament an eval run was held for, and what it added."""

    tournament: Benchmark | RoundRobin
    checkpoints: tuple[ArtifactRef, ...] = ()
    entrants: tuple[Agent, ...] = ()
    # (k, n): of the schedule's keys in sorted order, the run plays every nth from the kth.
    share: tuple[int, int] = (0, 1)

    @model_validator(mode="before")
    @classmethod
    def one_checkpoint(cls, data: Any) -> Any:
        # Files from etude-sdk 0.2 name at most one checkpoint, as `checkpoint`.
        if isinstance(data, dict) and "checkpoint" in data:
            given = data.pop("checkpoint")
            data = {**data, "checkpoints": [] if given is None else [given]}
        return data


class Scheduled(Record):
    """A match a tournament calls for; a benchmark's also names its matchup and, for a
    matchup with a model seat, its checkpoint."""

    match: Match
    matchup: str | None = None
    checkpoint: Digest | None = None
    tags: tuple[Name, ...] = ()


class Tournament(Record):
    """A tournament as its eval runs hold it: every matchup, checkpoint and entrant they added."""

    definition: Benchmark | RoundRobin
    checkpoints: tuple[ArtifactRef, ...] = ()
    entrants: tuple[Agent, ...] = ()

    def schedule(self) -> list[Scheduled]:
        """Each matchup for each checkpoint, or once when fixed, or each pair of entrants;
        held or not."""
        if isinstance(self.definition, RoundRobin):
            return [Scheduled(match=match) for match in self.definition.matches(self.entrants)]
        fixed = [
            Scheduled(match=match, matchup=matchup.name, tags=matchup.tags)
            for matchup in self.definition.matchups
            if matchup.fixed
            for match in matchup.matches()
        ]
        return fixed + [
            Scheduled(
                match=match, matchup=matchup.name, checkpoint=checkpoint.sha256, tags=matchup.tags
            )
            for matchup in self.definition.matchups
            if not matchup.fixed
            for checkpoint in self.checkpoints
            for match in matchup.matches(checkpoint)
        ]


def assemble(held: Iterable[TournamentRun]) -> Tournament:
    """The tournament its eval runs make together, from the first of them on.

    Raises `EtudeError("tournament_conflict")` when they disagree on its
    format or environment, or a round robin's conditions or games, and
    `EtudeError("matchup_conflict")` when they define one matchup name two ways.
    """
    runs = list(held)
    first = runs[0].tournament
    matchups: dict[str, Matchup] = {}
    checkpoints: dict[Digest, ArtifactRef] = {}
    entrants: dict[Digest, Agent] = {}
    for run in runs:
        given = run.tournament
        if type(given) is not type(first) or given.environment != first.environment:
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if isinstance(given, RoundRobin) and given != first:
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if isinstance(given, Benchmark):
            for each in given.matchups:
                if matchups.setdefault(each.name, each) != each:
                    raise EtudeError("matchup_conflict", 409) from ValueError(each.name)
        for checkpoint in run.checkpoints:
            if checkpoint.sha256 is not None:
                checkpoints.setdefault(checkpoint.sha256, checkpoint)
        entrants.update((each.identity, each) for each in run.entrants)
    definition = (
        first.model_copy(update={"matchups": tuple(matchups[name] for name in sorted(matchups))})
        if isinstance(first, Benchmark)
        else first
    )
    return Tournament(
        definition=definition,
        checkpoints=tuple(checkpoints[key] for key in sorted(checkpoints)),
        entrants=tuple(entrants[key] for key in sorted(entrants)),
    )
