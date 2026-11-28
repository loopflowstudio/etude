"""Game-process event loop, forward submission and match streaming.

Each spawned game process runs one `asyncio` event loop consuming one or more
tournament streams (`MatchStream[Rules]`) over its parent pipe. `play_many`
consumes `MatchStream` directly and submits forwards eagerly on call; Etude drives
`async def play` as bounded match tasks behind a zero-delay forward barrier.
Both share one `ForwardBus` (`LocalForwardBus` or `SocketForwardBus`) and send
each finished match to the parent for storage.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import os
import sys
import threading
import time
import typing
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import aclosing, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cached_property, lru_cache, partial
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Generic, cast

from pydantic import Field

from etude.call import Function
from etude.errors import RetryRequested, explain
from etude.eval.agents import Forward, ForwardError, SocketForwardBus
from etude.eval.bus import BusReport, ForwardBus, ForwardCall
from etude.eval.composition import build_agents
from etude.eval.records import Match, _MatchConfig
from etude.eval.resources import Resources, seconds_left
from etude.eval.result import Result, Tables
from etude.records import Record

__all__ = ("MatchStream",)

if TYPE_CHECKING:
    from numpy import ndarray

    from etude.checkpoints import CheckpointRef

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

    def warning(self, *, count_limit: bool = True) -> str | None:
        if not self.heavy and (not count_limit or self.count <= MAX_GAME_IMPORTS):
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
    tournaments: tuple[str, ...]
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
    streaming: bool
    target_slots: int
    initial: tuple[Match, ...] = ()


@dataclass(frozen=True)
class Take:
    count: int
    stream: int = 0


@dataclass(frozen=True)
class Grant:
    """Matches for this take and whether this stream can request more."""

    matches: tuple[Match, ...]
    drained: bool


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


@dataclass(frozen=True)
class _TakenMatch:
    match: Match  # Keep the object alive while its id identifies the grant.
    scheduled_key: str
    started: float

    @cached_property
    def authored_key(self) -> str:
        return self.match.key


class MatchStream(Generic[_MatchConfig]):  # noqa: UP046  # Python 3.12 lacks PEP 696 default syntax.
    """One tournament stream's match grants and the seats built for them."""

    def __init__(
        self,
        request: Callable[[int], Awaitable[Grant]],
        initial: tuple[Match, ...],
        kind: type[Match[_MatchConfig]],
        bind: Callable[[CheckpointRef], Forward] | None,
        build: Callable[[object, Mapping[str, Forward]], object] | None,
        *,
        target_slots: int,
    ) -> None:
        self._request = request
        self._initial = initial
        self._kind = kind
        self._bind = bind
        self._build = build
        self._target_slots = target_slots
        self._drained = False
        self._held: dict[int, _TakenMatch] = {}

    @property
    def target_slots(self) -> int:
        """Suggested pool size for this stream, capped at `max_batch` with a loader."""
        return self._target_slots

    async def take(self, count: int) -> list[tuple[Match[_MatchConfig], Sequence[object]]]:
        """Request up to `count` pending matches and their built seat agents."""
        if count <= 0 or self._drained:
            return []
        if self._initial:
            granted, self._initial = self._initial[:count], self._initial[count:]
        else:
            grant = await self._request(count)
            granted = grant.matches
            self._drained = grant.drained
        seats: list[tuple[Match[_MatchConfig], Sequence[object]]] = []
        for match in granted:
            if self._kind is Match:
                authored = cast(Match[_MatchConfig], match)
            else:
                authored = self._kind.model_validate_json(match.model_dump_json())
            # Config validation can fill defaults. Retain the parent's scheduled key.
            self._held[id(authored)] = _TakenMatch(authored, match.key, time.monotonic())
            seats.append((authored, build_agents(match.players, self._bind, self._build)))
        return seats

    def _release(self, match: Match) -> _TakenMatch:
        taken = self._held.pop(id(match), None)
        if taken is not None:
            return taken
        if isinstance(match, Match):
            identity = match.key
            matching = [key for key, held in self._held.items() if held.authored_key == identity]
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


@dataclass(frozen=True)
class _PendingCall:
    inputs: list[object]
    rows: int
    future: asyncio.Future[list[object]]
    locked: list[ndarray] = field(default_factory=list)

    def fail(self, error: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(error)
            # A stream can abandon its awaitable. A later await still raises this error.
            self.future.exception()


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
        if not inputs:
            if rows:
                raise ValueError("rows must be 0 or omitted for empty inputs")
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
        self.eager = eager
        self._handles: dict[str, _BoundForward] = {}
        self._pending: dict[str, list[_PendingCall]] = {}
        self._loading_failures: list[CheckpointFailure] = []
        self._failed_checkpoints: set[str] = set()

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
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[object]] = loop.create_future()
        if self.eager:
            self._submit([(checkpoint, [_PendingCall(inputs, rows, future)])])
        else:
            pending = _PendingCall(inputs, rows, future, self._bus.flight.lock(inputs))
            if not self._pending:
                loop.call_soon(self._flush)
            self._pending.setdefault(checkpoint, []).append(pending)
        return future

    def clear_pending(self) -> None:
        """Release locks on calls that have not yet reached the bus."""
        for calls in self._pending.values():
            for call in calls:
                self._bus.flight.unlock(call.locked)
                call.future.cancel()
        self._pending.clear()

    def _chunks(self, calls: list[_PendingCall]) -> Iterator[list[_PendingCall]]:
        chunk: list[_PendingCall] = []
        rows = 0
        for call in calls:
            if chunk and rows + call.rows > self.max_batch:
                yield chunk
                chunk = []
                rows = 0
            chunk.append(call)
            rows += call.rows
        if chunk:
            yield chunk

    def _flush(self) -> None:
        groups = [
            (key, chunk) for key, calls in self._pending.items() for chunk in self._chunks(calls)
        ]
        self._pending.clear()
        if groups:
            self._submit(groups)

    def _submit(self, groups: list[tuple[str, list[_PendingCall]]]) -> None:
        calls = []
        for key, chunk in groups:
            if len(chunk) == 1:
                (call,) = chunk
                calls.append(ForwardCall(key, call.inputs, call.rows))
            else:
                inputs = [value for call in chunk for value in call.inputs]
                calls.append(ForwardCall(key, inputs, sum(call.rows for call in chunk)))
        try:
            futures = self._bus.submit(calls)
        except BaseException as error:
            for _, chunk in groups:
                for call in chunk:
                    call.fail(error)
            return
        finally:
            # Submit acquires its own references before the barrier releases
            # its references, so overlapping calls never unlock shared inputs.
            for _, chunk in groups:
                for call in chunk:
                    self._bus.flight.unlock(call.locked)
        for (_, chunk), bus_future in zip(groups, futures, strict=True):
            bus_future.add_done_callback(partial(self._on_answer, chunk))

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
                and raised.checkpoint not in self._failed_checkpoints
            ):
                self._failed_checkpoints.add(raised.checkpoint)
                self._loading_failures.append(
                    CheckpointFailure(raised.checkpoint, str(raised), raised.retry)
                )
            for call in chunk:
                call.fail(raised)
            return
        outputs = bus_future.result()
        offset = 0
        for call in chunk:
            size = len(call.inputs)
            if not call.future.done():
                call.future.set_result(outputs[offset : offset + size])
            offset += size

    def drain_loading_failures(self) -> tuple[CheckpointFailure, ...]:
        fresh = tuple(self._loading_failures)
        self._loading_failures.clear()
        return fresh


def _portable_error(error: BaseException) -> BaseException:
    try:
        ForkingPickler.dumps(error)
        return error
    except Exception:
        return RuntimeError(f"{type(error).__name__}: {error}")


async def _drive_play(
    play: Callable[[Match, Sequence[object]], Awaitable[Tables | Result]],
    matches: MatchStream,
) -> _Outcomes:
    """Run async `play` tasks up to the stream's slot count and backfill each slot."""
    concurrency = matches.target_slots
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

    try:
        while True:
            if len(active) < concurrency:
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


class _ImportTracker:
    """Thread-filtered game import observer spanning module load, wrappers and play."""

    def __init__(self, module_label: str) -> None:
        self._module_label = module_label
        self._game_thread = threading.current_thread()
        self._before = frozenset(sys.modules)
        self._game_modules: set[str] = set()
        self._game_heavy: set[str] = set()
        self._first_importer: str | None = None
        self._paused = False
        self._original = builtins.__import__
        self._snapshot: Imports | None = None

    def __enter__(self) -> _ImportTracker:
        builtins.__import__ = self._importing
        return self

    def __exit__(self, *_: object) -> None:
        builtins.__import__ = self._original

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Exclude Etude's own lazy internal imports from game-side counts."""
        previous = self._paused
        self._paused = True
        try:
            yield
        finally:
            self._paused = previous

    def capture_pre_bus(self) -> None:
        """Record modules loaded on the game thread before starting the bus."""
        self._game_modules.update(set(sys.modules) - self._before)
        self._game_heavy.update(_HEAVY.intersection(sys.modules))

    def _importing(
        self,
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        if self._paused or threading.current_thread() is not self._game_thread:
            return self._original(name, globals, locals, fromlist, level)
        top = name.split(".", 1)[0] if level == 0 else ""
        set_importer = top in _HEAVY and self._first_importer is None
        if set_importer:
            self._first_importer = str((globals or {}).get("__name__", "__main__"))
        try:
            module = self._original(name, globals, locals, fromlist, level)
        except BaseException:
            if set_importer:
                self._first_importer = None
            raise
        if top in _HEAVY:
            self._game_heavy.add(top)
        if module.__name__ not in self._before:
            self._game_modules.add(module.__name__)
        if fromlist:
            for item in fromlist:
                sub = f"{module.__name__}.{item}"
                if sub in sys.modules and sub not in self._before:
                    self._game_modules.add(sub)
        return module

    def finish(self) -> Imports:
        """Restore `builtins.__import__` and build the process-exit `Imports` report."""
        if self._snapshot is None:
            builtins.__import__ = self._original
            self._snapshot = Imports(
                pid=os.getpid(),
                module=self._module_label,
                count=len(self._game_modules),
                total=len(sys.modules),
                heavy=tuple(sorted(self._game_heavy)),
                importer=self._first_importer,
            )
        return self._snapshot


async def _serve_stream(
    pipe: Connection,
    streams: Sequence[Stream],
    *,
    agents: str | None,
    checkout: str | None,
    build: Function | None,
    load: Function | None,
    checkpoint_bytes: int,
    max_batch: int,
    artifacts: Path | None,
    imports: _ImportTracker,
) -> None:
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    bus: ForwardBus | None = None
    bus_report = BusReport()
    forwarder: _Forwarder | None = None
    played_count = 0
    pending_grant: asyncio.Future[Grant] | None = None
    take_lock = asyncio.Lock()
    drained = Grant(matches=(), drained=True)
    tournaments = tuple(spec.tournament for spec in streams)

    async def close_bus() -> None:
        nonlocal bus, bus_report
        if forwarder is not None:
            forwarder.clear_pending()
        if bus is not None:
            active, bus = bus, None
            bus_report = await active.close()
            # Let `fail_all` callbacks run so loading failures drain and unawaited
            # futures settle before process exit.
            await asyncio.sleep(0)

    def drain_failures(error: BaseException | None = None) -> tuple[CheckpointFailure, ...]:
        drained = forwarder.drain_loading_failures() if forwarder else ()
        if (
            isinstance(error, ForwardError)
            and error.loading
            and not any(failure.checkpoint == error.checkpoint for failure in drained)
        ):
            return (*drained, CheckpointFailure(error.checkpoint, str(error), error.retry))
        return drained

    def make_report() -> GameReport:
        return GameReport(
            pid=os.getpid(),
            tournaments=tournaments,
            imports=imports.finish(),
            matches=played_count,
            wall_seconds=time.monotonic() - started,
            bus=bus_report,
        )

    def on_pipe_readable() -> None:
        try:
            message = cast(Grant, pipe.recv())
        except (EOFError, OSError):
            loop.remove_reader(pipe.fileno())
            if pending_grant is not None and not pending_grant.done():
                pending_grant.set_result(drained)
            return
        if pending_grant is not None and not pending_grant.done():
            pending_grant.set_result(message)

    starting = True
    loop.add_reader(pipe.fileno(), on_pipe_readable)
    try:
        build_hook = (
            None
            if build is None
            else cast(Callable[[object, Mapping[str, Forward]], object], build.load(checkout))
        )
        loaded_streams: list[tuple[Callable[..., object], type[Match]] | Exception] = []
        for spec in streams:
            try:
                play_fn = spec.function.load(checkout)
                kind = _match_type(play_fn, streaming=spec.streaming)
            except Exception as load_error:
                startup_error = (
                    load_error
                    if isinstance(load_error, RetryRequested)
                    else RetryRequested(
                        f"game process failed during startup: {explain(load_error)}"
                    )
                )
                loaded_streams.append(startup_error)
            else:
                loaded_streams.append((play_fn, kind))
        # Capture pre-bus top-level imports, then keep the thread-filtered hook
        # active through wrapper loading and play so direct-mode inference-thread
        # imports are ignored while wrapper and lazy game imports stay visible.
        imports.capture_pre_bus()
        if any(not isinstance(entry, Exception) for entry in loaded_streams):
            if load is not None:
                with imports.paused():
                    # Process entry point: only direct-inference games import the local bus.
                    from etude.eval.batching import LocalForwardBus  # noqa: PLC0415

                bus = LocalForwardBus(
                    load,
                    checkout=checkout,
                    max_batch=max_batch,
                    memory=checkpoint_bytes,
                    artifacts=artifacts,
                )
            elif agents is not None:
                bus = await SocketForwardBus.connect(agents)
        if bus is not None:
            forwarder = _Forwarder(bus, max_batch=max_batch)

        async def request_grant(stream_index: int, count: int) -> Grant:
            nonlocal pending_grant
            async with take_lock:
                future: asyncio.Future[Grant] = loop.create_future()
                pending_grant = future
                try:
                    pipe.send(Take(count, stream=stream_index))
                except (EOFError, OSError):
                    pending_grant = None
                    return drained
                try:
                    return await future
                finally:
                    pending_grant = None

        starting = False
        for stream_index, (spec, loaded) in enumerate(zip(streams, loaded_streams, strict=True)):
            stream_error: BaseException | None = None
            if isinstance(loaded, Exception):
                stream_error = loaded
            else:
                if forwarder is not None:
                    forwarder.eager = spec.streaming
                try:
                    play, kind = loaded
                    source: MatchStream = MatchStream(
                        partial(request_grant, stream_index),
                        spec.initial,
                        kind,
                        forwarder.bind if forwarder is not None else None,
                        build_hook,
                        target_slots=spec.target_slots,
                    )
                    stream = (
                        cast(
                            Callable[
                                [MatchStream],
                                AsyncGenerator[tuple[Match, Tables | Result | Exception]],
                            ],
                            play,
                        )(source)
                        if spec.streaming
                        else _drive_play(cast(Play, play), source)
                    )
                    async with aclosing(stream):
                        async for match, outcome in stream:
                            taken = source._release(match)
                            elapsed = time.monotonic() - taken.started
                            played_count += 1
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
                            pipe.send(
                                Outcome(
                                    match_key=taken.scheduled_key,
                                    result=result,
                                    seconds=elapsed,
                                    error=_portable_error(error) if error is not None else None,
                                    loading_failures=drain_failures(error),
                                    stream=stream_index,
                                )
                            )
                except RetryRequested:
                    raise
                except Exception as error:
                    stream_error = error
                finally:
                    if forwarder is not None:
                        forwarder.clear_pending()
            pipe.send(
                StreamEnd(
                    stream=stream_index,
                    error=_portable_error(stream_error) if stream_error is not None else None,
                    loading_failures=drain_failures(),
                )
            )
        await close_bus()
        pipe.send(Done(report=make_report(), loading_failures=drain_failures()))
    except Exception as error:
        with suppress(Exception):
            await close_bus()
        if starting and not isinstance(error, RetryRequested):
            error = RetryRequested(f"game process failed during startup: {explain(error)}")
        try:
            pipe.send(
                Fatal(
                    error=_portable_error(error),
                    report=make_report(),
                    loading_failures=drain_failures(),
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
    checkpoint_bytes: int,
    threads: int = 1,
    agents: str | None = None,
    checkout: str | None = None,
    build: Function | None = None,
    load: Function | None = None,
    max_batch: int = 32,
) -> None:
    """Spawn target for one game process; configures child environment before author imports."""
    resources.configure(threads)
    module_label = ", ".join(dict.fromkeys(spec.function.module for spec in streams))
    try:
        with _ImportTracker(module_label) as imports:
            asyncio.run(
                _serve_stream(
                    pipe,
                    streams,
                    agents=agents,
                    checkout=checkout,
                    build=build,
                    load=load,
                    checkpoint_bytes=checkpoint_bytes,
                    max_batch=max_batch,
                    artifacts=resources.artifacts,
                    imports=imports,
                )
            )
    finally:
        pipe.close()
