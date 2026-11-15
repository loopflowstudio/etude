"""Direct game-to-agent transport and one process owning loaded players.

Games send picklable rows over a private Unix socket. The agent process groups
requests by player and owns loading, eviction and the retained batch report.
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
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from etude.errors import RetryRequested, explain
from etude.eval.resources import active_games, available_cpus, memory_bytes

if TYPE_CHECKING:
    from etude.eval.records import Player
    from etude.jobs import AgentLoader


class Agent(Protocol):
    """Answer each row as if alone, in order; every stochastic row owns its seed."""

    def act(self, rows: Sequence[object]) -> Sequence[object]: ...


@dataclass(frozen=True)
class Request:
    player: Player
    rows: list[object]
    tournament: str
    match: str


@dataclass(frozen=True)
class Answer:
    rows: list[object]
    error: str | None = None
    retry: bool = False


@dataclass(frozen=True)
class _RemoteAgent:
    connection: Connection
    player: Player
    tournament: str
    match: str

    def act(self, rows: Sequence[object]) -> Sequence[object]:
        if not rows:
            return []
        try:
            self.connection.send(Request(self.player, list(rows), self.tournament, self.match))
            if not self.connection.poll(_remaining()):
                raise RetryRequested("agent response exceeded the evaluation deadline")
            answer = cast(Answer, self.connection.recv())
        except (EOFError, OSError) as error:
            raise RetryRequested("agent process exited before answering") from error
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
def connect_agents(
    address: str, players: Sequence[Player], tournament: str, match: str
) -> Iterator[Sequence[Agent]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(_remaining())
        try:
            peer.connect(address)
        except OSError as error:
            raise RetryRequested("agent process is unavailable") from error
        peer.settimeout(None)
        with Connection(peer.detach()) as connection:
            yield [_RemoteAgent(connection, p, tournament, match) for p in players]


@dataclass(frozen=True)
class AgentProcess:
    address: str
    process: multiprocessing.context.SpawnProcess
    exited: Future[None]

    def check(self) -> None:
        if self.process.exitcode is not None:
            raise RetryRequested("agent process exited unexpectedly")


def _run(
    address: str,
    module: str,
    name: str,
    path: str,
    config: str,
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
            config,
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
    pending: int,
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
            definition.config,
            definition.max_batch,
            definition.max_delay_seconds,
            memory,
            max(
                1,
                available_cpus()
                - active_games(pending, agents=True, memory_gb=definition.memory_gb),
            ),
            child,
            str(artifacts),
        ),
    )
    process.start()
    child.close()
    exited: Future[None] = Future()
    sentinel = process.sentinel

    def observe_exit() -> None:
        wait([sentinel])
        exited.set_result(None)

    observer = threading.Thread(target=observe_exit, name="etude-agents")
    observer.start()
    try:
        ready = wait([parent, process.sentinel], timeout=_remaining())
        if parent not in ready:
            raise RetryRequested("agent process failed to start before the deadline")
        try:
            parent.recv()
        except EOFError as error:
            raise RetryRequested("agent process exited during startup") from error
        print(
            f"etude: agents: started for {', '.join(tournaments)}; "
            f"{active_games(pending, agents=True, memory_gb=definition.memory_gb)} games at a time",
            file=sys.stderr,
            flush=True,
        )
        yield AgentProcess(address=address, process=process, exited=exited)
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
        observer.join()
        parent.close()
        exitcode = process.exitcode
        process.close()
        if not failed and exitcode != 0:
            raise RetryRequested(f"agent process exited with status {exitcode}")
