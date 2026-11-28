"""The one transport boundary between game code and loaded checkpoints.

`ForwardBus` takes `ForwardCall`s from a game process and resolves one future
per call with its outputs, or with `ForwardError`. Two transports implement it:
`LocalForwardBus` (`etude.eval.batching`, an inference thread in the game
process) and `SocketForwardBus` (`etude.eval.agents`, the shared agent process).

Both transports normalize buffer and CPU DLPack leaves on the producing
thread. Leaves are found inside lists, tuples, dicts and dataclass instances.
NumPy inputs can travel by pointer. `_FlightBook` owns their locks from
barrier staging through the reply, along with request IDs and transfer counts.
`freeze` protects the model's inputs.
Games exchanging plain values can run without importing NumPy.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
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

    checkpoint: str  # Key registered by bind().
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
    @property
    def flight(self) -> Flight:
        """The bus's lock ledger, also used by the game before submitting a batch."""

    def bind(self, checkpoint: CheckpointRef) -> None:
        """Make `checkpoint` callable by key; binding twice is a no-op."""

    def submit(self, calls: Sequence[ForwardCall]) -> list[asyncio.Future[list[object]]]:
        """Send `calls` as one unit, holding inputs read-only even if an await is cancelled."""

    async def close(self) -> BusReport: ...


@dataclass(frozen=True)
class Normalized:
    """Game inputs with non-NumPy array leaves materialized as owned ndarrays."""

    values: list[object]
    copied_bytes: int
    owned_ids: frozenset[int]


def _numpy() -> ModuleType | None:
    return sys.modules.get("numpy")


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
    direction: Literal["input", "output"],
) -> ndarray | None:
    """A buffer, else CPU DLPack, exporter as an owned ndarray; reject non-CPU tensors."""
    try:
        view = memoryview(value)  # type: ignore[arg-type]  # Probing the buffer protocol.
    except (TypeError, BufferError):
        if isinstance(value, type):
            return None
        has_dlpack = callable(getattr(value, "__dlpack__", None))
        has_cuda = getattr(value, "__cuda_array_interface__", None) is not None
        if not has_dlpack and not has_cuda:
            return None
        if not has_dlpack:
            raise _non_cpu_error(value, direction) from None
        np = importlib.import_module("numpy")
        try:
            array = np.from_dlpack(value)
        except (BufferError, RuntimeError, TypeError, ValueError) as error:
            raise _non_cpu_error(value, direction, error) from error
        owned: ndarray = np.array(array, copy=True, order="C")
    else:
        np = importlib.import_module("numpy")
        try:
            owned = np.array(view, copy=True, order="C")
        finally:
            view.release()
    return owned


def _normalize(
    values: list[object], *, copy_arrays: bool = False, copied: list[ndarray] | None = None
) -> list[object]:
    """Convert array leaves, retaining containers that have no converted descendants."""
    active: set[int] = set()
    leaves: dict[int, object] = {}
    cyclic = False

    def visit(item: object) -> object:
        nonlocal cyclic
        kind = type(item)
        if kind in _PLAIN:
            return item
        key = id(item)
        if key in active:
            cyclic = True
            return item
        if kind in (list, tuple, dict) or (is_dataclass(item) and not isinstance(item, type)):
            active.add(key)
            try:
                return visit_container(item)
            finally:
                active.remove(key)
        if key not in leaves:
            np = _numpy()
            if np is not None and isinstance(item, np.ndarray):
                leaves[key] = np.array(item, copy=True, order="C") if copy_arrays else item
            else:
                array = _as_array(item, "output" if copied is None else "input")
                leaves[key] = item if array is None else array
                if array is not None and copied is not None:
                    copied.append(array)
        return leaves[key]

    def visit_container(item: object) -> object:
        if type(item) is list or type(item) is tuple:
            items = [visit(child) for child in item]
            if all(new is old for new, old in zip(items, item, strict=True)):
                return item
            return items if type(item) is list else tuple(items)
        if type(item) is dict:
            entries = {name: visit(child) for name, child in item.items()}
            return item if all(child is item[name] for name, child in entries.items()) else entries
        changed: dict[str, object] = {}
        for each in fields(item):  # type: ignore[arg-type]  # The caller checked the dataclass.
            original = getattr(item, each.name)
            normalized = visit(original)
            if normalized is not original:
                changed[each.name] = normalized
        if not changed:
            return item
        # Preserve frozen, slotted and init=False fields without running __post_init__.
        replaced = copy.copy(item)
        for name, child in changed.items():
            object.__setattr__(replaced, name, child)
        return replaced

    results = []
    for item in values:
        cyclic = False
        leaves.clear()
        normalized = visit(item)
        # Reconnect back-edges to the normalized graph, reusing the converted array
        # leaves. The shallow copies made during discovery can still point at originals.
        if cyclic and normalized is not item:
            normalized = copy.deepcopy(item, leaves.copy())
        results.append(normalized)
    return results


def normalize_inputs(values: list[object]) -> Normalized:
    """Convert non-NumPy inputs on the game thread and account for their copies."""
    copied: list[ndarray] = []
    normalized = _normalize(values, copied=copied)
    return Normalized(
        values=normalized,
        copied_bytes=sum(array.nbytes for array in copied),
        owned_ids=frozenset(map(id, copied)),
    )


def normalize_outputs(values: list[object], *, copy_arrays: bool = False) -> list[object]:
    """Materialize model outputs on the producing thread, copying ndarrays for local delivery."""
    return _normalize(values, copy_arrays=copy_arrays)


def arrays(value: object) -> Iterator[ndarray]:
    """Every ndarray leaf of a normalized value, visiting each container or dataclass once."""
    np = _numpy()
    if np is None:
        return
    pending = [value]
    visited: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, np.ndarray):
            yield item
            continue
        if type(item) in _PLAIN or id(item) in visited:
            continue
        if type(item) in (list, tuple):
            visited.add(id(item))
            pending.extend(item)  # type: ignore[arg-type]
        elif type(item) is dict:
            visited.add(id(item))
            pending.extend(item.values())
        elif is_dataclass(item) and not isinstance(item, type):
            visited.add(id(item))
            pending.extend(getattr(item, each.name) for each in fields(item))


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

    _held: dict[int, int] = field(default_factory=dict)

    def lock(self, values: list[object]) -> list[ndarray]:
        locked: list[ndarray] = []
        for array in arrays(values):
            key = id(array)
            count = self._held.get(key, 0)
            if count == 0:
                if not array.flags.writeable:
                    continue
                array.flags.writeable = False
            self._held[key] = count + 1
            locked.append(array)
        return locked

    def unlock(self, locked: list[ndarray]) -> None:
        for array in locked:
            key = id(array)
            count = self._held[key]
            if count > 1:
                self._held[key] = count - 1
            else:
                del self._held[key]
                array.flags.writeable = True


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
        locked = self._book.flight.lock(call.inputs)
        request_id = self._book._reserve_id()
        future: asyncio.Future[list[object]] = self._loop.create_future()
        self._entries.append(_Flight(request_id, call.checkpoint, future, locked, self._sent_at))
        return request_id, future

    def rollback_last(self) -> None:
        """Undo the most recently staged call (e.g. when its input packing failed)."""
        entry = self._entries.pop()
        self._book.flight.unlock(entry.locked)

    def rollback(self) -> None:
        """Unlock every uncommitted staged call."""
        while self._entries:
            self.rollback_last()

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
        self.flight = Flight()
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
        self.flight.unlock(entry.locked)
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
            self.flight.unlock(entry.locked)
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
