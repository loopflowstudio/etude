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
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import cast

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import (
    Answer,
    CheckpointRequest,
    ForwardError,
    ForwardResult,
    recv_frame,
    send_frame,
)
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


def _responses(result: ForwardResult, batch: list[_Pending]) -> list[memoryview]:
    responses: list[memoryview] = []
    offset = 0
    for item in batch:
        stop = offset + len(item.request.inputs)
        part = result if isinstance(result, ForwardError) else result[offset:stop]
        responses.append(ForkingPickler.dumps(Answer(item.request.id, part)))
        offset = stop
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
        requests: Sequence[CheckpointRequest],
        receive_seconds: float = 0.0,
    ) -> None:
        if not requests:
            return
        share = receive_seconds / len(requests)
        for request in requests:
            self.add(peer, request, share)

    def _forward(
        self, key: str, batch: list[_Pending], rows: int
    ) -> tuple[ForwardResult, list[Batch]]:
        checkpoint = batch[0].request.checkpoint
        inputs = [item for pending in batch for item in pending.request.inputs]
        # Opaque pre-collated inputs have no Etude-owned row slicing operation.
        size = self.max_batch if rows == len(inputs) else len(inputs)
        outputs: list[object] = []
        observations: list[Batch] = []
        for start in range(0, len(inputs), size):
            chunk = inputs[start : start + size]
            count = len(chunk) if rows == len(inputs) else rows
            protected: Set[str] = self.pending.keys()
            if start + size < len(inputs):
                protected = protected | {key}
            before_calls, before_loads = self.forwards.calls, self.forwards.loads
            before_forward, before_load = self.forwards.forward_seconds, self.forwards.load_seconds
            answer = self.forwards.answer(checkpoint, chunk, protected, rows=count)
            loads = self.forwards.loads - before_loads
            if loads:
                print(f"etude: agents: loaded {key}", file=sys.stderr, flush=True)
            observations.append(
                Batch(
                    checkpoint=key,
                    rows=count,
                    requests=len(batch) if start == 0 else 0,
                    queue_wait_seconds=0.0,
                    receive_seconds=0.0,
                    load_seconds=self.forwards.load_seconds - before_load,
                    forward_seconds=self.forwards.forward_seconds - before_forward,
                    encode_seconds=0.0,
                    send_seconds=0.0,
                    forward_calls=self.forwards.calls - before_calls,
                    loads=loads,
                )
            )
            if isinstance(answer, ForwardError):
                return answer, observations
            outputs.extend(answer)
        return outputs, observations

    def flush(self, queue_wait_seconds: float = 0.0) -> list[Batch]:
        """Flush ready requests by checkpoint, batching up to `max_batch` rows."""
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
            answer, forwarded = self._forward(key, batch, rows)
            # Serialize every reply before delivery: an unpicklable answer fails
            # the whole batch, while successful payloads are encoded only once.
            started = time.monotonic()
            try:
                responses = _responses(answer, batch)
            except Exception as error:
                answer = ForwardError(
                    key,
                    f"agent {key}: {explain(error)}",
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
            forwarded[0] = forwarded[0].model_copy(
                update={
                    "queue_wait_seconds": remaining_wait,
                    "receive_seconds": receive_seconds,
                    "encode_seconds": encode_seconds,
                    "send_seconds": send_seconds,
                }
            )
            self.rows += sum(batch.rows for batch in forwarded)
            self.batches += len(forwarded)
            observations.extend(forwarded)
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
                            request = cast(list[CheckpointRequest], ForkingPickler.loads(payload))
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
