"""The agent process's zero-delay batching loop and durable batch observations.

`_Batches` owns pending requests; `Forwards` owns loading and byte-based eviction.
`_run_agents` imports the authored loader and records batches as frozen Pydantic
records; game processes never import this module.
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
from etude.eval.agents import Answer, BatchRequest, CheckpointRequest, recv_frame, send_frame
from etude.eval.forwards import Forwards
from etude.records import Record


class Batch(Record):
    checkpoint: str
    rows: int
    requests: int
    queue_wait_seconds: float
    receive_seconds: float
    load_seconds: float
    forward_seconds: float
    encode_seconds: float
    send_seconds: float
    forward_calls: int
    loads: int


@dataclass
class _Pending:
    peer: socket.socket
    request: CheckpointRequest
    receive_seconds: float


def _responses(answer: Answer, batch: list[_Pending]) -> list[memoryview]:
    responses: list[memoryview] = []
    offset = 0
    for item in batch:
        size = len(item.request.inputs)
        responses.append(
            ForkingPickler.dumps(
                Answer(
                    id=item.request.id,
                    checkpoint=answer.checkpoint,
                    outputs=answer.outputs[offset : offset + size],
                    error=answer.error,
                    retry=answer.retry,
                    loading=answer.loading,
                )
            )
        )
        offset += size
    return responses


class _Batches:
    """Single-thread ownership of loaded agents and queued per-checkpoint requests."""

    def __init__(
        self,
        load: Callable[..., object],
        max_batch: int,
        memory: int,
    ) -> None:
        self.forwards = Forwards(load, memory)
        self.max_batch = max_batch
        self.pending: dict[str, list[_Pending]] = {}
        self.batches = 0
        self.rows = 0

    @property
    def loads(self) -> int:
        return self.forwards.loads

    def add(
        self,
        peer: socket.socket,
        request: CheckpointRequest,
        receive_seconds: float = 0.0,
    ) -> None:
        self.pending.setdefault(request.checkpoint.key, []).append(
            _Pending(peer, request, receive_seconds)
        )

    def add_batch(
        self,
        peer: socket.socket,
        batch: BatchRequest,
        receive_seconds: float = 0.0,
    ) -> None:
        if not batch.requests:
            return
        share = receive_seconds / len(batch.requests)
        for request in batch.requests:
            self.add(peer, request, share)

    def flush(self, queue_wait_seconds: float = 0.0) -> list[Batch]:
        """Forward one chunk per pending checkpoint, up to `max_batch` rows each."""
        observations: list[Batch] = []
        remaining_wait = queue_wait_seconds
        for key, items in list(self.pending.items()):
            batch: list[_Pending] = []
            rows = 0
            for item in items:
                if batch and rows + item.request.rows > self.max_batch:
                    break
                batch.append(item)
                rows += item.request.rows
            del items[: len(batch)]
            if not items:
                del self.pending[key]
            before_calls, before_loads = self.forwards.calls, self.forwards.loads
            before_forward, before_load = self.forwards.forward_seconds, self.forwards.load_seconds
            answer = self.forwards.answer(
                batch[0].request.checkpoint,
                [item for pending in batch for item in pending.request.inputs],
                self.pending.keys(),
                rows=rows,
            )
            forward_seconds = self.forwards.forward_seconds - before_forward
            load_seconds = self.forwards.load_seconds - before_load
            forward_calls = self.forwards.calls - before_calls
            loads = self.forwards.loads - before_loads
            if loads:
                print(f"etude: agents: loaded {key}", file=sys.stderr, flush=True)
            # Serialize every reply before delivery: an unpicklable answer fails
            # the whole batch, while successful payloads are encoded only once.
            started = time.monotonic()
            try:
                responses = _responses(answer, batch)
            except Exception as error:
                answer = Answer(
                    id=0,
                    checkpoint=key,
                    outputs=[],
                    error=f"agent {key}: {explain(error)}",
                    retry=isinstance(error, RetryRequested),
                )
                responses = _responses(answer, batch)
            encode_seconds = time.monotonic() - started
            receive_seconds = sum(p.receive_seconds for p in batch)
            started = time.monotonic()
            for item, response in zip(batch, responses, strict=True):
                try:
                    send_frame(item.peer, response)
                except (OSError, EOFError):
                    pass  # The game may have been cancelled at its deadline.
            send_seconds = time.monotonic() - started
            self.rows += rows
            self.batches += 1
            observations.append(
                Batch(
                    checkpoint=key,
                    rows=rows,
                    requests=len(batch),
                    queue_wait_seconds=remaining_wait,
                    receive_seconds=receive_seconds,
                    load_seconds=load_seconds,
                    forward_seconds=forward_seconds,
                    encode_seconds=encode_seconds,
                    send_seconds=send_seconds,
                    forward_calls=forward_calls,
                    loads=loads,
                )
            )
            remaining_wait = 0.0
        return observations


def _run_agents(
    address: str,
    load: Function,
    checkout: str | None,
    max_batch: int,
    memory: int,
    control: Connection,
    artifacts: str,
) -> None:
    batches = _Batches(load.load(checkout), max_batch, memory)
    peers: list[socket.socket] = []
    report = Path(artifacts, "batches.json").open("w")
    report.write("[")
    wait_seconds = 0.0
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
                wait_started = time.monotonic()
                events = selector.select(0.0 if batches.pending else None)
                wait_seconds += time.monotonic() - wait_started
                for event, _ in events:
                    if event.fileobj is control:
                        return
                    if event.fileobj is listener:
                        peer, _ = listener.accept()
                        peers.append(peer)
                        selector.register(peer, selectors.EVENT_READ)
                    else:
                        peer = cast(socket.socket, event.fileobj)
                        started = time.monotonic()
                        try:
                            payload = recv_frame(peer)
                            request = cast(BatchRequest, ForkingPickler.loads(payload))
                        except (EOFError, OSError):
                            selector.unregister(peer)
                            peer.close()
                            peers.remove(peer)
                        else:
                            batches.add_batch(peer, request, time.monotonic() - started)
                flushed = batches.flush(wait_seconds)
                if flushed:
                    wait_seconds = 0.0
                for batch in flushed:
                    if report.tell() > 1:
                        report.write(",")
                    report.write(batch.model_dump_json())
                if flushed:
                    report.flush()
    finally:
        for peer in peers:
            peer.close()
        report.write("]")
        report.close()
        print(
            f"etude: agents: stopped; {batches.loads} checkpoints loaded, {batches.rows} rows in {batches.batches} batches",
            file=sys.stderr,
            flush=True,
        )
