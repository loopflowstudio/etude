"""Game-process event loop, zero-delay forward barrier and match streaming.

Each spawned game process runs one `asyncio` event loop consuming a match stream
(`MatchStream[Rules]`) over its parent pipe. `play_many` consumes it directly;
Etude drives `async def play` as bounded match tasks. Both share the zero-delay
forward barrier and send each finished match to the parent for storage.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import os
import sys
import time
import typing
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing, suppress
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, field
from functools import cached_property, lru_cache, partial
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from types import ModuleType
from typing import TYPE_CHECKING, Generic, cast

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import AgentClient, Forward, ForwardError, ForwardResult
from etude.eval.composition import build_agents
from etude.eval.records import Match, _MatchConfig
from etude.eval.resources import Resources, seconds_left
from etude.eval.result import Result, Tables
from etude.records import Record

__all__ = ("MatchStream",)

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef
    from etude.eval.forwards import Forwards

MAX_GAME_IMPORTS = 256
_HEAVY = frozenset({"torch", "jax", "tensorflow", "mlx"})

# Per-match ForwardError tracker for `play` tasks so caught forward errors still fail the match.
_match_errors: ContextVar[list[ForwardError] | None] = ContextVar("_match_errors", default=None)


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
            "define your game function in a lightweight module and move the import to load's module"
        )


class GameReport(Record):
    """One game process's startup imports and measured work."""

    pid: int
    tournament: str
    imports: Imports
    matches: int
    requests: int
    answers: int
    request_bytes: int
    answer_bytes: int
    round_trip_seconds: float
    transport_seconds: float
    wall_seconds: float
    local_rows: int = 0
    local_forward_calls: int = 0
    local_loads: int = 0
    local_forward_seconds: float = 0.0
    local_load_seconds: float = 0.0


@dataclass(frozen=True)
class Take:
    count: int


@dataclass(frozen=True)
class Outcome:
    match_key: str
    result: Tables | Result | BaseException
    seconds: float
    loading_failures: tuple[ForwardError, ...] = ()


@dataclass(frozen=True)
class Done:
    report: GameReport
    error: BaseException | None = None
    loading_failures: tuple[ForwardError, ...] = ()


@dataclass(frozen=True)
class _TakenMatch:
    match: Match  # Keep the object alive while its id identifies the grant.
    scheduled_key: str
    started: float

    @cached_property
    def key(self) -> str:
        return self.match.key


class MatchStream(Generic[_MatchConfig]):  # noqa: UP046  # Python 3.12 lacks PEP 696 default syntax.
    """One game process's match grants and the seats built for them."""

    def __init__(
        self,
        pipe: Connection,
        initial: tuple[Match, ...],
        kind: type[Match[_MatchConfig]],
        bind: Callable[[CheckpointRef], Forward] | None,
        build: Callable[[object, Mapping[str, Forward]], object] | None,
        *,
        target_matches: int,
    ) -> None:
        self._pipe = pipe
        self._initial = initial
        self._kind = kind
        self._bind = bind
        self._build = build
        self._target_matches = target_matches
        self._lock = asyncio.Lock()
        self._exhausted = False
        self._held: dict[int, _TakenMatch] = {}

    @property
    def target_matches(self) -> int:
        """Suggested matches to hold at once in this process, capped at `max_batch` with a loader."""
        return self._target_matches

    async def take(self, count: int) -> list[tuple[Match[_MatchConfig], Sequence[object]]]:
        """Request up to `count` pending matches and their built seat agents."""
        if count <= 0:
            return []
        async with self._lock:
            if self._exhausted:
                return []
            if self._initial:
                granted, self._initial = self._initial[:count], self._initial[count:]
            else:
                granted = await self._request(count)
                # Pending work only shrinks. An empty grant ends requests from this stream.
                self._exhausted = not granted
        seats: list[tuple[Match[_MatchConfig], Sequence[object]]] = []
        for match in granted:
            if self._kind is Match:
                # The callback accepts the scheduled match's original config type.
                authored = cast(Match[_MatchConfig], match)
            else:
                authored = self._kind.model_validate_json(match.model_dump_json())
            # Config validation can fill defaults. Retain the parent's scheduled key.
            self._held[id(authored)] = _TakenMatch(authored, match.key, time.monotonic())
            seats.append((authored, build_agents(match.players, self._bind, self._build)))
        return seats

    async def _request(self, count: int) -> tuple[Match, ...]:
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        loop.add_reader(self._pipe.fileno(), ready.set)
        try:
            self._pipe.send(Take(count))
            await ready.wait()
            return cast(tuple[Match, ...], self._pipe.recv())
        finally:
            loop.remove_reader(self._pipe.fileno())

    def _release(self, match: Match) -> _TakenMatch:
        taken = self._held.pop(id(match), None)
        if taken is not None:
            return taken
        if isinstance(match, Match):
            identity = match.key
            matching = [key for key, held in self._held.items() if held.key == identity]
            if len(matching) == 1:
                return self._held.pop(matching[0])
        else:
            identity = type(match).__name__
        raise ValueError(
            f"play_many yielded an unknown, ambiguous or already-yielded match: {identity}"
        )


type Play = Callable[..., Awaitable[Tables | Result]]
type PlayMany = Callable[..., AsyncIterator[tuple[Match, Tables | Result | Exception]]]
type _Outcomes = AsyncGenerator[tuple[Match, Tables | Result | BaseException]]


@lru_cache(maxsize=128)
def _match_type(play: Callable[..., object], *, streaming: bool = False) -> type[Match]:
    authored = inspect.unwrap(play)
    if streaming:
        if not inspect.isasyncgenfunction(authored):
            raise TypeError("play_many must be an async def generator that yields outcomes")
    elif not inspect.iscoroutinefunction(authored):
        raise TypeError("play must be an async def function")
    parameters = list(inspect.signature(play).parameters.values())
    if len(parameters) != (1 if streaming else 2) or any(
        p.kind not in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for p in parameters
    ):
        if streaming:
            raise TypeError("play_many must take matches")
        raise TypeError("play must take match and agents")
    hints = typing.get_type_hints(authored)
    if streaming:
        source = hints.get(parameters[0].name, MatchStream)
        if source is MatchStream:
            return Match
        if typing.get_origin(source) is not MatchStream:
            raise TypeError("play_many's parameter must be annotated with MatchStream[YourConfig]")
        (config,) = typing.get_args(source)
        return Match[config]  # type: ignore[valid-type]  # Specialize from the author's annotation.
    kind = hints.get(parameters[0].name, Match)
    if not isinstance(kind, type) or not issubclass(kind, Match):
        raise TypeError("play's parameter must be annotated with Match[YourConfig]")
    return kind


@dataclass
class _ForwardBatch:
    """Calls for one checkpoint in one loop turn, with their ordered reply slices."""

    checkpoint: CheckpointRef
    inputs: list[object] = field(default_factory=list)
    rows: int = 0
    replies: list[tuple[int, asyncio.Future[list[object]]]] = field(default_factory=list)

    def add(self, inputs: list[object], rows: int) -> asyncio.Future[list[object]]:
        future: asyncio.Future[list[object]] = asyncio.get_running_loop().create_future()
        self.inputs.extend(inputs)
        self.rows += rows
        self.replies.append((len(inputs), future))
        return future

    def deliver(self, result: list[object] | BaseException) -> None:
        offset = 0
        for size, future in self.replies:
            if not future.done():
                if isinstance(result, BaseException):
                    # Each game owns the traceback and attributes of its ForwardError.
                    future.set_exception(
                        copy(result) if isinstance(result, ForwardError) else result
                    )
                else:
                    future.set_result(result[offset : offset + size])
            offset += size


class _Forwarder:
    """Zero-delay barrier batching all waiting checkpoint forwards in one game process."""

    def __init__(self, inference: AgentClient | Forwards, max_batch: int) -> None:
        self.inference = inference
        self.max_batch = max_batch
        self._handles: dict[str, Forward] = {}
        self._pending: dict[str, list[_ForwardBatch]] = {}
        self._failed_loads: set[str] = set()
        self._loading_failures: list[ForwardError] = []

    def drain_loading_failures(self) -> tuple[ForwardError, ...]:
        failures = tuple(self._loading_failures)
        self._loading_failures.clear()
        return failures

    def bind(self, checkpoint: CheckpointRef) -> Forward:
        if checkpoint.key not in self._handles:
            self._handles[checkpoint.key] = partial(self._forward, checkpoint)
        return self._handles[checkpoint.key]

    async def _forward(
        self, checkpoint: CheckpointRef, inputs: list[object], *, rows: int | None = None
    ) -> list[object]:
        if not inputs:
            if rows:
                raise ValueError("rows must be 0 or omitted for empty inputs")
            return []
        count = len(inputs) if rows is None else rows
        if count < 1:
            raise ValueError("rows must be positive for non-empty inputs")
        # The first call schedules the flush behind games already ready to run.
        if not self._pending:
            asyncio.get_running_loop().call_soon(self._flush)
        batches = self._pending.setdefault(checkpoint.key, [])
        if not batches or batches[-1].rows + count > self.max_batch:
            batches.append(_ForwardBatch(checkpoint))
        future = batches[-1].add(inputs, count)
        try:
            return await asyncio.wait_for(future, timeout=seconds_left())
        except TimeoutError as error:
            raise RetryRequested("agent response exceeded the evaluation deadline") from error
        except ForwardError as error:
            tracker = _match_errors.get()
            if tracker is not None:
                tracker.append(error)
            raise

    def _deliver(self, batch: _ForwardBatch, result: ForwardResult) -> None:
        if (
            isinstance(result, ForwardError)
            and result.loading
            and result.checkpoint not in self._failed_loads
        ):
            self._failed_loads.add(result.checkpoint)
            self._loading_failures.append(result)
        batch.deliver(result)

    def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        batches = [batch for group in pending.values() for batch in group]
        if not isinstance(self.inference, AgentClient):
            for batch in batches:
                result = self.inference.answer(
                    batch.checkpoint, batch.inputs, pending.keys(), rows=batch.rows
                )
                self._deliver(batch, result.result)
            return

        try:
            futures = self.inference.send_batch([(b.checkpoint, b.inputs, b.rows) for b in batches])
        except Exception as error:
            for batch in batches:
                batch.deliver(error)
            return
        for batch, future in zip(batches, futures, strict=True):
            future.add_done_callback(partial(self._answer, batch))

    def _answer(self, batch: _ForwardBatch, future: asyncio.Future[ForwardResult]) -> None:
        try:
            result = future.result()
        except BaseException as error:
            batch.deliver(error)
        else:
            self._deliver(batch, result)


def _portable_error(error: BaseException) -> BaseException:
    try:
        ForkingPickler.dumps(error)
        return error
    except Exception:
        return RuntimeError(f"{type(error).__name__}: {error}")


async def _drive_play(
    play: Callable[[Match, Sequence[object]], Awaitable[Tables | Result]],
    matches: MatchStream,
    concurrency: int,
) -> _Outcomes:
    """Run up to `concurrency` async `play` tasks at once, backfilling each slot."""
    active: dict[asyncio.Task[Tables | Result], Match] = {}

    async def run_one(match: Match, agents: Sequence[object]) -> Tables | Result:
        errors: list[ForwardError] = []
        token = _match_errors.set(errors)
        try:
            return await play(match, agents)
        finally:
            _match_errors.reset(token)
            if errors:
                raise errors[0]

    try:
        while True:
            for match, agents in await matches.take(concurrency - len(active)):
                active[asyncio.create_task(run_one(match, agents))] = match
            if not active:
                return
            done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for task in [t for t in active if t in done]:
                match = active.pop(task)
                error = task.exception()
                yield match, task.result() if error is None else error
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


@dataclass(frozen=True, kw_only=True)
class Game:
    """One tournament's callback, initial matches and inference setup."""

    play: Function
    tournament: str
    streaming: bool
    target_matches: int
    initial: tuple[Match, ...]
    agent_address: str | None
    checkout: str | None
    build: Function | None
    load: Function | None
    memory: int
    max_batch: int


async def _serve_stream(
    pipe: Connection,
    game: Game,
    observe_imports: Callable[[], Imports],
) -> None:
    started = time.monotonic()
    client: AgentClient | None = None
    local: Forwards | None = None
    forwarder: _Forwarder | None = None
    played_count = 0

    starting = True
    stream_error: BaseException | None = None
    try:
        play = game.play.load(game.checkout)
        kind = _match_type(play, streaming=game.streaming)
        build_hook = (
            None
            if game.build is None
            else cast(
                Callable[[object, Mapping[str, Forward]], object], game.build.load(game.checkout)
            )
        )
        if (load := game.load) is not None:
            # Process entry point: only CPU games import the local checkpoint owner.
            from etude.eval.forwards import Forwards  # noqa: PLC0415

            loaded_fn: Callable[..., object] | None = None

            def load_checkpoint(checkpoint: CheckpointRef) -> object:
                nonlocal loaded_fn
                if loaded_fn is None:
                    loaded_fn = load.load(game.checkout)
                return loaded_fn(checkpoint)

            local = Forwards(load_checkpoint, game.memory, game.max_batch)
            forwarder = _Forwarder(local, game.max_batch)
        elif game.agent_address is not None:
            client = await AgentClient.connect(game.agent_address)
            forwarder = _Forwarder(client, game.max_batch)

        source: MatchStream = MatchStream(
            pipe,
            game.initial,
            kind,
            forwarder.bind if forwarder else None,
            build_hook,
            target_matches=game.target_matches,
        )
        starting = False
        stream = (
            cast(Callable[[MatchStream], _Outcomes], play)(source)
            if game.streaming
            else _drive_play(cast(Play, play), source, source.target_matches)
        )
        async with aclosing(stream):
            async for match, outcome in stream:
                taken = source._release(match)
                elapsed = time.monotonic() - taken.started
                played_count += 1
                if isinstance(outcome, BaseException):
                    outcome = _portable_error(outcome)
                elif not isinstance(outcome, (Result, Mapping)):
                    raise TypeError("game outcomes must be tables, a Result or an exception")
                failures = forwarder.drain_loading_failures() if forwarder else ()
                pipe.send(Outcome(taken.scheduled_key, outcome, elapsed, failures))
    except Exception as error:
        if starting and not isinstance(error, RetryRequested):
            stream_error = RetryRequested(f"game process failed during startup: {explain(error)}")
        else:
            stream_error = _portable_error(error)
    finally:
        if client is not None:
            await client.close()
    report = GameReport(
        pid=os.getpid(),
        tournament=game.tournament,
        imports=observe_imports(),
        matches=played_count,
        requests=client.requests if client else 0,
        answers=client.answers if client else 0,
        request_bytes=client.request_bytes if client else 0,
        answer_bytes=client.answer_bytes if client else 0,
        round_trip_seconds=client.round_trip_seconds if client else 0.0,
        transport_seconds=client.transport_seconds if client else 0.0,
        wall_seconds=time.monotonic() - started,
        local_rows=local.rows if local else 0,
        local_forward_calls=local.calls if local else 0,
        local_loads=local.loads if local else 0,
        local_forward_seconds=local.forward_seconds if local else 0.0,
        local_load_seconds=local.load_seconds if local else 0.0,
    )
    failures = forwarder.drain_loading_failures() if forwarder else ()
    with suppress(EOFError, OSError):
        pipe.send(Done(report, stream_error, failures))


def run_game_process(
    pipe: Connection,
    resources: Resources,
    game: Game,
) -> None:
    """Spawn target for one game process; configures child environment before author imports."""
    resources.configure(1)
    before = frozenset(sys.modules)
    first_importer: str | None = None
    original = builtins.__import__

    def importing(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        nonlocal first_importer
        if first_importer is None and name.split(".", 1)[0] in _HEAVY:
            first_importer = str((globals or {}).get("__name__", "__main__"))
        return original(name, globals, locals, fromlist, level)

    builtins.__import__ = importing

    def observe_imports() -> Imports:
        return Imports(
            pid=os.getpid(),
            module=game.play.module,
            count=len(set(sys.modules) - before),
            total=len(sys.modules),
            heavy=tuple(sorted(_HEAVY.intersection(sys.modules))),
            importer=first_importer,
        )

    try:
        asyncio.run(_serve_stream(pipe, game, observe_imports))
    finally:
        builtins.__import__ = original
        pipe.close()
