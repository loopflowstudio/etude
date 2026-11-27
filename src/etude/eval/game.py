"""Game-process event loop, zero-delay forward barrier and match streaming.

Each spawned game process runs one `asyncio` event loop consuming a match stream
(`Matches[Rules]`) over its parent pipe. `play_many` consumes it directly;
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
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Mapping,
    Sequence,
)
from contextlib import aclosing, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Generic, cast

from pydantic import Field

from etude.call import Function
from etude.errors import RetryRequested
from etude.eval.agents import Forward, ForwardError, SocketForwardBus
from etude.eval.bus import BusReport, ForwardBus, ForwardCall
from etude.eval.composition import build_agents
from etude.eval.records import Match, _MatchConfig
from etude.eval.resources import Resources, agent_bytes, seconds_left
from etude.eval.result import Result, Tables
from etude.records import Record

__all__ = ("Matches",)

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef

MAX_GAME_IMPORTS = 256
_HEAVY = frozenset({"torch", "jax", "tensorflow"})

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
    wall_seconds: float
    bus: BusReport = Field(default_factory=BusReport)


@dataclass(frozen=True)
class CheckpointFailure:
    checkpoint: str
    error: str
    retry: bool


@dataclass(frozen=True)
class Stream:
    """One tournament stream executed by a game process."""

    tournament: str
    function: Function
    concurrency: int | None


@dataclass(frozen=True)
class Take:
    count: int
    stream: int = 0


@dataclass(frozen=True)
class Grant:
    matches: tuple[Match, ...]


@dataclass(frozen=True)
class Outcome:
    match_key: str
    result: Tables | Result | None
    seconds: float
    error: BaseException | None = None
    loading_failures: tuple[CheckpointFailure, ...] = ()
    stream: int = 0


@dataclass(frozen=True)
class StreamEnd:
    stream: int
    matches: int
    error: BaseException | None = None
    loading_failures: tuple[CheckpointFailure, ...] = ()


@dataclass(frozen=True)
class Done:
    report: GameReport
    loading_failures: tuple[CheckpointFailure, ...] = ()


@dataclass(frozen=True)
class Fatal:
    error: BaseException
    report: GameReport | None = None
    loading_failures: tuple[CheckpointFailure, ...] = ()


class Matches(Generic[_MatchConfig]):  # noqa: UP046  # Python 3.12 lacks PEP 696 default syntax.
    """Pull-based match source for a game process."""

    def __init__(
        self,
        take_raw: Callable[[int], Awaitable[Sequence[Match]]],
        validate: Callable[[Match], Match[_MatchConfig]],
        build: Callable[[Match], Sequence[object]],
    ) -> None:
        self._take_raw = take_raw
        self._validate = validate
        self._build = build

    async def take(self, count: int) -> list[tuple[Match[_MatchConfig], Sequence[object]]]:
        """Request up to `count` pending matches and their built seat agents."""
        if count <= 0:
            return []
        granted = await self._take_raw(count)
        return [(self._validate(match), self._build(match)) for match in granted]


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
        source = hints.get(parameters[0].name, Matches)
        if source is Matches:
            return Match
        if typing.get_origin(source) is not Matches:
            raise TypeError("play_many's parameter must be annotated with Matches[YourConfig]")
        (config,) = typing.get_args(source)
        return Match[config]  # type: ignore[valid-type]  # Specialize from the author's annotation.
    kind = hints.get(parameters[0].name, Match)
    if not isinstance(kind, type) or not issubclass(kind, Match):
        raise TypeError("play's parameter must be annotated with Match[YourConfig]")
    return kind


@dataclass(frozen=True)
class _PendingCall:
    inputs: list[object]
    rows: int
    future: asyncio.Future[list[object]]


class _PendingAnswer:
    """Awaitable returned by `_BoundForward.__call__` without creating an unawaited coroutine."""

    def __init__(self, future: asyncio.Future[list[object]] | None) -> None:
        self._future = future

    async def _resolve(self) -> list[object]:
        if self._future is None:
            return []
        try:
            return await asyncio.wait_for(self._future, timeout=seconds_left())
        except TimeoutError as error:
            raise RetryRequested("agent response exceeded the evaluation deadline") from error
        except ForwardError as error:
            tracker = _match_errors.get()
            if tracker is not None:
                tracker.append(error)
            raise

    def __await__(self) -> Generator[object, None, list[object]]:
        return self._resolve().__await__()


class _BoundForward:
    """Shared per-checkpoint awaitable handle inside one game process."""

    def __init__(self, owner: _Forwarder, checkpoint: str) -> None:
        self._owner = owner
        self._checkpoint = checkpoint

    def __call__(self, inputs: list[object], *, rows: int | None = None) -> Awaitable[list[object]]:
        if rows is not None and rows < 0:
            raise ValueError("rows must be non-negative")
        if not inputs:
            return _PendingAnswer(None)
        count = len(inputs) if rows is None else rows
        if count < 1:
            raise ValueError("rows must be positive for non-empty inputs")
        future = self._owner.call(self._checkpoint, list(inputs), count)
        return _PendingAnswer(future)


class _Forwarder:
    """Barrier (`play`) or eager (`play_many`) forward submission in one game process."""

    def __init__(self, bus: ForwardBus, *, max_batch: int, eager: bool = False) -> None:
        self._bus = bus
        self.max_batch = max_batch
        self._eager = eager
        self._handles: dict[str, _BoundForward] = {}
        self._pending: dict[str, list[_PendingCall]] = {}
        self._flush_scheduled = False
        self._loading_failures: dict[str, CheckpointFailure] = {}
        self._reported_failures: set[str] = set()

    def bind(self, checkpoint: CheckpointRef) -> Forward:
        handle = self._handles.get(checkpoint.key)
        if handle is None:
            self._bus.bind(checkpoint)
            handle = _BoundForward(self, checkpoint.key)
            self._handles[checkpoint.key] = handle
        return handle

    def call(
        self, checkpoint: str, inputs: list[object], rows: int
    ) -> asyncio.Future[list[object]]:
        if self._eager:
            return self._submit_now(checkpoint, inputs, rows)
        return self.enqueue(checkpoint, inputs, rows)

    def _submit_now(
        self, checkpoint: str, inputs: list[object], rows: int
    ) -> asyncio.Future[list[object]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[object]] = loop.create_future()
        pending = _PendingCall(inputs, rows, future)
        try:
            (bus_future,) = self._bus.submit([ForwardCall(checkpoint, inputs, rows)])
        except BaseException as error:
            future.set_exception(error)
            return future
        bus_future.add_done_callback(self._callback([pending]))
        return future

    def enqueue(
        self, checkpoint: str, inputs: list[object], rows: int
    ) -> asyncio.Future[list[object]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[object]] = loop.create_future()
        self._pending.setdefault(checkpoint, []).append(_PendingCall(inputs, rows, future))
        if not self._flush_scheduled:
            self._flush_scheduled = True
            loop.call_soon(self._flush)
        return future

    def _chunks(self, calls: list[_PendingCall]) -> list[list[_PendingCall]]:
        chunks: list[list[_PendingCall]] = []
        current: list[_PendingCall] = []
        rows = 0
        for call in calls:
            if current and rows + call.rows > self.max_batch:
                chunks.append(current)
                current = []
                rows = 0
            current.append(call)
            rows += call.rows
        if current:
            chunks.append(current)
        return chunks

    def _flush(self) -> None:
        self._flush_scheduled = False
        pending = self._pending
        self._pending = {}
        if not pending:
            return
        grouped: list[tuple[list[_PendingCall], ForwardCall]] = []
        for key, calls in pending.items():
            for chunk in self._chunks(calls):
                merged = [item for call in chunk for item in call.inputs]
                rows = sum(call.rows for call in chunk)
                grouped.append((chunk, ForwardCall(key, merged, rows)))
        try:
            futures = self._bus.submit([call for _, call in grouped])
        except BaseException as error:
            for chunk, _ in grouped:
                for call in chunk:
                    if not call.future.done():
                        call.future.set_exception(error)
            return
        for (chunk, _), bus_future in zip(grouped, futures, strict=True):
            bus_future.add_done_callback(self._callback(chunk))

    def _callback(
        self, chunk: list[_PendingCall]
    ) -> Callable[[asyncio.Future[list[object]]], None]:
        def done(bus_future: asyncio.Future[list[object]]) -> None:
            self._on_answer(chunk, bus_future)

        return done

    def _on_answer(
        self, chunk: list[_PendingCall], bus_future: asyncio.Future[list[object]]
    ) -> None:
        if bus_future.cancelled():
            return
        raised = bus_future.exception()
        if raised is not None:
            if (
                isinstance(raised, ForwardError)
                and raised.loading
                and raised.checkpoint not in self._loading_failures
            ):
                self._loading_failures[raised.checkpoint] = CheckpointFailure(
                    checkpoint=raised.checkpoint,
                    error=str(raised),
                    retry=raised.retry,
                )
            for call in chunk:
                if not call.future.done():
                    call.future.set_exception(raised)
            return
        outputs = bus_future.result()
        offset = 0
        for call in chunk:
            size = len(call.inputs)
            if not call.future.done():
                call.future.set_result(outputs[offset : offset + size])
            offset += size

    def drain_loading_failures(self) -> tuple[CheckpointFailure, ...]:
        fresh = tuple(
            failure
            for key, failure in self._loading_failures.items()
            if key not in self._reported_failures
        )
        self._reported_failures.update(failure.checkpoint for failure in fresh)
        return fresh


def _portable_error(error: BaseException) -> BaseException:
    try:
        ForkingPickler.dumps(error)
        return error
    except Exception:
        return RuntimeError(f"{type(error).__name__}: {error}")


async def _drive_play(
    play: Callable[[Match, Sequence[object]], Awaitable[Tables | Result]],
    matches: Matches,
    concurrency: int,
) -> _Outcomes:
    """Run up to `concurrency` async `play` tasks at once, backfilling each slot."""
    active: dict[asyncio.Task[Tables | Result], Match] = {}

    async def run_one(match: Match, agents: Sequence[object]) -> Tables | Result:
        errors: list[ForwardError] = []
        token = _match_errors.set(errors)
        try:
            result = await play(match, agents)
        except BaseException:
            if errors:
                raise errors[0]
            raise
        finally:
            _match_errors.reset(token)
        if errors:
            raise errors[0]
        return result

    exhausted = False
    try:
        while True:
            if not exhausted and len(active) < concurrency:
                taken = await matches.take(concurrency - len(active))
                if not taken:
                    exhausted = True
                for match, agents in taken:
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


@dataclass(frozen=True)
class _TakenMatch:
    match: Match  # Keep the object alive while its id identifies the grant.
    key: str
    started: float


async def _serve_stream(
    pipe: Connection,
    streams: Sequence[Stream],
    *,
    agents: str | None,
    checkout: str | None,
    build: Function | None,
    load: Function | None,
    memory: int | None,
    max_batch: int,
    artifacts: Path | None,
    observe_imports: Callable[[], Imports],
) -> None:
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    bus: ForwardBus | None = None
    bus_report = BusReport()
    forwarder: _Forwarder | None = None
    played_count = 0
    pending_grant: asyncio.Future[tuple[Match, ...]] | None = None
    take_lock = asyncio.Lock()
    tournament_label = ", ".join(spec.tournament for spec in streams)

    async def close_bus() -> BusReport:
        nonlocal bus, bus_report
        if bus is not None:
            active, bus = bus, None
            bus_report = await active.close()
        return bus_report

    def make_report() -> GameReport:
        return GameReport(
            pid=os.getpid(),
            tournament=tournament_label,
            imports=observe_imports(),
            matches=played_count,
            wall_seconds=time.monotonic() - started,
            bus=bus_report,
        )

    def on_pipe_readable() -> None:
        nonlocal pending_grant
        try:
            message = cast(Grant, pipe.recv())
        except (EOFError, OSError):
            loop.remove_reader(pipe.fileno())
            if pending_grant is not None and not pending_grant.done():
                pending_grant.set_result(())
            return
        if pending_grant is not None and not pending_grant.done():
            pending_grant.set_result(message.matches)

    loop.add_reader(pipe.fileno(), on_pipe_readable)
    try:
        build_hook = (
            None
            if build is None
            else cast(Callable[[object, Mapping[str, Forward]], object], build.load(checkout))
        )
        if load is not None:
            # Process entry point: only direct-inference games import the local bus.
            from etude.eval.batching import LocalForwardBus  # noqa: PLC0415

            allowance = agent_bytes(None) if memory is None else memory
            bus = LocalForwardBus(
                load,
                checkout=checkout,
                max_batch=max_batch,
                memory=allowance,
                artifacts=artifacts,
            )
        elif agents is not None:
            bus = await SocketForwardBus.connect(agents)
        if bus is not None:
            forwarder = _Forwarder(
                bus,
                max_batch=max_batch,
                eager=all(spec.concurrency is None for spec in streams),
            )

        bind: Callable[[CheckpointRef], Forward] | None = (
            forwarder.bind if forwarder is not None else None
        )

        def build_seats(match: Match) -> Sequence[object]:
            return build_agents(match.players, bind, build_hook)

        for stream_index, spec in enumerate(streams):
            held: dict[int, _TakenMatch] = {}
            stream_matches = 0
            try:
                play = spec.function.load(checkout)
                kind = _match_type(play, streaming=spec.concurrency is None)

                def validate(
                    match: Match,
                    *,
                    target_kind: type[Match] = kind,
                    target_held: dict[int, _TakenMatch] = held,
                ) -> Match:
                    authored = (
                        target_kind.model_validate_json(match.model_dump_json())
                        if target_kind is not Match
                        else match
                    )
                    # The author's config type may fill defaults. Keep the scheduled key.
                    target_held[id(authored)] = _TakenMatch(authored, match.key, time.monotonic())
                    return authored

                async def take_raw(
                    count: int, *, current_stream: int = stream_index
                ) -> Sequence[Match]:
                    nonlocal pending_grant
                    async with take_lock:
                        future: asyncio.Future[tuple[Match, ...]] = loop.create_future()
                        pending_grant = future
                        try:
                            pipe.send(Take(count, stream=current_stream))
                        except (EOFError, OSError):
                            pending_grant = None
                            return ()
                        try:
                            return await future
                        finally:
                            pending_grant = None

                source: Matches = Matches(take_raw, validate, build_seats)
                stream = (
                    cast(
                        Callable[
                            [Matches], AsyncGenerator[tuple[Match, Tables | Result | Exception]]
                        ],
                        play,
                    )(source)
                    if spec.concurrency is None
                    else _drive_play(cast(Play, play), source, spec.concurrency)
                )
                async with aclosing(stream):
                    async for match, outcome in stream:
                        taken = held.pop(id(match), None)
                        if taken is None:
                            identity = (
                                match.key if isinstance(match, Match) else type(match).__name__
                            )
                            raise ValueError(
                                f"play_many yielded an unknown or already-yielded match: {identity}"
                            )
                        elapsed = time.monotonic() - taken.started
                        played_count += 1
                        stream_matches += 1
                        result: Tables | Result | None = None
                        error: BaseException | None = None
                        if isinstance(outcome, BaseException):
                            error = outcome
                        elif isinstance(outcome, (Result, Mapping)):
                            result = outcome
                        else:
                            raise TypeError(
                                "game outcomes must be tables, a Result or an exception"
                            )
                        drained = forwarder.drain_loading_failures() if forwarder else ()
                        if (
                            isinstance(error, ForwardError)
                            and error.loading
                            and not any(
                                failure.checkpoint == error.checkpoint for failure in drained
                            )
                        ):
                            drained = (
                                *drained,
                                CheckpointFailure(error.checkpoint, str(error), error.retry),
                            )
                        pipe.send(
                            Outcome(
                                match_key=taken.key,
                                result=result,
                                seconds=elapsed,
                                error=_portable_error(error) if error is not None else None,
                                loading_failures=drained,
                                stream=stream_index,
                            )
                        )
            except RetryRequested:
                raise
            except Exception as error:
                drained = forwarder.drain_loading_failures() if forwarder else ()
                pipe.send(
                    StreamEnd(
                        stream=stream_index,
                        matches=stream_matches,
                        error=_portable_error(error),
                        loading_failures=drained,
                    )
                )
            else:
                drained = forwarder.drain_loading_failures() if forwarder else ()
                pipe.send(
                    StreamEnd(
                        stream=stream_index,
                        matches=stream_matches,
                        loading_failures=drained,
                    )
                )
        await close_bus()
        loading_failures = forwarder.drain_loading_failures() if forwarder else ()
        pipe.send(Done(report=make_report(), loading_failures=loading_failures))
    except Exception as error:
        with suppress(Exception):
            await close_bus()
        loading_failures = forwarder.drain_loading_failures() if forwarder else ()
        try:
            pipe.send(
                Fatal(
                    error=_portable_error(error),
                    report=make_report(),
                    loading_failures=loading_failures,
                )
            )
        except (EOFError, OSError):
            pass
    finally:
        loop.remove_reader(pipe.fileno())
        with suppress(Exception):
            await close_bus()


def run_game_process(
    pipe: Connection,
    resources: Resources,
    streams: Sequence[Stream],
    *,
    threads: int = 1,
    agents: str | None = None,
    checkout: str | None = None,
    build: Function | None = None,
    load: Function | None = None,
    memory: int | None = None,
    max_batch: int = 32,
) -> None:
    """Spawn target for one game process; configures child environment before author imports."""
    resources.configure(threads)
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
    module_label = ", ".join(dict.fromkeys(spec.function.module for spec in streams))

    def observe_imports() -> Imports:
        return Imports(
            pid=os.getpid(),
            module=module_label,
            count=len(set(sys.modules) - before),
            total=len(sys.modules),
            heavy=tuple(sorted(_HEAVY.intersection(sys.modules))),
            importer=first_importer,
        )

    try:
        asyncio.run(
            _serve_stream(
                pipe,
                streams,
                agents=agents,
                checkout=checkout,
                build=build,
                load=load,
                memory=memory,
                max_batch=max_batch,
                artifacts=resources.artifacts,
                observe_imports=observe_imports,
            )
        )
    finally:
        builtins.__import__ = original
        pipe.close()
