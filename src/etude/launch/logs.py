"""Log snapshots and plain-text views, independent of execution decisions."""

from datetime import UTC, datetime
from typing import cast

from etude.launch.logging import CLOSED, TRUNCATED, bounded, portions
from etude.launch.records import Diagnostic, LogChunk, LogProducer, LogSnapshot, LogStream


def snapshot_stream(
    session: str, attempt: str | None, producer: str, raw: bytes | None, closed: bool
) -> LogStream:
    if producer not in {"bootstrap", "worker", "job"}:
        raise ValueError("unknown log producer")
    text = bounded(raw.decode(errors="replace")) if raw is not None else ""
    # Captured child output may itself mention closure. Only the owning producer
    # can close this stream, using its own timestamped operational line.
    last_message = text.removesuffix("\n").rsplit("\n", 1)[-1].partition(" ")[2]
    opening, tail, omitted = portions(text)
    diagnostics: list[Diagnostic] = []
    chunks: list[LogChunk] = []
    for offset, body in ((0, opening), (len(opening) + omitted, tail)):
        if not body:
            continue
        if producer != "worker":
            chunks.append(LogChunk(offset=offset, text=body))
            continue
        start = position = 0
        for line in body.splitlines(keepends=True):
            diagnostic = None
            if " diagnostic: " in line:
                try:
                    diagnostic = Diagnostic.model_validate_json(line.split(" diagnostic: ", 1)[1])
                except ValueError:
                    pass  # a truncated record is explicitly incomplete evidence
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                if position > start:
                    chunks.append(LogChunk(offset=offset + start, text=body[start:position]))
                start = position + len(line)
            position += len(line)
        if start < len(body):
            chunks.append(LogChunk(offset=offset + start, text=body[start:]))
    return LogStream(
        id=f"{session}/{attempt or producer}",
        diagnostics=tuple(diagnostics),
        producer=cast(LogProducer, producer),  # validated at this transport boundary above
        session=session,
        attempt=attempt,
        chunks=tuple(chunks),
        position=len(opening) + omitted + len(tail),
        received=raw is not None,
        observed_at=datetime.now(UTC) if raw is not None else None,
        complete=raw is not None
        and (closed or (producer != "job" and last_message == f"{producer}: {CLOSED}")),
        truncated=TRUNCATED in text,
    )


def _render_diagnostic(diagnostic: Diagnostic) -> str:
    labels = " ".join(
        f"{name}={value}"
        for name, value in (
            ("launch", diagnostic.launch),
            ("session", diagnostic.session),
            ("attempt", diagnostic.attempt),
        )
        if value
    )
    return (
        f"{diagnostic.observed_at.isoformat()} {labels} "
        f"{diagnostic.operation}: {diagnostic.message}\n"
    )


def _render_stream_header(stream: LogStream) -> str:
    state = "collected final output" if stream.complete else "incomplete"
    if not stream.received:
        state = "output not received"
    elif not stream.text:
        state += "; silent producer" if stream.complete else "; no output yet"
    if stream.truncated:
        state += "; retention truncated"
    return (
        f"[{stream.producer} machine={stream.machine} session={stream.session} "
        f"attempt={stream.attempt or '-'}; {state}; collected={stream.observed_at}]\n"
    )


def render(snapshot: LogSnapshot, *, job_only: bool = False) -> str:
    lines: list[str] = []
    if not job_only:
        for launch in snapshot.launches:
            lines.append(f"launch {launch.id}: {launch.state}\n")
        lines.extend(_render_diagnostic(diagnostic) for diagnostic in snapshot.diagnostics)
    for stream in snapshot.streams:
        if job_only and stream.producer != "job":
            continue
        lines.append(_render_stream_header(stream))
        lines.append(stream.text)
        if stream.retrieval_error:
            lines.append(f"\nretrieval failed: {stream.retrieval_error}\n")
    lines.append(
        f"[snapshot {'complete' if snapshot.complete else 'incomplete'}; observed={snapshot.observed_at.isoformat()}]\n"
    )
    return "".join(lines)


class LogCursor:
    """Positions in sanitized producer text; no cross-producer clock ordering."""

    def __init__(self) -> None:
        self._positions: dict[str, int] = {}
        self._diagnostics: set[str] = set()
        self._launches: dict[str, str] = {}
        self._states: dict[str, tuple[bool, bool, bool, str | None]] = {}

    def render(self, snapshot: LogSnapshot, *, job_only: bool = False) -> str:
        output: list[str] = []
        if not job_only:
            for launch in snapshot.launches:
                if self._launches.get(launch.id) != launch.state:
                    output.append(f"launch {launch.id}: {launch.state}\n")
                    self._launches[launch.id] = launch.state
            fresh = tuple(d for d in snapshot.diagnostics if d.id not in self._diagnostics)
            output.extend(_render_diagnostic(diagnostic) for diagnostic in fresh)
            self._diagnostics.update(d.id for d in fresh)
        for stream in snapshot.streams:
            if job_only and stream.producer != "job":
                continue
            cursor = self._positions.get(stream.id, 0)
            end = stream.position
            state = (stream.received, stream.complete, stream.truncated, stream.retrieval_error)
            if end > cursor or self._states.get(stream.id) != state:
                output.append(_render_stream_header(stream))
                for chunk in stream.chunks:
                    if chunk.offset + len(chunk.text) <= cursor:
                        continue
                    if stream.truncated and chunk.offset > cursor:
                        output.append("\n[retention truncated; some output not received]\n")
                    output.append(chunk.text[max(0, cursor - chunk.offset) :])
                    cursor = chunk.offset + len(chunk.text)
                if stream.retrieval_error:
                    output.append(f"retrieval failed: {stream.retrieval_error}\n")
            self._positions[stream.id] = max(cursor, end)
            self._states[stream.id] = state
        return "".join(output)
