"""Direct game-to-agent transport and one process owning loaded checkpoints.

Games send picklable rows over a private Unix socket. The agent process groups
requests by checkpoint and owns loading, eviction and the retained batch report.
This module's game-side imports are standard library only.
"""

from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing.connection import Connection, wait
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from queue import SimpleQueue
from typing import TYPE_CHECKING, Protocol, cast

from etude.errors import RetryRequested, explain
from etude.eval.resources import Cpus, memory_bytes

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef
    from etude.jobs import AgentLoader


class Agent(Protocol):
    """Answer each row as if alone, in order; every stochastic row owns its seed."""

    def act(self, rows: Sequence[object]) -> Sequence[object]: ...


@dataclass(frozen=True)
class Request:
    checkpoint: CheckpointRef
    rows: list[object]
    tournament: str
    match: str
    player: str


@dataclass(frozen=True)
class Answer:
    rows: list[object]
    error: str | None = None
    retry: bool = False


@dataclass(frozen=True)
class AgentFailure:
    checkpoint: str
    tournament: str
    match: str
    error: str
    retry: bool
    loading: bool = False


class AgentConnection(Connection):
    """One game's direct channel; counters exclude the four-byte framing header.

    Latencies use 64 power-of-two microsecond buckets, bounded regardless of
    game length. Each bucket's upper bound is used for reported percentiles.
    """

    def __init__(self, handle: int) -> None:
        super().__init__(handle)
        self.requests = 0
        self.answers = 0
        self.request_bytes = 0
        self.answer_bytes = 0
        self.round_trip_seconds = 0.0
        self.transport_seconds = 0.0
        self.latencies = [0] * 64

    def send(self, obj: object) -> None:
        started = time.monotonic()
        try:
            payload = ForkingPickler.dumps(obj)
            self.send_bytes(payload)
            self.requests += 1
            self.request_bytes += len(payload)
        finally:
            self.transport_seconds += time.monotonic() - started

    def recv(self) -> object:
        started = time.monotonic()
        try:
            payload = self.recv_bytes()
            self.answers += 1
            self.answer_bytes += len(payload)
            return ForkingPickler.loads(payload)
        finally:
            self.transport_seconds += time.monotonic() - started

    def observe(self, seconds: float) -> None:
        self.round_trip_seconds += seconds
        bucket = min(63, max(0, int(seconds * 1_000_000).bit_length()))
        self.latencies[bucket] += 1


@dataclass(frozen=True)
class _RemoteAgent:
    connection: AgentConnection
    checkpoint: CheckpointRef
    tournament: str
    match: str
    player: str

    def act(self, rows: Sequence[object]) -> Sequence[object]:
        if not rows:
            return []
        started = time.monotonic()
        try:
            self.connection.send(
                Request(self.checkpoint, list(rows), self.tournament, self.match, self.player)
            )
            if not self.connection.poll(_remaining()):
                raise RetryRequested("agent response exceeded the evaluation deadline")
            answer = cast(Answer, self.connection.recv())
        except (EOFError, OSError) as error:
            raise RetryRequested("agent process exited before answering") from error
        finally:
            self.connection.observe(time.monotonic() - started)
        if answer.error is not None:
            if answer.retry:
                raise RetryRequested(answer.error)
            raise RuntimeError(answer.error)
        return answer.rows


def _remaining() -> float | None:
    deadline = os.environ.get("ETUDE_DEADLINE")
    return (
        None if not deadline else max(0, datetime.fromisoformat(deadline).timestamp() - time.time())
    )


@contextmanager
def connect_agents(address: str) -> Iterator[AgentConnection]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(_remaining())
        try:
            peer.connect(address)
        except OSError as error:
            raise RetryRequested("agent process is unavailable") from error
        peer.settimeout(None)
        with AgentConnection(peer.detach()) as connection:
            yield connection


@dataclass(frozen=True)
class AgentProcess:
    address: str
    process: multiprocessing.context.SpawnProcess
    exited: Future[None]
    failures: dict[tuple[str, str], AgentFailure] = field(default_factory=dict)
    checkpoint_failures: SimpleQueue[AgentFailure] = field(default_factory=SimpleQueue)

    def check(self) -> None:
        if self.process.exitcode is not None:
            raise RetryRequested("agent process exited unexpectedly")


def _run(
    address: str,
    module: str,
    name: str,
    path: str,
    max_batch: int,
    max_delay: float,
    memory: int,
    threads: int,
    control: Connection,
    artifacts: str,
) -> None:
    try:
        # Process entry point: keep durable records and model loading out of game clients.
        from etude.eval.batching import _run_agents  # noqa: PLC0415

        _run_agents(
            address,
            module,
            name,
            path,
            max_batch,
            max_delay,
            memory,
            threads,
            control,
            artifacts,
        )
    except Exception as error:
        print(f"etude: agents: {explain(error)}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


@contextmanager
def running_agents(
    definition: AgentLoader,
    checkout: str | None,
    artifacts: Path,
    tournaments: Sequence[str],
    allocation: Cpus,
) -> Iterator[AgentProcess]:
    address = str(Path(os.environ["ETUDE_SCRATCH_DIR"], "agents.sock"))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    memory = (
        int(definition.memory_gb * 1024**3)
        if definition.memory_gb is not None
        else memory_bytes() // 2
    )
    process = context.Process(
        target=_run,
        args=(
            address,
            definition.load.module,
            definition.load.name,
            str((Path(checkout or ".") / definition.load.path).resolve()),
            definition.max_batch,
            definition.max_delay_seconds,
            memory,
            allocation.threads,
            child,
            str(artifacts),
        ),
    )
    process.start()
    child.close()
    agent = AgentProcess(address=address, process=process, exited=Future())
    sentinel = process.sentinel

    def observe_exit() -> None:
        try:
            while True:
                ready = wait([parent, sentinel])
                if parent in ready:
                    for failure in cast(list[AgentFailure], parent.recv()):
                        if failure.loading:
                            agent.checkpoint_failures.put(failure)
                        else:
                            agent.failures.setdefault((failure.tournament, failure.match), failure)
                    # Register failures before a game can catch its exception and return.
                    parent.send(None)
                if sentinel in ready:
                    break
        except (EOFError, OSError):
            wait([sentinel])
        finally:
            agent.exited.set_result(None)

    observer = threading.Thread(target=observe_exit, name="etude-agents")
    try:
        ready = wait([parent, process.sentinel], timeout=_remaining())
        if parent not in ready:
            raise RetryRequested("agent process failed to start before the deadline")
        try:
            parent.recv()
        except EOFError as error:
            raise RetryRequested("agent process exited during startup") from error
        observer.start()
        print(
            f"etude: agents: started for {', '.join(tournaments)}; {allocation.games} games at a time; {allocation.threads} inference threads",
            file=sys.stderr,
            flush=True,
        )
        yield agent
    finally:
        failed = sys.exc_info()[0] is not None
        if failed and process.is_alive():
            process.terminate()
        if process.is_alive():
            try:
                parent.send(None)
            except (OSError, EOFError):
                pass
        process.join(timeout=_remaining())
        if process.is_alive():
            process.terminate()
            process.join()
        if observer.ident is not None:
            observer.join()
        parent.close()
        exitcode = process.exitcode
        process.close()
        if not failed and exitcode != 0:
            raise RetryRequested(f"agent process exited with status {exitcode}")
