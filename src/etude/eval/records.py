"""File contracts for evaluation: players, environments, matches and tournaments.

An eval run is a run with kind "evaluation" and no clocks: `run.json`, then
`environment.json`, then sealed blocks of matches (`etude.eval.block`), each
holding the author's tables as Parquet, the blobs its matches kept and
Etude's record of each match (`MatchRecord`). Older eval runs kept a file per
fact (`matches/KEY.json`, `tables/TABLE_FILE`); `etude convert-eval` makes
blocks of them.

An eval run held for a tournament (`Benchmark` or `RoundRobin`) also stores
`tournament.json` (`TournamentRun`): the definition it was held with and what
it added. A tournament is nothing more than the eval runs naming it;
`assemble` makes one from them, and its schedule follows by
`Matchup.matches` and `RoundRobin.matches` wherever it is read.

A `Player` is whatever takes a seat: a `Model`, a checkpoint and how it plays
(`Inference`), or `Code`. A player is identified by its key: the checkpoint's
own key (`CheckpointKey`) or the code's name, together with its config. One
checkpoint at two search budgets is two models; two checkpoint keys holding
the same bytes are two models too, since where a checkpoint's bytes came from
is not part of what it is. The code's commit and source digest are its
`Version`, a checkpoint's position on its clocks is `CheckpointRef.at` and
the learner it belongs to is `CheckpointRef.line`; all are recorded with each
match but not part of the key: a match one
version played counts as played for every version, and reads can select a
version. A match seats players by position (`seat`), names groups of seats
and fixes the conditions it is played under. A match's key is the digest of
its players' keys, its groups and conditions.

Records published etude-sdk wrote, with `agents` and etude-sdk 0.2's digest
identities, load as players (`_player`); a 0.2 player's key equals no key
since, so its matches stay readable and are never counted as played again.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
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

# Author table and column names: plain SQL identifiers. A leading underscore is Etude's.
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Columns Etude adds to every row.
ADDED = ("run", "match")
# A rollup's count of the rows it summarizes.
ROLLUP_ROWS = "rows"
# Rollup `by` columns Etude fills from a row's seat: its player's key, and the others'.
ROLLUP_SEATED = ("player", "opponents")

# An eval run held for a tournament keeps this file in its folder (`TournamentRun`).
TOURNAMENT_FILE = "tournament.json"

# Every match has this group, holding every seat.
EVERYONE = "all"

# The per-file layout's `tables/TABLE.WRITTEN.WRITER.parquet`: WRITTEN is zero-padded
# nanoseconds, so a table's file names order by write time; WRITER is a match key or
# a merging process. Only `etude convert-eval` reads it.
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
    """A checkpoint to seat: its key, and the file holding it, which must carry its digest.

    `at` is where it stood on each clock, such as `{"updates": 600}`: a
    declared fact the seat carries to its matches so a report can place it,
    and part of no key. `line` names the learner it belongs to, such as a
    seed or a recipe, so a report compares it with that learner's other
    checkpoints; also part of no key. A tracked checkpoint's ref fills both
    from its run; give them yourself for a checkpoint no Etude run recorded.
    """

    key: CheckpointKey
    artifact: ArtifactRef
    at: dict[str, float] = {}
    line: str | None = None

    @model_validator(mode="after")
    def verifiable(self) -> Self:
        if self.artifact.sha256 is None:
            raise ValueError("model_checkpoint_requires_digest")
        return self


class Version(Record):
    """Which version of a player's code played: provenance, not identity."""

    # The git commit its source was at; None outside a checkout or launched job.
    commit: str | None = None
    # The digest of its named source files, so an uncommitted edit shows (`digest_files`).
    source: Digest | None = None


def _versioned(data: Any) -> Any:
    """Source files given as `source` become their `Version`; a stored record holds that."""
    if isinstance(data, dict) and "source" in data:
        data = dict(data)
        source = data.pop("source")
        if source:
            data["version"] = version_of(source)
    return data


def _config(value: object) -> object:
    return as_config(value) if value is None or isinstance(value, BaseModel) else value


# A config as an author gives it: a Pydantic model or None becomes stored JSON.
type _GivenConfig = Annotated[Config, BeforeValidator(_config)]


def _encoded(player: str, config: Config) -> str:
    if not config:
        return player
    return player + " " + json.dumps(config, sort_keys=True, separators=(",", ":"))


class Inference(Record):
    """How a checkpoint plays: the author's `config` and the inference code's `Version`.

    `config` is the author's typed model or JSON, such as whether search runs
    on top of the network and how; it is part of each model's key. `source`
    is the inference code's files: their `Version`, recorded with each match
    but not part of the key, so changed inference code reuses what was played.
    In a matchup it is a seat each checkpoint the benchmark is held for fills.
    """

    config: _GivenConfig = {}
    version: Version = Version()

    if TYPE_CHECKING:
        # What a person writes; the validators below make the stored fields of it.
        def __init__(
            self,
            *,
            source: Sequence[Path | str] = (),
            config: Mapping[str, object] | BaseModel | None = None,
        ) -> None: ...

    @model_validator(mode="before")
    @classmethod
    def versioned(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("code"), str):
            # etude-sdk 0.2 stored the inference files' digest as `code`.
            data = dict(data)
            data["version"] = {"source": data.pop("code")}
        return _versioned(data)

    def model(self, checkpoint: CheckpointRef) -> Model:
        """The model `checkpoint` is when it plays this way, named for its file."""
        return Model(
            name=re.sub(r"\s", "_", checkpoint.key.label), checkpoint=checkpoint, inference=self
        )


class Model(Record):
    """A checkpoint and how it plays (`Inference`). One checkpoint at two configs is two models."""

    kind: Literal["model"] = "model"
    # What a report calls it.
    name: Name
    checkpoint: CheckpointRef
    inference: Inference

    @property
    def key(self) -> str:
        """Its identity as one string, for files, columns and indexes."""
        return _encoded(f"checkpoint:{self.checkpoint.key}", self.config)

    # A player's config and version, as `Code` has them; `inference` holds both.
    @property
    def config(self) -> Config:
        return self.inference.config

    @property
    def version(self) -> Version:
        return self.inference.version


class Code(Record):
    """Code that plays, such as a heuristic, by its author's `name`, with its config."""

    kind: Literal["code"] = "code"
    name: Name
    config: _GivenConfig = {}
    version: Version = Version()

    if TYPE_CHECKING:
        # What a person writes; the validators below make the stored fields of it.
        def __init__(
            self,
            *,
            name: str,
            source: Sequence[Path | str] = (),
            config: Mapping[str, object] | BaseModel | None = None,
        ) -> None: ...

    @model_validator(mode="before")
    @classmethod
    def versioned(cls, data: Any) -> Any:
        return _versioned(data)

    @property
    def key(self) -> str:
        """Its identity as one string, for files, columns and indexes."""
        return _encoded(f"code:{self.name}", self.config)


def _player(data: Any) -> Any:
    """A player as published etude-sdk stored it (an `Agent`), as a `Model` or `Code`.

    An Agent was keyed by `key.checkpoint` or `key.code` with its config; an
    etude-sdk 0.2 agent by a digest of `kind`, `artifact`, `code` and `config`,
    which becomes the checkpoint address or code name `etude-sdk-0.2:DIGEST`,
    equal to no key since, so its matches are never counted as played again.
    """
    if not isinstance(data, dict) or data.get("kind") == "model":
        return data
    if "key" in data:
        key, version = data["key"], data.get("version") or {}
        config = key.get("config") or {}
        if key.get("checkpoint") is not None:
            ref = {"key": key["checkpoint"], "artifact": data.get("artifact")}
            inference = {"config": config, "version": version}
            return {
                "kind": "model",
                "name": data["name"],
                "checkpoint": ref,
                "inference": inference,
            }
        if key.get("code") is not None:
            return {"kind": "code", "name": key["code"], "config": config, "version": version}
        return data
    if data.get("kind") == "checkpoint" or (
        data.get("kind") == "code" and {"artifact", "uri", "code"} & data.keys()
    ):
        # etude-sdk 0.2 stored `kind`, a digest `artifact`, `uri`, `code` and `config`.
        config = data.get("config") or {}
        code = data.get("code")
        parts = [data["kind"], data.get("artifact"), *([code] if code else []), config]
        recorded = f"etude-sdk-0.2:{digest(parts)}"
        if data["kind"] == "code":
            return {"kind": "code", "name": recorded, "config": config}
        ref = {
            "key": {"address": recorded},
            "artifact": {"uri": data.get("uri") or recorded, "sha256": data.get("artifact")},
        }
        inference = {"config": config}
        return {"kind": "model", "name": data["name"], "checkpoint": ref, "inference": inference}
    return data


# What takes a seat: a checkpoint playing some way, or code.
type Player = Annotated[Model | Code, Field(discriminator="kind"), BeforeValidator(_player)]


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
    """A seating of players under fixed conditions, played for some games.

    The unit Etude schedules, resumes and resamples. Group `all` holds every
    seat and is added when not given.
    """

    # By seat: `seat` in a row is a position here.
    players: tuple[Player, ...] = Field(min_length=1)
    groups: dict[Name, tuple[int, ...]] = {}
    conditions: Config = {}
    games: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def everyone(cls, data: Any) -> Any:
        if isinstance(data, dict) and "agents" in data:
            # Published etude-sdk stores the players as `agents`.
            data = {("players" if key == "agents" else key): value for key, value in data.items()}
        if isinstance(data, dict) and EVERYONE not in data.get("groups", {}):
            seats = tuple(range(len(data.get("players", ()))))
            data = {**data, "groups": {EVERYONE: seats, **data.get("groups", {})}}
        return data

    @model_validator(mode="after")
    def seated(self) -> Self:
        if self.groups[EVERYONE] != tuple(range(len(self.players))):
            raise ValueError("group_all_is_every_seat")
        for seats in self.groups.values():
            if not seats or len(set(seats)) != len(seats):
                raise ValueError("group_seats_empty_or_repeated")
            if not all(0 <= seat < len(self.players) for seat in seats):
                raise ValueError("group_seat_out_of_range")
        return self

    @property
    def key(self) -> Digest:
        """Identifies the match wherever it is played."""
        seating = [player.key for player in self.players]
        groups = {name: list(seats) for name, seats in self.groups.items()}
        return digest([seating, groups, self.conditions, self.games])

    @property
    def seed(self) -> int:
        """Derived from the key, so a replayed match draws the same games."""
        return int(self.key[:15], 16)


class MatchRecord(Record):
    """How one match went, kept in its block's index after its tables and blobs.

    A failed match's record is replaced by a later done one, wherever it is kept.
    """

    match: Match
    state: Literal["done", "failed"]
    error: str | None = None
    # Each table's column types in this match's write; a resume holds later writes to them.
    columns: dict[Name, dict[Name, ColumnType]] = {}
    # When it was recorded: of two runs holding one match, a tournament takes the later.
    wall_ms: int = Field(ge=0)
    # The SHA256 of each blob the match kept, which its rows may name.
    blobs: tuple[Digest, ...] = ()


class Rollup(Record):
    """An additive summary of an author table, taken once per match as it is stored.

    `statsdb` keeps table `name`: for each run and each value of the `by`
    columns, how many rows there were (`rows`) and the total of each of
    `sums`, a DuckDB expression over a row (`"deck_copies > 0"`,
    `"opening_copies * win"`) in which a boolean counts as 1 and a null as 0;
    a mean is a total over `rows`. Only rows for which `where` holds count.

    A row is `table`'s, joined to each table in `join` on whichever of run,
    match, seat and game both have; a column of both is `table`'s. `by` may
    name `player`, the key of the player in the row's seat, and `opponents`,
    the other seats' player keys in seat order, comma-joined. A column no
    row of a match gave reads as null there.

    Each block keeps its matches' partial totals in `NAME.parquet`, so
    `statsdb` adds a block's totals and never reads an earlier block or a raw
    row again.
    """

    name: str = Field(pattern=IDENTIFIER.pattern)
    table: str = Field(pattern=IDENTIFIER.pattern)
    join: tuple[Annotated[str, Field(pattern=IDENTIFIER.pattern)], ...] = ()
    by: tuple[Annotated[str, Field(pattern=IDENTIFIER.pattern)], ...]
    sums: dict[Annotated[str, Field(pattern=IDENTIFIER.pattern)], str] = {}
    where: str | None = None

    @model_validator(mode="after")
    def distinct_columns(self) -> Self:
        columns = [*self.by, *self.sums]
        if len(set(columns)) != len(columns) or {*ADDED, ROLLUP_ROWS} & set(columns):
            raise ValueError("rollup_columns_must_be_distinct_author_columns")
        tables = [self.table, *self.join]
        if len(set(tables)) != len(tables):
            raise ValueError("rollup_tables_must_be_distinct")
        return self


class Matchup(Record):
    """A named line of a benchmark: `players` in seat order, each a fixed player or an `Inference`.

    An `Inference` seat is filled by each checkpoint the benchmark is held
    for. Its value is the first player's against the second. Both may be
    inference seats, such as search against no search. With no inference seat
    it is played once, not for each checkpoint, so two chosen checkpoints can
    meet: `Inference.model(checkpoint)` seats one as the benchmark would.
    """

    name: str = Field(min_length=1, max_length=200)
    players: tuple[Player | Inference, Player | Inference]
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)
    # The author's labels, such as "consecutive" or "held-out", for choosing
    # which matchups a reading counts; `schedule` carries them.
    tags: tuple[Name, ...] = ()

    @property
    def fixed(self) -> bool:
        """Whether every seat is a fixed player, so no checkpoint changes its matches."""
        return not any(isinstance(each, Inference) for each in self.players)

    @property
    def definition(self) -> Digest:
        """What two eval runs must agree on to share the name: keys, not versions."""
        # An inference seat's literal "model" predates the name `Inference`; it stays, so
        # every matchup keeps the definition it was first held with.
        players = [
            ["model", each.config] if isinstance(each, Inference) else each.key
            for each in self.players
        ]
        return digest([self.name, players, self.conditions, self.games, self.tags])

    def matches(self, checkpoint: CheckpointRef | None = None) -> list[Match]:
        """One match per condition, with `checkpoint` in every inference seat."""
        seated: list[Player] = []
        for each in self.players:
            if not isinstance(each, Inference):
                seated.append(each)
            elif checkpoint is None:
                raise ValueError("inference_seat_needs_a_checkpoint")
            else:
                seated.append(each.model(checkpoint))
        return [
            Match(players=tuple(seated), conditions=dict(given), games=self.games)
            for given in self.conditions
        ]


class Benchmark(Record):
    """Matchups a model's checkpoints are each held for, to follow it through training.

    Its matchups are every matchup any of its eval runs was held with. A
    matchup of fixed players is held once, such as one checkpoint against an
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
    """Hand-picked players, every two of them once per condition.

    Its entrants are every player any of its eval runs entered. Seat swaps
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

    def matches(self, entrants: Iterable[Player]) -> list[Match]:
        """Each pair once per condition, in key order; of two entrants with one key, the later."""
        ordered = sorted({each.key: each for each in entrants}.items())
        return [
            Match(players=(a, b), conditions=dict(given), games=self.games)
            for (_, a), (_, b) in combinations(ordered, 2)
            for given in self.conditions
        ]


class TournamentRun(Record):
    """`RUN/tournament.json`: the tournament an eval run was held for, and what it added."""

    tournament: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Player, ...] = ()
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
    matchup with an inference seat, its checkpoint."""

    match: Match
    matchup: str | None = None
    checkpoint: CheckpointKey | None = None
    tags: tuple[Name, ...] = ()


class Tournament(Record):
    """A tournament as its eval runs hold it: every matchup, checkpoint and entrant they added."""

    definition: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Player, ...] = ()

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
    entrants: dict[str, Player] = {}
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
        entrants.update((each.key, each) for each in run.entrants)
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
