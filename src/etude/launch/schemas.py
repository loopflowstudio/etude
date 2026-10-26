"""What the launcher asks of a machine's session, and what crosses to that session.

One packet goes out per session; one journal entry per attempt comes back. The
service never decides anything about jobs. The `session` key and service
arguments identify one working session, on added and rented machines alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, FiniteFloat, Json, JsonValue

from etude.launch.records import (
    Cause,
    Input,
    Limit,
    Outcome,
    Output,
    Preparation,
    Record,
    Runtime,
    Variable,
)
from etude.launch.storage.download import Download
from etude.launch.storage.s3 import Grant
from etude.launch.usage import Transfer


class Work(Record):
    """One attempt as the session sees it."""

    setup: str | None = None
    cleanup: str | None = None
    stop_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cleanup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    runtime: Runtime | None = None
    prepare: Preparation | None = None
    attempt: str
    command: str
    limit_seconds: int
    limit_kind: Limit
    seed: int | None
    until: str | None  # the job's target, as "clock=value"
    config: str | None
    repo: str | None
    commit: str | None
    run: str  # names the run's folder in storage, shared by all its attempts
    # A later attempt of its run: the run's folder is restored first where the
    # machine does not hold it (`etude.carriage.resume_file`).
    resumes: bool = False
    job: str | None = None
    job_name: str | None = None
    cohort: str | None = None
    experiment: str | None = None
    inputs: tuple[Input, ...] = ()  # delivered to SESSION/inputs/SHA256
    environment: tuple[Variable, ...] = ()


class Packet(Record):
    delivery_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    log_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    startup_deadline: datetime | None = None
    session: str  # the session's id, which also names its journal folder
    deadline: datetime | None
    work: tuple[Work, ...]
    kept: bool = False  # stay until the deadline and take work as it is added
    autorun: bool = False
    cap_usd: Decimal = Decimal(0)


class Prepared(Record):
    """What an attempt's preparation found and did (`etude.launch.worker.prepared`).

    `rejected` is always followed by a build; `size_bytes` is None when the
    build failed.
    """

    key: str
    outcome: Literal["reused", "built", "rejected"]
    seconds: float = Field(ge=0)  # verifying a reused directory, or building one
    size_bytes: int | None = None
    cause: str | None = None  # why a kept directory was rejected


class Progress(Record):
    """The journal for one attempt, as far as it has been written."""

    attempt: str
    started_at: datetime | None
    finished_at: datetime | None
    outcome: Outcome | None
    cause: Cause | None
    clocks: dict[str, float]
    outputs: tuple[Output, ...]
    exit_code: int | None
    prepared: Prepared | None = None


class Observation(Record):
    alive: bool  # False once the session can write nothing more
    progress: tuple[Progress, ...]
    transfers: tuple[Transfer, ...] = ()
    start_unknown: bool = False  # durable startup intent, but attempt claims may not have arrived


def progress_from_json(
    raw: Mapping[str, Any], attempt: str, started_at: datetime | None
) -> Progress:
    """Claims supply the authoritative attempt and start time; old journal keys survive."""
    return Progress.model_validate({**raw, "attempt": attempt, "started_at": started_at})


class Result(Record):
    """`etude-result.json`: the progress a job reports, where each of its clocks stands."""

    clocks: dict[str, FiniteFloat] = Field(default_factory=dict)


class Bootstrap(Record):
    packet: Json[Packet]
    out: Grant  # the session's own folder: its receipt, usage and end note
    runs: dict[str, Grant] = Field(default_factory=dict)  # each run's folder, for the packet's work
    inbox_url: str
    fetch: dict[str, Download] = Field(default_factory=dict)
    restore: dict[str, str] = Field(default_factory=dict)  # see Delivery


class Delivery(Record):
    work: Json[Work]
    access: Grant  # the run's folder
    fetch: dict[str, Download] = Field(default_factory=dict)
    # Read links to what storage holds of a resumed run's folder, by RUN/KEY.
    restore: dict[str, str] = Field(default_factory=dict)


class AttemptRecord(Record):
    """`<run>/attempts/<attempt>.json`: one attempt as far as it has got.

    Rewritten as it moves: started, reporting progress, then finished, with
    `finished` holding the worker's final journal entry. `log_ends` names the
    stored pieces of what the job printed (`log_piece`) by where each ends, in
    bytes; it is None until the job's log exists. A piece is stored before the
    record that names it, so a reader of the record finds every piece it names.
    """

    started_at: datetime | None = None
    result: Result = Field(default_factory=Result)
    finished: dict[str, JsonValue] | None = None
    log_ends: tuple[int, ...] | None = None

    def log_pieces(self, attempt: str) -> tuple[str, ...]:
        """The keys, in the run's folder, of the log's pieces in order."""
        return log_pieces(attempt_log(attempt), self.log_ends or ())


class WorkerLog(Record):
    """`sessions/SESSION/out/worker-log.json`: where the stored pieces of the worker's log end.

    The worker's log goes to its session's folder as an attempt's goes to its
    run's: in pieces (`log_piece`) stored before the record naming them. A
    worker that never read its bootstrap has no grant for them, and sends its
    log whole, once, to `out/worker.log` by the link it was started with.
    """

    ends: tuple[int, ...] = ()

    def pieces(self) -> tuple[str, ...]:
        """The keys, in the session's `out/` folder, of the log's pieces in order."""
        return log_pieces(WORKER_LOG, self.ends)


WORKER_LOG = "worker.log"


def attempt_log(attempt: str) -> str:
    """The key, in its run's folder, under which an attempt's log pieces are kept."""
    return f"attempts/{attempt}.log"


def log_piece(log: str, start: int) -> str:
    """Where the piece of `log` that starts at byte `start` is kept.

    Pieces are sealed once and never rewritten. Named, as Kafka names log
    segments, by their first offset, zero-padded so names sort in log order.
    """
    return f"{log}/{start:020d}"


def log_pieces(log: str, ends: tuple[int, ...]) -> tuple[str, ...]:
    """The keys of `log`'s pieces in order, given where each ends."""
    return tuple(log_piece(log, start) for start in (0, *ends[:-1]))


class Inbox(Record):
    stop: tuple[str, ...] = ()
    work: tuple[Delivery, ...] = ()
    # No more work will come: the session ends once its work is done, after
    # delivering its final files and its end receipt.
    finish: bool = False
