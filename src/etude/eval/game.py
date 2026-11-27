"""Load a game's defining module, validate its match and report startup imports.

The parent sends a Function and a portable Match; the game process imports only the
play module it names. `initialize_game` starts each game process: imports are
measured from there, before that module loads, so a warning can name the code
that pulled training libraries into game processes.
"""

from __future__ import annotations

import builtins
import inspect
import os
import sys
import time
import typing
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import TYPE_CHECKING, cast

from etude.call import Function
from etude.eval.agents import (
    LATENCY_BUCKETS,
    AgentConnection,
    Forward,
    _RemoteForward,
    connect_agents,
)
from etude.eval.composition import build_agents
from etude.eval.records import Match
from etude.eval.resources import Resources, agent_bytes
from etude.eval.result import Result, Tables
from etude.records import Record

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef
    from etude.eval.forwards import LocalAgents

_local_agents: LocalAgents | None = None

MAX_GAME_IMPORTS = 256
_HEAVY = frozenset({"torch", "jax", "tensorflow", "mlx"})


_startup_modules: frozenset[str] | None = None
_first_importer: str | None = None


def initialize_game(resources: Resources) -> None:
    """Measure imports before the game process unpickles a match or agent address."""
    global _startup_modules
    resources.configure(1)
    _startup_modules = frozenset(sys.modules)
    original = builtins.__import__

    def importing(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        global _first_importer
        if _first_importer is None and name.split(".", 1)[0] in _HEAVY:
            _first_importer = str((globals or {}).get("__name__", "__main__"))
        return original(name, globals, locals, fromlist, level)

    builtins.__import__ = importing


class Imports(Record):
    pid: int
    module: str
    count: int
    total: int
    heavy: tuple[str, ...]
    importer: str | None

    @property
    def warning(self) -> str | None:
        if not self.heavy and self.count <= MAX_GAME_IMPORTS:
            return None
        reason = (
            f"imported {', '.join(self.heavy)}" if self.heavy else f"loaded {self.count} modules"
        )
        return (
            f"etude: game module {self.module}: {reason} via {self.importer or self.module}; "
            "define play in a lightweight module and move the import to load's module"
        )


class Performance(Record):
    """A match's measured work; bucket counts can be summed across matches."""

    tournament: str
    match: str
    requests: int
    answers: int
    request_bytes: int
    answer_bytes: int
    round_trip_seconds: float
    transport_seconds: float
    game_seconds: float
    latency_buckets: tuple[int, ...]
    round_trip_p50_seconds: float | None
    round_trip_p95_seconds: float | None
    # Same-host monotonic coordinates allow concurrent play intervals to be overlaid.
    # Released reports lack them; a killed process may leave no interval at all.
    started_monotonic: float | None = None
    finished_monotonic: float | None = None
    local_inputs: int = 0
    local_forward_calls: int = 0
    local_loads: int = 0
    local_forward_seconds: float = 0.0
    local_load_seconds: float = 0.0


def _percentile(buckets: Sequence[int], percent: int) -> float | None:
    count = sum(buckets)
    if not count:
        return None
    target = (count * percent + 99) // 100
    seen = 0
    for index, size in enumerate(buckets):
        seen += size
        if seen >= target:
            return 2.0**index / 1_000_000
    raise AssertionError("latency histogram lost observations")


def _performance(
    connection: AgentConnection | None,
    started: float,
    finished: float,
    tournament: str,
    match: str,
    local: LocalAgents | None = None,
) -> Performance:
    round_trips = connection.round_trip_seconds if connection else 0.0
    buckets = tuple(connection.latencies) if connection else (0,) * LATENCY_BUCKETS
    return Performance(
        tournament=tournament,
        match=match,
        requests=connection.requests if connection else 0,
        answers=connection.answers if connection else 0,
        request_bytes=connection.request_bytes if connection else 0,
        answer_bytes=connection.answer_bytes if connection else 0,
        round_trip_seconds=round_trips,
        transport_seconds=connection.transport_seconds if connection else 0.0,
        game_seconds=max(
            0.0,
            finished
            - started
            - round_trips
            - (local.forward_seconds + local.load_seconds if local else 0.0),
        ),
        latency_buckets=buckets,
        round_trip_p50_seconds=_percentile(buckets, 50),
        round_trip_p95_seconds=_percentile(buckets, 95),
        started_monotonic=started,
        finished_monotonic=finished,
        local_inputs=local.inputs if local else 0,
        local_forward_calls=local.calls if local else 0,
        local_loads=local.loads if local else 0,
        local_forward_seconds=local.forward_seconds if local else 0.0,
        local_load_seconds=local.load_seconds if local else 0.0,
    )


@dataclass(frozen=True)
class Execution:
    result: Tables | Result
    seconds: float
    imports: Imports
    performance: Performance


@lru_cache(maxsize=128)
def _match_type(play: Callable[..., object]) -> type[Match]:
    parameters = list(inspect.signature(play).parameters.values())
    if len(parameters) != 2 or any(
        p.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for p in parameters
    ):
        raise TypeError("play must take match and agents")
    kind = typing.get_type_hints(play).get(parameters[0].name, Match)
    if not isinstance(kind, type) or not issubclass(kind, Match):
        raise TypeError("play's parameter must be annotated with Match[YourConfig]")
    return kind


def play_match(
    function: Function,
    match: Match,
    agents: str | None = None,
    checkout: str | None = None,
    tournament: str = "",
    build: Function | None = None,
    load: Function | None = None,
    memory: int | None = None,
) -> Execution:
    if _startup_modules is None:
        raise RuntimeError("initialize_game must start the game process")
    before = _startup_modules

    def observed() -> Imports:
        return Imports(
            pid=os.getpid(),
            module=function.module,
            count=len(set(sys.modules) - before),
            total=len(sys.modules),
            heavy=tuple(sorted(_HEAVY.intersection(sys.modules))),
            importer=_first_importer,
        )

    connection: AgentConnection | None = None
    local: LocalAgents | None = None
    play_started: float | None = None
    started = time.monotonic()
    try:
        if load is not None:
            # Process entry point: only CPU games import the local checkpoint owner.
            from etude.eval import forwards  # noqa: PLC0415

            global _local_agents
            allowance = agent_bytes(None) if memory is None else memory
            if _local_agents is None or (
                _local_agents.load,
                _local_agents.checkout,
                _local_agents.memory,
            ) != (load, checkout, allowance):
                _local_agents = forwards.LocalAgents(load, checkout, allowance)
            local = _local_agents
            local.begin(tournament, match.key)
        play = cast(Callable[..., Tables | Result], function.load(checkout))
        kind = _match_type(play)
        checked = kind.model_validate_json(match.model_dump_json()) if kind is not Match else match
        with connect_agents(agents) if agents is not None else nullcontext(None) as connection:
            play_started = time.monotonic()
            bind: Callable[[CheckpointRef, str], Forward] | None = None
            if local is not None:
                bind = local.bind
            elif connection is not None:
                channel = connection

                def remote(checkpoint: CheckpointRef, player: str) -> Forward:
                    return _RemoteForward(channel, checkpoint, tournament, match.key, player)

                bind = remote
            players = build_agents(match.players, bind, build, checkout)
            result = play(checked, players)
            if local is not None:
                local.check()
            play_finished = time.monotonic()
        return Execution(
            result=result,
            seconds=time.monotonic() - started,
            imports=observed(),
            performance=_performance(
                connection, play_started, play_finished, tournament, match.key, local
            ),
        )
    except Exception as error:
        if local is not None and local.failures:
            setattr(error, "_etude_agent_failures", tuple(local.failures.values()))
        # Exception dictionaries survive ProcessPoolExecutor transport; keep the real error type.
        setattr(error, "_etude_imports", observed().model_dump_json())
        if play_started is not None:
            finished = time.monotonic()
            setattr(
                error,
                "_etude_performance",
                _performance(
                    connection, play_started, finished, tournament, match.key, local
                ).model_dump_json(),
            )
        raise
