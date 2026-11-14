"""The eval run: `evaluate` plays matches in worker processes and stores their tables.

Players (`etude.eval.records.CheckpointPlayer` and `Code`), `environment` and `Match`
describe what `evaluate` plays; it knows nothing of tournaments
(`etude.eval.tournament` arranges eval runs). The author's `play(match)`
returns tables of rows, or a `Result` that also keeps blobs: bytes such as a
game's replay evidence, which rows name by SHA256. A `Rollup` declared by the
run is summed once per match as it is stored. Etude checks that every row
names a `seat` or a `group` of its match and that a column keeps one type
through the run, adds `run` and `match`, and stages the match
(`etude.eval.block.Staging`). A refused or raising match, including one
that returns no rows, is recorded as failed, nothing else of it is stored,
and the run stops and ends failed.

Staged matches are sealed into a block every few minutes, on a failure and on
finish, and each block is offered to every destination once sealed: a run
delivers a few objects per block, never one per match or blob. SIGTERM, as
the launcher's termination sends, seals what is staged before the run ends.
Under `etude launch` a run also says how far it has got (`_Report`), for
`etude status`.

A resume reads the blocks already delivered to any destination, so a
machine with an empty disk skips what was delivered. Workers are fresh
processes (`spawn`), so `play` must be importable by name.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import threading
import time
import warnings
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

from pydantic import BaseModel

from etude.errors import EtudeError
from etude.eval.records import (
    Environment,
    Match,
    Rollup,
    as_config,
)
from etude.eval.run import ETUDE_TABLES as ETUDE_TABLES
from etude.eval.run import EvalRun as _EvalRun
from etude.eval.run import Result as Result
from etude.eval.run import Tables as Tables
from etude.eval.schedule import ordered
from etude.eval.source import digest_files
from etude.records import Record
from etude.run import declare, destinations
from etude.storage import DirectoryStorage, Storage

KIND = "evaluation"
# A running eval run says how far it has got at most this often: each report
# becomes one write to the run's attempt record in storage.
_REPORT_SECONDS = 15.0
# Matches a recent pace is taken over.
_PACE_MATCHES = 50


class EvalResult(Record):
    """What one call to `evaluate` did."""

    experiment: str
    name: str
    environment: Environment
    played: int
    # Matches already stored by an earlier attempt.
    skipped: int


def environment(
    name: str,
    *,
    files: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Environment:
    """What matches are played in: its defining files, hashed like code, and its config."""
    return Environment(name=name, files=digest_files(files), config=as_config(config))


def evaluate(
    *,
    experiment: str,
    environment: Environment,
    matches: Sequence[Match],
    play: Callable[[Match], Tables | Result],
    workers: int = 1,
    run: str | None = None,
    directory: Path | str | None = None,
    storages: Sequence[Storage] = (),
    rollups: Sequence[Rollup] = (),
) -> EvalResult:
    """Play every match not already stored in this eval run, `workers` at a time.

    Matches start in the order `etude.eval.schedule.ordered` gives, so
    consecutive matches reuse the checkpoints the author's runtime keeps loaded.

    The eval run is named `run` (see `etude.run.declare` for its
    defaults); resuming it requires the same environment and `rollups`
    (`EtudeError("eval_run_rollups_conflict")`): a sealed block's partial
    totals are never taken again. Prints one line to
    stderr as each match is stored: how many of the pending are, and since
    when. Raises
    `EtudeError("match_failed")` after recording the first match that raised,
    returned no rows or returned rows Etude refuses. Delivery failures do not
    raise: the files stay in the run's directory for `etude sync`, and stderr
    says so.
    """
    if workers < 1:
        raise EtudeError("workers_must_be_positive", 422)
    if len({scheduled.key for scheduled in matches}) != len(matches):
        raise EtudeError("duplicate_match", 422)
    names = [rollup.name for rollup in rollups]
    if len(set(names)) != len(names) or set(names) & set(ETUDE_TABLES):
        raise EtudeError("rollup_name_refused", 422) from ValueError(names)
    declared = declare(
        experiment=experiment, clocks=(), run=run, kind=KIND, directory=directory, resume=True
    )
    if declared.record.kind != KIND:
        raise EtudeError("run_is_not_an_eval_run", 409)
    path = declared.root / "environment.json"
    if not path.is_file():
        DirectoryStorage(declared.root).put(
            "environment.json", environment.model_dump_json().encode()
        )
    elif Environment.model_validate_json(path.read_bytes()) != environment:
        raise EtudeError("eval_run_environment_conflict", 409)
    writer = _EvalRun(
        declared.root,
        declared.name,
        destinations(declared.base, storages),
        tuple(rollups),
        experiment=experiment,
    )
    pending = [scheduled for scheduled in matches if scheduled.key not in writer.done]
    started = time.monotonic()
    report = _Report(total=len(matches), done=len(matches) - len(pending))
    report.write()
    try:
        with _terminable():
            for count, (scheduled, outcome) in enumerate(_played(pending, play, workers), 1):
                if isinstance(outcome, BaseException):
                    writer.fail(scheduled, outcome)
                writer.store(scheduled, outcome)
                report.played()
                print(
                    f"etude: {declared.name}: {count}/{len(pending)} matches played,"
                    f" {time.monotonic() - started:.0f}s elapsed",
                    file=sys.stderr,
                    flush=True,
                )
    except BaseException:
        writer.finish("failed")
        report.write(failed=writer.failed)
        raise
    writer.finish("finished")
    report.write()
    return EvalResult(
        experiment=experiment,
        name=declared.name,
        environment=environment,
        played=len(pending),
        skipped=len(matches) - len(pending),
    )


class _Report:
    """Under `etude launch`, how far the eval run has got, for the launcher's status.

    `etude-result.json` in `ETUDE_OUTPUT_DIR` holds a tally, the shape of
    `etude.launch.records.Tally`; evaluation does not import the launcher.
    It is progress only: results are the run's blocks.
    """

    def __init__(self, *, total: int, done: int) -> None:
        self.total, self.done = total, done
        self.outputs = os.environ.get("ETUDE_OUTPUT_DIR")
        self.recent: deque[float] = deque(maxlen=_PACE_MATCHES)
        self.written = time.monotonic()

    def played(self) -> None:
        """Count one stored match, and report when the last report is old enough."""
        self.done += 1
        now = time.monotonic()
        self.recent.append(now)
        if now - self.written >= _REPORT_SECONDS:
            self.write()

    def write(self, *, failed: int = 0) -> None:
        if self.outputs is None:
            return
        elapsed = self.recent[-1] - self.recent[0] if self.recent else 0.0
        tally = {
            "unit": "matches",
            "done": self.done,
            "total": self.total,
            "failed": failed,
            "per_hour": (len(self.recent) - 1) / elapsed * 3600 if elapsed > 0 else None,
        }
        DirectoryStorage(Path(self.outputs)).put(
            "etude-result.json", json.dumps({"tally": tally}).encode()
        )
        self.written = time.monotonic()


class Terminated(Exception):
    """SIGTERM reached an eval run: what is staged is sealed before it ends."""


@contextmanager
def _terminable() -> Iterator[None]:
    """Turn SIGTERM into `Terminated` while matches are played, on the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def terminated(signum: int, frame: FrameType | None) -> NoReturn:
        raise Terminated()

    before = signal.signal(signal.SIGTERM, terminated)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, before)


def _played(
    pending: Sequence[Match], play: Callable[[Match], Tables | Result], workers: int
) -> Iterator[tuple[Match, Tables | Result | BaseException]]:
    """Each match with its tables or what it raised, in the order they finish.

    Matches start in `etude.eval.schedule` order, at most `workers` at once, so
    the order they start in is the order an author's runtime loads checkpoints in.
    """
    order = ordered(pending)
    if workers == 1:
        for scheduled in order:
            try:
                yield scheduled, play(scheduled)
            except Exception as error:  # noqa: BLE001 - recorded with the match, then raised
                yield scheduled, error
        return
    # Fresh processes everywhere, so an eval run behaves alike on Linux and macOS.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(min(workers, len(pending) or 1), mp_context=context) as pool:
        running: dict[Future[Tables | Result], Match] = {}

        def start() -> None:
            scheduled = next(order, None)
            if scheduled is not None:
                running[pool.submit(play, scheduled)] = scheduled

        for _ in range(workers):
            start()
        try:
            while running:
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    raised = future.exception()
                    yield running.pop(future), raised if raised is not None else future.result()
                    # Started only once the last is stored: a failure stops the run.
                    start()
        finally:
            pool.shutdown(cancel_futures=True)


def __getattr__(name: str) -> object:
    if name == "EvalRun":
        warnings.warn(
            "etude.eval.match.EvalRun is now EvalResult; removed in 0.5. "
            "The live run is etude.eval.run.EvalRun",
            DeprecationWarning,
            stacklevel=2,
        )
        return EvalResult
    raise AttributeError(name)
