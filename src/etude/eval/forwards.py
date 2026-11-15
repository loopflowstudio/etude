"""Checkpoint forwards shared by direct CPU calls and the GPU batching process.

Forwards owns loading, failed-loader caching, byte charging and eviction.
LocalAgents binds those same forwards to a game's failure scope without transport.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from etude.errors import RetryRequested, explain
from etude.eval.agents import AgentFailure, Answer, Forward

if TYPE_CHECKING:
    from etude.call import Function
    from etude.checkpoints import CheckpointRef


class Forwards:
    def __init__(self, load: Callable[..., object], memory: int) -> None:
        self.load = load
        self.memory = memory
        self.loaded: OrderedDict[str, tuple[Forward, int]] = OrderedDict()
        self.failed: dict[str, Answer] = {}
        self.loads = 0
        self.calls = 0
        self.inputs = 0
        self.load_seconds = 0.0
        self.forward_seconds = 0.0

    def answer(
        self, checkpoint: CheckpointRef, inputs: list[object], protected: Set[str] = frozenset()
    ) -> Answer:
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
                    self.failed[key] = Answer(
                        [],
                        f"load failed for {key}: {explain(error)}",
                        isinstance(error, RetryRequested),
                    )
                    raise
                finally:
                    self.load_seconds += time.monotonic() - started
                self.loaded[key] = (cast(Forward, forward), length)
                self.loads += 1
            self.loaded.move_to_end(key)
            started = time.monotonic()
            self.calls += 1
            self.inputs += len(inputs)
            try:
                outputs = list(self.loaded[key][0](inputs))
            finally:
                self.forward_seconds += time.monotonic() - started
            if len(outputs) != len(inputs):
                raise ValueError("forward must return one output per input")
            return Answer(outputs)
        except Exception as error:
            return self.failed.get(key) or Answer(
                [], f"agent {key}: {explain(error)}", isinstance(error, RetryRequested)
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


@dataclass(frozen=True)
class _LocalForward:
    agents: LocalAgents
    checkpoint: CheckpointRef

    def __call__(self, inputs: list[object]) -> list[object]:
        if not inputs:
            return []
        answer = self.agents.forwards.answer(self.checkpoint, inputs)
        if answer.error is not None:
            self.agents.failures.setdefault(
                self.checkpoint.key,
                AgentFailure(
                    checkpoint=self.checkpoint.key,
                    tournament=self.agents.tournament,
                    match=self.agents.match,
                    error=answer.error,
                    retry=answer.retry,
                    loading=self.checkpoint.key in self.agents.forwards.failed,
                ),
            )
        return answer.result()


class LocalAgents:
    """One game process's loaded checkpoints; per-match observations reset at begin."""

    def __init__(self, load: Function, checkout: str | None, memory: int) -> None:
        self.load = load
        self.checkout = checkout
        self.memory = memory
        self._function: Callable[..., object] | None = None
        self.forwards = Forwards(self._load_checkpoint, memory)
        self.failures: dict[str, AgentFailure] = {}
        self.tournament = ""
        self.match = ""
        self._loads = self._calls = self._inputs = 0
        self._load_seconds = self._forward_seconds = 0.0

    def _load_checkpoint(self, checkpoint: CheckpointRef) -> object:
        if self._function is None:
            self._function = self.load.load(self.checkout)
        return self._function(checkpoint)

    def begin(self, tournament: str, match: str) -> None:
        self.tournament, self.match = tournament, match
        self.failures.clear()
        self._loads, self._calls = self.forwards.loads, self.forwards.calls
        self._inputs = self.forwards.inputs
        self._load_seconds = self.forwards.load_seconds
        self._forward_seconds = self.forwards.forward_seconds

    def bind(self, checkpoint: CheckpointRef, player: str) -> Forward:
        return _LocalForward(self, checkpoint)

    def check(self) -> None:
        for failure in self.failures.values():
            Answer([], failure.error, failure.retry).result()

    @property
    def loads(self) -> int:
        return self.forwards.loads - self._loads

    @property
    def calls(self) -> int:
        return self.forwards.calls - self._calls

    @property
    def load_seconds(self) -> float:
        return self.forwards.load_seconds - self._load_seconds

    @property
    def forward_seconds(self) -> float:
        return self.forwards.forward_seconds - self._forward_seconds

    @property
    def inputs(self) -> int:
        return self.forwards.inputs - self._inputs
