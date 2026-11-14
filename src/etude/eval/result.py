"""Game results without the parent evaluation process or its storage machinery."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from etude.eval.records import Config, Environment
from etude.records import Record

type Value = bool | int | float | str | None
type Row = Mapping[str, Value]
type Tables = Mapping[str, Sequence[Row]]


@dataclass(frozen=True)
class Result:
    """What `play` returns with tables, optional blobs and execution provenance.

    Rows name a blob by its SHA256 (`hashlib.sha256(blob).hexdigest()`);
    `etude.eval.query.read_blobs` gives its bytes back.
    `execution` records the actual device and runtime settings. It changes
    neither the requested match nor whether its games have already been played.
    """

    tables: Tables = field(default_factory=dict)
    blobs: Sequence[bytes] = ()
    execution: Config = field(default_factory=dict)


class EvalResult(Record):
    """What one `EvalRun.play` did."""

    experiment: str
    name: str
    environment: Environment
    played: int
    # Matches already stored by an earlier attempt.
    skipped: int
