"""Load a game's defining module, validate its match and report startup imports.

The parent sends a Function and a portable Match; the game process imports only the
play module it names. Imports are measured before loading that module, so a
warning can name the code that pulled training libraries into game processes.
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
from etude.eval.agents import connect_agents
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


@dataclass(frozen=True)
class Execution:
    result: Tables | Result
    seconds: float
    imports: Imports


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
) -> Execution:
    before = _startup_modules if _startup_modules is not None else frozenset(sys.modules)
    first: str | None = None
    original = builtins.__import__

    def importing(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        nonlocal first
        if first is None and name.split(".", 1)[0] in _HEAVY:
            first = str((globals or {}).get("__name__", function.module))
        return original(name, globals, locals, fromlist, level)

    builtins.__import__ = importing

    def observed() -> Imports:
        return Imports(
            pid=os.getpid(),
            module=function.module,
            count=len(set(sys.modules) - before),
            total=len(sys.modules),
            heavy=tuple(sorted(_HEAVY.intersection(sys.modules))),
            importer=_first_importer or first,
        )

    started = time.monotonic()
    try:
        play = cast(Callable[..., Tables | Result], function.load(checkout))
        kind = _match_type(play, int(agents is not None))
        checked = kind.model_validate_json(match.model_dump_json()) if kind is not Match else match
        if agents is None:
            result = play(checked)
        else:
            with connect_agents(agents, match.players, tournament, match.key) as players:
                result = play(checked, players)
        return Execution(result=result, seconds=time.monotonic() - started, imports=observed())
    except Exception as error:
        # Exception dictionaries survive ProcessPoolExecutor transport; keep the real error type.
        setattr(error, "_etude_imports", observed().model_dump_json())
        raise
    finally:
        builtins.__import__ = original
