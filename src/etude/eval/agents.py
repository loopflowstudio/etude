"""Direct game-to-agent transport and one process owning loaded checkpoints.

Games send picklable inputs over a private Unix socket. The agent process groups
requests by checkpoint, using the common forward cache, and retains batch reports.
This module's game-side imports are standard library only.
"""

from __future__ import annotations

import multiprocessing
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from multiprocessing.reduction import ForkingPickler
from queue import SimpleQueue
from typing import TYPE_CHECKING, cast

from etude.errors import RetryRequested, explain
from etude.eval.resources import ExecutionPlan, Resources, seconds_left

__all__ = ("Forward",)


if TYPE_CHECKING:
    from etude.call import Function
    from etude.checkpoints import CheckpointRef
    from etude.eval.run import AgentLoader


type Forward = Callable[[list[object]], list[object]]


@dataclass(frozen=True)
class Request:
    checkpoint: CheckpointRef
    inputs: list[object]
    tournament: str
    match: str
    player: str


@dataclass(frozen=True)
class Answer:
    outputs: list[object]
    error: str | None = None
    retry: bool = False

    def result(self) -> list[object]:
        if self.error is not None:
            if self.retry:
                raise RetryRequested(self.error)
            raise RuntimeError(self.error)
        return self.outputs


@dataclass(frozen=True)
class AgentFailure:
    checkpoint: str
    tournament: str
    match: str
    error: str
    retry: bool
    loading: bool = False


LATENCY_BUCKETS = 64


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
        self.latencies = [0] * LATENCY_BUCKETS

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
        bucket = min(LATENCY_BUCKETS - 1, max(0, int(seconds * 1_000_000).bit_length()))
        self.latencies[bucket] += 1


@dataclass(frozen=True)
class _RemoteForward:
    connection: AgentConnection
    checkpoint: CheckpointRef
    tournament: str
    match: str
    player: str

    def __call__(self, inputs: list[object]) -> list[object]:
        if not inputs:
            return []
        started = time.monotonic()
        try:
            self.connection.send(
                Request(self.checkpoint, list(inputs), self.tournament, self.match, self.player)
            )
            if not self.connection.poll(seconds_left()):
                raise RetryRequested("agent response exceeded the evaluation deadline")
            answer = cast(Answer, self.connection.recv())
        except (EOFError, OSError) as error:
            raise RetryRequested("agent process exited before answering") from error
        finally:
            self.connection.observe(time.monotonic() - started)
        return answer.result()


@contextmanager
def connect_agents(address: str) -> Iterator[AgentConnection]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(seconds_left())
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
    load: Function,
    checkout: str | None,
    max_batch: int,
    max_delay: float,
    memory: int,
    threads: int,
    control: Connection,
    resources: Resources,
) -> None:
    resources.configure(threads)
    try:
        # Process entry point: keep durable records and model loading out of game clients.
        from etude.eval.batching import _run_agents  # noqa: PLC0415

        _run_agents(
            address,
            load,
            checkout,
            max_batch,
            max_delay,
            memory,
            control,
            str(resources.artifacts),
        )
    except Exception as error:
        print(f"etude: agents: {explain(error)}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


@contextmanager
def running_agents(
    definition: AgentLoader,
    checkout: str | None,
    resources: Resources,
    plan: ExecutionPlan,
) -> Iterator[AgentProcess]:
    if definition.load is None:
        raise ValueError("an agent process needs a checkpoint loader")
    address = str(resources.scratch / "agents.sock")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_run,
        args=(
            address,
            definition.load,
            checkout,
            definition.max_batch,
            definition.max_delay_seconds,
            plan.checkpoint_bytes,
            plan.threads,
            child,
            resources,
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
        ready = wait([parent, process.sentinel], timeout=seconds_left())
        if parent not in ready:
            raise RetryRequested("agent process failed to start before the deadline")
        try:
            parent.recv()
        except EOFError as error:
            raise RetryRequested("agent process exited during startup") from error
        observer.start()
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
        process.join(timeout=seconds_left())
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
