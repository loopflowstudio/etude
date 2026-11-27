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
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from multiprocessing.connection import Connection
from multiprocessing.reduction import ForkingPickler
from types import ModuleType
from typing import TYPE_CHECKING, Generic, cast

from etude.call import Function
from etude.errors import RetryRequested
from etude.eval.agents import AgentClient, Answer, Forward, ForwardError
from etude.eval.composition import build_agents
from etude.eval.records import Match, _MatchConfig
from etude.eval.resources import Resources, agent_bytes, seconds_left
from etude.eval.result import Result, Tables
from etude.records import Record

__all__ = ("Matches",)

if TYPE_CHECKING:
    from etude.checkpoints import CheckpointRef
    from etude.eval.forwards import Forwards

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
    requests: int
    answers: int
    request_bytes: int
    answer_bytes: int
    round_trip_seconds: float
    transport_seconds: float
    wall_seconds: float
    local_inputs: int = 0
    local_forward_calls: int = 0
    local_loads: int = 0
    local_forward_seconds: float = 0.0
    local_load_seconds: float = 0.0


@dataclass(frozen=True)
class CheckpointFailure:
    checkpoint: str
    error: str
    retry: bool


@dataclass(frozen=True)
class Take:
    count: int


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


class _BoundForward:
    """Shared per-checkpoint awaitable handle inside one game process."""

    def __init__(self, owner: _Forwarder, checkpoint: CheckpointRef) -> None:
        self._owner = owner
        self._checkpoint = checkpoint

    async def __call__(self, inputs: list[object], *, rows: int | None = None) -> list[object]:
        if rows is not None and rows < 0:
            raise ValueError("rows must be non-negative")
        if not inputs:
            return []
        count = len(inputs) if rows is None else rows
        if count < 1:
            raise ValueError("rows must be positive for non-empty inputs")
        future = self._owner.enqueue(self._checkpoint, list(inputs), count)
        try:
            return await asyncio.wait_for(future, timeout=seconds_left())
        except TimeoutError as error:
            raise RetryRequested("agent response exceeded the evaluation deadline") from error
        except ForwardError as error:
            tracker = _match_errors.get()
            if tracker is not None:
                tracker.append(error)
            raise


class _Forwarder:
    """Zero-delay barrier batching all waiting checkpoint forwards in one game process."""

    def __init__(
        self,
        *,
        client: AgentClient | None,
        local: Forwards | None,
        max_batch: int,
    ) -> None:
        self.client = client
        self.local = local
        self.max_batch = max_batch
        self._handles: dict[str, _BoundForward] = {}
        self._checkpoints: dict[str, CheckpointRef] = {}
        self._pending: dict[str, list[_PendingCall]] = {}
        self._flush_scheduled = False
        self._loading_failures: dict[str, CheckpointFailure] = {}
        self._reported_failures: set[str] = set()

    def bind(self, checkpoint: CheckpointRef) -> Forward:
        handle = self._handles.get(checkpoint.key)
        if handle is None:
            handle = _BoundForward(self, checkpoint)
            self._handles[checkpoint.key] = handle
            self._checkpoints[checkpoint.key] = checkpoint
        return handle

    def enqueue(
        self, checkpoint: CheckpointRef, inputs: list[object], rows: int
    ) -> asyncio.Future[list[object]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[object]] = loop.create_future()
        self._checkpoints[checkpoint.key] = checkpoint
        self._pending.setdefault(checkpoint.key, []).append(_PendingCall(inputs, rows, future))
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

    def _deliver(self, chunk: list[_PendingCall], answer: Answer) -> None:
        if answer.error is not None:
            error = ForwardError(
                answer.checkpoint,
                answer.error,
                retry=answer.retry,
                loading=answer.loading,
            )
            if answer.loading and answer.checkpoint not in self._loading_failures:
                self._loading_failures[answer.checkpoint] = CheckpointFailure(
                    checkpoint=answer.checkpoint,
                    error=answer.error,
                    retry=answer.retry,
                )
            for call in chunk:
                if not call.future.done():
                    call.future.set_exception(error)
            return
        offset = 0
        for call in chunk:
            size = len(call.inputs)
            if not call.future.done():
                call.future.set_result(answer.outputs[offset : offset + size])
            offset += size

    def _flush(self) -> None:
        self._flush_scheduled = False
        pending = self._pending
        self._pending = {}
        if not pending:
            return
        grouped: list[tuple[CheckpointRef, list[_PendingCall], list[object], int]] = []
        for key, calls in pending.items():
            checkpoint = self._checkpoints[key]
            for chunk in self._chunks(calls):
                merged = [item for call in chunk for item in call.inputs]
                rows = sum(call.rows for call in chunk)
                grouped.append((checkpoint, chunk, merged, rows))
        if self.client is not None:
            try:
                futures = self.client.send_batch(
                    [(cp, merged, rows) for cp, _, merged, rows in grouped]
                )
            except BaseException as error:
                for _, chunk, _, _ in grouped:
                    for call in chunk:
                        if not call.future.done():
                            call.future.set_exception(error)
                return
            for (_, chunk, _, _), answer_future in zip(grouped, futures, strict=True):
                answer_future.add_done_callback(self._callback(chunk))
            return
        assert self.local is not None
        protected = set(pending)
        for checkpoint, chunk, merged, rows in grouped:
            answer = self.local.answer(checkpoint, merged, protected, rows=rows)
            self._deliver(chunk, answer)

    def _callback(self, chunk: list[_PendingCall]) -> Callable[[asyncio.Future[Answer]], None]:
        def done(answer_future: asyncio.Future[Answer]) -> None:
            self._on_remote(chunk, answer_future)

        return done

    def _on_remote(self, chunk: list[_PendingCall], answer_future: asyncio.Future[Answer]) -> None:
        if answer_future.cancelled():
            return
        raised = answer_future.exception()
        if raised is not None:
            for call in chunk:
                if not call.future.done():
                    call.future.set_exception(raised)
            return
        self._deliver(chunk, answer_future.result())

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
    function: Function,
    *,
    tournament: str,
    concurrency: int | None,
    agents: str | None,
    checkout: str | None,
    build: Function | None,
    load: Function | None,
    memory: int | None,
    max_batch: int,
    observe_imports: Callable[[], Imports],
) -> None:
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    client: AgentClient | None = None
    local: Forwards | None = None
    forwarder: _Forwarder | None = None
    played_count = 0
    pending_grant: asyncio.Future[tuple[Match, ...]] | None = None
    take_lock = asyncio.Lock()
    held: dict[int, _TakenMatch] = {}

    def make_report() -> GameReport:
        return GameReport(
            pid=os.getpid(),
            tournament=tournament,
            imports=observe_imports(),
            matches=played_count,
            requests=client.requests if client else 0,
            answers=client.answers if client else 0,
            request_bytes=client.request_bytes if client else 0,
            answer_bytes=client.answer_bytes if client else 0,
            round_trip_seconds=client.round_trip_seconds if client else 0.0,
            transport_seconds=client.transport_seconds if client else 0.0,
            wall_seconds=time.monotonic() - started,
            local_inputs=local.inputs if local else 0,
            local_forward_calls=local.calls if local else 0,
            local_loads=local.loads if local else 0,
            local_forward_seconds=local.forward_seconds if local else 0.0,
            local_load_seconds=local.load_seconds if local else 0.0,
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
        play = function.load(checkout)
        kind = _match_type(play, streaming=concurrency is None)
        build_hook = (
            None
            if build is None
            else cast(Callable[[object, Mapping[str, Forward]], object], build.load(checkout))
        )
        if load is not None:
            # Process entry point: only CPU games import the local checkpoint owner.
            from etude.eval.forwards import Forwards  # noqa: PLC0415

            loaded_fn: Callable[..., object] | None = None

            def load_checkpoint(checkpoint: CheckpointRef) -> object:
                nonlocal loaded_fn
                if loaded_fn is None:
                    loaded_fn = load.load(checkout)
                return loaded_fn(checkpoint)

            allowance = agent_bytes(None) if memory is None else memory
            local = Forwards(load_checkpoint, allowance)
        elif agents is not None:
            client = await AgentClient.connect(agents)
        if client is not None or local is not None:
            forwarder = _Forwarder(client=client, local=local, max_batch=max_batch)

        bind: Callable[[CheckpointRef], Forward] | None = (
            forwarder.bind if forwarder is not None else None
        )

        def validate(match: Match) -> Match:
            authored = (
                kind.model_validate_json(match.model_dump_json()) if kind is not Match else match
            )
            # The author's config type may fill defaults. Keep the scheduled key.
            held[id(authored)] = _TakenMatch(authored, match.key, time.monotonic())
            return authored

        def build_seats(match: Match) -> Sequence[object]:
            return build_agents(match.players, bind, build_hook)

        async def take_raw(count: int) -> Sequence[Match]:
            nonlocal pending_grant
            async with take_lock:
                future: asyncio.Future[tuple[Match, ...]] = loop.create_future()
                pending_grant = future
                try:
                    pipe.send(Take(count))
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
                Callable[[Matches], AsyncGenerator[tuple[Match, Tables | Result | Exception]]], play
            )(source)
            if concurrency is None
            else _drive_play(cast(Play, play), source, concurrency)
        )
        async with aclosing(stream):
            async for match, outcome in stream:
                taken = held.pop(id(match), None)
                if taken is None:
                    identity = match.key if isinstance(match, Match) else type(match).__name__
                    raise ValueError(
                        f"play_many yielded an unknown or already-yielded match: {identity}"
                    )
                elapsed = time.monotonic() - taken.started
                played_count += 1
                result: Tables | Result | None = None
                error: BaseException | None = None
                if isinstance(outcome, BaseException):
                    error = outcome
                elif isinstance(outcome, (Result, Mapping)):
                    result = outcome
                else:
                    raise TypeError("game outcomes must be tables, a Result or an exception")
                pipe.send(
                    Outcome(
                        match_key=taken.key,
                        result=result,
                        seconds=elapsed,
                        error=_portable_error(error) if error is not None else None,
                        loading_failures=(
                            (CheckpointFailure(error.checkpoint, str(error), error.retry),)
                            if isinstance(error, ForwardError) and error.loading
                            else ()
                        ),
                    )
                )
        loading_failures = forwarder.drain_loading_failures() if forwarder else ()
        pipe.send(Done(report=make_report(), loading_failures=loading_failures))
    except Exception as error:
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
        if client is not None:
            client.close()


def run_game_process(
    pipe: Connection,
    resources: Resources,
    function: Function,
    tournament: str,
    concurrency: int | None,
    agents: str | None = None,
    checkout: str | None = None,
    build: Function | None = None,
    load: Function | None = None,
    memory: int | None = None,
    max_batch: int = 32,
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
            module=function.module,
            count=len(set(sys.modules) - before),
            total=len(sys.modules),
            heavy=tuple(sorted(_HEAVY.intersection(sys.modules))),
            importer=first_importer,
        )

    try:
        asyncio.run(
            _serve_stream(
                pipe,
                function,
                tournament=tournament,
                concurrency=concurrency,
                agents=agents,
                checkout=checkout,
                build=build,
                load=load,
                memory=memory,
                max_batch=max_batch,
                observe_imports=observe_imports,
            )
        )
    finally:
        builtins.__import__ = original
        pipe.close()
