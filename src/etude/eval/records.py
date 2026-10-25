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

An agent is identified by its key (`AgentKey`): the checkpoint it plays, by
the checkpoint's own key (`CheckpointKey`), or the name of the code that plays
it, together with its config. One checkpoint at two search budgets is two
agents; two checkpoint keys holding the same bytes are two agents too, since
where a checkpoint's bytes came from is not part of what it is. The code's
commit and source digest are its `Version`, recorded with each match but not
part of the key: a match one version played counts as played for every
version, and reads can select a version. A match seats agents by position
(`player`), names groups of seats and fixes the conditions it is played under.
A match's key is the digest of its seats' keys, its groups and conditions.

Agents recorded by etude-sdk 0.2 were identified by a digest; they load with
that digest as their key (`AgentKey.recorded`), equal to no key since, so their
matches stay readable and are never counted as played again.
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

from etude.eval.source import digest_files, source_commit
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


def version_of(code: Sequence[Path | str]) -> Version:
    """The version of the code in these source files: its commit, and their digest."""
    return Version(commit=source_commit(code), source=digest_files(code))


class CheckpointKey(Record):
    """A checkpoint by where its experiment keeps it: a training run's file, or an outside address.

    `run` and `name` name a training run's checkpoint (`RUN/checkpoints/NAME`);
    `address` names any other, such as a URI. Never its bytes' digest: two keys
    holding the same bytes are two checkpoints.
    """

    run: Name | None = None
    name: str | None = Field(default=None, min_length=1, max_length=500)
    address: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def one_place(self) -> Self:
        if (self.run is None) != (self.name is None) or (self.run is None) == (
            self.address is None
        ):
            raise ValueError("checkpoint_key_is_a_run_and_name_or_an_address")
        return self

    def __str__(self) -> str:
        return self.address if self.address is not None else f"{self.run}/checkpoints/{self.name}"

    @property
    def label(self) -> str:
        """Its last path part, as a report names it."""
        return str(self).rstrip("/").rsplit("/", 1)[-1] or str(self)


class CheckpointRef(Record):
    """A checkpoint to seat: its key, and the file holding it, which must carry its digest."""

    key: CheckpointKey
    artifact: ArtifactRef

    @model_validator(mode="after")
    def verifiable(self) -> Self:
        if self.artifact.sha256 is None:
            raise ValueError("checkpoint_agent_requires_digest")
        return self


class AgentKey(Record):
    """What identifies an agent: the checkpoint or code that plays, and its config."""

    checkpoint: CheckpointKey | None = None
    # A code agent's name, as its author calls it, such as "heuristic".
    code: Name | None = None
    # An etude-sdk 0.2 agent's digest identity.
    recorded: Digest | None = None
    config: Config = {}

    @model_validator(mode="after")
    def one_player(self) -> Self:
        given = [self.checkpoint is not None, self.code is not None, self.recorded is not None]
        if sum(given) != 1:
            raise ValueError("agent_key_is_a_checkpoint_or_code")
        return self

    @property
    def encoded(self) -> str:
        """The key as one string, for files, columns and indexes."""
        if self.checkpoint is not None:
            player = f"checkpoint:{self.checkpoint}"
        elif self.code is not None:
            player = f"code:{self.code}"
        else:
            player = f"etude-sdk-0.2:{self.recorded}"
        if not self.config:
            return player
        return player + " " + json.dumps(self.config, sort_keys=True, separators=(",", ":"))


class Version(Record):
    """Which version of an agent's code played: provenance, not identity."""

    # The git commit its source was at; None outside a checkout or launched job.
    commit: str | None = None
    # The digest of its named source files, so an uncommitted edit shows (`digest_files`).
    source: Digest | None = None


class Agent(Record):
    """A saved model or code, and the configuration it plays with: its key, and how it ran."""

    key: AgentKey
    # What a report calls it.
    name: Name
    # A checkpoint's file: where it lies here, and the digest it must have.
    artifact: ArtifactRef | None = None
    version: Version = Version()

    @model_validator(mode="before")
    @classmethod
    def recorded(cls, data: Any) -> Any:
        # etude-sdk 0.2 stored `kind`, a digest `artifact`, `uri`, `code` and `config`.
        if not isinstance(data, dict) or "kind" not in data:
            return data
        old = {key: data.get(key) for key in ("kind", "artifact", "code")}
        config = data.get("config") or {}
        parts = [old["kind"], old["artifact"], *([old["code"]] if old["code"] else []), config]
        uri = data.get("uri")
        return {
            "key": {"recorded": digest(parts), "config": config},
            "name": data["name"],
            "artifact": {"uri": uri, "sha256": old["artifact"]}
            if old["kind"] == "checkpoint" and uri
            else None,
        }

    @property
    def kind(self) -> Literal["checkpoint", "code"]:
        return "checkpoint" if self.key.checkpoint is not None or self.artifact else "code"

    @property
    def config(self) -> Config:
        return self.key.config


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
        seating = [agent.key.encoded for agent in self.agents]
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
    on top of the network and how; it is part of each agent's key. `code` is
    the inference code's source files: its `Version`, recorded with each match
    but not part of the key, so changed inference code reuses what was played.
    """

    config: Config = {}
    version: Version = Version()

    if TYPE_CHECKING:
        # What a person writes; the validators below make the stored fields of it.
        def __init__(
            self,
            *,
            code: Sequence[Path | str] = (),
            config: Mapping[str, object] | BaseModel | None = None,
        ) -> None: ...

    @model_validator(mode="before")
    @classmethod
    def versioned(cls, data: Any) -> Any:
        # Source files given as `code`; a stored record holds their version.
        if isinstance(data, dict) and "code" in data:
            data = dict(data)
            code = data.pop("code")
            if isinstance(code, list | tuple):
                if code:
                    data["version"] = version_of(code)
            elif isinstance(code, str):
                data["version"] = {"source": code}  # etude-sdk 0.2: the files' digest
        return data

    @field_validator("config", mode="before")
    @classmethod
    def dumped(cls, value: object) -> object:
        return as_config(value) if value is None or isinstance(value, BaseModel) else value

    def agent(self, checkpoint: CheckpointRef) -> Agent:
        """The agent this side is once `checkpoint` fills it."""
        return Agent(
            key=AgentKey(checkpoint=checkpoint.key, config=self.config),
            name=re.sub(r"\s", "_", checkpoint.key.label),
            artifact=checkpoint.artifact,
            version=self.version,
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

    @property
    def definition(self) -> Digest:
        """What two eval runs must agree on to share the name: keys, not versions."""
        players = [
            each.key.encoded if isinstance(each, Agent) else ["model", each.config]
            for each in self.players
        ]
        return digest([self.name, players, self.conditions, self.games, self.tags])

    def matches(self, checkpoint: CheckpointRef | None = None) -> list[Match]:
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
        """Each pair once per condition, in key order; of two entrants with one key, the later."""
        ordered = sorted({each.key.encoded: each for each in entrants}.items())
        return [
            Match(agents=(a, b), conditions=dict(given), games=self.games)
            for (_, a), (_, b) in combinations(ordered, 2)
            for given in self.conditions
        ]


class TournamentRun(Record):
    """`RUN/tournament.json`: the tournament an eval run was held for, and what it added."""

    tournament: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Agent, ...] = ()
    # (k, n): of the schedule's keys in sorted order, the run plays every nth from the kth.
    share: tuple[int, int] = (0, 1)

    @model_validator(mode="before")
    @classmethod
    def one_checkpoint(cls, data: Any) -> Any:
        # Files from etude-sdk 0.2 name at most one checkpoint, as `checkpoint`, by its
        # file alone; its key is its digest, which no key since is, so nothing is reused.
        if isinstance(data, dict) and "checkpoint" in data:
            data = dict(data)
            given = data.pop("checkpoint")
            held = (
                []
                if given is None
                else [
                    {"key": {"address": f"etude-sdk-0.2:{given.get('sha256')}"}, "artifact": given}
                ]
            )
            data["checkpoints"] = held
        return data


class Scheduled(Record):
    """A match a tournament calls for; a benchmark's also names its matchup and, for a
    matchup with a model seat, its checkpoint."""

    match: Match
    matchup: str | None = None
    checkpoint: CheckpointKey | None = None
    tags: tuple[Name, ...] = ()


class Tournament(Record):
    """A tournament as its eval runs hold it: every matchup, checkpoint and entrant they added."""

    definition: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
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
                match=match, matchup=matchup.name, checkpoint=checkpoint.key, tags=matchup.tags
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
    Versions are not definitions: a later run's matchup, checkpoint or entrant
    with the same key replaces an earlier one, so what is played next records
    the version that plays it.
    """
    runs = list(held)
    first = runs[0].tournament
    matchups: dict[str, Matchup] = {}
    checkpoints: dict[str, CheckpointRef] = {}
    entrants: dict[str, Agent] = {}
    for run in runs:
        given = run.tournament
        if type(given) is not type(first) or given.environment != first.environment:
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if isinstance(given, RoundRobin) and given != first:
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if isinstance(given, Benchmark):
            for each in given.matchups:
                if matchups.setdefault(each.name, each).definition != each.definition:
                    raise EtudeError("matchup_conflict", 409) from ValueError(each.name)
                matchups[each.name] = each
        checkpoints.update((str(each.key), each) for each in run.checkpoints)
        entrants.update((each.key.encoded, each) for each in run.entrants)
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
