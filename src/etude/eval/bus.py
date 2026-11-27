"""The one transport boundary between game code and loaded checkpoints.

`ForwardBus` takes `ForwardCall`s from a game process and resolves one future
per call with its outputs, or with `ForwardError`. Two transports implement it:
`LocalForwardBus` (`etude.eval.batching`, an inference thread in the game
process) and `SocketForwardBus` (`etude.eval.agents`, the shared agent process).

The codec here runs on both. `normalize_inputs` and `normalize_outputs` turn
array leaves at any depth of `tuple`/`list`/`dict` into `numpy.ndarray`, so
games and models only ever see NumPy. `Flight` locks the game's arrays
read-only from submit until the answer. `freeze` gives the model read-only
inputs, and `detach` copies an output leaf that shares memory with an input.
NumPy is found through `sys.modules` and imported only when a non-NumPy array
is first seen, so a game process that never sees an array never loads it.
"""

from __future__ import annotations

import asyncio
import importlib
import pickle
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from etude.records import Record

if TYPE_CHECKING:
    from numpy import ndarray

    from etude.checkpoints import CheckpointRef

_IMMUTABLE_PLAIN = (type(None), bool, int, float, complex, str, bytes)
_PLAIN = (*_IMMUTABLE_PLAIN, bytearray)


@dataclass(frozen=True)
class ForwardCall:
    """One merged forward request: a checkpoint key and its inputs."""

    checkpoint: str  # key; the CheckpointRef crosses once, in bind()
    inputs: list[object]
    rows: int


class BusReport(Record):
    """What one game process's bus moved and how.

    `arrays` went by pointer; `arrays_copied` and `copied_bytes` count array
    payloads copied (into a pickle, a contiguous copy or out of a non-NumPy
    array). `pickled` and `pickled_bytes` count game-side pickle encodings and
    their in-band bytes; `wire_bytes` crossed a socket in either direction.
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
        """Send `calls` as one unit; each future raises `ForwardError` on failure."""

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


def _numpy() -> ModuleType | None:
    return sys.modules.get("numpy")


def _require_numpy() -> ModuleType:
    found = _numpy()
    if found is None:
        # NumPy is a dependency, imported only once a non-NumPy array is seen so
        # code-only and plain-value game processes stay light.
        found = importlib.import_module("numpy")
    return found


def _as_array(value: object, *, copy: bool, stats: _NormalizeStats | None) -> object:
    """A buffer, else CPU DLPack, exporter as an ndarray; other objects unchanged."""
    try:
        view = memoryview(value)  # type: ignore[arg-type]  # Probing the buffer protocol.
    except (TypeError, BufferError):
        exporter = getattr(value, "__dlpack__", None)
        if exporter is None:
            return value
        np = _require_numpy()
        try:
            array = np.from_dlpack(value)
        except (BufferError, RuntimeError, TypeError, ValueError):
            return value  # A device array stays the author's object.
        if copy:
            owned = np.array(array, copy=True, order="C")
            if stats is not None:
                stats.copied += 1
                stats.copied_bytes += int(owned.nbytes)
                stats.owned_ids.add(id(owned))
            return owned
        return array
    np = _require_numpy()
    if copy:
        try:
            owned = np.array(view, copy=True, order="C")
        finally:
            view.release()
        if stats is not None:
            stats.copied += 1
            stats.copied_bytes += int(owned.nbytes)
            stats.owned_ids.add(id(owned))
        return owned
    return np.asarray(view)


def _normalize(value: object, *, copy: bool, stats: _NormalizeStats | None) -> object:
    kind = type(value)
    if kind in _PLAIN:
        return value
    np = _numpy()
    if np is not None and kind is np.ndarray:
        return value
    if kind is list:
        return [_normalize(item, copy=copy, stats=stats) for item in value]  # type: ignore[attr-defined]
    if kind is tuple:
        return tuple(_normalize(item, copy=copy, stats=stats) for item in value)  # type: ignore[attr-defined]
    if kind is dict:
        return {k: _normalize(v, copy=copy, stats=stats) for k, v in value.items()}  # type: ignore[attr-defined]
    if np is not None and isinstance(value, np.ndarray):
        return value
    return _as_array(value, copy=copy, stats=stats)


def normalize_inputs(values: list[object]) -> Normalized:
    """Convert non-NumPy array leaves in game inputs into bus-owned C-contiguous ndarrays."""
    stats = _NormalizeStats()
    normalized = [_normalize(item, copy=True, stats=stats) for item in values]
    return Normalized(
        values=normalized,
        copied=stats.copied,
        copied_bytes=stats.copied_bytes,
        owned_ids=frozenset(stats.owned_ids),
    )


def normalize_outputs(values: list[object]) -> list[object]:
    """Materialize non-NumPy array leaves in model outputs on the producing thread."""
    return [_normalize(item, copy=False, stats=None) for item in values]


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


def detach(outputs: list[object], inputs: list[object]) -> list[object]:
    """Copy any output array leaf that may share memory with an input array."""
    sources = list(arrays(inputs))
    if not sources:
        return outputs
    np = _require_numpy()

    def owned(value: object) -> object:
        if isinstance(value, np.ndarray):
            if any(np.may_share_memory(value, source) for source in sources):
                return value.copy()
            return value
        if type(value) is list:
            return [owned(item) for item in value]
        if type(value) is tuple:
            return tuple(owned(item) for item in value)
        if type(value) is dict:
            return {k: owned(v) for k, v in value.items()}
        return value

    return [owned(output) for output in outputs]


def _is_fast_array(value: object, np: ModuleType | None) -> bool:
    if np is None or type(value) is not np.ndarray:
        return False
    array = cast("ndarray", value)
    return bool(array.flags.c_contiguous) and not bool(array.dtype.hasobject)


def _is_plain_array(value: object, np: ModuleType) -> bool:
    if type(value) is not np.ndarray:
        return False
    array = cast("ndarray", value)
    return not bool(array.dtype.hasobject)


def pack_local_inputs(normalized: Normalized) -> PackResult:
    """Encode normalized game inputs for the local inference thread.

    Top-level non-object ndarrays skip pickle: C-contiguous ones pass a read-only
    view by pointer, and non-contiguous ones copy into a contiguous array. Every
    other shape uses pickle protocol 5 so nested contiguous ndarrays stay
    zero-copy while containers arrive as the inference thread's own objects.
    Call after `Flight.lock` so locked game arrays expose read-only buffers.
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
    for array in arrays(values):
        if id(array) in normalized.owned_ids:
            array.flags.writeable = False

    buffers: list[pickle.PickleBuffer] = []

    def collect(buf: pickle.PickleBuffer) -> bool:
        view = memoryview(buf)
        readonly = view.readonly
        view.release()
        if readonly:
            buffers.append(buf)
            return False
        return True

    data = pickle.dumps(values, protocol=5, buffer_callback=collect)
    pointer_arrays = 0
    copied_arrays = 0
    copied_bytes = 0
    for array in arrays(values):
        if id(array) in normalized.owned_ids:
            continue
        if (
            np is not None
            and type(array) is np.ndarray
            and (array.flags.c_contiguous or array.flags.f_contiguous)
            and not array.dtype.hasobject
        ):
            pointer_arrays += 1
        else:
            copied_arrays += 1
            copied_bytes += int(array.nbytes)
    return PackResult(
        payload=PickledPayload(data=data, buffers=buffers),
        arrays=pointer_arrays,
        arrays_copied=normalized.copied + copied_arrays,
        copied_bytes=normalized.copied_bytes + copied_bytes,
        pickled=1,
        pickled_bytes=len(data),
    )


def pack_local_outputs(outputs: list[object]) -> list[object] | PickledPayload:
    """Encode normalized, detached model outputs for return to the game loop."""
    np = _numpy()
    if all(type(item) in _IMMUTABLE_PLAIN or _is_fast_array(item, np) for item in outputs):
        return outputs
    buffers: list[pickle.PickleBuffer] = []
    data = pickle.dumps(outputs, protocol=5, buffer_callback=buffers.append)
    return PickledPayload(data=data, buffers=buffers)


def unpack_local(payload: list[object] | PickledPayload) -> list[object]:
    """Reconstruct a local-bus payload on the receiving thread."""
    if isinstance(payload, PickledPayload):
        return cast(list[object], pickle.loads(payload.data, buffers=payload.buffers))
    return payload


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
