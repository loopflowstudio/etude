"""The one transport boundary between game code and loaded checkpoints.

`ForwardBus` takes `ForwardCall`s from a game process and resolves one future
per call with its outputs, or with `ForwardError`. Two transports implement it:
`LocalForwardBus` (`etude.eval.batching`, an inference thread in the game
process) and `SocketForwardBus` (`etude.eval.agents`, the shared agent process).

The codec and flight state here run on both. `normalize_inputs` and
`normalize_outputs` turn array leaves at any depth of `tuple`/`list`/`dict`
into owned CPU `numpy.ndarray`s and reject non-CPU device tensors with
`ValueError`, so games and models only ever exchange NumPy. `Flight` locks the
game's arrays read-only from call until the answer; `_FlightBook` owns the
shared in-flight table, atomic stage-commit-rollback, and `BusReport`
accounting across transports. `freeze` gives the model read-only inputs.
NumPy is found through `sys.modules` and imported only when a non-NumPy array
is first seen, so a game process that never sees an array never loads it.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Literal, Protocol

from etude.records import Record

if TYPE_CHECKING:
    from numpy import ndarray

    from etude.checkpoints import CheckpointRef

_PLAIN = (type(None), bool, int, float, complex, str, bytes, bytearray)


@dataclass(frozen=True)
class ForwardCall:
    """One merged forward request: a checkpoint key and its inputs."""

    checkpoint: str  # key; the CheckpointRef crosses once, in bind()
    inputs: list[object]
    rows: int


@dataclass(frozen=True)
class ForwardOutcome:
    """Transport-neutral result of a checkpoint forward (no wire id)."""

    checkpoint: str
    outputs: list[object]
    error: str | None = None
    retry: bool = False
    loading: bool = False


class BusReport(Record):
    """What one game process's bus moved and how.

    `arrays` went by pointer; `arrays_copied` and `copied_bytes` count game-side
    input array payloads copied (into a pickle, a contiguous copy or out of a
    non-NumPy array). `pickled` and `pickled_bytes` count game-side pickled
    calls and their in-band bytes; `wire_bytes` crossed a socket in either
    direction.
    """

    calls: int = 0
    answers: int = 0
    arrays: int = 0
    arrays_copied: int = 0
    pickled: int = 0
    pickled_bytes: int = 0
    copied_bytes: int = 0
    wire_bytes: int = 0
    handoff_seconds: float = 0.0
    round_trip_seconds: float = 0.0


class ForwardBus(Protocol):
    def bind(self, checkpoint: CheckpointRef) -> None:
        """Make `checkpoint` callable by key; binding twice is a no-op."""

    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]:
        """Send `calls` as one unit, holding inputs read-only even if an await is cancelled."""

    async def close(self) -> BusReport: ...


@dataclass
class _NormalizeStats:
    copied: int = 0
    copied_bytes: int = 0
    owned_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class Normalized:
    """Game inputs with non-NumPy array leaves materialized as owned ndarrays."""

    values: list[object]
    copied: int
    copied_bytes: int
    owned_ids: frozenset[int]


def _numpy() -> ModuleType | None:
    return sys.modules.get("numpy")


def _require_numpy() -> ModuleType:
    found = _numpy()
    if found is None:
        # NumPy is a dependency, imported only once a non-NumPy array is seen so
        # code-only and plain-value game processes stay light.
        found = importlib.import_module("numpy")
    return found


def _non_cpu_error(
    value: object, direction: Literal["input", "output"], cause: Exception | None = None
) -> ValueError:
    where = (
        "move model outputs to CPU/NumPy in load() before returning"
        if direction == "output"
        else "move game inputs to CPU/NumPy before calling forward()"
    )
    detail = f": {cause}" if cause is not None else ""
    return ValueError(
        f"cannot convert {type(value).__qualname__} to a CPU numpy array; {where}{detail}"
    )


def _as_array(
    value: object,
    *,
    stats: _NormalizeStats | None,
    direction: Literal["input", "output"],
) -> object:
    """A buffer, else CPU DLPack, exporter as an owned ndarray; reject non-CPU tensors."""
    try:
        view = memoryview(value)  # type: ignore[arg-type]  # Probing the buffer protocol.
    except (TypeError, BufferError):
        if isinstance(value, type):
            return value
        has_dlpack = callable(getattr(value, "__dlpack__", None))
        has_cuda = getattr(value, "__cuda_array_interface__", None) is not None
        if not has_dlpack and not has_cuda:
            return value
        if not has_dlpack:
            raise _non_cpu_error(value, direction) from None
        np = _require_numpy()
        try:
            array = np.from_dlpack(value)
        except (BufferError, RuntimeError, TypeError, ValueError) as error:
            raise _non_cpu_error(value, direction, error) from error
        owned = np.array(array, copy=True, order="C")
    else:
        np = _require_numpy()
        try:
            owned = np.array(view, copy=True, order="C")
        finally:
            view.release()
    if stats is not None:
        stats.copied += 1
        stats.copied_bytes += int(owned.nbytes)
        stats.owned_ids.add(id(owned))
    return owned


def _normalize(
    value: object,
    *,
    copy_arrays: bool,
    stats: _NormalizeStats | None,
    direction: Literal["input", "output"],
) -> object:
    kind = type(value)
    if kind in _PLAIN:
        return value
    np = _numpy()
    if np is not None and kind is np.ndarray:
        return np.array(value, copy=True, order="C") if copy_arrays else value
    if kind is list:
        return [
            _normalize(item, copy_arrays=copy_arrays, stats=stats, direction=direction)
            for item in value  # type: ignore[attr-defined]
        ]
    if kind is tuple:
        return tuple(
            _normalize(item, copy_arrays=copy_arrays, stats=stats, direction=direction)
            for item in value  # type: ignore[attr-defined]
        )
    if kind is dict:
        return {
            k: _normalize(v, copy_arrays=copy_arrays, stats=stats, direction=direction)
            for k, v in value.items()  # type: ignore[attr-defined]
        }
    if np is not None and isinstance(value, np.ndarray):
        return np.array(value, copy=True, order="C") if copy_arrays else value
    return _as_array(value, stats=stats, direction=direction)


def normalize_inputs(values: list[object]) -> Normalized:
    """Convert non-NumPy array leaves in game inputs into bus-owned C-contiguous ndarrays."""
    stats = _NormalizeStats()
    normalized = [
        _normalize(item, copy_arrays=False, stats=stats, direction="input") for item in values
    ]
    return Normalized(
        values=normalized,
        copied=stats.copied,
        copied_bytes=stats.copied_bytes,
        owned_ids=frozenset(stats.owned_ids),
    )


def normalize_outputs(values: list[object], *, copy_arrays: bool = False) -> list[object]:
    """Materialize model outputs into owned CPU ndarrays on the producing thread.

    Non-NumPy buffer/DLPack leaves always copy and release their backing view on
    this thread. When `copy_arrays` is true, existing `ndarray` leaves are also
    copied into owned C-contiguous writable arrays so outputs can never alias
    in-flight inputs or reused model staging buffers.
    """
    return [
        _normalize(item, copy_arrays=copy_arrays, stats=None, direction="output") for item in values
    ]


def arrays(value: object) -> Iterator[ndarray]:
    """Every ndarray leaf of a normalized value."""
    np = _numpy()
    if np is None:
        return
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, np.ndarray):
            yield item
        elif type(item) in (list, tuple):
            pending.extend(item)  # type: ignore[arg-type]
        elif type(item) is dict:
            pending.extend(item.values())


def freeze(inputs: list[object]) -> None:
    """Make every array the model will read read-only, on either transport."""
    for array in arrays(inputs):
        array.flags.writeable = False


@dataclass
class Flight:
    """Game-side arrays locked read-only while a forward holds them.

    Only arrays writeable on entry are locked, and each is counted so one
    array submitted twice unlocks after its last answer. Lock state is touched
    only on the game process's event loop.
    """

    _held: dict[int, tuple[ndarray, int]] = field(default_factory=dict)

    def lock(self, values: list[object]) -> list[ndarray]:
        locked: list[ndarray] = []
        for array in arrays(values):
            key = id(array)
            entry = self._held.get(key)
            if entry is not None:
                self._held[key] = (array, entry[1] + 1)
            elif array.flags.writeable:
                array.flags.writeable = False
                self._held[key] = (array, 1)
            else:
                continue
            locked.append(array)
        return locked

    def unlock(self, locked: list[ndarray]) -> None:
        for array in locked:
            key = id(array)
            held, count = self._held[key]
            if count > 1:
                self._held[key] = (held, count - 1)
            else:
                del self._held[key]
                held.flags.writeable = True


@dataclass(frozen=True)
class _Flight:
    id: int
    checkpoint: str
    future: asyncio.Future[list[object]]
    locked: list[ndarray]
    sent_at: float


type _FlightError = BaseException | Callable[[str], BaseException]


class _FlightStage:
    """Transactional staging handle for one `ForwardBus.submit` call."""

    def __init__(self, book: _FlightBook, loop: asyncio.AbstractEventLoop, sent_at: float) -> None:
        self._book = book
        self._loop = loop
        self._sent_at = sent_at
        self._entries: list[_Flight] = []

    def add(self, call: ForwardCall) -> tuple[int, asyncio.Future[list[object]]]:
        """Lock `inputs`, reserve a request id and future, and record for commit/rollback."""
        locked = self._book._flight.lock(call.inputs)
        request_id = self._book._reserve_id()
        future: asyncio.Future[list[object]] = self._loop.create_future()
        self._entries.append(_Flight(request_id, call.checkpoint, future, locked, self._sent_at))
        return request_id, future

    def rollback_last(self) -> None:
        """Undo the most recently staged call (e.g. when its input packing failed)."""
        entry = self._entries.pop()
        self._book._flight.unlock(entry.locked)

    def rollback(self) -> None:
        """Unlock every uncommitted staged call."""
        while self._entries:
            entry = self._entries.pop()
            self._book._flight.unlock(entry.locked)

    def commit(
        self,
        *,
        arrays: int = 0,
        arrays_copied: int = 0,
        copied_bytes: int = 0,
        pickled: int = 0,
        pickled_bytes: int = 0,
        wire_bytes: int = 0,
    ) -> None:
        """Register all staged calls in flight and record their transfer counts."""
        for entry in self._entries:
            self._book._in_flight[entry.id] = entry
        self._book._calls += len(self._entries)
        self._book._arrays += arrays
        self._book._arrays_copied += arrays_copied
        self._book._copied_bytes += copied_bytes
        self._book._pickled += pickled
        self._book._pickled_bytes += pickled_bytes
        self._book._wire_bytes += wire_bytes
        self._entries.clear()


class _FlightBook:
    """Shared in-flight request table, lock owner and `BusReport` accumulator.

    Every `ForwardBus` transport composes one `_FlightBook` so request ids,
    transactional lock staging, `handoff_seconds`, per-call `pickled` counts and
    `fail_all` shutdown semantics stay identical across transports.
    """

    def __init__(self, make_error: Callable[[ForwardOutcome], BaseException]) -> None:
        self._make_error = make_error
        self._flight = Flight()
        self._next_id = 0
        self._in_flight: dict[int, _Flight] = {}
        self.closed_error: _FlightError | None = None
        self._calls = 0
        self._answers = 0
        self._arrays = 0
        self._arrays_copied = 0
        self._copied_bytes = 0
        self._pickled = 0
        self._pickled_bytes = 0
        self._wire_bytes = 0
        self._handoff_seconds = 0.0
        self._round_trip_seconds = 0.0

    def _reserve_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def stage(self, loop: asyncio.AbstractEventLoop, sent_at: float) -> _FlightStage:
        return _FlightStage(self, loop, sent_at)

    def failed(
        self, loop: asyncio.AbstractEventLoop, error: BaseException
    ) -> asyncio.Future[list[object]]:
        future: asyncio.Future[list[object]] = loop.create_future()
        future.set_exception(error)
        return future

    def rejected(
        self, loop: asyncio.AbstractEventLoop, checkpoint: str
    ) -> asyncio.Future[list[object]]:
        error = self.closed_error
        assert error is not None
        return self.failed(loop, error if isinstance(error, BaseException) else error(checkpoint))

    def add_handoff(self, seconds: float, *, wire_bytes: int = 0) -> None:
        self._handoff_seconds += seconds
        self._wire_bytes += wire_bytes

    def deliver(self, request_id: int, outcome: ForwardOutcome) -> None:
        entry = self._in_flight.pop(request_id, None)
        if entry is None:
            return
        self._flight.unlock(entry.locked)
        self._answers += 1
        self._round_trip_seconds += time.monotonic() - entry.sent_at
        future = entry.future
        if future.done():
            return
        if outcome.error is not None:
            future.set_exception(self._make_error(outcome))
        else:
            future.set_result(outcome.outputs)

    def fail_all(self, error: _FlightError) -> None:
        if self.closed_error is None:
            self.closed_error = error
        for entry in self._in_flight.values():
            self._flight.unlock(entry.locked)
            if not entry.future.done():
                entry.future.set_exception(
                    error if isinstance(error, BaseException) else error(entry.checkpoint)
                )
        self._in_flight.clear()

    def report(self) -> BusReport:
        return BusReport(
            calls=self._calls,
            answers=self._answers,
            arrays=self._arrays,
            arrays_copied=self._arrays_copied,
            pickled=self._pickled,
            pickled_bytes=self._pickled_bytes,
            copied_bytes=self._copied_bytes,
            wire_bytes=self._wire_bytes,
            handoff_seconds=self._handoff_seconds,
            round_trip_seconds=self._round_trip_seconds,
        )
