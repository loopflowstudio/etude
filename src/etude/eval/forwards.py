"""Checkpoint forwards shared by CPU game processes and the GPU batching process.

Forwards owns the row limit, loading, failed-loader caching and byte-based eviction.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from etude.errors import RetryRequested, explain
from etude.eval.agents import ForwardError, ForwardResult, ModelForward

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef


@dataclass(frozen=True)
class ForwardBatch:
    rows: int
    calls: int
    loads: int
    forward_seconds: float
    load_seconds: float


@dataclass(frozen=True)
class ForwardAnswer:
    result: ForwardResult
    batches: list[ForwardBatch]


class Forwards:
    def __init__(self, load: Callable[..., object], memory: int, max_batch: int) -> None:
        self.load = load
        self.memory = memory
        self.max_batch = max_batch
        self.loaded: OrderedDict[str, tuple[ModelForward, int]] = OrderedDict()
        self.failed: dict[str, ForwardError] = {}
        self.loads = 0
        self.calls = 0
        self.rows = 0
        self.load_seconds = 0.0
        self.forward_seconds = 0.0

    def answer(
        self,
        checkpoint: CheckpointRef,
        inputs: list[object],
        protected: Set[str],
        *,
        rows: int,
    ) -> ForwardAnswer:
        """Split an oversized row-wise call, preserving its result order."""
        # Opaque pre-collated inputs have no Etude-owned row slicing operation.
        if rows <= self.max_batch or rows != len(inputs):
            return self._forward(checkpoint, inputs, protected, rows=rows)

        outputs: list[object] = []
        batches: list[ForwardBatch] = []
        for start in range(0, rows, self.max_batch):
            stop = start + self.max_batch
            chunk = inputs[start:stop]
            keep = protected | {checkpoint.key} if stop < rows else protected
            reply = self._forward(checkpoint, chunk, keep, rows=len(chunk))
            batches.extend(reply.batches)
            if isinstance(reply.result, ForwardError):
                if stop < rows:
                    self._evict(0, protected)
                return ForwardAnswer(reply.result, batches)
            outputs.extend(reply.result)
        return ForwardAnswer(outputs, batches)

    def _forward(
        self, checkpoint: CheckpointRef, inputs: list[object], protected: Set[str], *, rows: int
    ) -> ForwardAnswer:
        calls, loads = self.calls, self.loads
        forward_seconds, load_seconds = self.forward_seconds, self.load_seconds
        result = self._answer(checkpoint, inputs, protected, rows=rows)
        return ForwardAnswer(
            result,
            [
                ForwardBatch(
                    rows=rows,
                    calls=self.calls - calls,
                    loads=self.loads - loads,
                    forward_seconds=self.forward_seconds - forward_seconds,
                    load_seconds=self.load_seconds - load_seconds,
                )
            ],
        )

    def _answer(
        self, checkpoint: CheckpointRef, inputs: list[object], protected: Set[str], *, rows: int
    ) -> ForwardResult:
        key = checkpoint.key
        if key in self.failed:
            return self.failed[key]
        try:
            if key not in self.loaded:
                length = checkpoint.artifact.size_bytes
                if length is None:
                    raise ValueError(f"checkpoint {key} has no byte length")
                self._evict(length, protected)
                started = time.monotonic()
                try:
                    forward = self.load(checkpoint)
                    if not callable(forward):
                        raise TypeError("load must return a callable batched forward")
                except Exception as error:
                    self.failed[key] = ForwardError(
                        key,
                        f"load failed for {key}: {explain(error)}",
                        retry=isinstance(error, RetryRequested),
                        loading=True,
                    )
                    return self.failed[key]
                finally:
                    self.load_seconds += time.monotonic() - started
                self.loaded[key] = (cast(ModelForward, forward), length)
                self.loads += 1
            self.loaded.move_to_end(key)
            started = time.monotonic()
            self.calls += 1
            self.rows += rows
            try:
                outputs = list(self.loaded[key][0](inputs))
            finally:
                self.forward_seconds += time.monotonic() - started
            if len(outputs) != len(inputs):
                raise ValueError("forward must return one output per input")
            return outputs
        except Exception as error:
            return ForwardError(
                key,
                f"agent {key}: {explain(error)}",
                retry=isinstance(error, RetryRequested),
            )
        finally:
            self._evict(0, protected)

    def _evict(self, incoming: int, protected: Set[str]) -> None:
        used = sum(size for _, size in self.loaded.values())
        for key in list(self.loaded):
            if used + incoming <= self.memory:
                break
            if key not in protected:
                _, size = self.loaded.pop(key)
                used -= size
