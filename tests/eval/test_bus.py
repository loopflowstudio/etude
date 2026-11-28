"""Contract, counting and equivalence tests for `ForwardBus` transports."""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from multiprocessing import Pipe
from multiprocessing.connection import Connection, wait
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

import numpy as np
import pytest

from etude.checkpoints import CheckpointRef
from etude.controller.declarations import asked, read_experiment
from etude.errors import EtudeError, RetryRequested
from etude.eval.agents import (
    Forward,
    ForwardError,
    ModelForward,
    SocketForwardBus,
    running_agents,
)
from etude.eval.batching import LocalForwardBus, _run_agents
from etude.eval.bus import BusReport, ForwardBus, ForwardCall, arrays, normalize_inputs
from etude.eval.composition import build_agents
from etude.eval.game import GameReport, Matches, _Forwarder
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Match, compose
from etude.eval.resources import TournamentQueue, place, resources
from etude.eval.result import Tables
from etude.eval.tournament import evaluating
from etude.jobs import Agents, EvalGroup, ExperimentState, evaluation_group, hold_group
from tests.eval import mirror as mirror_panel
from tests.eval.arithmetic import checkpoint
from tests.test_eval_groups import group, job

type _Transport = Literal["local", "socket"]


@asynccontextmanager
async def _open_bus(
    transport: _Transport,
    load: Callable[[CheckpointRef], object],
    *,
    max_batch: int = 32,
    memory: int = 1024 * 1024,
    artifacts: Path | None = None,
) -> AsyncIterator[ForwardBus]:
    if transport == "local":
        bus: ForwardBus = LocalForwardBus(
            load, max_batch=max_batch, memory=memory, artifacts=artifacts
        )
        try:
            yield bus
        finally:
            await bus.close()
        return

    # Short temp dir under /tmp so AF_UNIX path length stays well under macOS limits.
    with TemporaryDirectory(dir="/tmp", prefix="etude-bus-") as scratch:
        address = scratch + "/agents.sock"
        parent, child = Pipe()
        with parent, child:
            server = threading.Thread(
                target=_run_agents,
                args=(address, load, max_batch, memory, child, scratch),
            )
            server.start()
            try:
                assert parent.poll(5), "socket listener did not start"
                parent.recv()
                bus = await SocketForwardBus.connect(address)
                try:
                    yield bus
                finally:
                    await bus.close()
            finally:
                parent.send(None)
                server.join(5)
                assert not server.is_alive(), "socket listener did not stop"


class _LazyBuffer:
    """Buffer-protocol exporter recording the threads that read and release its view."""

    def __init__(self, data: np.ndarray) -> None:
        self._data = data
        self.read_threads: list[int] = []
        self.release_threads: list[int] = []

    def __buffer__(self, flags: int) -> memoryview:
        self.read_threads.append(threading.get_ident())
        return memoryview(self._data)

    def __release_buffer__(self, view: memoryview) -> None:
        self.release_threads.append(threading.get_ident())


class _CpuDlpack:
    """CPU DLPack exporter without the buffer protocol."""

    def __init__(self, data: np.ndarray) -> None:
        self._data = data

    def __dlpack__(self, stream: int | None = None) -> object:
        return self._data.__dlpack__(stream=stream)

    def __dlpack_device__(self) -> tuple[int, int]:
        return self._data.__dlpack_device__()


class _NonCpuDlpack:
    """Simulated GPU/MPS DLPack tensor whose CPU export fails."""

    def __dlpack__(self, stream: int | None = None) -> object:
        raise BufferError("Unsupported device in DLTensor")

    def __dlpack_device__(self) -> tuple[int, int]:
        return (2, 0)


class _CudaArrayOnly:
    """Simulated CUDA array exposing only __cuda_array_interface__."""

    __cuda_array_interface__ = {
        "shape": (4,),
        "typestr": "<f4",
        "data": (0, False),
        "version": 3,
    }


@dataclass(frozen=True)
class _Action:
    mask: np.ndarray
    ids: np.ndarray


@dataclass
class _Cycle:
    values: np.ndarray
    link: _Cycle | None = None


@dataclass
class _Prepared:
    """Shaped like a game's prepared step: nested dataclasses, dicts and lists of arrays."""

    observation: dict[str, np.ndarray]
    action: _Action
    rows: list[np.ndarray]
    count: int
    totals: dict[str, float] = field(init=False, default_factory=dict)


def _prepared(rows: int) -> _Prepared:
    return _Prepared(
        observation={"cards": np.ones((rows, 64), dtype=np.float32)},
        action=_Action(
            mask=np.ones((rows, 8), dtype=np.bool_), ids=np.zeros((rows, 8), dtype=np.int64)
        ),
        rows=[np.full(16, float(row), dtype=np.float32) for row in range(2)],
        count=rows,
    )


def _prepared_arrays(prepared: _Prepared) -> list[np.ndarray]:
    action = prepared.action
    return [prepared.observation["cards"], action.mask, action.ids, *prepared.rows]


def _has_no_external_owner(array: np.ndarray) -> bool:
    """Verify an output ndarray pins no PyCapsule, lazy buffer, or model object via .base."""
    cur: object = array.base
    while cur is not None:
        if type(cur) is np.ndarray:
            cur = cur.base
        elif isinstance(cur, memoryview):
            cur = cur.obj
        else:
            return False
    return True


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_splits_merged_batch_back_to_each_caller(transport: _Transport) -> None:
    cp = checkpoint("merge").checkpoint
    seen_sizes: list[int] = []

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            seen_sizes.append(len(inputs))
            return [int(str(x)) * 10 for x in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load, max_batch=8) as bus:
            bus.bind(cp)
            first, second = bus.submit(
                [ForwardCall(cp.key, [1, 2], 2), ForwardCall(cp.key, [3], 1)]
            )
            assert await first == [10, 20]
            assert await second == [30]

    asyncio.run(run())
    assert seen_sizes == [3]


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_flight_lock_blocks_writes_until_answer_and_preserves_readonly(
    transport: _Transport,
) -> None:
    cp_a = checkpoint("alpha").checkpoint
    cp_b = checkpoint("beta").checkpoint
    entered_a = threading.Event()
    entered_b = threading.Event()
    release_a = threading.Event()
    release_b = threading.Event()

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            if ref.key == cp_a.key:
                entered_a.set()
                release_a.wait(timeout=5.0)
            else:
                entered_b.set()
                release_b.wait(timeout=5.0)
            return [len(inputs) for _ in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp_a)
            bus.bind(cp_b)
            writable = np.arange(8, dtype=np.float32)
            readonly = np.arange(8, dtype=np.float32)
            readonly.flags.writeable = False

            fut_a, fut_b = bus.submit(
                [
                    ForwardCall(cp_a.key, [writable, readonly], 2),
                    ForwardCall(cp_b.key, [writable], 1),
                ]
            )
            assert await asyncio.to_thread(entered_a.wait, 5.0)
            with pytest.raises(ValueError, match="read-only"):
                writable[0] = 99.0

            release_a.set()
            assert await fut_a == [2, 2]
            assert await asyncio.to_thread(entered_b.wait, 5.0)
            # Still held by cp_b, so writable must remain locked.
            with pytest.raises(ValueError, match="read-only"):
                writable[0] = 99.0

            release_b.set()
            assert await fut_b == [1]
            writable[0] = 42.0
            assert writable[0] == 42.0
            assert not readonly.flags.writeable

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_model_inplace_write_fails_with_forward_error_and_leaves_game_array_intact(
    transport: _Transport,
) -> None:
    cp = checkpoint("mutator").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            arr = cast(np.ndarray, inputs[0])
            arr /= 2.0
            return [arr]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            obs = np.array([2.0, 4.0, 6.0], dtype=np.float32)
            [fut] = bus.submit([ForwardCall(cp.key, [obs], 1)])
            with pytest.raises(ForwardError, match="read-only") as info:
                await fut
            assert info.value.checkpoint == cp.key
            assert np.array_equal(obs, np.array([2.0, 4.0, 6.0], dtype=np.float32))
            assert obs.flags.writeable

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_outputs_never_share_memory_with_inputs(transport: _Transport) -> None:
    cp = checkpoint("alias").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            results: list[object] = []
            for item in inputs:
                if isinstance(item, np.ndarray):
                    results.append(item)
                else:
                    row = cast(dict[str, np.ndarray], item)
                    results.append({"slice": row["obs"][:2], "tuple": (row["obs"],)})
            return results

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            top = np.arange(8, dtype=np.float32)
            nested = np.arange(8, dtype=np.float32)
            [fut] = bus.submit([ForwardCall(cp.key, [top, {"obs": nested}], 2)])
            out_top, out_nested = await fut
            assert isinstance(out_top, np.ndarray)
            assert not np.shares_memory(out_top, top)
            out_top[0] = -1.0
            assert top[0] == 0.0

            mapping = cast(dict[str, object], out_nested)
            out_slice = cast(np.ndarray, mapping["slice"])
            (out_tup,) = cast(tuple[np.ndarray], mapping["tuple"])
            assert not np.shares_memory(out_slice, nested)
            assert not np.shares_memory(out_tup, nested)
            out_slice[0] = -2.0
            assert nested[0] == 0.0

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_containers_are_not_shared_with_model(transport: _Transport) -> None:
    cp = checkpoint("containers").checkpoint
    observed: list[object] = []

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            observed.extend(inputs)
            return ["ok" for _ in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            inner_list = [1, 2]
            payload = {"items": inner_list, "arr": np.arange(4, dtype=np.float32)}
            [fut] = bus.submit([ForwardCall(cp.key, [payload, inner_list], 2)])
            assert await fut == ["ok", "ok"]
            received_dict = cast(dict[str, object], observed[0])
            received_list = observed[1]
            assert received_dict is not payload
            assert received_dict["items"] is not inner_list
            assert received_list is not inner_list

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_non_numpy_leaves_normalize_on_producing_thread_at_any_depth(
    transport: _Transport,
) -> None:
    cp = checkpoint("lazy").checkpoint
    model_threads: list[int] = []
    lazy_records: list[tuple[list[int], list[int], weakref.ReferenceType[_LazyBuffer]]] = []
    lazy_backings: list[np.ndarray] = []
    input_checks: list[bool] = []
    top_backing = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    nested_backing = np.array([4.0, 5.0], dtype=np.float32)
    dl_backing = np.array([6.0, 7.0], dtype=np.float32)

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            model_threads.append(threading.get_ident())
            top_in = inputs[0]
            nested_in = cast(dict[str, object], inputs[1])
            input_checks.append(
                isinstance(top_in, np.ndarray)
                and not np.shares_memory(top_in, top_backing)
                and isinstance(nested_in["buf"], np.ndarray)
                and not np.shares_memory(cast(np.ndarray, nested_in["buf"]), nested_backing)
                and isinstance(nested_in["dl"], np.ndarray)
                and not np.shares_memory(cast(np.ndarray, nested_in["dl"]), dl_backing)
            )
            backings = [
                np.array([10.0, 20.0], dtype=np.float32),
                np.array([30.0], dtype=np.float32),
                np.array([40.0, 50.0], dtype=np.float32),
                np.array([60.0], dtype=np.float32),
            ]
            lazy_backings.extend(backings)
            a, b, c, d = (_LazyBuffer(arr) for arr in backings)
            for lazy in (a, b, c, d):
                lazy_records.append((lazy.read_threads, lazy.release_threads, weakref.ref(lazy)))
            return [(a, b), {"logits": c, "value": d}]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            [fut] = bus.submit(
                [
                    ForwardCall(
                        cp.key,
                        [
                            _LazyBuffer(top_backing),
                            {"buf": _LazyBuffer(nested_backing), "dl": _CpuDlpack(dl_backing)},
                        ],
                        2,
                    )
                ]
            )
            tup_out, dict_out = await fut
            logits_t, value_t = cast(tuple[object, object], tup_out)
            mapping = cast(dict[str, object], dict_out)
            leaves = [logits_t, value_t, mapping["logits"], mapping["value"]]
            expected = [
                np.array([10.0, 20.0], dtype=np.float32),
                np.array([30.0], dtype=np.float32),
                np.array([40.0, 50.0], dtype=np.float32),
                np.array([60.0], dtype=np.float32),
            ]
            for leaf, want, backing in zip(leaves, expected, lazy_backings, strict=True):
                assert isinstance(leaf, np.ndarray)
                assert np.array_equal(leaf, want)
                assert leaf.flags.writeable
                assert not np.shares_memory(leaf, backing)
                assert _has_no_external_owner(leaf)
                leaf[0] = -1.0
            gc.collect()
            assert all(ref() is None for _, _, ref in lazy_records)

    asyncio.run(run())
    assert input_checks == [True]
    assert len(model_threads) == 1
    assert all(
        read == [model_threads[0]] and released == [model_threads[0]]
        for read, released, _ in lazy_records
    )
    assert "torch" not in sys.modules
    assert "mlx" not in sys.modules


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_model_reused_output_buffer_does_not_alias_game_answers(transport: _Transport) -> None:
    cp_top = checkpoint("reused_top").checkpoint
    cp_nested = checkpoint("reused_nested").checkpoint
    backing = np.zeros(4, dtype=np.float32)

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            step = float(str(inputs[0]))
            backing[:] = step
            if ref.key == cp_top.key:
                return [backing]
            return [{"nested": backing, "tuple": (backing,)}]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp_top)
            bus.bind(cp_nested)
            [first_top_fut] = bus.submit([ForwardCall(cp_top.key, [1], 1)])
            [first_top] = await first_top_fut
            assert isinstance(first_top, np.ndarray)
            [first_nested_fut] = bus.submit([ForwardCall(cp_nested.key, [1], 1)])
            [first_nested] = await first_nested_fut
            first_map = cast(dict[str, object], first_nested)
            first_dict_arr = cast(np.ndarray, first_map["nested"])
            (first_tup_arr,) = cast(tuple[np.ndarray], first_map["tuple"])

            [second_fut] = bus.submit([ForwardCall(cp_top.key, [2], 1)])
            [second_top] = await second_fut
            assert isinstance(second_top, np.ndarray)

            assert np.all(first_top == 1.0)
            assert np.all(first_dict_arr == 1.0)
            assert np.all(first_tup_arr == 1.0)
            assert np.all(second_top == 2.0)
            assert not np.shares_memory(first_top, backing)
            assert not np.shares_memory(first_dict_arr, backing)
            assert not np.shares_memory(first_tup_arr, backing)
            assert first_top.flags.writeable and first_top.base is None
            assert first_dict_arr.flags.writeable and _has_no_external_owner(first_dict_arr)
            assert first_tup_arr.flags.writeable and _has_no_external_owner(first_tup_arr)

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_cpu_dlpack_outputs_arrive_writable_and_owned(transport: _Transport) -> None:
    cp_top = checkpoint("dlpack_top").checkpoint
    cp_nested = checkpoint("dlpack_nested").checkpoint
    top_backing = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    nested_backing = np.array([4.0, 5.0], dtype=np.float32)

    def load(ref: CheckpointRef) -> ModelForward:
        if ref.key == cp_top.key:
            return lambda inputs: [_CpuDlpack(top_backing) for _ in inputs]
        return lambda inputs: [{"dl": _CpuDlpack(nested_backing)} for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp_top)
            bus.bind(cp_nested)
            [top_fut] = bus.submit([ForwardCall(cp_top.key, [0], 1)])
            [top_out] = await top_fut
            assert isinstance(top_out, np.ndarray)
            assert np.array_equal(top_out, top_backing)
            assert top_out.flags.writeable
            assert top_out.base is None
            assert not np.shares_memory(top_out, top_backing)
            top_out[0] = -1.0
            assert top_backing[0] == 1.0

            [nested_fut] = bus.submit([ForwardCall(cp_nested.key, [0], 1)])
            [nested_out] = await nested_fut
            nested_arr = cast(np.ndarray, cast(dict[str, object], nested_out)["dl"])
            assert isinstance(nested_arr, np.ndarray)
            assert np.array_equal(nested_arr, nested_backing)
            assert nested_arr.flags.writeable
            assert _has_no_external_owner(nested_arr)
            assert not np.shares_memory(nested_arr, nested_backing)
            nested_arr[0] = -2.0
            assert nested_backing[0] == 4.0

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
@pytest.mark.parametrize("bad_factory", [_NonCpuDlpack, _CudaArrayOnly])
def test_non_cpu_tensor_outputs_and_inputs_fail_with_forward_error(
    transport: _Transport, bad_factory: Callable[[], object]
) -> None:
    cp_out = checkpoint("gpu_out").checkpoint
    cp_in = checkpoint("gpu_in").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        if ref.key == cp_out.key:
            return lambda inputs: [{"logits": bad_factory()} for _ in inputs]
        return lambda inputs: ["ok" for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp_out)
            bus.bind(cp_in)

            [out_fut] = bus.submit([ForwardCall(cp_out.key, [1], 1)])
            with pytest.raises(ForwardError, match="CPU") as out_info:
                await out_fut
            assert out_info.value.checkpoint == cp_out.key

            arr = np.arange(4, dtype=np.float32)
            [in_fut] = bus.submit([ForwardCall(cp_in.key, [{"arr": arr, "gpu": bad_factory()}], 1)])
            with pytest.raises(ForwardError, match="CPU") as in_info:
                await in_fut
            assert in_info.value.checkpoint == cp_in.key
            assert arr.flags.writeable

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_bytearray_outputs_are_copied_not_shared(transport: _Transport) -> None:
    cp = checkpoint("bytes").checkpoint
    backing = bytearray(b"abcd")

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            if inputs[0] == 2:
                backing[0] = ord("Z")
            return [backing, {"ba": backing}]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            [first] = bus.submit([ForwardCall(cp.key, [1, 1], 2)])
            top1, nested1 = await first
            [second] = bus.submit([ForwardCall(cp.key, [2, 2], 2)])
            top2, _ = await second
            assert top1 == bytearray(b"abcd")
            assert cast(dict[str, object], nested1)["ba"] == bytearray(b"abcd")
            assert top2 == bytearray(b"Zbcd")

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_unpicklable_input_fails_only_that_call_and_unlocks_arrays(
    transport: _Transport,
) -> None:
    cp = checkpoint("unpicklable").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: ["ok" for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            good_fut, bad_fut = bus.submit(
                [
                    ForwardCall(cp.key, [np.arange(4, dtype=np.float32)], 1),
                    ForwardCall(cp.key, [{"arr": arr, "fn": lambda: None}], 1),
                ]
            )
            assert await good_fut == ["ok"]
            with pytest.raises(ForwardError) as info:
                await bad_fut
            assert info.value.checkpoint == cp.key
            assert arr.flags.writeable

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_atomic_submit_rollback_when_later_call_is_unbound(transport: _Transport) -> None:
    cp = checkpoint("bound").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: ["ok" for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            with pytest.raises(KeyError, match="unbound"):
                bus.submit(
                    [
                        ForwardCall(cp.key, [arr], 1),
                        ForwardCall("unbound", [arr], 1),
                    ]
                )
            assert arr.flags.writeable
            [fut] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            assert await fut == ["ok"]
            assert arr.flags.writeable
            report = await bus.close()
            assert report.calls == report.answers == 1

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_play_locks_inputs_immediately_on_call_before_barrier_flush(transport: _Transport) -> None:
    cp = checkpoint("play_lock").checkpoint
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    release_second = threading.Event()
    calls = 0

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                release_first.wait(timeout=5.0)
            elif calls == 2:
                second_entered.set()
                release_second.wait(timeout=5.0)
            return [float(cast(np.ndarray, item)[0]) for item in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load, max_batch=8) as bus:
            forwarder = _Forwarder(bus, max_batch=8, eager=False)
            forward = forwarder.bind(cp)
            obs = np.array([5.0, 6.0], dtype=np.float32)
            readonly = np.array([7.0, 8.0], dtype=np.float32)
            readonly.flags.writeable = False

            # Locking happens synchronously on call, before the barrier flushes.
            first = forward([obs, readonly])
            with pytest.raises(ValueError, match="read-only"):
                obs[0] = 999.0

            async def await_first() -> list[object]:
                return await first

            first_task: asyncio.Task[list[object]] = asyncio.create_task(await_first())
            assert await asyncio.to_thread(first_entered.wait, 5.0)

            # Enqueue a second call sharing `obs` while call 1 is in flight, then
            # let call 1 finish while call 2 is held; `obs` stays locked throughout.
            second = forward([obs])
            release_first.set()
            assert await first_task == [5.0, 7.0]
            assert await asyncio.to_thread(second_entered.wait, 5.0)
            with pytest.raises(ValueError, match="read-only"):
                obs[0] = 888.0

            release_second.set()
            assert await second == [5.0]
            obs[0] = 42.0
            assert obs[0] == 42.0
            assert not readonly.flags.writeable

            report = await bus.close()
            assert report.calls == report.answers == 2
            if transport == "local":
                assert report.arrays == 3
                assert report.arrays_copied == report.copied_bytes == 0
                assert report.pickled == report.pickled_bytes == report.wire_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_play_barrier_unlocks_inputs_when_submit_raises(transport: _Transport) -> None:
    cp = checkpoint("play_submit_failure").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: ["ok" for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            forwarder = _Forwarder(bus, max_batch=8)
            forward = forwarder.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            valid = forward([arr])
            unbound = forwarder.call("unbound", [arr], rows=1)
            assert not arr.flags.writeable

            with pytest.raises(KeyError, match="unbound"):
                await unbound
            with pytest.raises(KeyError, match="unbound"):
                await valid
            assert arr.flags.writeable
            assert await forward([arr]) == ["ok"]

    asyncio.run(run())


def test_play_barrier_keeps_in_flight_inputs_locked_if_close_cannot_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cp = checkpoint("play_close").checkpoint
    entered = threading.Event()
    release = threading.Event()

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            entered.set()
            release.wait(timeout=5.0)
            return ["done" for _ in inputs]

        return forward

    async def run() -> None:
        async with _open_bus("local", load) as bus:
            forwarder = _Forwarder(bus, max_batch=8)
            forward = forwarder.bind(cp)
            running = np.arange(8, dtype=np.float32)
            staged = np.arange(4, dtype=np.float32)
            answer = forward([running])
            try:
                assert await asyncio.to_thread(entered.wait, 5.0)
                staged_answer = forward([staged])
                assert not staged.flags.writeable
                forwarder.clear_pending()
                assert staged.flags.writeable
                with pytest.raises(asyncio.CancelledError):
                    await staged_answer

                with monkeypatch.context() as patch:
                    patch.setattr("etude.eval.batching.seconds_left", lambda: 0.0)
                    report = await bus.close()
                assert report.calls == 1
                assert report.answers == 0
                assert not running.flags.writeable
            finally:
                release.set()

            assert await asyncio.wait_for(answer, timeout=5.0) == ["done"]
            assert running.flags.writeable

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_cancelled_await_keeps_arrays_locked_until_completion(transport: _Transport) -> None:
    cp = checkpoint("cancel").checkpoint
    entered = threading.Event()
    release = threading.Event()

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            entered.set()
            release.wait(timeout=5.0)
            return ["done" for _ in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            arr = np.arange(8, dtype=np.float32)
            [fut] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            assert await asyncio.to_thread(entered.wait, 5.0)
            fut.cancel()
            with pytest.raises(asyncio.CancelledError):
                await fut
            # Forward is still held; array must stay locked after cancellation.
            with pytest.raises(ValueError, match="read-only"):
                arr[0] = 1.0
            release.set()
            # A later call on the same FIFO bus completes only after the first reply unlocks arr.
            [sync] = bus.submit([ForwardCall(cp.key, ["sync"], 1)])
            assert await sync == ["done"]
            assert arr.flags.writeable
            arr[0] = 7.0
            assert arr[0] == 7.0

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_gc_referents_from_seat_agents_never_reach_loaded_model(transport: _Transport) -> None:
    player = checkpoint("isolated")
    loaded_ids: list[int] = []

    class _Model:
        def __call__(self, inputs: list[object]) -> list[object]:
            return [1 for _ in inputs]

    def load(ref: CheckpointRef) -> ModelForward:
        model = _Model()
        loaded_ids.append(id(model))
        return model

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            forwarder = _Forwarder(bus, max_batch=8)
            seats = build_agents((player,), forwarder.bind, None)
            forward = cast(Forward, seats[0])
            assert await forward([0]) == [1]
            assert len(loaded_ids) == 1

            seen: set[int] = set()
            stack: list[object] = list(seats)
            while stack:
                obj = stack.pop()
                oid = id(obj)
                if oid in seen:
                    continue
                seen.add(oid)
                assert oid != loaded_ids[0]
                for ref in gc.get_referents(obj):
                    if id(ref) not in seen and not isinstance(ref, type):
                        stack.append(ref)

    asyncio.run(run())


def _raising_load(ref: CheckpointRef) -> ModelForward:
    def forward(inputs: list[object]) -> list[object]:
        raise RuntimeError("fixture boom")

    return forward


async def _catches_forward_error(match: Match, agents: Sequence[Forward]) -> Tables:
    try:
        await agents[0]([1])
    except ForwardError as error:
        assert error.checkpoint == "boom"
    return {"scores": [{"seat": 0, "score": 1}]}


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_raising_forward_fails_play_match_even_when_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
) -> None:
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator",
        lambda: "cuda" if transport == "socket" else "no accelerator detected",
    )
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    cp = checkpoint("boom")
    match = Match(players=(cp,), games=1)
    with (
        pytest.raises(EtudeError, match="fixture boom"),
        evaluating(
            environment("errors"),
            experiment="errors",
            matches=(match,),
            run=transport,
            directory=tmp_path,
        ) as active,
    ):
        active.play(_catches_forward_error, agents=Agents(load=_raising_load))
    db = statsdb("errors", tmp_path)
    try:
        assert db.sql("select state from matches").fetchall() == [("failed",)]
    finally:
        db.close()


def _identity_load(ref: CheckpointRef) -> ModelForward:
    return lambda inputs: inputs


async def _catches_unpicklable_input(match: Match, agents: Sequence[Forward]) -> Tables:
    try:
        await agents[0]([{"fn": lambda: None}])
    except ForwardError as error:
        assert error.checkpoint == "unpicklable"
    return {"scores": [{"seat": 0, "score": 1}]}


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_unpicklable_input_in_play_fails_match_even_when_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
) -> None:
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator",
        lambda: "cuda" if transport == "socket" else "no accelerator detected",
    )
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 2)
    cp = checkpoint("unpicklable")
    match = Match(players=(cp,), games=1)
    with (
        pytest.raises(EtudeError),
        evaluating(
            environment("unpicklable_play"),
            experiment="unpicklable_play",
            matches=(match,),
            run=transport,
            directory=tmp_path,
        ) as active,
    ):
        active.play(_catches_unpicklable_input, agents=Agents(load=_identity_load))
    db = statsdb("unpicklable_play", tmp_path)
    try:
        assert db.sql("select state from matches").fetchall() == [("failed",)]
    finally:
        db.close()


def _selective_failing_load(ref: CheckpointRef) -> ModelForward:
    if ref.key == "broken":
        raise ValueError("fixture cancelled load error")
    return lambda inputs: [int(str(x)) + 1 for x in inputs]


async def _cancels_load_await(match: Match, agents: Sequence[Forward]) -> Tables:
    if match.index == 1:
        raise AssertionError("unstarted match must not run after cancelled load failure")

    async def call_broken() -> list[object]:
        return await agents[0]([1])

    task: asyncio.Task[list[object]] = asyncio.create_task(call_broken())
    # Let the zero-delay barrier submit "broken", then cancel the awaiting task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Awaiting a healthy checkpoint on the same FIFO bus guarantees "broken"'s
    # load-failure reply has already been delivered to `_Forwarder`.
    assert await agents[1]([1]) == [2]
    return {"scores": [{"seat": 0, "score": 1}]}


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_cancelled_load_failure_fails_unstarted_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
) -> None:
    monkeypatch.setattr(
        "etude.eval.resources._detect_accelerator",
        lambda: "cuda" if transport == "socket" else "no accelerator detected",
    )
    monkeypatch.setattr(
        "etude.eval.resources.available_cpus", lambda: 2 if transport == "socket" else 1
    )
    broken = checkpoint("broken")
    healthy = checkpoint("healthy")
    matches = [
        Match(players=(broken, healthy), index=0, games=1),
        Match(players=(broken, healthy), index=1, games=1),
    ]
    with (
        pytest.raises(EtudeError, match="fixture cancelled load error"),
        evaluating(
            environment("loadfail"),
            experiment="loadfail",
            matches=matches,
            run=transport,
            directory=tmp_path,
        ) as active,
    ):
        active.play(_cancels_load_await, agents=Agents(load=_selective_failing_load, max_batch=1))
    db = statsdb("loadfail", tmp_path)
    try:
        rows = db.sql("select match, state, error from matches order by match").fetchall()
        assert len(rows) == 2
        assert all(
            state == "failed" and "fixture cancelled load error" in err for _, state, err in rows
        )
    finally:
        db.close()


def test_local_bus_counts_zero_pickle_and_zero_copy_for_contiguous_arrays() -> None:
    cp = checkpoint("fast").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: [cast(np.ndarray, x) + 1.0 for x in inputs]

    async def run() -> BusReport:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        bus.bind(cp)
        for index in range(5):
            arr = np.full((64, 32), float(index), dtype=np.float32)
            [fut] = bus.submit([ForwardCall(cp.key, [arr], rows=64)])
            [out] = await fut
            assert isinstance(out, np.ndarray)
            assert np.all(out == float(index + 1))
        return await bus.close()

    report = asyncio.run(run())
    assert report.calls == report.answers == 5
    assert report.arrays == 5
    assert report.arrays_copied == 0
    assert report.copied_bytes == 0
    assert report.pickled == 0
    assert report.pickled_bytes == 0
    assert report.wire_bytes == 0


def test_local_bus_counts_non_contiguous_array_in_copy_tier() -> None:
    cp = checkpoint("strided").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: [cast(np.ndarray, x).sum() for x in inputs]

    async def run() -> tuple[BusReport, int]:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        bus.bind(cp)
        base = np.arange(32, dtype=np.float32)
        strided = base[::2]
        assert not strided.flags.c_contiguous
        [fut] = bus.submit([ForwardCall(cp.key, [strided], rows=1)])
        [total] = await fut
        assert total == float(strided.sum())
        return await bus.close(), int(strided.nbytes)

    report, expected_bytes = asyncio.run(run())
    assert report.arrays == 0
    assert report.arrays_copied == 1
    assert report.copied_bytes == expected_bytes
    assert report.pickled == 0
    assert report.wire_bytes == 0


def test_local_bus_counts_dict_of_arrays_with_small_pickle_and_zero_array_copies() -> None:
    cp = checkpoint("dict").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: [
            {"sum": float(cast(dict[str, np.ndarray], row)["obs"].sum())} for row in inputs
        ]

    async def run() -> BusReport:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        bus.bind(cp)
        for _ in range(4):
            obs = np.ones((128, 64), dtype=np.float32)
            mask = np.ones((128, 8), dtype=np.bool_)
            [fut] = bus.submit([ForwardCall(cp.key, [{"obs": obs, "mask": mask}], rows=128)])
            [out] = await fut
            assert out == {"sum": float(128 * 64)}
        return await bus.close()

    report = asyncio.run(run())
    assert report.calls == report.answers == 4
    assert report.pickled == 4
    assert 0 < report.pickled_bytes < 4 * 500
    assert report.arrays == 8
    assert report.arrays_copied == 0
    assert report.copied_bytes == 0
    assert report.wire_bytes == 0
    assert report.handoff_seconds > 0.0


def test_local_bus_passes_dataclass_arrays_by_pointer_and_locks_them_in_flight() -> None:
    cp = checkpoint("prepared").checkpoint
    entered = threading.Event()
    release = threading.Event()

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            entered.set()
            release.wait(timeout=5.0)
            prepared = cast(_Prepared, inputs[0])
            leaves = _prepared_arrays(prepared)
            assert not any(leaf.flags.writeable for leaf in leaves)
            return [float(sum(leaf.sum() for leaf in leaves))]

        return forward

    async def run() -> BusReport:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        bus.bind(cp)
        prepared = _prepared(128)
        assert normalize_inputs([prepared]).values[0] is prepared
        leaves = _prepared_arrays(prepared)
        try:
            [fut] = bus.submit([ForwardCall(cp.key, [prepared], rows=128)])
            assert await asyncio.to_thread(entered.wait, 5.0)
            for leaf in leaves:
                with pytest.raises(ValueError, match="read-only"):
                    leaf[0] = 0
            release.set()
            assert await fut == [float(128 * 64 + 128 * 8 + 16)]
            assert all(leaf.flags.writeable for leaf in leaves)
        finally:
            release.set()
            report = await bus.close()
        return report

    report = asyncio.run(run())
    assert report.calls == report.answers == 1
    assert report.arrays == 5
    assert report.arrays_copied == 0
    assert report.copied_bytes == 0
    assert report.pickled == 1
    # Field names and class references only; the arrays alone are 41 KB.
    assert 0 < report.pickled_bytes < 1000
    assert report.wire_bytes == 0


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_dataclass_inputs_are_read_only_to_the_model_and_outputs_are_owned(
    transport: _Transport,
) -> None:
    cp_write = checkpoint("dataclass_write").checkpoint
    cp_echo = checkpoint("dataclass_echo").checkpoint
    backing = np.zeros(8, dtype=np.bool_)

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            prepared = cast(_Prepared, inputs[0])
            if ref.key == cp_write.key:
                np.add(prepared.action.ids, 1, out=prepared.action.ids)
            backing[:] = True
            return [_Action(mask=backing, ids=prepared.action.ids)]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp_write)
            bus.bind(cp_echo)
            prepared = _prepared(4)
            [written] = bus.submit([ForwardCall(cp_write.key, [prepared], 4)])
            with pytest.raises(ForwardError, match="read-only"):
                await written
            assert not prepared.action.ids.any()
            assert prepared.action.ids.flags.writeable

            [echoed] = bus.submit([ForwardCall(cp_echo.key, [prepared], 4)])
            [answer] = await echoed
            assert isinstance(answer, _Action)
            backing[:] = False
            assert answer.mask.all() and answer.mask.flags.writeable
            assert not np.shares_memory(answer.mask, backing)
            assert not np.shares_memory(answer.ids, prepared.action.ids)
            answer.ids[0, 0] = 7
            assert prepared.action.ids[0, 0] == 0

    asyncio.run(run())


def test_dataclass_buffer_leaves_normalize_into_a_copy_and_cycles_terminate() -> None:
    @dataclass
    class Node:
        data: object
        parent: Node | None = None

    source = np.arange(4, dtype=np.float32)
    root = Node(data=np.arange(2, dtype=np.float32))
    root.parent = root
    child = Node(data=_LazyBuffer(source))
    child.parent = child

    normalized = normalize_inputs([child, root])
    copied, kept = cast(list[Node], normalized.values)
    assert copied is not child and kept is root
    assert isinstance(copied.data, np.ndarray) and np.array_equal(copied.data, source)
    assert copied.parent is copied
    assert normalized.copied_bytes == source.nbytes
    assert len(list(arrays(normalized.values))) == 2


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_split_forward_snapshots_cyclic_outputs_before_reusing_buffers(
    transport: _Transport,
) -> None:
    cp = checkpoint("cyclic_outputs").checkpoint
    result = _Cycle(np.zeros(4, dtype=np.float32))
    result.link = result

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            [step] = inputs
            result.values.fill(cast(int, step))
            return [result]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load, max_batch=1) as bus:
            bus.bind(cp)
            [future] = bus.submit([ForwardCall(cp.key, [1, 2], 2)])
            first, second = cast(list[_Cycle], await future)
            assert first.link is first and second.link is second
            assert np.all(first.values == 1) and np.all(second.values == 2)
            assert not np.shares_memory(first.values, result.values)
            assert not np.shares_memory(first.values, second.values)

    asyncio.run(run())


def test_local_bus_counts_fortran_contiguous_consistently_at_top_level_and_nested() -> None:
    cp = checkpoint("fortran").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            top = cast(np.ndarray, inputs[0])
            nested = cast(dict[str, np.ndarray], inputs[1])["arr"]
            return [{"sum": float(top.sum())}, {"sum": float(nested.sum())}]

        return forward

    async def run() -> tuple[BusReport, int]:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        bus.bind(cp)
        f_top = np.asfortranarray(np.arange(12, dtype=np.float32).reshape(3, 4))
        f_nested = np.asfortranarray(np.arange(12, dtype=np.float32).reshape(3, 4))
        assert not f_top.flags.c_contiguous and f_top.flags.f_contiguous
        [fut_top, fut_nested] = bus.submit(
            [
                ForwardCall(cp.key, [f_top], rows=1),
                ForwardCall(cp.key, [{"arr": f_nested}], rows=1),
            ]
        )
        assert await fut_top == [{"sum": float(f_top.sum())}]
        assert await fut_nested == [{"sum": float(f_nested.sum())}]
        return await bus.close(), int(f_top.nbytes)

    report, nbytes = asyncio.run(run())
    assert report.calls == report.answers == 2
    assert report.arrays == 0
    assert report.arrays_copied == 2
    assert report.copied_bytes == 2 * nbytes
    assert report.pickled == 1
    assert report.handoff_seconds > 0.0


def test_socket_bus_counts_pickled_per_call() -> None:
    cp = checkpoint("sock_count").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: [x for x in inputs]

    async def run() -> BusReport:
        async with _open_bus("socket", load, max_batch=8) as bus:
            bus.bind(cp)
            f1, f2 = bus.submit([ForwardCall(cp.key, [1], 1), ForwardCall(cp.key, [2], 1)])
            assert await f1 == [1]
            assert await f2 == [2]
            return await bus.close()

    report = asyncio.run(run())
    assert report.calls == report.answers == 2
    assert report.pickled == 2


def test_local_bus_thread_exit_fails_pending_and_future_calls_with_retry() -> None:
    cp = checkpoint("die").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            raise SystemExit(1)

        return forward

    async def run() -> None:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024)
        try:
            bus.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            [first] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            with pytest.raises(RetryRequested, match="inference thread exited"):
                await first
            assert arr.flags.writeable
            [later] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            with pytest.raises(RetryRequested, match="inference thread exited"):
                await later
        finally:
            await bus.close()

    asyncio.run(run())


def test_local_bus_unwritable_report_fails_calls_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cp = checkpoint("unwritable").checkpoint
    (tmp_path / f"batches-{os.getpid()}.json").mkdir()

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: inputs

    async def run() -> None:
        bus = LocalForwardBus(load, max_batch=8, memory=1024 * 1024, artifacts=tmp_path)
        try:
            bus.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            [call] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            with pytest.raises(RetryRequested, match="IsADirectoryError"):
                await call
            assert arr.flags.writeable
        finally:
            await bus.close()

    asyncio.run(run())
    assert "etude: agents: IsADirectoryError" in capsys.readouterr().err


def _load_example_group(path: Path) -> EvalGroup:
    definition = read_experiment(path)
    assert definition.eval is not None
    state = ExperimentState(experiment=definition.name)
    return EvalGroup(
        evaluations=tuple(
            spec
            for each in asked(definition.eval, state)
            for spec in evaluation_group(each.lower(state)).evaluations
        )
    )


def test_local_and_socket_store_identical_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Seeded mirror panel under local vs socket.
    small = checkpoint("small")
    mirror = mirror_panel.Mirror(over=small)
    greedy = mirror.model_copy(update={"choice": "greedy"})
    mirror_results: dict[_Transport, list[tuple[object, ...]]] = {}
    transports: tuple[_Transport, ...] = ("local", "socket")
    for transport in transports:
        monkeypatch.setattr(
            "etude.eval.resources._detect_accelerator",
            lambda t=transport: "cuda" if t == "socket" else "no accelerator detected",
        )
        monkeypatch.setattr(
            "etude.eval.resources.available_cpus", lambda t=transport: 3 if t == "socket" else 1
        )
        root = tmp_path / f"mirror-{transport}"
        selected = job(
            "mirror", Agents(load=mirror_panel.load, build=mirror_panel.build, max_batch=2)
        )
        selected = selected.model_copy(
            update={
                "play": mirror_panel.play,
                "enter": (compose(mirror), compose(greedy)),
                "tournament": selected.tournament.model_copy(
                    update={"config": {}, "configs": (), "games": 2}
                ),
            }
        )
        assert hold_group(group(selected), directory=root)[0].played == 2
        artifacts = root / "grouped/artifacts/agents"
        assert (artifacts / "batches.json").exists() == (transport == "socket")
        db = statsdb("grouped", root)
        try:
            mirror_results[transport] = db.sql(
                "select match, seat, decision, action from choices order by match, decision, seat"
            ).fetchall()
        finally:
            db.close()
    assert mirror_results["local"] == mirror_results["socket"]
    assert len(mirror_results["local"]) == 8

    # 2. examples/agents.py under local vs socket.
    eval_group = _load_example_group(Path("examples/agents.py"))
    example_results: dict[_Transport, list[tuple[object, ...]]] = {}
    for transport in transports:
        monkeypatch.setattr(
            "etude.eval.resources._detect_accelerator",
            lambda t=transport: "cuda" if t == "socket" else "no accelerator detected",
        )
        monkeypatch.setattr(
            "etude.eval.resources.available_cpus", lambda t=transport: 3 if t == "socket" else 1
        )
        root = tmp_path / f"agents-{transport}"
        results = hold_group(eval_group, directory=root)
        assert [r.played for r in results] == [3, 3]
        artifacts = root / "agents/artifacts/agents"
        reports = [
            GameReport.model_validate_json(path.read_bytes())
            for path in artifacts.glob("game-*.json")
        ]
        assert reports
        if transport == "local":
            assert all(r.bus.wire_bytes == 0 for r in reports)
            assert list(artifacts.glob("batches-*.json"))
        else:
            assert all(r.bus.wire_bytes > 0 for r in reports)
            assert (artifacts / "batches.json").exists()
        db = statsdb("agents", root)
        try:
            example_results[transport] = db.sql(
                "select run, match, seat, score from scores order by run, match, seat"
            ).fetchall()
        finally:
            db.close()
    assert example_results["local"] == example_results["socket"]
    assert len(example_results["local"]) == 12


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_play_many_submits_eagerly_and_merges_queued_calls_without_a_clock(
    transport: _Transport,
) -> None:
    cp = checkpoint("eager").checkpoint
    first_entered = threading.Event()
    release_first = threading.Event()
    batch_rows: list[int] = []

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            total_rows = sum(int(cast(np.ndarray, x).shape[0]) for x in inputs)
            batch_rows.append(total_rows)
            if len(batch_rows) == 1:
                first_entered.set()
                release_first.wait(timeout=5.0)
            return [cast(np.ndarray, x) + 1.0 for x in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load, max_batch=8) as bus:
            forwarder = _Forwarder(bus, max_batch=8, eager=True)
            forward = forwarder.bind(cp)
            a = np.array([1.0, 2.0], dtype=np.float32)
            b = np.array([3.0, 4.0], dtype=np.float32)
            c = np.array([5.0, 6.0], dtype=np.float32)

            # Submitting on the call reaches the inference thread before any await.
            pending_a = forward([a], rows=2)
            assert first_entered.wait(timeout=5.0)
            with pytest.raises(ValueError, match="read-only"):
                a[0] = 0.0

            # Game code runs and submits two more calls while forward A is still held.
            pending_b = forward([b], rows=2)
            pending_c = forward([c], rows=2)
            release_first.set()

            [out_a] = await pending_a
            [out_b] = await pending_b
            [out_c] = await pending_c
            assert np.array_equal(cast(np.ndarray, out_a), np.array([2.0, 3.0], dtype=np.float32))
            assert np.array_equal(cast(np.ndarray, out_b), np.array([4.0, 5.0], dtype=np.float32))
            assert np.array_equal(cast(np.ndarray, out_c), np.array([6.0, 7.0], dtype=np.float32))

    asyncio.run(run())
    # Calls b and c arrived during forward A and merged into one 4-row batch.
    assert batch_rows == [2, 4]


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_play_barrier_runs_other_match_and_accepts_second_call_while_first_is_held(
    transport: _Transport,
) -> None:
    cp = checkpoint("play_overlap").checkpoint
    first_entered = threading.Event()
    release_first = threading.Event()
    calls_seen = 0

    def load(ref: CheckpointRef) -> ModelForward:
        def forward(inputs: list[object]) -> list[object]:
            nonlocal calls_seen
            calls_seen += 1
            if calls_seen == 1:
                first_entered.set()
                release_first.wait(timeout=5.0)
            return [int(str(x)) + 10 for x in inputs]

        return forward

    async def run() -> None:
        async with _open_bus(transport, load, max_batch=8) as bus:
            forwarder = _Forwarder(bus, max_batch=8, eager=False)
            forward = forwarder.bind(cp)
            match2_stepped_while_held = False

            async def match_one() -> list[object]:
                return await forward([1])

            async def match_two() -> list[object]:
                nonlocal match2_stepped_while_held
                assert await asyncio.to_thread(first_entered.wait, 5.0)
                # Another match's game code runs while match 1's forward is held.
                match2_stepped_while_held = True

                async def call_second() -> list[object]:
                    return await forward([2])

                second_task: asyncio.Task[list[object]] = asyncio.create_task(call_second())
                # Let the barrier flush match 2's call to the bus while match 1 is still held.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                release_first.set()
                return await second_task

            out1, out2 = await asyncio.gather(match_one(), match_two())
            assert match2_stepped_while_held
            assert out1 == [11]
            assert out2 == [12]

    asyncio.run(run())
    assert calls_seen == 2


def test_vectorized_example_uses_two_half_numpy_arrays_zero_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    eval_group = _load_example_group(Path("examples/vectorized.py"))
    results = hold_group(eval_group, directory=tmp_path)
    assert [r.played for r in results] == [8]
    artifacts = tmp_path / "vectorized/artifacts/agents"
    reports = [
        GameReport.model_validate_json(path.read_bytes()) for path in artifacts.glob("game-*.json")
    ]
    assert len(reports) == 1
    assert reports[0].tournaments == ("vector",)
    assert reports[0].matches == 8
    assert reports[0].imports.heavy == ()
    assert reports[0].bus.arrays > 0
    assert reports[0].bus.arrays_copied == 0
    assert reports[0].bus.copied_bytes == 0
    assert reports[0].bus.pickled == 0
    assert reports[0].bus.wire_bytes == 0


def test_vectorized_panel_reserves_work_before_a_fast_process_takes_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 8)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 8 * 1024**3)
    rounds = 0

    def first_process_twice(
        handles: Sequence[Connection | int], timeout: float | None = None
    ) -> Sequence[object]:
        nonlocal rounds
        if rounds < 2:
            rounds += 1
            first = next(handle for handle in handles if isinstance(handle, Connection))
            assert first.poll(10), "vectorized process did not request its next half"
            return (first,)
        return wait(handles, timeout=timeout)

    monkeypatch.setattr("etude.eval.run.wait", first_process_twice)
    eval_group = _load_example_group(Path("examples/vectorized.py"))
    assert [result.played for result in hold_group(eval_group, directory=tmp_path)] == [8]
    artifacts = tmp_path / "vectorized/artifacts/agents"
    reports = [
        GameReport.model_validate_json(path.read_bytes()) for path in artifacts.glob("game-*.json")
    ]
    assert [report.matches for report in reports] == [1] * 8
    assert all(
        report.bus.arrays > 0 and report.bus.arrays_copied == report.bus.wire_bytes == 0
        for report in reports
    )


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_unawaited_play_many_future_on_early_exit_emits_no_warning_and_unlocks(
    transport: _Transport,
) -> None:
    cp_bad = checkpoint("early_bad").checkpoint
    cp_slow = checkpoint("early_slow").checkpoint
    slow_entered = threading.Event()
    release_slow = threading.Event()
    loop_errors: list[dict[str, object]] = []

    def load(ref: CheckpointRef) -> ModelForward:
        if ref.key == cp_bad.key:
            raise ValueError("fixture early load failure")

        def forward(inputs: list[object]) -> list[object]:
            slow_entered.set()
            release_slow.wait(timeout=5.0)
            return ["ok" for _ in inputs]

        return forward

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))
        arr = np.arange(4, dtype=np.float32)
        async with _open_bus(transport, load, max_batch=8) as bus:
            forwarder = _Forwarder(bus, max_batch=8, eager=True)
            bad_forward = forwarder.bind(cp_bad)
            slow_forward = forwarder.bind(cp_slow)

            p1 = bad_forward([1])
            p2 = slow_forward([arr], rows=1)
            assert await asyncio.to_thread(slow_entered.wait, 5.0)
            with pytest.raises(ForwardError, match="fixture early load failure"):
                await p1
            # Exit without awaiting p2 while p2 is still in flight; drop local reference.
            del p2
            forwarder.clear_pending()
            release_slow.set()
            await bus.close()
            await asyncio.sleep(0)
            assert arr.flags.writeable
            assert any(f.checkpoint == cp_bad.key for f in forwarder.drain_loading_failures())
        gc.collect()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert not loop_errors


async def _early_exit_play_many(
    matches: Matches,
) -> AsyncIterator[tuple[Match, Tables]]:
    taken = await matches.take(2)
    assert taken
    forward = cast(Forward, taken[0][1][0])
    p1 = forward([1])
    _p2 = forward([2])
    await p1  # raises ForwardError; _p2 is abandoned unawaited
    yield taken[0][0], {"scores": [{"seat": 0, "score": 1}]}


def test_play_many_early_exit_with_unawaited_future_has_clean_stderr(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    cp = checkpoint("boom")
    matches = [Match(players=(cp,), index=index, games=1) for index in range(2)]
    with (
        pytest.raises(EtudeError, match="fixture boom"),
        evaluating(
            environment("early_exit"),
            experiment="early_exit",
            matches=matches,
            run="clean",
            directory=tmp_path,
        ) as active,
    ):
        active.play_many(_early_exit_play_many, agents=Agents(load=_raising_load, max_batch=1))
    stderr = capfd.readouterr().err
    assert "Future exception was never retrieved" not in stderr


class _ExplodingUnpickle:
    def __getstate__(self) -> dict[str, bool]:
        return {"trigger": True}

    def __setstate__(self, state: dict[str, bool]) -> None:
        raise RuntimeError("fixture decode error https://user:topsecret@example.com")


@pytest.mark.parametrize("transport", ["local", "socket"])
def test_unpicklable_reply_fails_fast_redacts_secrets_and_unlocks_arrays(
    transport: _Transport,
) -> None:
    cp = checkpoint("bad_reply").checkpoint

    def load(ref: CheckpointRef) -> ModelForward:
        return lambda inputs: [_ExplodingUnpickle() for _ in inputs]

    async def run() -> None:
        async with _open_bus(transport, load) as bus:
            bus.bind(cp)
            arr = np.arange(4, dtype=np.float32)
            [fut] = bus.submit([ForwardCall(cp.key, [arr], 1)])
            with pytest.raises(ForwardError) as info:
                await asyncio.wait_for(fut, timeout=5.0)
            assert info.value.checkpoint == cp.key
            assert not info.value.retry
            message = str(info.value)
            assert "fixture decode error" in message
            assert "topsecret" not in message
            assert arr.flags.writeable

    asyncio.run(run())


def test_unpicklable_request_disconnects_only_its_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr("etude.eval.resources._detect_accelerator", lambda: "cuda")
    definition = Agents(load=_identity_load).plain(None)
    placement = place((TournamentQueue(2, streaming=True),), loader=definition, checkpoints=True)
    cp = checkpoint("bad_request").checkpoint
    other = checkpoint("other_request").checkpoint

    async def run(address: str) -> None:
        healthy = await SocketForwardBus.connect(address)
        broken = await SocketForwardBus.connect(address)
        try:
            healthy.bind(cp)
            broken.bind(cp)
            broken.bind(other)
            [before] = healthy.submit([ForwardCall(cp.key, [1], 1)])
            assert await asyncio.wait_for(before, timeout=5) == [1]
            arr = np.arange(4, dtype=np.float32)
            bad, pending = broken.submit(
                [ForwardCall(cp.key, [_ExplodingUnpickle()], 1), ForwardCall(other.key, [arr], 1)]
            )
            for key, future in ((cp.key, bad), (other.key, pending)):
                with pytest.raises(ForwardError, match="agent request failed to decode") as info:
                    await asyncio.wait_for(future, timeout=5)
                assert info.value.checkpoint == key and not info.value.retry
                assert "topsecret" not in str(info.value)
            assert arr.flags.writeable
            [later] = broken.submit([ForwardCall(other.key, [3], 1)])
            with pytest.raises(ForwardError, match="agent request failed to decode") as info:
                await later
            assert info.value.checkpoint == other.key and not info.value.retry
            [after] = healthy.submit([ForwardCall(cp.key, [2], 1)])
            assert await asyncio.wait_for(after, timeout=5) == [2]
        finally:
            await broken.close()
            await healthy.close()

    with resources(tmp_path) as owned, running_agents(definition, None, owned, placement) as agent:
        asyncio.run(run(agent.address))
    stderr = capfd.readouterr().err
    assert "agent request failed to decode: " in stderr
    assert "fixture decode error" in stderr
    assert "topsecret" not in stderr


async def _broken_request_play(match: Match, agents: Sequence[Forward]) -> Tables:
    await agents[0]([_ExplodingUnpickle()])
    return {"scores": [{"seat": 0, "score": 0}]}


def test_socket_decode_error_stops_only_its_tournament(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 3)
    monkeypatch.setattr("etude.eval.resources.memory_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr("etude.eval.resources._detect_accelerator", lambda: "cuda")
    agents = Agents(load=_identity_load)
    broken = job("broken", agents, _broken_request_play)
    healthy = job("healthy", agents)
    with pytest.raises(EtudeError, match="agent request failed to decode"):
        hold_group(group(broken, healthy), directory=tmp_path)

    db = statsdb("grouped", tmp_path)
    try:
        assert db.sql("select state from runs order by state").fetchall() == [
            ("failed",),
            ("finished",),
        ]
        assert db.sql("select retry from matches where state = 'failed'").fetchall() == [(False,)]
        assert db.sql("select score from scores").fetchall() == [(1,), (1,)]
    finally:
        db.close()
