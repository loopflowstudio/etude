"""The agent process's batching loop and durable batch observations.

`_Batches` owns loaded agents, pending rows and byte-based eviction. `_run_agents`
imports the authored loader and records batches as frozen Pydantic records;
game processes never import this module.
"""

from __future__ import annotations

import importlib
import os
import pickle
import selectors
import socket
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast

from etude.errors import RetryRequested, explain
from etude.eval.agents import Agent, AgentFailure, Answer, Request
from etude.eval.resources import THREAD_VARIABLES
from etude.records import Record


class BatchRow(Record):
    player: str
    tournament: str
    match: str


class Batch(Record):
    checkpoint: str
    rows: tuple[BatchRow, ...]


@dataclass
class _Pending:
    connection: Connection
    request: Request
    since: float


class _Batches:
    """Single-thread ownership of loaded agents and queued requests."""

    def __init__(
        self,
        load: Callable[..., object],
        max_batch: int,
        max_delay: float,
        memory: int,
    ) -> None:
        self.load = load
        self.max_batch = max_batch
        self.max_delay = max_delay
        self.memory = memory
        self.pending: dict[str, list[_Pending]] = {}
        self.loaded: OrderedDict[str, tuple[Agent, int]] = OrderedDict()
        self.failed: dict[str, Answer] = {}
        self.batches = 0
        self.loads = 0
        self.rows = 0

    def add(self, connection: Connection, request: Request, now: float) -> None:
        self.pending.setdefault(request.checkpoint.key, []).append(
            _Pending(connection, request, now)
        )

    def delay(self, now: float) -> float | None:
        if not self.pending:
            return None
        return max(0, min(items[0].since + self.max_delay for items in self.pending.values()) - now)

    def flush(self, now: float, control: Connection | None = None) -> list[Batch]:
        observations: list[Batch] = []
        for key, items in list(self.pending.items()):
            if (
                sum(len(p.request.rows) for p in items) < self.max_batch
                and now < items[0].since + self.max_delay
            ):
                continue
            batch: list[_Pending] = []
            count = 0
            for item in items:
                size = len(item.request.rows)
                if batch and count + size > self.max_batch:
                    break
                batch.append(item)
                count += size
            del items[: len(batch)]
            if not items:
                del self.pending[key]
            answer = self.failed.get(key)
            if answer is None:
                try:
                    if key not in self.loaded:
                        checkpoint = batch[0].request.checkpoint
                        length = checkpoint.artifact.size_bytes
                        if length is None:
                            raise ValueError(f"checkpoint {key} has no byte length")
                        size = length
                        self._evict(size)
                        try:
                            agent = cast(Agent, self.load(checkpoint))
                        except Exception as error:
                            self.failed[key] = Answer(
                                [],
                                f"load failed for {key}: {explain(error)}",
                                isinstance(error, RetryRequested),
                            )
                            raise
                        self.loaded[key] = (agent, size)
                        self.loads += 1
                        print(f"etude: agents: loaded {key}", file=sys.stderr, flush=True)
                    self.loaded.move_to_end(key)
                    rows = [row for p in batch for row in p.request.rows]
                    answers = list(self.loaded[key][0].act(rows))
                    if len(answers) != len(rows):
                        raise ValueError("Agent.act must return one answer per row")
                    # Validate once before answering any member of the batch.
                    pickle.dumps(answers)
                    answer = Answer(answers)
                except Exception as error:
                    answer = self.failed.get(key) or Answer(
                        [], f"agent {key}: {explain(error)}", isinstance(error, RetryRequested)
                    )
            if answer.error is not None and control is not None:
                control.send(
                    [
                        AgentFailure(
                            checkpoint=key,
                            tournament=p.request.tournament,
                            match=p.request.match,
                            error=answer.error,
                            retry=answer.retry,
                            loading=key in self.failed,
                        )
                        for p in batch
                    ]
                )
                # Only failure metadata crosses the control pipe; rows stay direct.
                control.recv()
            offset = 0
            for item in batch:
                size = len(item.request.rows)
                response = Answer(answer.rows[offset : offset + size], answer.error, answer.retry)
                try:
                    item.connection.send(response)
                except (OSError, EOFError):
                    pass  # The game may have been cancelled at its deadline.
                offset += size
            self.rows += count
            self.batches += 1
            observations.append(
                Batch(
                    checkpoint=key,
                    rows=tuple(
                        BatchRow(
                            player=p.request.player,
                            tournament=p.request.tournament,
                            match=p.request.match,
                        )
                        for p in batch
                        for _ in p.request.rows
                    ),
                )
            )
            self._evict(0)
        return observations

    def _evict(self, incoming: int) -> None:
        for key in list(self.loaded):
            if sum(size for _, size in self.loaded.values()) + incoming <= self.memory:
                break
            if key not in self.pending:
                del self.loaded[key]


def _run_agents(
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
    for variable in THREAD_VARIABLES:
        os.environ[variable] = str(threads)
    sys.path.insert(0, path)
    function: object = importlib.import_module(module)
    for part in name.split("."):
        function = getattr(function, part)
    load = cast(Callable[..., object], function)
    batches = _Batches(load, max_batch, max_delay, memory)
    peers: list[Connection] = []
    report = Path(artifacts, "batches.json").open("w")
    report.write("[")
    try:
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
            selectors.DefaultSelector() as selector,
        ):
            listener.bind(address)
            listener.listen()
            selector.register(listener, selectors.EVENT_READ)
            selector.register(control, selectors.EVENT_READ)
            control.send(None)
            while True:
                for event, _ in selector.select(batches.delay(time.monotonic())):
                    if event.fileobj is control:
                        return
                    if event.fileobj is listener:
                        peer, _ = listener.accept()
                        connection = Connection(peer.detach())
                        peers.append(connection)
                        selector.register(connection, selectors.EVENT_READ)
                    else:
                        connection = cast(Connection, event.fileobj)
                        try:
                            request = cast(Request, connection.recv())
                        except (EOFError, OSError):
                            selector.unregister(connection)
                            connection.close()
                            peers.remove(connection)
                        else:
                            batches.add(connection, request, time.monotonic())
                for batch in batches.flush(time.monotonic(), control):
                    if report.tell() > 1:
                        report.write(",")
                    report.write(batch.model_dump_json())
                report.flush()
    finally:
        for connection in peers:
            connection.close()
        report.write("]")
        report.close()
        print(
            f"etude: agents: stopped; {batches.loads} checkpoints loaded, {batches.rows} rows in {batches.batches} batches",
            file=sys.stderr,
            flush=True,
        )
