"""The agent process's batching loop and durable batch observations.

`_Batches` owns loaded agents, pending rows and byte-based eviction. `_run_agents`
imports the authored loader and records batches as frozen Pydantic records;
game processes never import this module.
"""

from __future__ import annotations

import importlib
import os
import selectors
import socket
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
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
    forward_seconds: float
    load_seconds: float
    forward_calls: int
    loads: int
    transport_seconds: float


@dataclass
class _Pending:
    connection: Connection
    request: Request
    since: float
    receive_seconds: float


def _responses(answer: Answer, batch: list[_Pending]) -> list[memoryview]:
    responses: list[memoryview] = []
    offset = 0
    for item in batch:
        size = len(item.request.rows)
        responses.append(
            ForkingPickler.dumps(
                Answer(answer.rows[offset : offset + size], answer.error, answer.retry)
            )
        )
        offset += size
    return responses


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

    def add(
        self, connection: Connection, request: Request, now: float, receive_seconds: float = 0.0
    ) -> None:
        self.pending.setdefault(request.checkpoint.key, []).append(
            _Pending(connection, request, now, receive_seconds)
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
            forward_seconds = 0.0
            load_seconds = 0.0
            forward_calls = 0
            loads = 0
            answer = self.failed.get(key)
            if answer is None:
                try:
                    if key not in self.loaded:
                        checkpoint = batch[0].request.checkpoint
                        length = checkpoint.artifact.size_bytes
                        if length is None:
                            raise ValueError(f"checkpoint {key} has no byte length")
                        self._evict(length)
                        started = time.monotonic()
                        try:
                            agent = cast(Agent, self.load(checkpoint))
                        except Exception as error:
                            self.failed[key] = Answer(
                                [],
                                f"load failed for {key}: {explain(error)}",
                                isinstance(error, RetryRequested),
                            )
                            raise
                        finally:
                            load_seconds = time.monotonic() - started
                        self.loaded[key] = (agent, length)
                        self.loads += 1
                        loads = 1
                        print(f"etude: agents: loaded {key}", file=sys.stderr, flush=True)
                    self.loaded.move_to_end(key)
                    rows = [row for p in batch for row in p.request.rows]
                    started = time.monotonic()
                    forward_calls = 1
                    try:
                        answers = list(self.loaded[key][0].act(rows))
                    finally:
                        forward_seconds = time.monotonic() - started
                    if len(answers) != len(rows):
                        raise ValueError("Agent.act must return one answer per row")
                    answer = Answer(answers)
                except Exception as error:
                    answer = self.failed.get(key) or Answer(
                        [], f"agent {key}: {explain(error)}", isinstance(error, RetryRequested)
                    )
            # Serialize every reply before delivery: an unpicklable answer fails
            # the whole batch, while successful payloads are encoded only once.
            started = time.monotonic()
            try:
                responses = _responses(answer, batch)
            except Exception as error:
                answer = Answer(
                    [], f"agent {key}: {explain(error)}", isinstance(error, RetryRequested)
                )
                responses = _responses(answer, batch)
            transport_seconds = time.monotonic() - started + sum(p.receive_seconds for p in batch)
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
            started = time.monotonic()
            for item, response in zip(batch, responses, strict=True):
                try:
                    item.connection.send_bytes(response)
                except (OSError, EOFError):
                    pass  # The game may have been cancelled at its deadline.
            transport_seconds += time.monotonic() - started
            self.rows += count
            self.batches += 1
            observations.append(
                Batch(
                    checkpoint=key,
                    forward_seconds=forward_seconds,
                    load_seconds=load_seconds,
                    forward_calls=forward_calls,
                    loads=loads,
                    transport_seconds=transport_seconds,
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
        used = sum(size for _, size in self.loaded.values())
        for key in list(self.loaded):
            if used + incoming <= self.memory:
                break
            if key not in self.pending:
                _, size = self.loaded.pop(key)
                used -= size


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
                        started = time.monotonic()
                        try:
                            request = cast(Request, connection.recv())
                        except (EOFError, OSError):
                            selector.unregister(connection)
                            connection.close()
                            peers.remove(connection)
                        else:
                            received = time.monotonic()
                            batches.add(connection, request, received, received - started)
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
