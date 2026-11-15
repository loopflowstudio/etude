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

A player is a resolved checkpoint, code, or a composition of other players.
Compositions store an author's qualified record type and fields; their identity
replaces every checkpoint leaf by its key, never its current byte location.
Held and relative seats resolve before play. Old inference-configured players
retain their keys and provenance. Match identity fixes player keys by seat,
groups, config and game count.

Records published etude-sdk wrote, with `agents` and etude-sdk 0.2's digest
identities, load as players (`_player`); a 0.2 player's key equals no key
since, so its matches stay readable and are never counted as played again.
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from hashlib import sha256
from itertools import combinations, islice, product
from math import prod
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Generic, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    SerializeAsAny,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from typing_extensions import TypeVar

from etude.checkpoints import (
    CheckpointId,
    CheckpointRef,
    PolicyKey,
    _checkpoint_id,
    seen_from,
)
from etude.errors import EtudeError
from etude.eval.source import digest_files, source_commit
from etude.records import BUILT_IN_NAMES, Digest, Name, Record

__all__ = (
    "CheckpointPlayer",
    "Code",
    "Held",
    "Composition",
    "compose",
    "Benchmark",
    "RoundRobin",
    "Seating",
    "Seat",
    "Earlier",
    "Fraction",
    "At",
    "Final",
    "Aligned",
    "Match",
    "Environment",
    "Player",
    "Rollup",
)


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


class CheckpointPlayer(Record):
    """A resolved checkpoint leaf. Config and version read released model players."""

    kind: Literal["model"] = "model"
    name: Name
    checkpoint: CheckpointRef
    config: _GivenConfig = {}
    version: Version = Version()

    @model_validator(mode="before")
    @classmethod
    def _released(cls, value: object) -> object:
        if isinstance(value, dict) and "inference" in value:
            value = dict(value)
            inference = value.pop("inference")
            return {**value, **inference}
        return value

    @property
    def key(self) -> str:
        return _encoded(f"checkpoint:{self.checkpoint.key}", self.config)


def compose(player: BaseModel) -> Composition:
    """Make an author's player record portable, retaining its type and fields."""
    return Composition.model_validate(_encode_tree(player))


def checkpoint_player(checkpoint: CheckpointRef) -> CheckpointPlayer:
    """Seat a checkpoint directly, without an inference configuration."""
    return CheckpointPlayer(
        name=re.sub(r"\s", "_", (checkpoint.name or checkpoint.key).rstrip("/").rsplit("/", 1)[-1]),
        checkpoint=checkpoint,
    )


class Held(Record):
    """The checkpoint a benchmark is held for, used directly as a player."""

    kind: Literal["held"] = "held"

    def player(self, checkpoint: CheckpointRef) -> CheckpointPlayer:
        return checkpoint_player(checkpoint)


class Composition(Record):
    """Portable author record: its qualified type and recursively encoded fields.

    Only the game's build hook reconstructs author types. Reading results and
    scheduling never imports their code.
    """

    kind: Literal["composition"] = "composition"
    type: str  # "module:QualifiedName" of the author's record
    fields: dict[str, JsonValue]

    @property
    def key(self) -> str:
        if self.type == "etude:configured":
            over = _PLAYER.validate_python(self.fields["over"])
            config = _CONFIG.validate_python(self.fields["config"])
            if not isinstance(over, CheckpointPlayer):
                raise ValueError("configured checkpoint is unresolved")
            return _encoded(f"checkpoint:{over.checkpoint.key}", config)
        return "composition:" + digest([self.type, self.config])

    @property
    def name(self) -> str:
        """Qualified author type and fields, omitting fields set to None."""
        fields = ", ".join(
            f"{key}={_tree_name(value)}" for key, value in self.fields.items() if value is not None
        )
        return f"{self.type}({fields})"

    @property
    def config(self) -> Config:
        if self.type == "etude:configured":
            return _CONFIG.validate_python(self.fields["config"])
        return {key: _tree_identity(value) for key, value in self.fields.items()}

    @property
    def version(self) -> Version:
        if self.type == "etude:configured":
            return Version.model_validate(self.fields.get("version", {}))
        return Version()

    def player(self, checkpoint: CheckpointRef) -> Player:
        return _seat(self, checkpoint, None)


def _tree_identity(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        if value.get("kind") == "mapping":
            items = _CONFIG.validate_python(value["items"])
            return {key: _tree_identity(each) for key, each in items.items()}
        if value.get("kind") in ("model", "code", "composition"):
            return _PLAYER.validate_python(value).key
        return {key: _tree_identity(each) for key, each in value.items()}
    if isinstance(value, list):
        return [_tree_identity(each) for each in value]
    return value


def _tree_name(value: JsonValue) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind == "mapping":
            items = _CONFIG.validate_python(value["items"])
            return "{" + ", ".join(f"{k}: {_tree_name(v)}" for k, v in items.items()) + "}"
        if kind in ("model", "code", "composition"):
            return _PLAYER.validate_python(value).name
        if isinstance(kind, str):
            return kind
    if isinstance(value, list):
        return "[" + ", ".join(_tree_name(each) for each in value) + "]"
    return json.dumps(value)


def _encode_tree(value: object) -> JsonValue:
    if isinstance(value, (CheckpointPlayer, Code, Held, Composition, *_RELATIVE)):
        return _JSON.validate_python(value.model_dump(mode="json"))
    if isinstance(value, BaseModel):
        kind = type(value)
        if "<locals>" in kind.__qualname__:
            raise ValueError("player records must be importable module-level types")
        return {
            "kind": "composition",
            "type": f"{kind.__module__}:{kind.__qualname__}",
            "fields": {name: _encode_tree(getattr(value, name)) for name in kind.model_fields},
        }
    if isinstance(value, (list, tuple)):
        return [_encode_tree(each) for each in value]
    if isinstance(value, dict):
        # Tag mappings too: an author's literal {"kind": "model"} is data, not a seat.
        return {
            "kind": "mapping",
            "items": {str(key): _encode_tree(each) for key, each in value.items()},
        }
    return _JSON.validate_json(TypeAdapter(type(value)).dump_json(value))


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
    """A player as published etude-sdk stored it (an `Agent`), as a `CheckpointPlayer` or `Code`.

    An Agent was keyed by `key.checkpoint` or `key.code` with its config; an
    etude-sdk 0.2 agent by a digest of `kind`, `artifact`, `code` and `config`,
    which becomes the checkpoint address or code name `etude-sdk-0.2:DIGEST`,
    equal to no key since, so its matches are never counted as played again.
    """
    if isinstance(data, BaseModel) and not isinstance(data, (CheckpointPlayer, Code, Composition)):
        return _encode_tree(data)
    if not isinstance(data, dict) or data.get("kind") == "model":
        return data
    if "key" in data:
        key, version = data["key"], data.get("version") or {}
        config = key.get("config") or {}
        if key.get("checkpoint") is not None:
            ref = {"key": key["checkpoint"], "artifact": data.get("artifact")}
            return {
                "kind": "model",
                "name": data["name"],
                "checkpoint": ref,
                "config": config,
                "version": version,
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
        return {"kind": "model", "name": data["name"], "checkpoint": ref, "config": config}
    return data


# What takes a seat: a checkpoint playing some way, or code.
type Player = Annotated[
    CheckpointPlayer | Code | Composition, Field(discriminator="kind"), BeforeValidator(_player)
]


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


def _match_config_name(
    data: object, info: ValidationInfo, name: Literal["config", "configs"]
) -> object:
    # JSON retains the released field name so match keys and wire data stay stable.
    if info.mode == "python" and isinstance(data, dict) and "conditions" in data:
        warnings.warn(f"conditions is now {name}; removed in 0.5", DeprecationWarning, stacklevel=3)
    return data


_MatchConfig = TypeVar(
    "_MatchConfig",
    bound=BaseModel | Mapping[str, JsonValue],
    default=Mapping[str, JsonValue] | BaseModel,
    covariant=True,
)


class Match(Record, Generic[_MatchConfig]):
    """Players in their seats under a fixed config, played for some games.

    The unit Etude schedules, resumes and resamples. Group `all` holds every
    seat and is added when not given.
    """

    # By seat: `seat` in a row is a position here.
    players: tuple[Player, ...] = Field(min_length=1)
    groups: dict[Name, tuple[int, ...]] = {}
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)
    # The empty JSON default is validated into the specialized author type too.
    config: SerializeAsAny[_MatchConfig] = Field(
        default_factory=dict,
        validate_default=True,
        validation_alias=AliasChoices("config", "conditions"),
        serialization_alias="conditions",
    )

    games: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _config_name(cls, data: object, info: ValidationInfo) -> object:
        return _match_config_name(data, info, "config")

    @property
    def conditions(self) -> _MatchConfig:
        warnings.warn(
            "Match.conditions is now Match.config; removed in 0.5", DeprecationWarning, stacklevel=2
        )
        return self.config

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
        if any(
            not isinstance(leaf, (CheckpointPlayer, Code))
            for player in self.players
            for leaf in seat_leaves(player)
        ):
            raise ValueError("match_player_has_unresolved_checkpoint")
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
        return digest([seating, groups, as_config(self.config), self.games])

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
    # Infrastructure failures remain eligible; ordinary game errors require an explicit retry.
    retry: bool = False
    error: str | None = None
    # Each table's column types in this match's write; a resume holds later writes to them.
    columns: dict[Name, dict[Name, ColumnType]] = {}
    # When it was recorded: of two runs holding one match, a tournament takes the later.
    wall_ms: int = Field(ge=0)
    # Wall time inside play, excluding result sealing and delivery; older records lack it.
    seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
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
    of failures, the latest retry decision stands. `etude.eval.query.statsdb` keeps the same
    rule across the blocks of a run, in the order it reads them.
    """
    found: dict[Digest, MatchRecord] = {}
    for record in records:
        held = found.get(record.match.key)
        if (
            held is None
            or (held.state != "done" and record.state == "done")
            or (held.state == record.state == "failed" and record.wall_ms > held.wall_ms)
        ):
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


class _PolicySeat(Record):
    """A named policy selection; absent parts retain the released relative-seat definition."""

    experiment: str | None = None
    job: str | None = None
    seed: int | None = None
    role: str | None = None
    policy: PolicyKey | None = None

    @model_validator(mode="after")
    def _one_selection(self) -> Self:
        if self.policy is not None and any(
            value is not None for value in (self.experiment, self.job, self.seed, self.role)
        ):
            raise ValueError("a seat takes policy= or experiment/job/seed/role, not both")
        return self

    @property
    def named(self) -> bool:
        return self.policy is not None or any(
            value is not None for value in (self.experiment, self.job, self.seed, self.role)
        )


class _OnClock(_PolicySeat):
    """A checkpoint seat that looks for a value of `clock`.

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


class Final(_PolicySeat):
    """The held checkpoint's line's last checkpoint, once its run has ended, finished or failed."""

    kind: Literal["final"] = "final"


class Aligned(_OnClock):
    """Each of job `job`'s lines at exactly where the held checkpoint stood on `clock`."""

    kind: Literal["aligned"] = "aligned"


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

    `name` is its first run (with `/roles/ROLE` for a nondefault role), as
    `CheckpointRef.line` names it; `seed` the
    seed that run declared; `job` the full name the report gives its job,
    `launch` its launch name; `ended` whether its latest run has ended, so it
    saves no more. `checkpoints` are its stored checkpoints in the order they
    were saved. A job's seeds are its lines.
    """

    name: str
    experiment: str = ""
    role: str = "policy"
    seed: int | None = None
    job: str | None = None
    launch: str | None = None
    ended: bool = False
    checkpoints: tuple[CheckpointRef, ...] = ()

    @property
    def policy(self) -> PolicyKey:
        return PolicyKey(experiment=self.experiment, job=self.job, seed=self.seed, role=self.role)

    def seen_from(self, experiment: str) -> Line:
        """This line as `experiment`'s evaluation seats its checkpoints (`seen_from`)."""
        return self.model_copy(
            update={"checkpoints": tuple(seen_from(ref, experiment) for ref in self.checkpoints)}
        )

    @property
    def order(self) -> tuple[bool, int, str]:
        """Lines sort by seed, then name; one with no declared seed after those with one."""
        return self.seed is None, self.seed or 0, self.name


def select_policy(lines: Sequence[Line], key: PolicyKey, *, experiment: str) -> Line:
    """Resolve one policy, choosing the lowest available seed when omitted."""
    experiment = key.experiment or experiment
    candidates = _select_jobs(lines, experiment=experiment, job=key.job)
    seed = key.seed
    if seed is None and candidates:
        seed = min(candidates, key=lambda line: line.order).seed
    found = select_seat_lines(
        candidates, experiment=experiment, job=key.job, seed=seed, role=key.role
    )
    if len(found) != 1:
        given = ", ".join(f"{k}={v!r}" for k, v in key.model_dump(exclude_none=True).items())
        have = sorted(
            {
                f"{line.job}@{line.seed}" + ("" if line.role == "policy" else f" ({line.role})")
                for line in lines
                if (line.experiment or experiment) == experiment
            }
        )
        raise EtudeError(
            "policy_not_found" if not found else "ambiguous_policy",
            422,
            detail=f"PolicyKey({given}) in {experiment}; it has {', '.join(have) or 'no runs'}",
        )
    return found[0]


def select_seat_lines(
    lines: Sequence[Line],
    *,
    experiment: str,
    job: str | None = None,
    seed: int | None = None,
    role: str | None = None,
) -> tuple[Line, ...]:
    """Select a seat's policies, retaining every seed unless one is named."""
    candidates = _select_jobs(lines, experiment=experiment, job=job)
    if seed is not None:
        candidates = [line for line in candidates if line.seed == seed]
    if role is not None:
        candidates = [line for line in candidates if line.role == role]
    elif len(roles := {line.role for line in candidates}) > 1:
        where = f"{experiment}/{job}" if job else experiment
        raise EtudeError(
            "ambiguous_policy", 422, detail=f"{where}: choose role from {sorted(roles)}"
        )
    return tuple(candidates)


def _select_jobs(
    lines: Sequence[Line],
    *,
    experiment: str,
    job: str | None,
) -> list[Line]:
    candidates = [line for line in lines if (line.experiment or experiment) == experiment]
    if job is not None:
        return [line for line in candidates if job in (line.job, line.launch)]
    jobs = {line.job for line in candidates}
    if len(jobs) > 1:
        raise EtudeError(
            "ambiguous_policy",
            422,
            detail=f"{experiment}: choose job from {sorted(str(j) for j in jobs)}",
        )
    return candidates


class Pairing(Record):
    """One seed pair a seating plays for a held checkpoint, and its share of the point's games."""

    # By seat: the checkpoint a relative seat found; None for any other seat.
    seats: tuple[CheckpointRef | None, ...]
    games: int = Field(ge=1)
    resolved: dict[str, CheckpointRef] = Field(
        default_factory=dict, exclude_if=lambda refs: not refs
    )


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
    checkpoint: CheckpointId
    pairings: tuple[Pairing, ...]
    # The lines of both sides it counted, qualified by experiment and role.
    lines: tuple[str, ...] = ()


class Gap(Record):
    """A held checkpoint whose relative seat has no checkpoint, and why.

    `value` is where on `seat.clock` the seat looks for the held checkpoint;
    None for `Final`, or a held checkpoint whose `at` lacks that clock.
    """

    seating: str
    checkpoint: CheckpointId
    missing: Missing
    seat: Relative
    value: float | None = None


_SeatingConfig = TypeVar(
    "_SeatingConfig",
    bound=BaseModel | Mapping[str, JsonValue],
    default=Mapping[str, JsonValue] | BaseModel,
    covariant=True,
)


class Seating(Record, Generic[_SeatingConfig]):
    """The rule for who meets whom: `players` in seat order, played under each of `configs`.

    A seat is a fixed player, `Held()` for the checkpoint being evaluated,
    a relative seat, or an author's composition of these. No held leaf means
    it plays once, rather than once per held checkpoint. Each config makes
    one match per resolved pairing, with the first player's value reported.

    `games` is per config and point: an `at` of the held checkpoint's
    job, whatever its seeds. With `seeds="every"` a point plays every pair of
    a seed of that job and a seed of the other seat's (its own line, or a
    fixed player, counting as one), its games split evenly over the pairs,
    the remainder to the lowest by seed. With `seeds="one"` it plays them all
    between one seed per side, chosen from the seating's name and the seed
    sets: faster, but the point shows one pair of seeds, not the jobs.
    """

    name: str = Field(min_length=1, max_length=200)
    players: tuple[Seat, Seat]
    # Mapping, so a literal like `{"deal": 1}` type-checks as a config.
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)
    configs: tuple[SerializeAsAny[_SeatingConfig], ...] = Field(
        min_length=1,
        validation_alias=AliasChoices("configs", "conditions"),
        serialization_alias="conditions",
    )

    @model_validator(mode="before")
    @classmethod
    def _config_name(cls, data: object, info: ValidationInfo) -> object:
        return _match_config_name(data, info, "configs")

    @property
    def conditions(self) -> tuple[_SeatingConfig, ...]:
        warnings.warn("conditions is now configs; removed in 0.5", DeprecationWarning, stacklevel=2)
        return self.configs

    games: int = Field(ge=1)
    # The author's labels, such as "anchor" or "held-out", for choosing
    # which seatings a reading counts; `schedule` carries them.
    tags: tuple[Name, ...] = ()
    seeds: Literal["every", "one"] = "every"

    @model_validator(mode="after")
    def relative_to_a_held_checkpoint(self) -> Self:
        if self.relative and not any(isinstance(each, Held) for each in self.leaves):
            raise ValueError("relative_seat_needs_a_held_seat")
        return self

    @property
    def leaves(self) -> list[Seat]:
        return [leaf for player in self.players for leaf in seat_leaves(player)]

    @property
    def fixed(self) -> bool:
        """Whether every seat is a fixed player, so no checkpoint changes its matches."""
        return not any(isinstance(each, Held) for each in self.leaves)

    @property
    def relative(self) -> bool:
        """Whether a seat names a checkpoint relative to the held one."""
        return any(isinstance(each, _RELATIVE) for each in self.leaves)

    @property
    def definition(self) -> Digest:
        """What two eval runs must agree on to share the name: keys, not versions."""
        # Preserve the released definitions of held and relative seats.
        players = [_seat_definition(each) for each in self.players]
        # Default seeds leave the definition every seating was first held with.
        seeds = [] if self.seeds == "every" else [self.seeds]
        return digest(
            [
                self.name,
                players,
                [as_config(each) for each in self.configs],
                self.games,
                self.tags,
                *seeds,
            ]
        )

    def matches(
        self, checkpoint: CheckpointRef | None = None, seated: Seated | None = None
    ) -> list[Match]:
        """One match per config and pairing: `checkpoint` in every held seat, and in
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
                    _seat(each, checkpoint, found, pairing.resolved)
                    for each, found in zip(self.players, pairing.seats, strict=True)
                ),
                config=as_config(given),
                games=pairing.games,
            )
            for pairing in pairings
            for given in self.configs
        ]

    def resolve(
        self, checkpoint: CheckpointRef, lines: Sequence[Line], *, experiment: str | None = None
    ) -> Seated | Gap:
        """The pairings this seating plays for `checkpoint`, one seed of its job, among the
        experiment's `lines`; or why its relative seat finds nothing to play yet.

        A seat naming a job plays each of its lines with a checkpoint there, the
        held checkpoint's own job included, and waits while any may still save
        one. A seat that finds the held checkpoint itself seats it: self-play is
        a match like any other.
        """
        # By key first: another experiment's checkpoint names its experiment in its line.
        held = next(
            (line for line in lines if any(ref.key == checkpoint.key for ref in line.checkpoints)),
            None,
        )
        if held is None:
            candidates = [
                line
                for line in lines
                if line.name == checkpoint.line
                and (checkpoint.policy is None or line.policy == checkpoint.policy)
            ]
            held = candidates[0] if len(candidates) == 1 else None
        ordered = sorted(lines, key=lambda line: line.order)
        # The held checkpoint's side: its job's seeds, or its own line with no job.
        mine = (
            []
            if held is None
            else [held]
            if held.job is None
            else [
                line
                for line in ordered
                if line.job == held.job
                and line.experiment == held.experiment
                and line.role == held.role
            ]
        )
        relatives = {
            digest(_relative_definition(each)): each
            for each in self.leaves
            if isinstance(each, _RELATIVE)
        }
        axes: list[list[Line | None]] = []
        context = experiment if experiment is not None else held.experiment if held else ""
        for relative in relatives.values():
            if relative.policy is not None:
                try:
                    axes.append([select_policy(ordered, relative.policy, experiment=context)])
                except EtudeError as error:
                    if error.code != "policy_not_found":
                        raise
                    axes.append([])
            elif relative.named:
                axes.append(
                    list(
                        select_seat_lines(
                            ordered,
                            experiment=relative.experiment or context,
                            job=relative.job,
                            seed=relative.seed,
                            role=relative.role,
                        )
                    )
                )
            else:
                axes.append([None])
        counted = {
            f"{line.experiment}/{line.name}" if line.experiment else line.name
            for line in [*mine, *(line for axis in axes for line in axis if line is not None)]
        }
        if self.seeds == "one" and mine and all(axes):
            # Choose across all leaf axes together: independent choices could pick
            # incompatible held seeds and leave every seed with no matches.
            chosen = _chosen(self.name, [mine, *(axes or [[None]])])
            if held != chosen[0]:
                return Seated(
                    seating=self.name,
                    checkpoint=checkpoint.key,
                    pairings=(),
                    lines=tuple(sorted(counted)),
                )
            axes = [[line] for line in chosen[1:]] if axes else []
        choices: list[list[CheckpointRef]] = []
        for relative, axis in zip(relatives.values(), axes, strict=True):
            theirs = axis if relative.named else [held]
            results = [_find(relative, checkpoint, line) for line in theirs if line is not None]
            found = [each for each in results if isinstance(each, CheckpointRef)]
            if not found or "not_yet" in results:
                unknown = not results and (relative.named or checkpoint.line is not None)
                return Gap(
                    seating=self.name,
                    checkpoint=checkpoint.key,
                    missing="not_yet" if unknown or "not_yet" in results else "never",
                    seat=relative,
                    value=_wanted(relative, checkpoint),
                )
            choices.append(found)
        combinations_ = prod(len(choice) for choice in choices)
        seeds = 1 if self.seeds == "one" else max(len(mine), 1)
        first = (mine.index(held) if held in mine and self.seeds != "one" else 0) * combinations_
        pairs = seeds * combinations_
        pairings: list[Pairing] = []
        nested_relative = any(
            sum(isinstance(leaf, _RELATIVE) for leaf in seat_leaves(player)) > 1
            for player in self.players
        )
        # At most games pairings can get a game. Avoid materializing a Cartesian
        # product, especially when many checkpoint leaves each span several seeds.
        count = min(combinations_, max(self.games - first, 0))
        for index, refs in enumerate(islice(product(*choices), count), start=first):
            games = _share(self.games, pairs, index)
            if not games:
                continue
            resolved = dict(zip(relatives, refs, strict=True))
            pairings.append(
                Pairing(
                    seats=tuple(
                        next(
                            (
                                resolved[digest(_relative_definition(leaf))]
                                for leaf in seat_leaves(each)
                                if isinstance(leaf, _RELATIVE)
                            ),
                            None,
                        )
                        for each in self.players
                    ),
                    games=games,
                    # Flat released seats keep their original wire shape and definition.
                    resolved=resolved if nested_relative else {},
                )
            )
        return Seated(
            seating=self.name,
            checkpoint=checkpoint.key,
            pairings=tuple(pairings),
            lines=tuple(sorted(counted)),
        )


def _seat_definition(seat: Seat) -> JsonValue:
    if isinstance(seat, Held):
        return ["model", {}]
    if isinstance(seat, _RELATIVE):
        return ["relative", _relative_definition(seat)]
    if isinstance(seat, Composition) and seat.type == "etude:configured":
        over = _SEAT.validate_python(seat.fields["over"])
        config = identifying(_CONFIG.validate_python(seat.fields["config"]))
        if isinstance(over, Held):
            return ["model", config]
        if isinstance(over, _RELATIVE):
            return ["relative", {**_relative_definition(over), "inference": {"config": config}}]
    return seat.key


def _relative_definition(seat: Relative) -> dict[str, JsonValue]:
    # Empty inference was part of released seating identities.
    found = seat.model_dump(mode="json")
    for part in ("experiment", "seed", "role", "policy"):
        if found.get(part) is None:
            found.pop(part, None)
    return {**found, "inference": {"config": {}}}


def _seat(
    each: Seat,
    checkpoint: CheckpointRef | None,
    found: CheckpointRef | None,
    resolved: Mapping[str, CheckpointRef] | None = None,
) -> Player:
    if isinstance(each, Composition):
        if each.type == "etude:configured":
            over = _seat(_SEAT.validate_python(each.fields["over"]), checkpoint, found, resolved)
            if not isinstance(over, CheckpointPlayer):
                raise ValueError("stored config needs a checkpoint")
            return over.model_copy(update={"config": each.config, "version": each.version})
        return each.model_copy(
            update={
                "fields": _map_fields(
                    each.fields,
                    lambda seat: _seat(seat, checkpoint, found, resolved).model_dump(mode="json"),
                )
            }
        )
    if isinstance(each, _RELATIVE):
        found = (resolved or {}).get(digest(_relative_definition(each)), found)
        if found is None:
            raise ValueError("relative_seat_needs_its_checkpoints")
        return checkpoint_player(found)
    if isinstance(each, Held):
        if checkpoint is None:
            raise ValueError("held_seat_needs_a_checkpoint")
        return checkpoint_player(checkpoint)
    return each


def _seat_value(value: object) -> object:
    if (
        isinstance(value, dict)
        and not value.get("kind")
        and set(value) <= {"config", "version", "source", "code"}
    ):
        # Released Inference seats become an explicit configuration wrapper.
        config = value.get("config", {})
        return {
            "kind": "composition",
            "type": "etude:configured",
            "fields": {
                "over": {"kind": "held"},
                "config": config,
                "version": Version.model_validate(
                    value.get("version", {"source": value["code"]} if "code" in value else {})
                ).model_dump(mode="json"),
            },
        }
    if isinstance(value, BaseModel) and not isinstance(value, (Held, *_RELATIVE)):
        return _player(value)
    if isinstance(value, dict) and "inference" in value and value.get("kind") != "model":
        value = dict(value)
        inference = value.pop("inference")
        config = inference.get("config", {})
        return {
            "kind": "composition",
            "type": "etude:configured",
            "fields": {"over": value, "config": config},
        }
    return value


type Seat = Annotated[Player | Held | Relative, BeforeValidator(_seat_value)]
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_PLAYER: TypeAdapter[Player] = TypeAdapter(Player)
_SEAT: TypeAdapter[Seat] = TypeAdapter(Seat)


def _map_fields(
    fields: dict[str, JsonValue], visit: Callable[[Seat], JsonValue]
) -> dict[str, JsonValue]:
    def walk(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            if value.get("kind") == "mapping":
                items = _CONFIG.validate_python(value["items"])
                return {
                    "kind": "mapping",
                    "items": {key: walk(each) for key, each in items.items()},
                }
            if value.get("kind") in (
                "model",
                "code",
                "held",
                "composition",
                "earlier",
                "fraction",
                "at",
                "final",
                "aligned",
            ):
                return visit(_SEAT.validate_python(value))
            return {key: walk(each) for key, each in value.items()}
        if isinstance(value, list):
            return [walk(each) for each in value]
        return value

    return {key: walk(value) for key, value in fields.items()}


def seat_leaves(player: Seat) -> list[Seat]:
    if not isinstance(player, Composition):
        return [player]
    if player.type == "etude:configured":
        return seat_leaves(_SEAT.validate_python(player.fields["over"]))
    found: list[Seat] = []

    def visit(seat: Seat) -> JsonValue:
        found.extend(seat_leaves(seat))
        return None

    _map_fields(player.fields, visit)
    return found


def player_checkpoints(player: Seat) -> tuple[CheckpointRef, ...]:
    """Every checkpoint in a player tree, including repeated leaves."""
    return tuple(
        leaf.checkpoint for leaf in seat_leaves(player) if isinstance(leaf, CheckpointPlayer)
    )


def player_checkpoint(player: Player) -> CheckpointRef | None:
    """The checkpoint of a single-network player; multiple different leaves have no primary."""
    found = {checkpoint.key: checkpoint for checkpoint in player_checkpoints(player)}
    return next(iter(found.values())) if len(found) == 1 else None


def relocate_player(player: Player, here: Mapping[str, str]) -> Player:
    if isinstance(player, Composition):
        return player.model_copy(
            update={
                "fields": _map_fields(
                    player.fields,
                    lambda seat: relocate_player(_PLAYER.validate_python(seat), here).model_dump(
                        mode="json"
                    ),
                )
            }
        )
    if not isinstance(player, CheckpointPlayer) or player.checkpoint.key not in here:
        return player
    checkpoint = player.checkpoint
    return player.model_copy(
        update={
            "checkpoint": checkpoint.model_copy(
                update={
                    "artifact": checkpoint.artifact.model_copy(update={"uri": here[checkpoint.key]})
                }
            )
        }
    )


def _share(games: int, pairs: int, index: int) -> int:
    """Pair `index`'s games of `games` split evenly over `pairs`, the remainder to the lowest."""
    return games // pairs + (1 if index < games % pairs else 0)


def _chosen(seating: str, sides: Sequence[Sequence[Line | None]]) -> tuple[Line | None, ...]:
    """Choose one stable seed tuple without enumerating the Cartesian product."""
    seeds = [[None if line is None else [line.seed, line.name] for line in side] for side in sides]
    index = int(digest([seating, seeds])[:15], 16) % prod(len(side) for side in sides)
    chosen: list[Line | None] = []
    for side in reversed(sides):
        index, pick = divmod(index, len(side))
        chosen.append(side[pick])
    return tuple(reversed(chosen))


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
    rollups: tuple[Rollup, ...] = Field(default=(), exclude_if=lambda rollups: not rollups)
    seatings: tuple[Seating, ...] = ()

    @property
    def experiments(self) -> frozenset[str]:
        """Experiments explicitly named by relative seats."""
        return frozenset(
            name
            for seating in self.seatings
            for seat in seating.leaves
            if isinstance(seat, _RELATIVE)
            if (name := seat.policy.experiment if seat.policy is not None else seat.experiment)
            is not None
        )

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


class RoundRobin(Record, Generic[_SeatingConfig]):
    """Hand-picked players, every two of them once per config.

    Its entrants are every player any of its eval runs entered. Seat swaps
    belong inside the author's match.
    """

    name: str = Field(min_length=1, max_length=200)
    environment: Environment
    rollups: tuple[Rollup, ...] = Field(default=(), exclude_if=lambda rollups: not rollups)
    # Mapping, so a literal like `{"deal": 1}` type-checks as a config.
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)
    configs: tuple[SerializeAsAny[_SeatingConfig], ...] = Field(
        min_length=1,
        validation_alias=AliasChoices("configs", "conditions"),
        serialization_alias="conditions",
    )

    @model_validator(mode="before")
    @classmethod
    def _config_name(cls, data: object, info: ValidationInfo) -> object:
        return _match_config_name(data, info, "configs")

    @property
    def conditions(self) -> tuple[_SeatingConfig, ...]:
        warnings.warn("conditions is now configs; removed in 0.5", DeprecationWarning, stacklevel=2)
        return self.configs

    games: int = Field(ge=1)

    def matches(self, entrants: Iterable[Player]) -> list[Match]:
        """Each pair once per config, in key order; of two entrants with one key, the later."""
        ordered = sorted({each.key: each for each in entrants}.items())
        return [
            Match(players=(a, b), config=as_config(given), games=self.games)
            for (_, a), (_, b) in combinations(ordered, 2)
            for given in self.configs
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


def resolve_checkpoint(
    ref: CheckpointRef, lines: Sequence[Line], *, experiment: str
) -> CheckpointRef:
    """Fill a reference's policy defaults from the same lines relative seats use."""
    policy = ref.policy
    if policy is None or policy.experiment == "":
        return ref
    # An explicitly recorded None seed is an unseeded run, not an omitted default.
    if (
        all(part is not None for part in (policy.experiment, policy.job, policy.role))
        and "seed" in policy.model_fields_set
    ):
        return ref
    if lines:
        policy = select_policy(lines, policy, experiment=experiment).policy
    else:
        # With no lines, only a fully named policy can be seated by hand.
        if policy.job is None or policy.seed is None:
            raise EtudeError("checkpoint_policy_needs_lines", 422)
        policy = policy.model_copy(
            update={
                "experiment": policy.experiment or experiment,
                "role": policy.role or "policy",
            }
        )
    return ref.model_copy(update={"policy": policy})


def resolve_player(player: Player, lines: Sequence[Line], *, experiment: str) -> Player:
    """Resolve and label every checkpoint leaf from the observing experiment."""
    if isinstance(player, Composition):

        def leaf(seat: Seat) -> JsonValue:
            if isinstance(seat, (Code, Composition, CheckpointPlayer)):
                return resolve_player(seat, lines, experiment=experiment).model_dump(mode="json")
            return seat.model_dump(mode="json")

        return player.model_copy(update={"fields": _map_fields(player.fields, leaf)})
    if isinstance(player, CheckpointPlayer):
        return player.model_copy(
            update={
                "checkpoint": seen_from(
                    resolve_checkpoint(player.checkpoint, lines, experiment=experiment), experiment
                )
            }
        )
    return player


class TournamentRun(Record):
    """`RUN/tournament.json`: the tournament an eval run was held for, and what it added."""

    tournament: Benchmark | RoundRobin
    checkpoints: tuple[CheckpointRef, ...] = ()
    entrants: tuple[Player, ...] = ()
    # What relative seats found for the checkpoints it scheduled (`Seating.resolve`).
    seated: tuple[Seated, ...] = ()
    # The part of the scheduled matches the run plays (`etude.eval.tournament.hold`).
    share: Share = Share()

    def bind(self, lines: Sequence[Line], *, experiment: str) -> TournamentRun:
        """Resolve author references before comparing or storing their identities."""

        def checkpoint(ref: CheckpointRef) -> CheckpointRef:
            return resolve_checkpoint(ref, lines, experiment=experiment)

        def player(given: Seat) -> Seat:
            if isinstance(given, (Code, Composition, CheckpointPlayer)):
                return resolve_player(given, lines, experiment=experiment)
            return given

        tournament = self.tournament
        if isinstance(tournament, Benchmark):
            tournament = tournament.model_copy(
                update={
                    "seatings": tuple(
                        seating.model_copy(
                            update={"players": tuple(player(p) for p in seating.players)}
                        )
                        for seating in tournament.seatings
                    )
                }
            )
        return self.model_copy(
            update={
                "tournament": tournament,
                "checkpoints": tuple(checkpoint(ref) for ref in self.checkpoints),
                "entrants": tuple(player(p) for p in self.entrants),
            }
        )

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
    seating with a held seat, its checkpoint."""

    match: Match
    seating: str | None = None
    checkpoint: CheckpointId | None = None
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

    def resolve(
        self, lines: Sequence[Line], *, experiment: str | None = None
    ) -> tuple[Tournament, tuple[Gap, ...]]:
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
                result = seating.resolve(checkpoint, lines, experiment=experiment)
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


def validate_checkpoints(checkpoints: Iterable[CheckpointRef]) -> None:
    """Refuse different checkpoint contents under one portable key, even after relocation."""
    found: dict[str, CheckpointRef] = {}
    for checkpoint in checkpoints:
        prior = found.setdefault(checkpoint.key, checkpoint)
        a, b = prior.artifact, checkpoint.artifact
        if a.sha256 != b.sha256 or (
            a.size_bytes is not None and b.size_bytes is not None and a.size_bytes != b.size_bytes
        ):
            raise EtudeError(
                "checkpoint_key_conflict",
                409,
                detail=f"{checkpoint.key}: different contents at {a.uri} and {b.uri}",
            )


def _checkpoint_refs(run: TournamentRun) -> Iterable[CheckpointRef]:
    """Every checkpoint a tournament record can seat, before deduplication."""
    yield from run.checkpoints
    for seated in run.seated:
        for pairing in seated.pairings:
            yield from (c for c in pairing.seats if c is not None)
            yield from pairing.resolved.values()
    players: list[Seat] = list(run.entrants)
    if isinstance(run.tournament, Benchmark):
        players.extend(p for seating in run.tournament.seatings for p in seating.players)
    yield from (c for p in players for c in player_checkpoints(p))


def assemble(held: Iterable[TournamentRun]) -> Tournament:
    """The tournament its eval runs make together, from the first of them on.

    Raises `EtudeError("tournament_conflict")` when they disagree on its
    format or environment (its setup aside), or a round robin's configs or games, and
    `EtudeError("seating_conflict")` when they define one seating name two ways.
    Different contents under one checkpoint key raise `checkpoint_key_conflict`.
    Versions are not definitions: a later run's seating, checkpoint or entrant
    with the same key replaces an earlier one, so what is played next records
    the version that plays it.
    """
    runs = list(held)
    validate_checkpoints(c for run in runs for c in _checkpoint_refs(run))
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
        if isinstance(given, RoundRobin) and given.model_copy(
            update={"environment": first.environment}
        ).model_dump(mode="json") != first.model_dump(mode="json"):
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


def __getattr__(name: str) -> object:
    if name == "Model":
        warnings.warn(
            "Model is now CheckpointPlayer; removed in 0.5", DeprecationWarning, stacklevel=2
        )
        return CheckpointPlayer
    if name == "CheckpointKey":
        warnings.warn(
            "CheckpointKey is now CheckpointRef; removed in 0.5",
            DeprecationWarning,
            stacklevel=2,
        )
        return partial(_checkpoint_id, experiment="")
    raise AttributeError(name)
