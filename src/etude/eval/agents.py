"""Direct game-to-agent transport and one process owning loaded checkpoints.

Games send batched requests over a private Unix socket. The agent process merges
ready requests by checkpoint up to `max_batch` rows without a timer, using the
common forward cache, and retains batch reports. This module's game-side imports
are standard library only.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import pickle
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

from etude.errors import RetryRequested, explain
from etude.eval.bus import BusReport, Flight, ForwardCall, arrays, normalize_inputs
from etude.eval.resources import Placement, Resources, agent_bytes, seconds_left

__all__ = ("Forward", "ModelForward", "ForwardError")


if TYPE_CHECKING:
    from numpy import ndarray

    from etude.call import Function
    from etude.checkpoints import CheckpointRef
    from etude.jobs import AgentLoader


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


@dataclass(frozen=True)
class CheckpointRequest:
    id: int
    checkpoint: CheckpointRef
    inputs: list[object]
    rows: int


@dataclass(frozen=True)
class BatchRequest:
    requests: tuple[CheckpointRequest, ...]


@dataclass(frozen=True)
class Answer:
    id: int
    checkpoint: str
    outputs: list[object]
    error: str | None = None
    retry: bool = False
    loading: bool = False

    def result(self) -> list[object]:
        if self.error is not None:
            raise ForwardError(
                self.checkpoint,
                self.error,
                retry=self.retry,
                loading=self.loading,
            )
        return self.outputs


_HEADER = struct.Struct("!Q")


def send_frame(sock: socket.socket, payload: bytes | memoryview) -> None:
    """Write one length-prefixed payload to a blocking stream socket."""
    sock.sendall(_HEADER.pack(len(payload)))
    sock.sendall(payload)


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
        self._next_id = 0
        self._checkpoints: dict[str, CheckpointRef] = {}
        self._flight = Flight()
        self._in_flight: dict[int, tuple[asyncio.Future[list[object]], float, list[ndarray]]] = {}
        self._closed_error: BaseException | None = None
        self._calls = 0
        self._answers = 0
        self._arrays_copied = 0
        self._copied_bytes = 0
        self._pickled = 0
        self._pickled_bytes = 0
        self._wire_bytes = 0
        self._round_trip_seconds = 0.0
        self._handoff_seconds = 0.0

    @classmethod
    async def connect(cls, address: str) -> SocketForwardBus:
        loop = asyncio.get_running_loop()
        bus: SocketForwardBus | None = None

        def on_frame(payload: bytes) -> None:
            assert bus is not None
            bus._receive(payload)

        def on_eof() -> None:
            if bus is not None:
                bus._fail_all(RetryRequested("agent process exited before answering"))

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
        if self._closed_error is not None:
            return [self._failed(loop, self._closed_error) for _ in calls]

        started = time.monotonic()
        now = started
        staged: list[
            tuple[int, CheckpointRequest, asyncio.Future[list[object]], list[ndarray]]
        ] = []
        arrays_copied = 0
        copied_bytes = 0
        try:
            for call in calls:
                checkpoint = self._checkpoints.get(call.checkpoint)
                if checkpoint is None:
                    raise KeyError(f"checkpoint {call.checkpoint} is not bound")
                normalized = normalize_inputs(call.inputs)
                for array in arrays(normalized.values):
                    arrays_copied += 1
                    copied_bytes += int(array.nbytes)
                locked = self._flight.lock(call.inputs)
                self._next_id += 1
                request_id = self._next_id
                future: asyncio.Future[list[object]] = loop.create_future()
                staged.append(
                    (
                        request_id,
                        CheckpointRequest(request_id, checkpoint, normalized.values, call.rows),
                        future,
                        locked,
                    )
                )
            payload = ForkingPickler.dumps(BatchRequest(tuple(req for _, req, _, _ in staged)))
        except (pickle.PicklingError, TypeError, AttributeError) as error:
            for _, _, _, locked in staged:
                self._flight.unlock(locked)
            self._handoff_seconds += time.monotonic() - started
            if len(calls) == 1:
                return [self._failed(loop, error)]
            return [self.submit([call])[0] for call in calls]
        except BaseException:
            for _, _, _, locked in staged:
                self._flight.unlock(locked)
            self._handoff_seconds += time.monotonic() - started
            raise

        for request_id, _, future, locked in staged:
            self._in_flight[request_id] = (future, now, locked)
        try:
            self._transport.write(_HEADER.pack(len(payload)))
            self._transport.write(payload)
            self._calls += len(calls)
            self._pickled += 1
            self._pickled_bytes += len(payload)
            self._wire_bytes += len(payload)
            self._arrays_copied += arrays_copied
            self._copied_bytes += copied_bytes
        except OSError:
            self._fail_all(RetryRequested("agent process exited before answering"))
        finally:
            self._handoff_seconds += time.monotonic() - started
        return [future for _, _, future, _ in staged]

    def _failed(
        self, loop: asyncio.AbstractEventLoop, error: BaseException
    ) -> asyncio.Future[list[object]]:
        future: asyncio.Future[list[object]] = loop.create_future()
        future.set_exception(error)
        return future

    def _receive(self, payload: bytes) -> None:
        started = time.monotonic()
        answer = cast(Answer, ForkingPickler.loads(payload))
        now = time.monotonic()
        self._answers += 1
        self._wire_bytes += len(payload)
        self._handoff_seconds += now - started
        entry = self._in_flight.pop(answer.id, None)
        if entry is None:
            return
        future, sent, locked = entry
        self._flight.unlock(locked)
        self._round_trip_seconds += now - sent
        if future.done():
            return
        if answer.error is not None:
            future.set_exception(
                ForwardError(
                    answer.checkpoint,
                    answer.error,
                    retry=answer.retry,
                    loading=answer.loading,
                )
            )
        else:
            future.set_result(answer.outputs)

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
            arrays=0,
            arrays_copied=self._arrays_copied,
            pickled=self._pickled,
            pickled_bytes=self._pickled_bytes,
            copied_bytes=self._copied_bytes,
            wire_bytes=self._wire_bytes,
            handoff_seconds=self._handoff_seconds,
            round_trip_seconds=self._round_trip_seconds,
        )

    async def close(self) -> BusReport:
        with suppress(OSError):
            self._transport.close()
        self._fail_all(RetryRequested("forward bus is closed"))
        return self._report()


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
    tournaments: Sequence[str],
    placement: Placement,
    *,
    streaming: bool = False,
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
            agent_bytes(definition.memory_gb),
            placement.threads,
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
            raise RetryRequested("agent process failed to start before the deadline")
        try:
            parent.recv()
        except EOFError as error:
            raise RetryRequested("agent process exited during startup") from error
        games = placement.games(definition.max_batch)
        print(
            f"etude: agents: started for {', '.join(tournaments)}; "
            + ("" if streaming else f"{placement.processes} processes, {games} games each; ")
            + f"{placement.threads} inference threads",
            file=sys.stderr,
            flush=True,
        )
        yield agent
    finally:
        failed = sys.exc_info()[0] is not None
        if failed and process.is_alive():
            process.terminate()
        if process.is_alive():
            try:
                parent.send(None)
            except (OSError, EOFError):
                pass
        process.join(timeout=seconds_left())
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()
        exitcode = process.exitcode
        process.close()
        if not failed and exitcode != 0:
            raise RetryRequested(f"agent process exited with status {exitcode}")
