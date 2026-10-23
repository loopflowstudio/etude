"""What the launcher asks of a machine's session, and what crosses to that session.

One packet goes out per session; one journal entry per attempt comes back. The
service never decides anything about jobs. The `session` key and service
arguments identify one working session, on added and rented machines alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, FiniteFloat, Json, JsonValue

from etude.launch.records import Cause, Input, Limit, Outcome, Output, Record, Runtime, Variable
from etude.launch.storage.download import Download
from etude.launch.storage.s3 import Keys, PostForm
from etude.launch.usage import Transfer


class Work(Record):
    """One attempt as the session sees it."""

    setup: str | None = None
    cleanup: str | None = None
    stop_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cleanup_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    runtime: Runtime | None = None
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


class BucketAccess(Record):
    name: str
    region: str
    host: str
    keys: Keys


class RunAccess(Record):
    """What a machine may write for one run: everything under that run's folder.

    The form adds files of up to 5 GB. The keys, when AWS issued them, can
    also send a large file in parts and delete; they expire before the form.
    """

    form: PostForm
    bucket: BucketAccess | None = None


class Bootstrap(Record):
    packet: Json[Packet]
    form: PostForm  # the session's own folder: its receipt and end note
    runs: dict[str, RunAccess] = Field(default_factory=dict)  # for the packet's work
    inbox_url: str
    fetch: dict[str, Download] = Field(default_factory=dict)
    restore: dict[str, str] = Field(default_factory=dict)  # see Delivery


class Delivery(Record):
    work: Json[Work]
    access: RunAccess
    fetch: dict[str, Download] = Field(default_factory=dict)
    # Read links to what storage holds of a resumed run's folder, by RUN/KEY.
    restore: dict[str, str] = Field(default_factory=dict)


class AttemptRecord(Record):
    """`<run>/attempts/<attempt>.json`: one attempt as far as it has got.

    Rewritten as it moves: started, reporting progress, then finished, with
    `finished` holding the worker's final journal entry.
    """

    started_at: datetime | None = None
    result: Result = Field(default_factory=Result)
    finished: dict[str, JsonValue] | None = None


class Inbox(Record):
    stop: tuple[str, ...] = ()
    work: tuple[Delivery, ...] = ()
