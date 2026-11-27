"""Zero-delay checkpoint batching for both the shared agent process and `LocalForwardBus`.

`_Batches` owns pending requests and runs `Forwards` loading, eviction, input
freezing, output normalization, and anti-aliasing on both transports.
`_run_agents` drives `_Batches` over a private Unix socket; `LocalForwardBus`
drives the same `_Batches` on a dedicated non-daemon inference thread inside a
game process. Socket game processes never import this module.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import queue
import select
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import (
    Answer,
    BatchRequest,
    CheckpointRequest,
    ForwardError,
    recv_frame,
    send_frame,
)
from etude.eval.bus import (
    BusReport,
    Flight,
    ForwardCall,
    PickledPayload,
    detach,
    freeze,
    normalize_inputs,
    normalize_outputs,
    pack_local_inputs,
    pack_local_outputs,
    unpack_local,
)
from etude.eval.forwards import Forwards
from etude.eval.resources import seconds_left
from etude.records import Record

if TYPE_CHECKING:
    from numpy import ndarray

    from etude.checkpoints import CheckpointRef


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
class _Pending[Encoded]:
    reply: Callable[[Encoded], None]
    request: CheckpointRequest
    receive_seconds: float


def _identity_answer(answer: Answer) -> Answer:
    return answer


def _slice_answers[Encoded](answer: Answer, batch: list[_Pending[Encoded]]) -> list[Answer]:
    sliced: list[Answer] = []
    offset = 0
    for item in batch:
        size = len(item.request.inputs)
        sliced.append(
            Answer(
                id=item.request.id,
                checkpoint=answer.checkpoint,
                outputs=answer.outputs[offset : offset + size],
                error=answer.error,
                retry=answer.retry,
                loading=answer.loading,
            )
        )
        offset += size
    return sliced


class _Batches[Encoded]:
    """Single-thread ownership of loaded agents and queued per-checkpoint requests."""

    def __init__(
        self,
        load: Callable[..., object],
        max_batch: int,
        memory: int,
        *,
        encode: Callable[[Answer], Encoded] = cast(Callable[[Answer], Encoded], _identity_answer),
    ) -> None:
        self.forwards = Forwards(load, memory)
        self.max_batch = max_batch
        self._encode = encode
        self.pending: dict[str, list[_Pending[Encoded]]] = {}
        self.batches = 0
        self.rows = 0

    @property
    def loads(self) -> int:
        return self.forwards.loads

    def add(
        self,
        reply: Callable[[Encoded], None],
        request: CheckpointRequest,
        receive_seconds: float = 0.0,
    ) -> None:
        self.pending.setdefault(request.checkpoint.key, []).append(
            _Pending(reply, request, receive_seconds)
        )

    def add_batch(
        self,
        reply: Callable[[Encoded], None],
        batch: BatchRequest,
        receive_seconds: float = 0.0,
    ) -> None:
        if not batch.requests:
            return
        share = receive_seconds / len(batch.requests)
        for request in batch.requests:
            self.add(reply, request, share)

    def flush(self, queue_wait_seconds: float = 0.0) -> list[Batch]:
        """Forward one chunk per pending checkpoint, up to `max_batch` rows each."""
        observations: list[Batch] = []
        remaining_wait = queue_wait_seconds
        for key, items in list(self.pending.items()):
            batch: list[_Pending[Encoded]] = []
            rows = 0
            for item in items:
                if batch and rows + item.request.rows > self.max_batch:
                    break
                batch.append(item)
                rows += item.request.rows
            del items[: len(batch)]
            if not items:
                del self.pending[key]
            inputs = [value for pending in batch for value in pending.request.inputs]
            freeze(inputs)
            before_calls, before_loads = self.forwards.calls, self.forwards.loads
            before_forward, before_load = self.forwards.forward_seconds, self.forwards.load_seconds
            answer = self.forwards.answer(
                batch[0].request.checkpoint,
                inputs,
                self.pending.keys(),
                rows=rows,
            )
            forward_seconds = self.forwards.forward_seconds - before_forward
            load_seconds = self.forwards.load_seconds - before_load
            forward_calls = self.forwards.calls - before_calls
            loads = self.forwards.loads - before_loads
            if loads:
                print(f"etude: agents: loaded {key}", file=sys.stderr, flush=True)
            if answer.error is None:
                try:
                    outputs = normalize_outputs(answer.outputs)
                    outputs = detach(outputs, inputs)
                except Exception as error:
                    answer = Answer(
                        id=0,
                        checkpoint=key,
                        outputs=[],
                        error=f"agent {key}: {explain(error)}",
                        retry=isinstance(error, RetryRequested),
                    )
                else:
                    answer = Answer(id=0, checkpoint=key, outputs=outputs)
            # Encode every reply before delivery: an unpicklable answer fails
            # the whole batch, while successful payloads are encoded only once.
            started = time.monotonic()
            try:
                encoded = [
                    self._encode(item_answer) for item_answer in _slice_answers(answer, batch)
                ]
            except Exception as error:
                failed = Answer(
                    id=0,
                    checkpoint=key,
                    outputs=[],
                    error=f"agent {key}: {explain(error)}",
                    retry=isinstance(error, RetryRequested),
                )
                encoded = [
                    self._encode(item_answer) for item_answer in _slice_answers(failed, batch)
                ]
            encode_seconds = time.monotonic() - started
            receive_seconds = sum(p.receive_seconds for p in batch)
            started = time.monotonic()
            for item, response in zip(batch, encoded, strict=True):
                item.reply(response)
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


@dataclass(frozen=True)
class _BindMessage:
    checkpoint: CheckpointRef


@dataclass(frozen=True)
class _LocalCall:
    id: int
    checkpoint: str
    payload: list[object] | PickledPayload
    rows: int


@dataclass(frozen=True)
class _SubmitMessage:
    items: tuple[_LocalCall, ...]


@dataclass(frozen=True)
class _StopMessage:
    pass


type _LocalMessage = _BindMessage | _SubmitMessage | _StopMessage


@dataclass(frozen=True)
class _LocalReply:
    id: int
    checkpoint: str
    payload: list[object] | PickledPayload | None = None
    error: str | None = None
    retry: bool = False
    loading: bool = False


def _encode_local_answer(answer: Answer) -> _LocalReply:
    if answer.error is not None:
        return _LocalReply(
            id=answer.id,
            checkpoint=answer.checkpoint,
            error=answer.error,
            retry=answer.retry,
            loading=answer.loading,
        )
    return _LocalReply(
        id=answer.id,
        checkpoint=answer.checkpoint,
        payload=pack_local_outputs(answer.outputs),
    )


def _write_batches(report: TextIO | None, flushed: Sequence[Batch]) -> None:
    if report is None or not flushed:
        return
    for batch in flushed:
        if report.tell() > 1:
            report.write(",")
        report.write(batch.model_dump_json())
    report.flush()


def _inference_loop(
    inbox: queue.SimpleQueue[_LocalMessage],
    load: Function | Callable[..., object],
    checkout: str | None,
    max_batch: int,
    memory: int,
    report_path: Path | None,
    loop: asyncio.AbstractEventLoop,
    deliver: Callable[[_LocalReply], None],
    on_exit: Callable[[BaseException], None],
) -> None:
    """Dedicated inference thread body; owns `Forwards`, loader and all loaded models."""
    resolved: Callable[..., object] | None = None if isinstance(load, Function) else load

    def load_checkpoint(checkpoint: CheckpointRef) -> object:
        nonlocal resolved
        if resolved is None:
            assert isinstance(load, Function)
            resolved = load.load(checkout)
        return resolved(checkpoint)

    batches: _Batches[_LocalReply] = _Batches(
        load_checkpoint, max_batch, memory, encode=_encode_local_answer
    )
    bound: dict[str, CheckpointRef] = {}
    report: TextIO | None = None
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = report_path.open("w")
        report.write("[")
    wait_seconds = 0.0
    stopped_cleanly = False

    def post(reply: _LocalReply) -> None:
        try:
            loop.call_soon_threadsafe(deliver, reply)
        except RuntimeError:
            pass  # Event loop closed during shutdown.

    def handle(message: _BindMessage | _SubmitMessage) -> None:
        if isinstance(message, _BindMessage):
            bound.setdefault(message.checkpoint.key, message.checkpoint)
            return
        for item in message.items:
            checkpoint = bound.get(item.checkpoint)
            if checkpoint is None:
                post(
                    _LocalReply(
                        id=item.id,
                        checkpoint=item.checkpoint,
                        error=f"checkpoint {item.checkpoint} is not bound",
                    )
                )
                continue
            started = time.monotonic()
            try:
                inputs = unpack_local(item.payload)
            except Exception as error:
                post(
                    _LocalReply(
                        id=item.id,
                        checkpoint=item.checkpoint,
                        error=f"agent {item.checkpoint}: {explain(error)}",
                        retry=isinstance(error, RetryRequested),
                    )
                )
                continue
            batches.add(
                post,
                CheckpointRequest(item.id, checkpoint, inputs, item.rows),
                receive_seconds=time.monotonic() - started,
            )

    try:
        while True:
            if batches.pending:
                while True:
                    try:
                        message = inbox.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(message, _StopMessage):
                        stopped_cleanly = True
                        return
                    handle(message)
            else:
                wait_started = time.monotonic()
                message = inbox.get()
                wait_seconds += time.monotonic() - wait_started
                if isinstance(message, _StopMessage):
                    stopped_cleanly = True
                    return
                handle(message)
                while True:
                    try:
                        extra = inbox.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(extra, _StopMessage):
                        stopped_cleanly = True
                        return
                    handle(extra)
            flushed = batches.flush(wait_seconds)
            if flushed:
                wait_seconds = 0.0
                _write_batches(report, flushed)
    except BaseException:
        stopped_cleanly = False
    finally:
        if report is not None:
            report.write("]")
            report.close()
        if not stopped_cleanly:
            try:
                loop.call_soon_threadsafe(
                    on_exit, RetryRequested("inference thread exited unexpectedly")
                )
            except RuntimeError:
                pass


class LocalForwardBus:
    """In-process `ForwardBus` backed by one long-lived non-daemon inference thread."""

    def __init__(
        self,
        load: Function | Callable[..., object],
        *,
        max_batch: int,
        memory: int,
        checkout: str | None = None,
        artifacts: Path | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._inbox: queue.SimpleQueue[_LocalMessage] = queue.SimpleQueue()
        self._next_id = 0
        self._bound: set[str] = set()
        self._flight = Flight()
        self._in_flight: dict[int, tuple[asyncio.Future[list[object]], float, list[ndarray]]] = {}
        self._closed_error: BaseException | None = None
        self._calls = 0
        self._answers = 0
        self._arrays = 0
        self._arrays_copied = 0
        self._copied_bytes = 0
        self._pickled = 0
        self._pickled_bytes = 0
        self._handoff_seconds = 0.0
        self._round_trip_seconds = 0.0
        report_path = None if artifacts is None else artifacts / f"batches-{os.getpid()}.json"
        self._thread = threading.Thread(
            target=_inference_loop,
            name="inference",
            args=(
                self._inbox,
                load,
                checkout,
                max_batch,
                memory,
                report_path,
                self._loop,
                self._receive,
                self._fail_all,
            ),
            daemon=False,
        )
        self._thread.start()

    def bind(self, checkpoint: CheckpointRef) -> None:
        if checkpoint.key in self._bound:
            return
        self._bound.add(checkpoint.key)
        self._inbox.put(_BindMessage(checkpoint))

    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]:
        if not calls:
            return []
        if self._closed_error is not None:
            return [self._failed(self._closed_error) for _ in calls]

        started = time.monotonic()
        now = started
        queued: list[_LocalCall] = []
        futures: list[asyncio.Future[list[object]]] = []
        for call in calls:
            if call.checkpoint not in self._bound:
                raise KeyError(f"checkpoint {call.checkpoint} is not bound")
            locked: list[ndarray] = []
            try:
                normalized = normalize_inputs(call.inputs)
                locked = self._flight.lock(call.inputs)
                packed = pack_local_inputs(normalized)
            except (pickle.PicklingError, TypeError, AttributeError) as error:
                self._flight.unlock(locked)
                futures.append(self._failed(error))
                continue
            except BaseException:
                self._flight.unlock(locked)
                raise
            self._next_id += 1
            request_id = self._next_id
            future: asyncio.Future[list[object]] = self._loop.create_future()
            self._in_flight[request_id] = (future, now, locked)
            queued.append(_LocalCall(request_id, call.checkpoint, packed.payload, call.rows))
            futures.append(future)
            self._calls += 1
            self._arrays += packed.arrays
            self._arrays_copied += packed.arrays_copied
            self._copied_bytes += packed.copied_bytes
            self._pickled += packed.pickled
            self._pickled_bytes += packed.pickled_bytes
        if queued:
            self._inbox.put(_SubmitMessage(tuple(queued)))
        self._handoff_seconds += time.monotonic() - started
        return futures

    def _failed(self, error: BaseException) -> asyncio.Future[list[object]]:
        future: asyncio.Future[list[object]] = self._loop.create_future()
        future.set_exception(error)
        return future

    def _receive(self, reply: _LocalReply) -> None:
        entry = self._in_flight.pop(reply.id, None)
        if entry is None:
            return
        future, sent, locked = entry
        self._flight.unlock(locked)
        self._answers += 1
        self._round_trip_seconds += time.monotonic() - sent
        if future.done():
            return
        if reply.error is not None:
            future.set_exception(
                ForwardError(
                    reply.checkpoint,
                    reply.error,
                    retry=reply.retry,
                    loading=reply.loading,
                )
            )
            return
        try:
            outputs = unpack_local([] if reply.payload is None else reply.payload)
        except Exception as error:
            future.set_exception(
                ForwardError(
                    reply.checkpoint,
                    f"agent {reply.checkpoint}: {explain(error)}",
                    retry=isinstance(error, RetryRequested),
                )
            )
            return
        future.set_result(outputs)

    def _fail_all(self, error: BaseException) -> None:
        if self._closed_error is None:
            self._closed_error = error
        for future, _, locked in self._in_flight.values():
            self._flight.unlock(locked)
            if not future.done():
                future.set_exception(error)
        self._in_flight.clear()

    def _report(self) -> BusReport:
        return BusReport(
            calls=self._calls,
            answers=self._answers,
            arrays=self._arrays,
            arrays_copied=self._arrays_copied,
            pickled=self._pickled,
            pickled_bytes=self._pickled_bytes,
            copied_bytes=self._copied_bytes,
            wire_bytes=0,
            handoff_seconds=self._handoff_seconds,
            round_trip_seconds=self._round_trip_seconds,
        )

    async def close(self) -> BusReport:
        if self._closed_error is None:
            self._closed_error = RetryRequested("forward bus is closed")
        self._inbox.put(_StopMessage())
        remaining = seconds_left()
        timeout = None if remaining is None else max(0.0, remaining)
        await asyncio.to_thread(self._thread.join, timeout)
        await asyncio.sleep(0)  # Let replies posted just before thread exit settle on the loop.
        if not self._thread.is_alive():
            self._fail_all(self._closed_error)
        return self._report()


def _send_peer(peer: socket.socket) -> Callable[[memoryview], None]:
    def send(payload: memoryview) -> None:
        try:
            send_frame(peer, payload)
        except (OSError, EOFError):
            pass  # The game may have been cancelled at its deadline.

    return send


def _run_agents(
    address: str,
    load: Function,
    checkout: str | None,
    max_batch: int,
    memory: int,
    control: Connection,
    artifacts: str,
) -> None:
    batches: _Batches[memoryview] = _Batches(
        load.load(checkout), max_batch, memory, encode=ForkingPickler.dumps
    )
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
                        while True:
                            started = time.monotonic()
                            try:
                                payload = recv_frame(peer)
                                request = cast(BatchRequest, ForkingPickler.loads(payload))
                            except (EOFError, OSError):
                                selector.unregister(peer)
                                peer.close()
                                peers.remove(peer)
                                break
                            else:
                                batches.add_batch(
                                    _send_peer(peer), request, time.monotonic() - started
                                )
                            if not select.select([peer], [], [], 0.0)[0]:
                                break
                flushed = batches.flush(wait_seconds)
                if flushed:
                    wait_seconds = 0.0
                    _write_batches(report, flushed)
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
