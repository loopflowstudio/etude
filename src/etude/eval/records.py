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
`Seating.matches` and `RoundRobin.matches` wherever it is read.

A `Seating` is the rule for who meets whom in a benchmark. A relative seat
(`Earlier`, `Fraction`, `At`, `Final`, `Aligned`) names a checkpoint by where
it stands relative to the held one; it is found among the experiment's
training `Line`s, which the caller passes, since evaluation reads no training
run. What it finds is exact, so found for good: the hold records it
(`Seated`), and every later read schedules from the record.

A `Player` is whatever takes a seat: a `Model`, a checkpoint and how it plays
(`Inference`), or `Code`. A player is identified by its key: the checkpoint's
own key (`CheckpointKey`) or the code's name, together with its config. One
checkpoint at two search budgets is two models; two checkpoint keys holding
the same bytes are two models too, since where a checkpoint's bytes came from
is not part of what it is. The code's commit and source digest are its
`Version`, where a checkpoint stood on its clocks is `CheckpointRef.at` and
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
    field_validator,
    model_validator,
)

from etude.eval.source import digest_files, source_commit
from etude.tracking.errors import EtudeError
from etude.tracking.records import BUILT_IN_NAMES, ArtifactRef, Digest, Name, Record

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


def folder_name(name: str) -> str:
    """`name` as the start of an eval run's name, which is a folder: a tournament's name
    may hold spaces, as a seating's may; a name already safe is itself."""
    return re.sub(r"[^A-Za-z0-9._=-]+", "-", name).strip("-._=")[:150] or "tournament"


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
        if self.address is not None:
            return self.address
        key = (
            self.name
            if self.name is not None and self.name.startswith("out/")
            else f"checkpoints/{self.name}"
        )
        return f"{self.run}/{key}"

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


# The config key an author keeps execution setup under, such as `{"setup": {"device": "cuda"}}`:
# how a player or environment was run, recorded with each match and part of no identity, so a
# game played on one device counts as played on any.
SETUP = "setup"


def identifying(config: Config) -> Config:
    """A config without its execution setup: what identity takes of it."""
    return {key: value for key, value in config.items() if key != SETUP}


def _encoded(player: str, config: Config) -> str:
    config = identifying(config)
    if not config:
        return player
    return player + " " + json.dumps(config, sort_keys=True, separators=(",", ":"))


class Inference(Record):
    """How a checkpoint plays: the author's `config` and the inference code's `Version`.

    `config` is the author's typed model or JSON, such as whether search runs
    on top of the network and how; it is part of each model's key. `source`
    is the inference code's files: their `Version`, recorded with each match
    but not part of the key, so changed inference code reuses what was played.
    In a seating it is a seat each checkpoint the benchmark is held for fills.
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
    """What matches are played in. Results from different identities are never pooled.

    Its `config`'s `setup` is how it was run, such as the device, and no part
    of its identity: games played under any setup pool.
    """

    name: Name
    # The digest of the files that define it.
    files: Digest
    config: Config = {}

    @property
    def identity(self) -> Digest:
        return digest([self.name, self.files, identifying(self.config)])


class Match(Record):
    """Players in their seats under fixed conditions, played for some games.

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


# The error of a match that returned no rows: refused when played (`etude.eval.match`).
NO_ROWS = "match_has_no_rows"


class MatchRecord(Record):
    """How one match went, kept in its block's index after its tables and blobs.

    A failed match's record is replaced by a later done one, wherever it is kept
    (`outcomes`). A match is done only with rows: Etude refuses one that returns none, and
    reads an older run's done record without columns as failed, so its games
    are asked for again rather than counted as played.
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
    # Actual execution setup, supplied with the result; never part of a match's identity.
    execution: Config = {}

    @model_validator(mode="before")
    @classmethod
    def _failed_without_rows(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic's raw input
        if isinstance(data, dict) and data.get("state") == "done" and not data.get("columns"):
            return {**data, "state": "failed", "error": f"EtudeError: {NO_ROWS}"}
        return data


def outcomes(records: Iterable[MatchRecord]) -> dict[Digest, MatchRecord]:
    """Each match's record that stands among `records`, by match key, in the order given.

    A done record replaces a failed one; of several done, the first stands, and
    of failed ones alone, the first. `etude.eval.query.statsdb` keeps the same
    rule across the blocks of a run, in the order it reads them.
    """
    found: dict[Digest, MatchRecord] = {}
    for record in records:
        held = found.get(record.match.key)
        if held is None or (held.state != "done" and record.state == "done"):
            found[record.match.key] = record
    return found


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
    the other seats' player keys in seat order, a list. A block sealed by
    etude 0.3.0 or earlier stored `opponents` comma-joined, which cannot be
    split back because keys hold commas: it reads as a one-element list of
    that text, which is the opponent's key in a two-seat match. A column no
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

    @property
    def digest(self) -> Digest:
        """The digest of its definition: beside its name, what makes it this rollup."""
        return digest(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def distinct_columns(self) -> Self:
        columns = [*self.by, *self.sums]
        if len(set(columns)) != len(columns) or {*ADDED, ROLLUP_ROWS} & set(columns):
            raise ValueError("rollup_columns_must_be_distinct_author_columns")
        tables = [self.table, *self.join]
        if len(set(tables)) != len(tables):
            raise ValueError("rollup_tables_must_be_distinct")
        return self


class _OnClock(Record):
    """A relative seat that looks for a value of `clock`, playing as `inference`.

    It looks in the held checkpoint's line, or with `job` in each of that
    job's lines. Positions are exact: with no checkpoint there, the seat is a
    gap. With `at_or_before`, the latest checkpoint at or before there
    instead, once the line has saved past it or ended, so what it finds never
    changes after. With `trained`, only a checkpoint saved after training
    began: the line's initial weights, where every clock its run declared
    stands at 0, are never seated, and a held checkpoint with no trained one
    to meet is a gap for good.
    """

    clock: str
    inference: Inference
    job: str | None = None
    at_or_before: bool = False
    trained: bool = False


class Earlier(_OnClock):
    """The held checkpoint's line exactly `by` before it on `clock`."""

    kind: Literal["earlier"] = "earlier"
    by: float = Field(gt=0)


class Fraction(_OnClock):
    """The held checkpoint's line at exactly `value` × where it stood on `clock`.

    `Fraction(clock="updates", value=0.5, ...)` seats 3,000 for 6,000.
    """

    kind: Literal["fraction"] = "fraction"
    value: float = Field(gt=0, lt=1)


class At(_OnClock):
    """The held checkpoint's line at `value` on `clock`, such as its initial weights at 0."""

    kind: Literal["at"] = "at"
    value: float


class Final(Record):
    """The held checkpoint's line's last checkpoint, once its run has ended, finished or failed."""

    kind: Literal["final"] = "final"
    inference: Inference
    job: str | None = None


class Aligned(_OnClock):
    """Each of job `job`'s lines at exactly where the held checkpoint stood on `clock`."""

    kind: Literal["aligned"] = "aligned"
    job: str


# A seat naming a checkpoint by where it stands relative to the one a seating is held for.
type Relative = Annotated[Earlier | Fraction | At | Final | Aligned, Field(discriminator="kind")]
# The same seats, for isinstance.
_RELATIVE = (Earlier, Fraction, At, Final, Aligned)

# Why a relative seat has no checkpoint: none there yet, or there never will be.
type Missing = Literal["not_yet", "never"]

# Relative seat values match exactly, up to float arithmetic (0.3 × 1,000).
_EXACT = 1e-9


class Line(Record):
    """A learner line as relative seats find it: a training run and the runs continuing it.

    `name` is its first run, as `CheckpointRef.line` names it; `seed` the
    seed that run declared; `job` the full name the report gives its job,
    `launch` its launch name; `ended` whether its latest run has ended, so it
    saves no more. `checkpoints` are its stored checkpoints in the order they
    were saved. A job's seeds are its lines.
    """

    name: str
    seed: int | None = None
    job: str | None = None
    launch: str | None = None
    ended: bool = False
    checkpoints: tuple[CheckpointRef, ...] = ()

    @property
    def order(self) -> tuple[bool, int, str]:
        """Lines sort by seed, then name; one with no declared seed after those with one."""
        return self.seed is None, self.seed or 0, self.name


class Pairing(Record):
    """One seed pair a seating plays for a held checkpoint, and its share of the point's games."""

    # By seat: the checkpoint a relative seat found; None for any other seat.
    seats: tuple[CheckpointRef | None, ...]
    games: int = Field(ge=1)


class Seated(Record):
    """The pairings a seating plays for one held checkpoint (`Seating.resolve`).

    Found among the experiment's lines, so the hold records it
    (`TournamentRun.seated`) and the schedule reads it back without them. A
    seed added to either job changes the split, and lines are only ever
    added, so of two the one that counted more lines stands, whichever eval
    run recorded it. No pairings: under `seeds="one"`, a checkpoint of a seed
    not chosen.
    """

    seating: str
    checkpoint: CheckpointKey
    pairings: tuple[Pairing, ...]
    # The lines of both sides it counted, by name.
    lines: tuple[str, ...] = ()


class Gap(Record):
    """A held checkpoint whose relative seat has no checkpoint, and why.

    `value` is where on `seat.clock` the seat looks for the held checkpoint;
    None for `Final`, or a held checkpoint whose `at` lacks that clock.
    """

    seating: str
    checkpoint: CheckpointKey
    missing: Missing
    seat: Relative
    value: float | None = None


class Seating(Record):
    """The rule for who meets whom: `players` in seat order, played under each of `conditions`.

    A seat is a fixed player, an `Inference` that each checkpoint the
    benchmark is held for fills, or a relative seat (`Earlier`, `Fraction`,
    `At`, `Final`, `Aligned`) naming a checkpoint by where it stands relative
    to the held one. Its value is the first player's against the second. With
    no inference seat it is played once, not for each checkpoint, so two
    chosen checkpoints can meet: `Inference.model(checkpoint)` seats one as the
    benchmark would. It makes one match per condition (`matches`).

    `games` is per condition and point: an `at` of the held checkpoint's
    job, whatever its seeds. With `seeds="every"` a point plays every pair of
    a seed of that job and a seed of the other seat's (its own line, or a
    fixed player, counting as one), its games split evenly over the pairs,
    the remainder to the lowest by seed. With `seeds="one"` it plays them all
    between one seed per side, chosen from the seating's name and the seed
    sets: faster, but the point shows one pair of seeds, not the jobs.
    """

    name: str = Field(min_length=1, max_length=200)
    players: tuple[Player | Inference | Relative, Player | Inference | Relative]
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)
    # The author's labels, such as "anchor" or "held-out", for choosing
    # which seatings a reading counts; `schedule` carries them.
    tags: tuple[Name, ...] = ()
    seeds: Literal["every", "one"] = "every"

    @model_validator(mode="after")
    def relative_to_a_held_checkpoint(self) -> Self:
        if self.relative and not any(isinstance(each, Inference) for each in self.players):
            raise ValueError("relative_seat_needs_an_inference_seat")
        return self

    @property
    def fixed(self) -> bool:
        """Whether every seat is a fixed player, so no checkpoint changes its matches."""
        return not any(isinstance(each, Inference) for each in self.players)

    @property
    def relative(self) -> bool:
        """Whether a seat names a checkpoint relative to the held one."""
        return any(isinstance(each, _RELATIVE) for each in self.players)

    @property
    def definition(self) -> Digest:
        """What two eval runs must agree on to share the name: keys, not versions."""
        # An inference seat's literal "model" predates the name `Inference`; it stays, so
        # every seating keeps the definition it was first held with.
        players = [
            ["model", identifying(each.config)]
            if isinstance(each, Inference)
            else ["relative", _relative_definition(each)]
            if isinstance(each, _RELATIVE)
            else each.key
            for each in self.players
        ]
        # Default seeds leave the definition every seating was first held with.
        seeds = [] if self.seeds == "every" else [self.seeds]
        return digest([self.name, players, self.conditions, self.games, self.tags, *seeds])

    def matches(
        self, checkpoint: CheckpointRef | None = None, seated: Seated | None = None
    ) -> list[Match]:
        """One match per condition and pairing: `checkpoint` in every inference seat, and in
        a relative seat the checkpoint the pairing found. Without `seated`, one pairing
        playing every game, as for a checkpoint of a job with one seed."""
        if seated is None:
            if self.relative:
                raise ValueError("relative_seat_needs_its_checkpoints")
            pairings: tuple[Pairing, ...] = (
                Pairing(seats=(None,) * len(self.players), games=self.games),
            )
        else:
            pairings = seated.pairings
        return [
            Match(
                players=tuple(
                    _seat(each, checkpoint, found)
                    for each, found in zip(self.players, pairing.seats, strict=True)
                ),
                conditions=dict(given),
                games=pairing.games,
            )
            for pairing in pairings
            for given in self.conditions
        ]

    def resolve(self, checkpoint: CheckpointRef, lines: Sequence[Line]) -> Seated | Gap:
        """The pairings this seating plays for `checkpoint`, one seed of its job, among the
        experiment's `lines`; or why its relative seat finds nothing to play yet.

        A seat naming a job plays each of its lines with a checkpoint there, the
        held checkpoint's own job included, and waits while any may still save
        one. A seat that finds the held checkpoint itself seats it: self-play is
        a match like any other.
        """
        held = next((line for line in lines if line.name == checkpoint.line), None)
        ordered = sorted(lines, key=lambda line: line.order)
        # The held checkpoint's side: its job's seeds, or its own line with no job.
        mine = (
            []
            if held is None
            else [held]
            if held.job is None
            else [line for line in ordered if line.job == held.job]
        )
        relative = next((each for each in self.players if isinstance(each, _RELATIVE)), None)
        # The other side's seeds: the lines of the job a relative seat names; its own line,
        # or a fixed player, counts as one (None).
        theirs: list[Line | None] = [None]
        if relative is not None and relative.job is not None:
            job = relative.job
            theirs = [line for line in ordered if job in (line.job, line.launch)]
        counted = tuple(sorted({line.name for line in (*mine, *theirs) if line is not None}))
        if self.seeds == "one" and mine and theirs:
            chosen, pick = _chosen(self.name, mine, theirs)
            if held != chosen:
                return Seated(
                    seating=self.name, checkpoint=checkpoint.key, pairings=(), lines=counted
                )
            mine, theirs = [chosen], [pick]
        found: list[CheckpointRef | None] = [None]
        if relative is not None:
            # A seat with no job looks in the held checkpoint's own line.
            looked = [held] if relative.job is None else theirs
            results = [_find(relative, checkpoint, line) for line in looked if line is not None]
            found = [each for each in results if isinstance(each, CheckpointRef)]
            if not found or "not_yet" in results:
                # No line yet: the job has not started, or the held one's line is unknown.
                unknown = not results and checkpoint.line is not None
                return Gap(
                    seating=self.name,
                    checkpoint=checkpoint.key,
                    missing="not_yet" if unknown or "not_yet" in results else "never",
                    seat=relative,
                    value=_wanted(relative, checkpoint),
                )
        # Pairs in seed order, (mine, theirs); this checkpoint's are contiguous from `first`.
        pairs = max(len(mine), 1) * len(found)
        first = (mine.index(held) if held in mine else 0) * len(found)
        pairings = [
            Pairing(
                seats=tuple(ref if isinstance(each, _RELATIVE) else None for each in self.players),
                games=games,
            )
            for index, ref in enumerate(found, start=first)
            if (games := _share(self.games, pairs, index))
        ]
        return Seated(
            seating=self.name, checkpoint=checkpoint.key, pairings=tuple(pairings), lines=counted
        )


def _relative_definition(seat: Relative) -> dict[str, JsonValue]:
    """A relative seat as a seating's definition takes it: its inference by config, less setup."""
    found = seat.model_dump(mode="json", exclude={"inference"})
    return {**found, "inference": {"config": identifying(seat.inference.config)}}


def _seat(
    each: Player | Inference | Relative,
    checkpoint: CheckpointRef | None,
    found: CheckpointRef | None,
) -> Player:
    """Who takes a seating's seat in one pairing."""
    if isinstance(each, _RELATIVE):
        if found is None:
            raise ValueError("relative_seat_needs_its_checkpoints")
        return each.inference.model(found)
    if isinstance(each, Inference):
        if checkpoint is None:
            raise ValueError("inference_seat_needs_a_checkpoint")
        return each.model(checkpoint)
    return each


def _share(games: int, pairs: int, index: int) -> int:
    """Pair `index`'s games of `games` split evenly over `pairs`, the remainder to the lowest."""
    return games // pairs + (1 if index < games % pairs else 0)


def _chosen(
    seating: str, mine: Sequence[Line], theirs: Sequence[Line | None]
) -> tuple[Line, Line | None]:
    """The one seed pair a `seeds="one"` seating plays: drawn from its name and the seed sets,
    never the clock, so every point and hold draws the same until a seed is added."""
    seeds = [
        [None if line is None else [line.seed, line.name] for line in side]
        for side in (mine, theirs)
    ]
    # The same row-major pair order as every-seed play, without materializing the product.
    index = int(digest([seating, seeds])[:15], 16) % (len(mine) * len(theirs))
    one, other = divmod(index, len(theirs))
    return mine[one], theirs[other]


def _wanted(seat: Relative, held: CheckpointRef) -> float | None:
    """Where on its clock the seat looks for `held`; None for `Final`, or with no value there."""
    if isinstance(seat, Final):
        return None
    if isinstance(seat, At):
        return seat.value
    here = held.at.get(seat.clock)
    if here is None:
        return None
    if isinstance(seat, Earlier):
        return here - seat.by
    if isinstance(seat, Fraction):
        return here * seat.value
    return here


def _find(seat: Relative, held: CheckpointRef, line: Line) -> CheckpointRef | Missing:
    """The checkpoint of `line` the seat names for `held`, or why there is none.

    "never" once the line has ended or has passed the value without saving there.
    At or before, the latest checkpoint up to the value, once one is past it or
    the line has ended: until then a later save could still be nearer.
    """
    if isinstance(seat, Final):
        if not line.ended:
            return "not_yet"
        return line.checkpoints[-1] if line.checkpoints else "never"
    wanted = _wanted(seat, held)
    if wanted is None:
        return "never"
    clock, tolerance = seat.clock, _EXACT * max(1.0, abs(wanted))
    passed = any(each.at.get(clock, float("-inf")) > wanted for each in line.checkpoints)
    seatable = [c for c in line.checkpoints if not seat.trained or _trained(c)]
    if seat.at_or_before:
        if not (passed or line.ended):
            return "not_yet"
        before = [c for c in seatable if c.at.get(clock, float("inf")) <= wanted + tolerance]
        latest = max(before, key=lambda c: c.at[clock], default=None)
        return "never" if latest is None else latest
    for each in seatable:
        if abs(each.at.get(clock, float("inf")) - wanted) <= tolerance:
            return each
    return "never" if line.ended or passed else "not_yet"


def _trained(checkpoint: CheckpointRef) -> bool:
    """Whether it was saved after training began: a clock its run declared stands above 0.

    Etude's hours run from `track(...)`, so they are above 0 even for initial
    weights; they decide only for a run that declared no clock.
    """
    declared = [value for clock, value in checkpoint.at.items() if clock not in BUILT_IN_NAMES]
    return any(value > 0 for value in declared or checkpoint.at.values())


class Benchmark(Record):
    """Seatings a model's checkpoints are each held for, to follow it through training.

    Its seatings are every seating any of its eval runs was held with. A
    seating of fixed players is held once, such as one checkpoint against an
    earlier one, so an experiment can name the pairs it wants as checkpoints
    appear and a batch hold plays those not yet played.
    """

    name: str = Field(min_length=1, max_length=200)
    environment: Environment
    seatings: tuple[Seating, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def seatings_were_matchups(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic's raw input
        # etude-sdk before 0.3 stored them as `matchups`.
        if isinstance(data, dict) and "matchups" in data:
            data = {("seatings" if key == "matchups" else key): v for key, v in data.items()}
        return data

    @model_validator(mode="after")
    def named(self) -> Self:
        if len({each.name for each in self.seatings}) != len(self.seatings):
            raise ValueError("duplicate_seating_name")
        return self


class RoundRobin(Record):
    """Hand-picked players, every two of them once per condition.

    Its entrants are every player any of its eval runs entered. Seat swaps
    belong inside the author's match.
    """

    name: str = Field(min_length=1, max_length=200)
    environment: Environment
    # Mapping, so a literal like `{"deal": 1}` type-checks as a condition.
    conditions: tuple[Mapping[str, JsonValue], ...] = Field(min_length=1)
    games: int = Field(ge=1)

    def matches(self, entrants: Iterable[Player]) -> list[Match]:
        """Each pair once per condition, in key order; of two entrants with one key, the later."""
        ordered = sorted({each.key: each for each in entrants}.items())
        return [
            Match(players=(a, b), conditions=dict(given), games=self.games)
            for (_, a), (_, b) in combinations(ordered, 2)
            for given in self.conditions
        ]


class Share(Record):
    """Part `index` of `count` contiguous parts of a hold's scheduled matches in play order."""

    index: int = Field(default=0, ge=0)
    count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _within(self) -> Self:
        if self.index >= self.count:
            raise ValueError("share_out_of_range")
        return self


class TournamentRun(Record):
    """`RUN/tournament.json`: the tournament an eval run was held for, and what it added."""

    tournament: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Player, ...] = ()
    # What relative seats found for the checkpoints it scheduled (`Seating.resolve`).
    seated: tuple[Seated, ...] = ()
    # The part of the scheduled matches the run plays (`etude.eval.tournament.hold`).
    share: Share = Share()

    @field_validator("share", mode="before")
    @classmethod
    def _share_pair(cls, given: object) -> object:
        # etude 0.3.0 and earlier stored the share as an [index, count] pair.
        if isinstance(given, (list, tuple)) and len(given) == 2:
            return {"index": given[0], "count": given[1]}
        return given

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
    """A match a tournament calls for; a benchmark's also names its seating and, for a
    seating with an inference seat, its checkpoint."""

    match: Match
    seating: str | None = None
    checkpoint: CheckpointKey | None = None
    tags: tuple[Name, ...] = ()


class Tournament(Record):
    """A tournament as its eval runs hold it: every seating, checkpoint and entrant they added."""

    definition: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Player, ...] = ()
    seated: tuple[Seated, ...] = ()

    def schedule(self) -> list[Scheduled]:
        """Each seating for each checkpoint, or once when fixed, or each pair of entrants;
        held or not. A checkpoint plays the pairings `seated` records for it; a relative
        seating waits for one, and any other plays every game in one pairing until then."""
        if isinstance(self.definition, RoundRobin):
            return [Scheduled(match=match) for match in self.definition.matches(self.entrants)]
        found = {(each.seating, str(each.checkpoint)): each for each in self.seated}
        scheduled = [
            Scheduled(match=match, seating=seating.name, tags=seating.tags)
            for seating in self.definition.seatings
            if seating.fixed
            for match in seating.matches()
        ]
        for seating in self.definition.seatings:
            if seating.fixed:
                continue
            for checkpoint in self.checkpoints:
                seated = found.get((seating.name, str(checkpoint.key)))
                if seating.relative and seated is None:
                    continue
                scheduled += [
                    Scheduled(
                        match=match,
                        seating=seating.name,
                        checkpoint=checkpoint.key,
                        tags=seating.tags,
                    )
                    for match in seating.matches(checkpoint, seated)
                ]
        return scheduled

    def resolve(self, lines: Sequence[Line]) -> tuple[Tournament, tuple[Gap, ...]]:
        """The tournament with each held checkpoint's pairings among `lines` in `seated`, each
        replacing what was recorded; and each held checkpoint a relative seat still finds
        nothing for. A gap keeps a recorded resolution: it stands until the new one plays."""
        if not isinstance(self.definition, Benchmark):
            return self, ()
        seated = list(self.seated)
        gaps: list[Gap] = []
        recorded = {(each.seating, str(each.checkpoint)) for each in self.seated}
        for seating in self.definition.seatings:
            if seating.fixed:
                continue
            for checkpoint in self.checkpoints:
                result = seating.resolve(checkpoint, lines)
                if isinstance(result, Seated):
                    seated.append(result)
                elif (seating.name, str(checkpoint.key)) not in recorded:
                    gaps.append(result)
        return self.model_copy(update={"seated": _most_lines(seated)}), tuple(gaps)


def _most_lines(seated: Iterable[Seated]) -> tuple[Seated, ...]:
    """One `Seated` per seating and held checkpoint: of two, the one that counted more lines,
    and of those resolved alike, the later."""
    merged: dict[tuple[str, str], Seated] = {}
    for each in seated:
        key = (each.seating, str(each.checkpoint))
        kept = merged.get(key)
        if kept is None or len(each.lines) >= len(kept.lines):
            merged[key] = each
    return tuple(merged[key] for key in sorted(merged))


def assemble(held: Iterable[TournamentRun]) -> Tournament:
    """The tournament its eval runs make together, from the first of them on.

    Raises `EtudeError("tournament_conflict")` when they disagree on its
    format or environment (its setup aside), or a round robin's conditions or games, and
    `EtudeError("seating_conflict")` when they define one seating name two ways.
    Versions are not definitions: a later run's seating, checkpoint or entrant
    with the same key replaces an earlier one, so what is played next records
    the version that plays it.
    """
    runs = list(held)
    first = runs[0].tournament
    seatings: dict[str, Seating] = {}
    checkpoints: dict[str, CheckpointRef] = {}
    entrants: dict[str, Player] = {}
    for run in runs:
        given = run.tournament
        if (
            type(given) is not type(first)
            or given.environment.identity != first.environment.identity
        ):
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if (
            isinstance(given, RoundRobin)
            and given.model_copy(update={"environment": first.environment}) != first
        ):
            raise EtudeError("tournament_conflict", 409) from ValueError(first.name)
        if isinstance(given, Benchmark):
            for each in given.seatings:
                if seatings.setdefault(each.name, each).definition != each.definition:
                    raise EtudeError("seating_conflict", 409) from ValueError(each.name)
                seatings[each.name] = each
        checkpoints.update((str(each.key), each) for each in run.checkpoints)
        entrants.update((each.key, each) for each in run.entrants)
    definition = (
        first.model_copy(update={"seatings": tuple(seatings[name] for name in sorted(seatings))})
        if isinstance(first, Benchmark)
        else first
    )
    return Tournament(
        definition=definition,
        checkpoints=tuple(checkpoints[key] for key in sorted(checkpoints)),
        entrants=tuple(entrants[key] for key in sorted(entrants)),
        seated=_most_lines(each for run in runs for each in run.seated),
    )
