"""Direct game-to-agent transport and one process owning loaded checkpoints.

Games send batched requests over a private Unix socket. The agent process merges
ready requests by checkpoint up to `max_batch` rows without a timer, using the
common forward cache, and retains batch reports. This module's game-side imports
are standard library only.
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
from typing import TYPE_CHECKING, Protocol, cast

from etude.errors import EtudeError, RetryRequested, explain
from etude.eval.resources import ExecutionPlan, Resources, seconds_left

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

    def __reduce__(self) -> tuple[type[ForwardError], tuple[str, str], dict[str, bool]]:
        return (
            ForwardError,
            (self.checkpoint, str(self)),
            {"retry": self.retry, "loading": self.loading},
        )


type ForwardResult = list[object] | ForwardError


@dataclass(frozen=True)
class CheckpointRequest:
    id: int
    checkpoint: CheckpointRef
    inputs: list[object]
    rows: int


@dataclass(frozen=True)
class Answer:
    id: int
    checkpoint: str
    result: ForwardResult


_HEADER = struct.Struct("!Q")
_MESSAGE_HEADER = struct.Struct("!QQQ")


def _encode_message(id: int, checkpoint: str, value: object) -> tuple[bytes, memoryview]:
    key = checkpoint.encode()
    body = ForkingPickler.dumps(value)
    return _MESSAGE_HEADER.pack(id, len(key), len(body)) + key, body


def _messages(payload: bytes) -> Iterator[tuple[int, str, memoryview]]:
    """Read the identity before unpickling so a failed body can be routed."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        if len(view) - offset < _MESSAGE_HEADER.size:
            raise EOFError("truncated agent message header")
        request_id, key_size, body_size = _MESSAGE_HEADER.unpack_from(view, offset)
        key_start = offset + _MESSAGE_HEADER.size
        body_start = key_start + key_size
        offset = body_start + body_size
        if offset > len(view):
            raise EOFError("truncated agent message body")
        yield request_id, str(view[key_start:body_start], "utf-8"), view[body_start:offset]


def _wire_error(checkpoint: str, operation: str, error: Exception) -> ForwardError:
    return ForwardError(checkpoint, f"agent {checkpoint}: could not {operation}: {explain(error)}")


def encode_request(request: CheckpointRequest) -> tuple[bytes, memoryview]:
    return _encode_message(request.id, request.checkpoint.key, request)


def decode_requests(payload: bytes) -> Iterator[CheckpointRequest | Answer]:
    for request_id, key, body in _messages(payload):
        try:
            request = cast(CheckpointRequest, ForkingPickler.loads(body))
        except Exception as error:
            yield Answer(request_id, key, _wire_error(key, "decode inputs", error))
        else:
            yield request


def encode_answer(answer: Answer) -> tuple[bytes, memoryview]:
    try:
        return _encode_message(answer.id, answer.checkpoint, answer.result)
    except Exception as error:
        failure = _wire_error(answer.checkpoint, "encode outputs", error)
        return _encode_message(answer.id, answer.checkpoint, failure)


def decode_answer(payload: bytes) -> Answer:
    ((request_id, key, body),) = _messages(payload)
    try:
        result = cast(ForwardResult, ForkingPickler.loads(body))
    except Exception as error:
        result = _wire_error(key, "decode outputs", error)
    return Answer(request_id, key, result)


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


class AgentClient:
    """Async Unix-socket channel from one game process to the shared agent process."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._next_id = 0
        self._in_flight: dict[int, tuple[asyncio.Future[ForwardResult], float]] = {}
        self._closed_error: BaseException | None = None
        self.requests = 0
        self.answers = 0
        self.request_bytes = 0
        self.answer_bytes = 0
        self.round_trip_seconds = 0.0
        self.transport_seconds = 0.0
        self._reader_task = asyncio.create_task(self._read_answers(reader))

    @classmethod
    async def connect(cls, address: str) -> AgentClient:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(address), timeout=seconds_left()
            )
        except TimeoutError as error:
            raise RetryRequested("agent connection exceeded the evaluation deadline") from error
        except OSError as error:
            raise RetryRequested("agent process is unavailable") from error
        return cls(reader, writer)

    def send_batch(
        self, calls: Sequence[tuple[CheckpointRef, list[object], int]]
    ) -> list[asyncio.Future[ForwardResult]]:
        if self._closed_error is not None:
            raise self._closed_error
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        started = time.monotonic()
        futures: list[asyncio.Future[ForwardResult]] = []
        sent: dict[int, tuple[asyncio.Future[ForwardResult], float]] = {}
        parts: list[bytes | memoryview] = []
        try:
            for checkpoint, inputs, rows in calls:
                self._next_id += 1
                request = CheckpointRequest(self._next_id, checkpoint, inputs, rows)
                future: asyncio.Future[ForwardResult] = loop.create_future()
                futures.append(future)
                try:
                    parts.extend(encode_request(request))
                except Exception as error:
                    future.set_result(_wire_error(checkpoint.key, "encode inputs", error))
                else:
                    sent[request.id] = (future, now)
            if parts:
                size = sum(map(len, parts))
                self._writer.writelines([_HEADER.pack(size), *parts])
                self._in_flight.update(sent)
                self.requests += 1
                self.request_bytes += size
        except OSError as error:
            failure = RetryRequested("agent process exited before answering")
            self._fail_all(failure)
            raise failure from error
        finally:
            self.transport_seconds += time.monotonic() - started
        return futures

    async def _read_answers(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                (size,) = _HEADER.unpack(await reader.readexactly(_HEADER.size))
                payload = await reader.readexactly(size)
                started = time.monotonic()
                answer = decode_answer(payload)
                now = time.monotonic()
                self.answers += 1
                self.answer_bytes += len(payload)
                self.transport_seconds += now - started
                future, sent = self._in_flight.pop(answer.id)
                self.round_trip_seconds += now - sent
                if not future.done():
                    future.set_result(answer.result)
        except (EOFError, OSError):
            self._fail_all(RetryRequested("agent process exited before answering"))
        except Exception as error:
            self._fail_all(RetryRequested(f"invalid agent response: {explain(error)}"))

    def _fail_all(self, error: BaseException) -> None:
        if self._closed_error is None:
            self._closed_error = error
        for future, _ in self._in_flight.values():
            if not future.done():
                future.set_exception(error)
        self._in_flight.clear()

    async def close(self) -> None:
        self._reader_task.cancel()
        self._writer.close()
        with suppress(asyncio.CancelledError):
            await self._reader_task


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
            plan.checkpoint_bytes,
            plan.threads,
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
        if failed and process.is_alive():
            process.terminate()
        elif process.is_alive():
            try:
                parent.send(None)
            except (OSError, EOFError):
                pass
        process.join(timeout=seconds_left())
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        exitcode = process.exitcode
        process.close()
        if not failed and exitcode != 0:
            raise RetryRequested(f"agent process exited with status {exitcode}")
