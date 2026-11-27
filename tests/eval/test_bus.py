"""Contract, counting and equivalence tests for `ForwardBus` transports."""

from __future__ import annotations

import asyncio
import gc
import os
import pickle
import select
import selectors
import socket
import sys
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

import numpy as np
import pytest

from etude.checkpoints import CheckpointRef
from etude.controller.declarations import asked, read_experiment
from etude.errors import EtudeError, RetryRequested
from etude.eval.agents import (
    BatchRequest,
    Forward,
    ForwardError,
    ModelForward,
    SocketForwardBus,
    recv_frame,
    send_frame,
)
from etude.eval.batching import LocalForwardBus, _Batches
from etude.eval.bus import BusReport, ForwardBus, ForwardCall
from etude.eval.composition import build_agents
from etude.eval.game import GameReport, _Forwarder
from etude.eval.match import environment
from etude.eval.query import statsdb
from etude.eval.records import Match, compose
from etude.eval.result import Tables
from etude.eval.tournament import evaluating
from etude.jobs import Agents, EvalGroup, ExperimentState, evaluation_group, hold_group
from tests.eval import mirror as mirror_panel
from tests.eval.arithmetic import checkpoint
from tests.test_eval_groups import group, job

type _Transport = Literal["local", "socket"]


def _serve_socket(
    address: str,
    load: Callable[[CheckpointRef], object],
    max_batch: int,
    memory: int,
    ready: threading.Event,
    stop_read: int,
) -> None:
    batches: _Batches[memoryview] = _Batches(load, max_batch, memory, encode=ForkingPickler.dumps)
    peers: list[socket.socket] = []

    def send_to(sock: socket.socket) -> Callable[[memoryview], None]:
        def send(payload: memoryview) -> None:
            try:
                send_frame(sock, payload)
            except (OSError, EOFError):
                pass

        return send

    try:
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
            selectors.DefaultSelector() as selector,
        ):
            listener.bind(address)
            listener.listen()
            selector.register(listener, selectors.EVENT_READ)
            selector.register(stop_read, selectors.EVENT_READ)
            ready.set()
            while True:
                events = selector.select(0.0 if batches.pending else None)
                for event, _ in events:
                    if event.fileobj == stop_read:
                        return
                    if event.fileobj is listener:
                        peer, _ = listener.accept()
                        peers.append(peer)
                        selector.register(peer, selectors.EVENT_READ)
                    else:
                        peer = cast(socket.socket, event.fileobj)
                        while True:
                            try:
                                payload = recv_frame(peer)
                                request = cast(BatchRequest, ForkingPickler.loads(payload))
                            except (EOFError, OSError):
                                selector.unregister(peer)
                                peer.close()
                                peers.remove(peer)
                                break
                            else:
                                batches.add_batch(send_to(peer), request)
                            if not select.select([peer], [], [], 0.0)[0]:
                                break
                batches.flush()
    finally:
        for peer in peers:
            peer.close()
        os.close(stop_read)


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
        ready = threading.Event()
        stop_read, stop_write = os.pipe()
        server = threading.Thread(
            target=_serve_socket,
            args=(address, load, max_batch, memory, ready, stop_read),
            daemon=True,
        )
        server.start()
        ready.wait(timeout=5.0)
        bus = await SocketForwardBus.connect(address)
        try:
            yield bus
        finally:
            await bus.close()
            os.write(stop_write, b"x")
            os.close(stop_write)
            server.join(timeout=5.0)


class _LazyBuffer:
    """Buffer-protocol exporter that records the thread that first reads it."""

    def __init__(self, data: np.ndarray) -> None:
        self._data = data
        self.read_threads: list[int] = []

    def __buffer__(self, flags: int) -> memoryview:
        self.read_threads.append(threading.get_ident())
        return memoryview(self._data)


class _CpuDlpack:
    """CPU DLPack exporter without the buffer protocol."""

    def __init__(self, data: np.ndarray) -> None:
        self._data = data

    def __dlpack__(self, stream: int | None = None) -> object:
        return self._data.__dlpack__(stream=stream)

    def __dlpack_device__(self) -> tuple[int, int]:
        return self._data.__dlpack_device__()


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
            # Gate first forward so the second and third queue together and merge.
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
    lazy_outputs: list[_LazyBuffer] = []
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
            a = _LazyBuffer(np.array([10.0, 20.0], dtype=np.float32))
            b = _LazyBuffer(np.array([30.0], dtype=np.float32))
            c = _LazyBuffer(np.array([40.0, 50.0], dtype=np.float32))
            d = _LazyBuffer(np.array([60.0], dtype=np.float32))
            lazy_outputs.extend([a, b, c, d])
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
            assert isinstance(logits_t, np.ndarray) and np.array_equal(
                logits_t, np.array([10.0, 20.0], dtype=np.float32)
            )
            assert isinstance(value_t, np.ndarray) and np.array_equal(
                value_t, np.array([30.0], dtype=np.float32)
            )
            assert isinstance(mapping["logits"], np.ndarray) and np.array_equal(
                mapping["logits"], np.array([40.0, 50.0], dtype=np.float32)
            )
            assert isinstance(mapping["value"], np.ndarray) and np.array_equal(
                mapping["value"], np.array([60.0], dtype=np.float32)
            )

    asyncio.run(run())
    assert input_checks == [True]
    assert len(model_threads) == 1
    assert all(lazy.read_threads == [model_threads[0]] for lazy in lazy_outputs)
    assert "torch" not in sys.modules
    assert "mlx" not in sys.modules


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
            with pytest.raises((pickle.PicklingError, AttributeError, TypeError)):
                await bad_fut
            assert arr.flags.writeable

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
            # Poll the event loop briefly without sleeping long; completion unlocks arr.
            for _ in range(200):
                if arr.flags.writeable:
                    break
                await asyncio.sleep(0.005)
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
    monkeypatch.setattr("etude.eval.resources._has_gpu", lambda: transport == "socket")
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
    monkeypatch.setattr("etude.eval.resources._has_gpu", lambda: transport == "socket")
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
        monkeypatch.setattr("etude.eval.resources._has_gpu", lambda t=transport: t == "socket")
        monkeypatch.setattr(
            "etude.eval.resources.available_cpus", lambda t=transport: 2 if t == "socket" else 1
        )
        root = tmp_path / f"mirror-{transport}"
        selected = job(
            "mirror", Agents(load=mirror_panel.load, build=mirror_panel.build)
        ).model_copy(
            update={"play": mirror_panel.play, "enter": (compose(mirror), compose(greedy))}
        )
        assert hold_group(group(selected), directory=root)[0].played == 1
        db = statsdb("grouped", root)
        try:
            mirror_results[transport] = db.sql(
                "select match, seat, decision, action from choices order by match, decision, seat"
            ).fetchall()
        finally:
            db.close()
    assert mirror_results["local"] == mirror_results["socket"]
    assert len(mirror_results["local"]) == 4

    # 2. examples/agents.py under local vs socket.
    definition = read_experiment(Path("examples/agents.py"))
    assert definition.eval is not None
    state = ExperimentState(experiment=definition.name)
    agent_jobs = asked(definition.eval, state)
    eval_group = EvalGroup(
        evaluations=tuple(
            spec for each in agent_jobs for spec in evaluation_group(each.lower(state)).evaluations
        )
    )
    example_results: dict[_Transport, list[tuple[object, ...]]] = {}
    for transport in transports:
        monkeypatch.setattr("etude.eval.resources._has_gpu", lambda t=transport: t == "socket")
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
            stepped_while_held = True
            pending_b = forward([b], rows=2)
            pending_c = forward([c], rows=2)
            assert stepped_while_held
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
    monkeypatch.setattr("etude.eval.resources._has_gpu", lambda: False)
    monkeypatch.setattr("etude.eval.resources.available_cpus", lambda: 1)
    definition = read_experiment(Path("examples/vectorized.py"))
    assert definition.eval is not None
    state = ExperimentState(experiment=definition.name)
    vector_jobs = asked(definition.eval, state)
    eval_group = EvalGroup(
        evaluations=tuple(
            spec for each in vector_jobs for spec in evaluation_group(each.lower(state)).evaluations
        )
    )
    results = hold_group(eval_group, directory=tmp_path)
    assert [r.played for r in results] == [8]
    artifacts = tmp_path / "vectorized/artifacts/agents"
    reports = [
        GameReport.model_validate_json(path.read_bytes()) for path in artifacts.glob("game-*.json")
    ]
    assert len(reports) == 1
    assert reports[0].matches == 8
    assert reports[0].bus.arrays > 0
    assert reports[0].bus.arrays_copied == 0
    assert reports[0].bus.copied_bytes == 0
    assert reports[0].bus.pickled == 0
    assert reports[0].bus.wire_bytes == 0
