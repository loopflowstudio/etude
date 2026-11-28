"""Checkpoint forwards shared by CPU game processes and the GPU batching process.

Forwards owns loading, failed-loader caching, byte charging and eviction.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Set
from typing import TYPE_CHECKING, cast

from etude.errors import RetryRequested, explain
from etude.eval.agents import ForwardError, ForwardResult, ModelForward

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef


class Forwards:
    def __init__(self, load: Callable[..., object], memory: int) -> None:
        self.load = load
        self.memory = memory
        self.loaded: OrderedDict[str, tuple[ModelForward, int]] = OrderedDict()
        self.failed: dict[str, ForwardError] = {}
        self.loads = 0
        self.calls = 0
        self.inputs = 0
        self.load_seconds = 0.0
        self.forward_seconds = 0.0

    def answer(
        self,
        checkpoint: CheckpointRef,
        inputs: list[object],
        protected: Set[str],
        *,
        rows: int,
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
            self.inputs += rows
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
