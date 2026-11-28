"""Shared game-to-agent transport and one process owning loaded checkpoints.

Games send batched requests over a private Unix socket. The agent process merges
ready requests by checkpoint up to `max_batch` rows without a timer, using the
common forward cache, and retains batch reports. Game-side imports stay free of
NumPy (until an array is first seen), model libraries and batching code.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import socket
import struct
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.reduction import ForkingPickler
from typing import TYPE_CHECKING, Protocol

from etude.errors import EtudeError, RetryRequested, explain
from etude.eval.bus import (
    BusReport,
    Flight,
    ForwardCall,
    ForwardOutcome,
    _FlightBook,
    arrays,
    normalize_inputs,
)
from etude.eval.resources import Placement, Resources, seconds_left

__all__ = ("Forward", "ModelForward", "ForwardError")


if TYPE_CHECKING:
    from etude.call import Function
    from etude.checkpoints import CheckpointRef
    from etude.eval.loader import AgentLoader


type ModelForward = Callable[[list[object]], list[object]]


class Forward(Protocol):
    """Awaitable game-side handle for one checkpoint's batched forward."""

    def __call__(
        self, inputs: list[object], *, rows: int | None = None
    ) -> Awaitable[list[object]]: ...


def _unpickle_forward_error(
    checkpoint: str, message: str, retry: bool, loading: bool
) -> ForwardError:
    return ForwardError(checkpoint, message, retry=retry, loading=loading)


class ForwardError(RuntimeError):
    """A checkpoint load or forward call failed."""

    def __init__(
        self,
        checkpoint: str,
        message: str,
        *,
        retry: bool = False,
        loading: bool = False,
    ) -> None:
        super().__init__(message)
        self.checkpoint = checkpoint
        self.retry = retry
        self.loading = loading

    def __reduce__(
        self,
    ) -> tuple[Callable[[str, str, bool, bool], ForwardError], tuple[str, str, bool, bool]]:
        return _unpickle_forward_error, (self.checkpoint, str(self), self.retry, self.loading)


def _forward_error(outcome: ForwardOutcome) -> ForwardError:
    assert outcome.error is not None
    return ForwardError(
        outcome.checkpoint,
        outcome.error,
        retry=outcome.retry,
        loading=outcome.loading,
    )


@dataclass(frozen=True)
class CheckpointRequest:
    id: int
    checkpoint: CheckpointRef
    inputs: list[object]
    rows: int


@dataclass(frozen=True)
class Answer:
    id: int
    outcome: ForwardOutcome


_HEADER = struct.Struct("!Q")
_MESSAGE_HEADER = struct.Struct("!QQQ")
_PEER_ERROR_ID = 0  # Flight ids start at one. This answer closes the peer.


type _Message = tuple[bytes, memoryview]


def _encode_message(request_id: int, checkpoint: str, value: object) -> _Message:
    key = checkpoint.encode()
    body = ForkingPickler.dumps(value)
    return _MESSAGE_HEADER.pack(request_id, len(key), len(body)) + key, body


def _messages(payload: bytes) -> Iterator[tuple[int, str, memoryview]]:
    """Read routing metadata before unpickling so a body failure has an owner."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        key_start = offset + _MESSAGE_HEADER.size
        if key_start > len(view):
            raise EOFError("truncated agent message header")
        request_id, key_size, body_size = _MESSAGE_HEADER.unpack_from(view, offset)
        body_start = key_start + key_size
        offset = body_start + body_size
        if offset > len(view):
            raise EOFError("truncated agent message body")
        yield request_id, str(view[key_start:body_start], "utf-8"), view[body_start:offset]


def encode_request(request: CheckpointRequest) -> _Message:
    return _encode_message(request.id, request.checkpoint.key, request)


def decode_requests(payload: bytes) -> Iterator[CheckpointRequest | Answer]:
    for request_id, key, body in _messages(payload):
        try:
            request = ForkingPickler.loads(body)
            if not isinstance(request, CheckpointRequest):
                raise TypeError("expected CheckpointRequest")
            if request.id != request_id or request.checkpoint.key != key:
                raise ValueError("agent request identity does not match its frame")
        except Exception as error:
            yield Answer(
                request_id,
                ForwardOutcome(key, [], error=f"agent request failed to decode: {explain(error)}"),
            )
        else:
            yield request


def encode_answer(request_id: int, outcome: ForwardOutcome) -> _Message:
    return _encode_message(request_id, outcome.checkpoint, outcome)


def decode_answer(payload: bytes) -> Answer:
    ((request_id, key, body),) = _messages(payload)
    try:
        outcome = ForkingPickler.loads(body)
        if not isinstance(outcome, ForwardOutcome):
            raise TypeError("expected ForwardOutcome")
        if outcome.checkpoint != key:
            raise ValueError("agent reply identity does not match its frame")
    except Exception as error:
        outcome = ForwardOutcome(key, [], error=f"agent reply failed to decode: {explain(error)}")
    return Answer(request_id, outcome)


def send_frame(sock: socket.socket, header: bytes, body: memoryview) -> None:
    """Write one length-prefixed message without copying its payload."""
    sock.sendall(_HEADER.pack(len(header) + len(body)) + header)
    sock.sendall(body)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray(size)
    view = memoryview(buf)
    offset = 0
    while offset < size:
        read = sock.recv_into(view[offset:])
        if read == 0:
            raise EOFError("peer closed connection")
        offset += read
    return bytes(buf)


def recv_frame(sock: socket.socket) -> bytes:
    """Read one length-prefixed payload from a blocking stream socket."""
    (size,) = _HEADER.unpack(_recv_exact(sock, _HEADER.size))
    return _recv_exact(sock, size)


class _FrameProtocol(asyncio.Protocol):
    def __init__(self, on_frame: Callable[[bytes], None], on_eof: Callable[[], None]) -> None:
        self._on_frame = on_frame
        self._on_eof = on_eof
        self._buffer = bytearray()
        self._expected: int | None = None

    def data_received(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            if self._expected is None:
                if len(self._buffer) < _HEADER.size:
                    return
                (self._expected,) = _HEADER.unpack(self._buffer[: _HEADER.size])
                del self._buffer[: _HEADER.size]
            if len(self._buffer) < self._expected:
                return
            payload = bytes(self._buffer[: self._expected])
            del self._buffer[: self._expected]
            self._expected = None
            self._on_frame(payload)

    def connection_lost(self, exc: Exception | None) -> None:
        self._on_eof()


class SocketForwardBus:
    """Async Unix-socket `ForwardBus` from one game process to the shared agent process."""

    def __init__(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        self._checkpoints: dict[str, CheckpointRef] = {}
        self._book = _FlightBook(_forward_error)

    @property
    def flight(self) -> Flight:
        return self._book.flight

    @classmethod
    async def connect(cls, address: str) -> SocketForwardBus:
        loop = asyncio.get_running_loop()
        bus: SocketForwardBus | None = None

        def on_frame(payload: bytes) -> None:
            assert bus is not None
            bus._receive(payload)

        def on_eof() -> None:
            if bus is not None:
                bus._book.fail_all(RetryRequested("agent process exited before answering"))

        try:
            transport, _ = await asyncio.wait_for(
                loop.create_unix_connection(lambda: _FrameProtocol(on_frame, on_eof), address),
                timeout=seconds_left(),
            )
        except TimeoutError as error:
            raise RetryRequested("agent connection exceeded the evaluation deadline") from error
        except OSError as error:
            raise RetryRequested("agent process is unavailable") from error
        bus = cls(transport)
        return bus

    def bind(self, checkpoint: CheckpointRef) -> None:
        self._checkpoints.setdefault(checkpoint.key, checkpoint)

    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]:
        loop = asyncio.get_running_loop()
        if not calls:
            return []
        if self._book.closed_error is not None:
            return [self._book.rejected(loop, call.checkpoint) for call in calls]

        for call in calls:
            if call.checkpoint not in self._checkpoints:
                raise KeyError(f"checkpoint {call.checkpoint} is not bound")

        started = time.monotonic()
        stage = self._book.stage(loop, started)
        parts: list[bytes | memoryview] = []
        futures: list[asyncio.Future[list[object]]] = []
        arrays_copied = 0
        copied_bytes = 0
        try:
            for call in calls:
                checkpoint = self._checkpoints[call.checkpoint]
                request_id, future = stage.add(call)
                try:
                    normalized = normalize_inputs(call.inputs)
                    parts.extend(
                        encode_request(
                            CheckpointRequest(request_id, checkpoint, normalized.values, call.rows)
                        )
                    )
                except Exception as error:
                    stage.rollback_last()
                    futures.append(
                        self._book.failed(
                            loop,
                            ForwardError(
                                call.checkpoint,
                                f"agent {call.checkpoint}: could not encode inputs: {explain(error)}",
                            ),
                        )
                    )
                    continue
                for array in arrays(normalized.values):
                    arrays_copied += 1
                    copied_bytes += int(array.nbytes)
                futures.append(future)
            if parts:
                size = sum(map(len, parts))
                stage.commit(
                    arrays_copied=arrays_copied,
                    copied_bytes=copied_bytes,
                    pickled=len(parts) // 2,
                    pickled_bytes=sum(map(len, parts[1::2])),
                    wire_bytes=size,
                )
                try:
                    self._transport.writelines([_HEADER.pack(size), *parts])
                except OSError:
                    self._book.fail_all(RetryRequested("agent process exited before answering"))
        except BaseException:
            stage.rollback()
            raise
        finally:
            self._book.add_handoff(time.monotonic() - started)
        return futures

    def _receive(self, payload: bytes) -> None:
        if self._book.closed_error is not None:
            return
        started = time.monotonic()
        try:
            answer = decode_answer(payload)
        except EOFError:
            self._book.fail_all(RetryRequested("agent process exited before answering"))
            self._transport.close()
            return
        except Exception as error:
            self._fail_peer(f"agent reply failed to decode: {explain(error)}")
            return
        finally:
            self._book.add_handoff(time.monotonic() - started, wire_bytes=len(payload))
        if answer.id == _PEER_ERROR_ID:
            self._fail_peer(answer.outcome.error or "agent peer failed")
            return
        self._book.deliver(answer.id, answer.outcome)

    def _fail_peer(self, message: str) -> None:
        self._book.fail_all(lambda checkpoint: ForwardError(checkpoint, message))
        with suppress(OSError):
            self._transport.close()

    async def close(self) -> BusReport:
        with suppress(OSError):
            self._transport.close()
        self._book.fail_all(RetryRequested("forward bus is closed"))
        return self._book.report()


@dataclass(frozen=True)
class AgentProcess:
    address: str
    process: multiprocessing.context.SpawnProcess

    def check(self) -> None:
        if self.process.exitcode is not None:
            raise RetryRequested("agent process exited unexpectedly")


def _run(
    address: str,
    load: Function,
    checkout: str | None,
    max_batch: int,
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
    placement: Placement,
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
            placement.checkpoint_bytes,
            placement.inference_threads,
            child,
            resources,
        ),
    )
    process.start()
    child.close()
    agent = AgentProcess(address=address, process=process)
    try:
        ready = wait([parent, process.sentinel], timeout=seconds_left())
        if parent not in ready:
            if ready:
                raise RetryRequested("agent process exited during startup")
            raise RetryRequested("agent process failed to start before the deadline")
        try:
            startup_error: RuntimeError | RetryRequested | None = parent.recv()
        except EOFError as error:
            raise RetryRequested("agent process exited during startup") from error
        if isinstance(startup_error, RetryRequested):
            raise startup_error
        if startup_error is not None:
            raise EtudeError(
                "eval_startup_failed", 500, detail=str(startup_error)
            ) from startup_error
        yield agent
    finally:
        failed = sys.exc_info()[0] is not None
        if process.is_alive():
            with suppress(OSError, EOFError):
                parent.send(None)
        # Let the agent close its batch report even when play failed.
        remaining = seconds_left()
        process.join(timeout=2.0 if remaining is None else min(remaining, 2.0))
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        exitcode = process.exitcode
        process.close()
        if not failed and exitcode != 0:
            raise RetryRequested(f"agent process exited with status {exitcode}")
