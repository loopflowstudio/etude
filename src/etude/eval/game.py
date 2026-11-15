"""Load a game's defining module, validate its match and report startup imports.

The parent sends a Function and a portable Match; the game process imports only the
play module it names. `initialize_game` starts each game process: imports are
measured from there, before that module loads, so a warning can name the code
that pulled training libraries into game processes.
"""

import builtins
import inspect
import os
import sys
import time
import typing
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import cast

from etude.call import Function
from etude.eval.agents import AgentConnection, connect_agents
from etude.eval.composition import build_agents
from etude.eval.records import Match
from etude.eval.resources import THREAD_VARIABLES
from etude.eval.result import Result, Tables
from etude.records import Record

MAX_GAME_IMPORTS = 256
_HEAVY = frozenset({"torch", "jax", "tensorflow"})


_startup_modules: frozenset[str] | None = None
_first_importer: str | None = None


def initialize_game() -> None:
    """Measure imports before the game process unpickles a match or agent address."""
    global _startup_modules
    for variable in THREAD_VARIABLES:
        os.environ[variable] = "1"
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
    connection: AgentConnection, seconds: float, tournament: str, match: str
) -> Performance:
    return Performance(
        tournament=tournament,
        match=match,
        requests=connection.requests,
        answers=connection.answers,
        request_bytes=connection.request_bytes,
        answer_bytes=connection.answer_bytes,
        round_trip_seconds=connection.round_trip_seconds,
        transport_seconds=connection.transport_seconds,
        game_seconds=max(0.0, seconds - connection.round_trip_seconds),
        latency_buckets=tuple(connection.latencies),
        round_trip_p50_seconds=_percentile(connection.latencies, 50),
        round_trip_p95_seconds=_percentile(connection.latencies, 95),
    )


@dataclass(frozen=True)
class Execution:
    result: Tables | Result
    seconds: float
    imports: Imports
    performance: Performance | None = None


@lru_cache(maxsize=128)
def _match_type(play: Callable[..., object], arguments: int = 0) -> type[Match]:
    parameters = list(inspect.signature(play).parameters.values())
    if len(parameters) != 1 + arguments or any(
        p.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for p in parameters
    ):
        raise TypeError(
            "play must take one Match parameter"
            + (" and agents" if arguments else " without agents=")
        )
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
    play_started: float | None = None
    play_seconds = 0.0
    started = time.monotonic()
    try:
        play = cast(Callable[..., Tables | Result], function.load(checkout))
        kind = _match_type(play, int(agents is not None))
        checked = kind.model_validate_json(match.model_dump_json()) if kind is not Match else match
        if agents is None:
            result = play(checked)
        else:
            with connect_agents(agents) as connection:
                play_started = time.monotonic()
                players = build_agents(
                    match.players, connection, tournament, match.key, build, checkout
                )
                result = play(checked, players)
                play_seconds = time.monotonic() - play_started
        return Execution(
            result=result,
            seconds=time.monotonic() - started,
            imports=observed(),
            performance=None
            if connection is None
            else _performance(connection, play_seconds, tournament, match.key),
        )
    except Exception as error:
        # Exception dictionaries survive ProcessPoolExecutor transport; keep the real error type.
        setattr(error, "_etude_imports", observed().model_dump_json())
        if connection is not None:
            seconds = 0.0 if play_started is None else time.monotonic() - play_started
            setattr(
                error,
                "_etude_performance",
                _performance(connection, seconds, tournament, match.key).model_dump_json(),
            )
        raise
