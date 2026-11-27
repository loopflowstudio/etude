"""Zero-delay checkpoint batching for both the shared agent process and `LocalForwardBus`.

`_Batches` owns pending requests and runs `Forwards` loading, eviction, input
freezing and output normalization on both transports using transport-neutral
`ForwardOutcome`s. `_run_agents` drives `_Batches` over a private Unix socket
(with `Answer`/`CheckpointRequest`/`BatchRequest` wire envelopes confined there);
`LocalForwardBus` drives the same `_Batches` on a dedicated non-daemon inference
thread inside a game process. Socket game processes never import this module.
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
from types import ModuleType
from typing import TYPE_CHECKING, TextIO, cast

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import (
    _PEER_ERROR_ID,
    Answer,
    BatchRequest,
    ForwardError,
    _forward_error,
    recv_frame,
    send_frame,
)
from etude.eval.bus import (
    BusReport,
    ForwardCall,
    ForwardOutcome,
    Normalized,
    _FlightBook,
    _numpy,
    arrays,
    freeze,
    normalize_inputs,
    normalize_outputs,
)
from etude.eval.forwards import Forwards
from etude.eval.resources import seconds_left
from etude.records import Record

if TYPE_CHECKING:
    from numpy import ndarray

    from etude.checkpoints import CheckpointRef

_IMMUTABLE_PLAIN = (type(None), bool, int, float, complex, str, bytes)


@dataclass(frozen=True)
class PickledPayload:
    """Pickle protocol 5 control bytes plus out-of-band zero-copy buffers."""

    data: bytes
    buffers: list[pickle.PickleBuffer]


@dataclass(frozen=True)
class PackResult:
    """Encoded local-bus inputs with transfer accounting for `BusReport`."""

    payload: list[object] | PickledPayload
    arrays: int
    arrays_copied: int
    copied_bytes: int
    pickled: int
    pickled_bytes: int


def _is_plain_array(value: object, np: ModuleType) -> bool:
    return type(value) is np.ndarray and not bool(cast("ndarray", value).dtype.hasobject)


def _is_fast_array(value: object, np: ModuleType | None) -> bool:
    return (
        np is not None
        and _is_plain_array(value, np)
        and bool(cast("ndarray", value).flags.c_contiguous)
    )


def _pickle_with_buffers(
    values: list[object], pointer_ids: set[int], *, require_readonly: bool
) -> PickledPayload:
    """Pickle protocol 5 emitting only selected ndarray buffers out-of-band."""
    buffers: list[pickle.PickleBuffer] = []

    def collect(buf: pickle.PickleBuffer) -> bool:
        view = memoryview(buf)
        by_pointer = (not require_readonly or view.readonly) and id(view.obj) in pointer_ids
        view.release()
        if by_pointer:
            buffers.append(buf)
            return False
        return True

    data = pickle.dumps(values, protocol=5, buffer_callback=collect)
    return PickledPayload(data=data, buffers=buffers)


def pack_local_inputs(normalized: Normalized) -> PackResult:
    """Encode normalized game inputs for the local inference thread.

    Top-level non-object ndarrays skip pickle: C-contiguous ones pass a read-only
    view by pointer, and non-contiguous ones copy into a contiguous array. Every
    other shape uses pickle protocol 5 so nested C-contiguous ndarrays stay
    zero-copy while containers and non-C-contiguous arrays copy into the
    inference thread's own objects. Call after `Flight.lock` so locked game
    arrays expose read-only buffers.
    """
    np = _numpy()
    values = normalized.values
    if np is not None and values and all(_is_plain_array(item, np) for item in values):
        views: list[object] = []
        pointer_arrays = 0
        copied_arrays = 0
        copied_bytes = 0
        for item in values:
            array = cast("ndarray", item)
            if array.flags.c_contiguous:
                view = array.view()
                view.flags.writeable = False
                views.append(view)
                if id(array) not in normalized.owned_ids:
                    pointer_arrays += 1
            else:
                contiguous = np.ascontiguousarray(array)
                contiguous.flags.writeable = False
                views.append(contiguous)
                copied_arrays += 1
                copied_bytes += int(contiguous.nbytes)
        return PackResult(
            payload=views,
            arrays=pointer_arrays,
            arrays_copied=normalized.copied + copied_arrays,
            copied_bytes=normalized.copied_bytes + copied_bytes,
            pickled=0,
            pickled_bytes=0,
        )

    # Mark freshly materialized non-NumPy arrays read-only so pickle emits them
    # out-of-band alongside locked game arrays, while mutable bytearrays copy in-band.
    pointer_ids: set[int] = set()
    pointer_arrays = 0
    copied_arrays = 0
    copied_bytes = 0
    for array in arrays(values):
        owned = id(array) in normalized.owned_ids
        if owned:
            array.flags.writeable = False
        if _is_fast_array(array, np):
            pointer_ids.add(id(array))
            if not owned:
                pointer_arrays += 1
        elif not owned:
            copied_arrays += 1
            copied_bytes += int(array.nbytes)

    payload = _pickle_with_buffers(values, pointer_ids, require_readonly=True)
    return PackResult(
        payload=payload,
        arrays=pointer_arrays,
        arrays_copied=normalized.copied + copied_arrays,
        copied_bytes=normalized.copied_bytes + copied_bytes,
        pickled=1,
        pickled_bytes=len(payload.data),
    )


def pack_local_outputs(outputs: list[object]) -> list[object] | PickledPayload:
    """Encode normalized, owned model outputs for return to the game loop.

    Top-level immutable scalars and owned C-contiguous ndarrays pass directly by
    pointer. Everything else round-trips through pickle protocol 5 with
    out-of-band buffers only for owned C-contiguous ndarray leaves, copying
    containers, mutable bytearrays and opaque objects in-band.
    """
    np = _numpy()
    if all(type(item) in _IMMUTABLE_PLAIN or _is_fast_array(item, np) for item in outputs):
        return outputs
    owned_ids = {id(array) for array in arrays(outputs) if _is_fast_array(array, np)}
    return _pickle_with_buffers(outputs, owned_ids, require_readonly=False)


def unpack_local(payload: list[object] | PickledPayload) -> list[object]:
    """Reconstruct a local-bus payload on the receiving thread."""
    if isinstance(payload, PickledPayload):
        return cast(list[object], pickle.loads(payload.data, buffers=payload.buffers))
    return payload


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
    id: int
    checkpoint: CheckpointRef
    inputs: list[object]
    rows: int
    receive_seconds: float


def _failed_outcome(checkpoint: str, error: Exception) -> ForwardOutcome:
    return ForwardOutcome(
        checkpoint=checkpoint,
        outputs=[],
        error=f"agent {checkpoint}: {explain(error)}",
        retry=isinstance(error, RetryRequested),
    )


def _identity_outcome(request_id: int, outcome: ForwardOutcome) -> tuple[int, ForwardOutcome]:
    return (request_id, outcome)


def _slice_outcomes[Encoded](
    outcome: ForwardOutcome, batch: list[_Pending[Encoded]]
) -> list[tuple[int, ForwardOutcome]]:
    sliced: list[tuple[int, ForwardOutcome]] = []
    offset = 0
    for item in batch:
        size = len(item.inputs)
        sliced.append(
            (
                item.id,
                ForwardOutcome(
                    checkpoint=outcome.checkpoint,
                    outputs=outcome.outputs[offset : offset + size],
                    error=outcome.error,
                    retry=outcome.retry,
                    loading=outcome.loading,
                ),
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
        encode: Callable[[int, ForwardOutcome], Encoded] = cast(
            Callable[[int, ForwardOutcome], Encoded], _identity_outcome
        ),
        copy_outputs: bool = False,
    ) -> None:
        self.forwards = Forwards(load, memory)
        self.max_batch = max_batch
        self._encode = encode
        self._copy_outputs = copy_outputs
        self.pending: dict[str, list[_Pending[Encoded]]] = {}
        self.batches = 0
        self.rows = 0

    @property
    def loads(self) -> int:
        return self.forwards.loads

    def add(
        self,
        reply: Callable[[Encoded], None],
        request_id: int,
        checkpoint: CheckpointRef,
        inputs: list[object],
        rows: int,
        receive_seconds: float = 0.0,
    ) -> None:
        self.pending.setdefault(checkpoint.key, []).append(
            _Pending(reply, request_id, checkpoint, inputs, rows, receive_seconds)
        )

    def flush(self, queue_wait_seconds: float = 0.0) -> list[Batch]:
        """Forward one chunk per pending checkpoint, up to `max_batch` rows each."""
        observations: list[Batch] = []
        remaining_wait = queue_wait_seconds
        for key, items in list(self.pending.items()):
            batch: list[_Pending[Encoded]] = []
            rows = 0
            for item in items:
                if batch and rows + item.rows > self.max_batch:
                    break
                batch.append(item)
                rows += item.rows
            del items[: len(batch)]
            if not items:
                del self.pending[key]
            inputs = [value for pending in batch for value in pending.inputs]
            freeze(inputs)
            before_calls, before_loads = self.forwards.calls, self.forwards.loads
            before_forward, before_load = self.forwards.forward_seconds, self.forwards.load_seconds
            outcome = self.forwards.answer(
                batch[0].checkpoint,
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
            # Normalize, copy (on local) and encode every reply before delivery:
            # an unpicklable or non-CPU output fails the whole batch, while
            # successful payloads are encoded only once.
            started = time.monotonic()
            try:
                if outcome.error is None:
                    outputs = normalize_outputs(outcome.outputs, copy_arrays=self._copy_outputs)
                    outcome = ForwardOutcome(checkpoint=key, outputs=outputs)
                encoded = [
                    self._encode(request_id, item_outcome)
                    for request_id, item_outcome in _slice_outcomes(outcome, batch)
                ]
            except Exception as error:
                failed = _failed_outcome(key, error)
                encoded = [
                    self._encode(request_id, item_outcome)
                    for request_id, item_outcome in _slice_outcomes(failed, batch)
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
class _LocalCall:
    id: int
    checkpoint: str
    payload: list[object] | PickledPayload
    rows: int


# None is the stop sentinel; CheckpointRef binds a checkpoint; a tuple submits calls.
type _LocalMessage = CheckpointRef | tuple[_LocalCall, ...] | None


@dataclass(frozen=True)
class _PackedReply:
    id: int
    checkpoint: str
    payload: PickledPayload


type _LocalReply = tuple[int, ForwardOutcome] | _PackedReply


def _encode_local(request_id: int, outcome: ForwardOutcome) -> _LocalReply:
    if outcome.error is not None:
        return (request_id, outcome)
    packed = pack_local_outputs(outcome.outputs)
    if isinstance(packed, PickledPayload):
        return _PackedReply(request_id, outcome.checkpoint, packed)
    return (request_id, outcome)


def _write_batches(report: TextIO | None, flushed: Sequence[Batch]) -> None:
    if report is None or not flushed:
        return
    for batch in flushed:
        if report.tell() > 1:
            report.write(",")
        report.write(batch.model_dump_json())
    report.flush()


def _lazy_loader(
    load: Function | Callable[..., object], checkout: str | None
) -> Callable[[CheckpointRef], object]:
    """Resolve an authored `Function` loader inside `Forwards.answer` on first use.

    Keeping module resolution inside `Forwards.answer` ensures broken `load`
    modules fail permanently (`loading=True, retry=False`) with redacted errors
    on both local and socket transports, while true agent-process startup
    crashes still raise `RetryRequested`.
    """
    if not isinstance(load, Function):
        return load
    resolved: Callable[..., object] | None = None
    failed: Exception | None = None

    def load_checkpoint(checkpoint: CheckpointRef) -> object:
        nonlocal resolved, failed
        if failed is not None:
            raise failed
        if resolved is None:
            try:
                resolved = load.load(checkout)
            except Exception as error:
                failed = error
                raise
        return resolved(checkpoint)

    return load_checkpoint


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
    bound: dict[str, CheckpointRef] = {}
    report: TextIO | None = None
    wait_seconds = 0.0
    stopped_cleanly = False
    cause = ""

    def post(reply: _LocalReply) -> None:
        try:
            loop.call_soon_threadsafe(deliver, reply)
        except RuntimeError:
            pass  # Event loop closed during shutdown.

    def handle(message: CheckpointRef | tuple[_LocalCall, ...]) -> None:
        if not isinstance(message, tuple):
            bound.setdefault(message.key, message)
            return
        for item in message:
            checkpoint = bound.get(item.checkpoint)
            if checkpoint is None:
                post(
                    (
                        item.id,
                        ForwardOutcome(
                            checkpoint=item.checkpoint,
                            outputs=[],
                            error=f"checkpoint {item.checkpoint} is not bound",
                        ),
                    )
                )
                continue
            started = time.monotonic()
            try:
                inputs = unpack_local(item.payload)
            except Exception as error:
                post((item.id, _failed_outcome(item.checkpoint, error)))
                continue
            batches.add(
                post,
                item.id,
                checkpoint,
                inputs,
                item.rows,
                receive_seconds=time.monotonic() - started,
            )

    try:
        # Setup sits inside the guard: a report file that cannot open must fail
        # callers at once rather than leave them waiting for the deadline.
        batches: _Batches[_LocalReply] = _Batches(
            _lazy_loader(load, checkout), max_batch, memory, encode=_encode_local, copy_outputs=True
        )
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report = report_path.open("w")
            report.write("[")
        while True:
            if not batches.pending:
                wait_started = time.monotonic()
                message = inbox.get()
                wait_seconds += time.monotonic() - wait_started
                if message is None:
                    stopped_cleanly = True
                    return
                handle(message)
            while True:
                try:
                    message = inbox.get_nowait()
                except queue.Empty:
                    break
                if message is None:
                    stopped_cleanly = True
                    return
                handle(message)
            flushed = batches.flush(wait_seconds)
            if flushed:
                wait_seconds = 0.0
                _write_batches(report, flushed)
    except BaseException as error:
        stopped_cleanly = False
        cause = explain(error)
        print(f"etude: agents: {cause}", file=sys.stderr, flush=True)
    finally:
        if report is not None:
            try:
                report.write("]")
                report.close()
            except OSError:
                pass  # A full disk must not keep callers from failing fast.
        if not stopped_cleanly:
            try:
                loop.call_soon_threadsafe(
                    on_exit, RetryRequested(f"inference thread exited unexpectedly: {cause}")
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
        self._bound: set[str] = set()
        self._book = _FlightBook(_forward_error)
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
                self._book.fail_all,
            ),
            daemon=False,
        )
        self._thread.start()

    def bind(self, checkpoint: CheckpointRef) -> None:
        if checkpoint.key in self._bound:
            return
        self._bound.add(checkpoint.key)
        self._inbox.put(checkpoint)

    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]:
        if not calls:
            return []
        if self._book.closed_error is not None:
            return [self._book.rejected(self._loop, call.checkpoint) for call in calls]

        for call in calls:
            if call.checkpoint not in self._bound:
                raise KeyError(f"checkpoint {call.checkpoint} is not bound")

        started = time.monotonic()
        stage = self._book.stage(self._loop, started)
        queued: list[_LocalCall] = []
        futures: list[asyncio.Future[list[object]]] = []
        arrays_count = 0
        arrays_copied = 0
        copied_bytes = 0
        pickled = 0
        pickled_bytes = 0
        try:
            for call in calls:
                request_id, future = stage.add(call)
                try:
                    packed = pack_local_inputs(normalize_inputs(call.inputs))
                except (pickle.PicklingError, TypeError, AttributeError, ValueError) as error:
                    stage.rollback_last()
                    futures.append(
                        self._book.failed(
                            self._loop,
                            ForwardError(
                                call.checkpoint, f"agent {call.checkpoint}: {explain(error)}"
                            ),
                        )
                    )
                    continue
                queued.append(_LocalCall(request_id, call.checkpoint, packed.payload, call.rows))
                futures.append(future)
                arrays_count += packed.arrays
                arrays_copied += packed.arrays_copied
                copied_bytes += packed.copied_bytes
                pickled += packed.pickled
                pickled_bytes += packed.pickled_bytes
            if queued:
                stage.commit(
                    arrays=arrays_count,
                    arrays_copied=arrays_copied,
                    copied_bytes=copied_bytes,
                    pickled=pickled,
                    pickled_bytes=pickled_bytes,
                )
                self._inbox.put(tuple(queued))
        except BaseException:
            stage.rollback()
            raise
        finally:
            self._book.add_handoff(time.monotonic() - started)
        return futures

    def _receive(self, reply: _LocalReply) -> None:
        if isinstance(reply, _PackedReply):
            started = time.monotonic()
            try:
                outcome = ForwardOutcome(
                    checkpoint=reply.checkpoint, outputs=unpack_local(reply.payload)
                )
            except Exception as error:
                outcome = _failed_outcome(reply.checkpoint, error)
            finally:
                self._book.add_handoff(time.monotonic() - started)
            self._book.deliver(reply.id, outcome)
            return
        request_id, outcome = reply
        self._book.deliver(request_id, outcome)

    async def close(self) -> BusReport:
        if self._book.closed_error is None:
            self._book.closed_error = RetryRequested("forward bus is closed")
        self._inbox.put(None)
        remaining = seconds_left()
        timeout = None if remaining is None else max(0.0, remaining)
        await asyncio.to_thread(self._thread.join, timeout)
        await asyncio.sleep(0)  # Let replies posted just before thread exit settle on the loop.
        if not self._thread.is_alive():
            self._book.fail_all(self._book.closed_error)
        return self._book.report()


def _encode_socket(request_id: int, outcome: ForwardOutcome) -> memoryview:
    return ForkingPickler.dumps(
        Answer(
            id=request_id,
            checkpoint=outcome.checkpoint,
            outputs=outcome.outputs,
            error=outcome.error,
            retry=outcome.retry,
            loading=outcome.loading,
        )
    )


def _add_socket_batch(
    batches: _Batches[memoryview],
    reply: Callable[[memoryview], None],
    batch: BatchRequest,
    receive_seconds: float = 0.0,
) -> None:
    if not batch.requests:
        return
    share = receive_seconds / len(batch.requests)
    for request in batch.requests:
        batches.add(reply, request.id, request.checkpoint, request.inputs, request.rows, share)


def _send_peer(peer: socket.socket) -> Callable[[memoryview], None]:
    def send(payload: memoryview) -> None:
        try:
            send_frame(peer, payload)
        except (OSError, EOFError):
            pass  # The game may have been cancelled at its deadline.

    return send


def _receive_request(peer: socket.socket) -> BatchRequest:
    payload = recv_frame(peer)
    try:
        request = ForkingPickler.loads(payload)
        if not isinstance(request, BatchRequest):
            raise TypeError("expected BatchRequest")
    except Exception as error:
        raise ValueError(f"agent request failed to decode: {explain(error)}") from None
    return request


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
        _lazy_loader(load, checkout), max_batch, memory, encode=_encode_socket, copy_outputs=False
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
                                request = _receive_request(peer)
                            except (EOFError, OSError, ValueError) as error:
                                if isinstance(error, ValueError):
                                    print(f"etude: agents: {error}", file=sys.stderr, flush=True)
                                    _send_peer(peer)(
                                        _encode_socket(
                                            _PEER_ERROR_ID,
                                            ForwardOutcome("", [], error=str(error)),
                                        )
                                    )
                                selector.unregister(peer)
                                peer.close()
                                peers.remove(peer)
                                break
                            _add_socket_batch(
                                batches, _send_peer(peer), request, time.monotonic() - started
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
