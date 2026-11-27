"""The agent process's batching loop and durable batch observations.

`_Batches` owns pending inputs; Forwards owns loading and byte-based eviction. `_run_agents`
imports the authored loader and records batches as frozen Pydantic records;
game processes never import this module.
"""

from __future__ import annotations

import selectors
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import cast

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import AgentFailure, Answer, Request
from etude.eval.forwards import Forwards
from etude.records import Record


class BatchInput(Record):
    player: str
    tournament: str
    match: str


class Batch(Record):
    checkpoint: str
    inputs: tuple[BatchInput, ...]
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
        size = len(item.request.inputs)
        responses.append(
            ForkingPickler.dumps(
                Answer(answer.outputs[offset : offset + size], answer.error, answer.retry)
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
        self.forwards = Forwards(load, memory)
        self.max_batch = max_batch
        self.max_delay = max_delay
        self.pending: dict[str, list[_Pending]] = {}
        self.batches = 0
        self.inputs = 0

    @property
    def loads(self) -> int:
        return self.forwards.loads

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

    def _forward(self, batch: list[_Pending]) -> tuple[Answer, list[Batch]]:
        """Cap forwards even when one game requests more than a batch of rows."""
        checkpoint = batch[0].request.checkpoint
        inputs = [item for pending in batch for item in pending.request.inputs]
        origins = tuple(
            BatchInput(
                player=p.request.player, tournament=p.request.tournament, match=p.request.match
            )
            for p in batch
            for _ in p.request.inputs
        )
        outputs: list[object] = []
        observations: list[Batch] = []
        for start in range(0, len(inputs), self.max_batch):
            stop = start + self.max_batch
            before_calls, before_loads = self.forwards.calls, self.forwards.loads
            before_forward, before_load = self.forwards.forward_seconds, self.forwards.load_seconds
            answer = self.forwards.answer(checkpoint, inputs[start:stop], self.pending.keys())
            loads = self.forwards.loads - before_loads
            if loads:
                print(f"etude: agents: loaded {checkpoint.key}", file=sys.stderr, flush=True)
            observations.append(
                Batch(
                    checkpoint=checkpoint.key,
                    inputs=origins[start:stop],
                    forward_seconds=self.forwards.forward_seconds - before_forward,
                    load_seconds=self.forwards.load_seconds - before_load,
                    forward_calls=self.forwards.calls - before_calls,
                    loads=loads,
                    transport_seconds=0,
                )
            )
            if answer.error is not None:
                return answer, observations
            outputs.extend(answer.outputs)
        return Answer(outputs), observations

    def flush(self, now: float, control: Connection | None = None) -> list[Batch]:
        observations: list[Batch] = []
        for key, items in list(self.pending.items()):
            if (
                sum(len(p.request.inputs) for p in items) < self.max_batch
                and now < items[0].since + self.max_delay
            ):
                continue
            batch: list[_Pending] = []
            count = 0
            for item in items:
                size = len(item.request.inputs)
                if batch and count + size > self.max_batch:
                    break
                batch.append(item)
                count += size
            del items[: len(batch)]
            if not items:
                del self.pending[key]
            answer, forwarded = self._forward(batch)
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
                            loading=key in self.forwards.failed,
                        )
                        for p in batch
                    ]
                )
                # Only failure metadata crosses the control pipe; inputs stay direct.
                control.recv()
            started = time.monotonic()
            for item, response in zip(batch, responses, strict=True):
                try:
                    item.connection.send_bytes(response)
                except (OSError, EOFError):
                    pass  # The game may have been cancelled at its deadline.
            transport_seconds += time.monotonic() - started
            if forwarded:
                # One transport exchange covers all slices of an oversized request.
                forwarded[0] = forwarded[0].model_copy(
                    update={"transport_seconds": transport_seconds}
                )
            self.inputs += sum(len(each.inputs) for each in forwarded)
            self.batches += len(forwarded)
            observations.extend(forwarded)
        return observations


def _run_agents(
    address: str,
    load: Function,
    checkout: str | None,
    max_batch: int,
    max_delay: float,
    memory: int,
    control: Connection,
    artifacts: str,
) -> None:
    batches = _Batches(load.load(checkout), max_batch, max_delay, memory)
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
            f"etude: agents: stopped; {batches.loads} checkpoints loaded, {batches.inputs} inputs in {batches.batches} batches",
            file=sys.stderr,
            flush=True,
        )
