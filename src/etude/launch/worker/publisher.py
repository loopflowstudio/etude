"""Publish sealed run evidence from the attempt's host, independently of delivery."""

import os
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from etude.block_file import parse, select
from etude.errors import explain
from etude.launch.logging import LIMIT, TRUNCATED, Console
from etude.launch.logs import ConsoleTail
from etude.launch.schemas import read_progress
from etude.observations import (
    MAX_FILE_BYTES,
    AttemptObservation,
    ConsoleObservation,
    ViewGrantTarget,
    evaluation_file,
    evidence_file,
    member_folder,
)
from etude.records import RunRecord


class _JobConsole:
    """Byte cursor in the local job log. Only whole sanitized lines leave the machine."""

    def __init__(self) -> None:
        self.position = 0
        self.lines: deque[str] = deque()
        self.size = 0
        self.gaps = False
        self._skip_partial = False
        self.console = Console("job", retain=self._retain)

    def _retain(self, line: str) -> None:
        self.lines.append(line)
        self.size += len(line.encode()) + 1
        self.gaps |= TRUNCATED in line
        while self.size > LIMIT or len(self.lines) > ConsoleTail.max_lines:
            self.size -= len(self.lines.popleft().encode()) + 1
            self.gaps = True

    def collect(self, path: Path, finished: bool) -> ConsoleObservation | None:
        try:
            stream = path.open("rb")
        except FileNotFoundError:
            return None
        with stream:
            # Snapshot the end so a noisy producer cannot monopolize publication.
            end = os.fstat(stream.fileno()).st_size
            if end - self.position > LIMIT:
                self.position = end - LIMIT
                self.lines.clear()
                self.size = 0
                self.gaps = self._skip_partial = True
                self.console = Console("job", retain=self._retain)
            stream.seek(self.position)
            while self.position < end:
                chunk = stream.read(min(end - self.position, 65536))
                if not chunk:
                    break
                self.position += len(chunk)
                if self._skip_partial:
                    # Starting inside a line could expose the rest of a credential.
                    _, newline, chunk = chunk.partition(b"\n")
                    self._skip_partial = not newline
                self.console.write(chunk)
        complete = finished and self.position == end
        if complete:
            self.console.write(b"", final=True)
        return ConsoleObservation(
            position=self.position, body="\n".join(self.lines), gaps=self.gaps, complete=complete
        )


class AttemptPublisher:
    def __init__(
        self, root: Path, journal: Path, target: ViewGrantTarget, halt: threading.Event
    ) -> None:
        self.root = root
        self.journal = journal
        self.console = _JobConsole()
        self.last: AttemptObservation | None = None
        self.heartbeat = 0.0
        self.sequence = 0
        self.target = target
        self.halt = halt
        self.finish = threading.Event()
        self.stop = threading.Event()
        self._members: tuple[str, ...] = ()
        self._sent: dict[str, tuple[int, int, int]] = {}

    def _files(self) -> Iterator[str]:
        yield "run.json"
        for member in self._members:
            root = self.root / member
            if root.resolve() == root.absolute():
                yield member + "/run.json"
                yield from (
                    member + "/" + key for key in self._run_files(root) if evaluation_file(key)
                )
        yield from self._run_files(self.root)

    def _run_files(self, root: Path) -> Iterator[str]:
        yield "environment.json"
        yield "tournament.json"
        metrics = root / "metrics"
        if metrics.is_dir() and not metrics.is_symlink():
            keys = ["metrics/" + p.name for p in metrics.iterdir() if not p.is_symlink()]
            yield from sorted(key for key in keys if key.endswith(".json") and evidence_file(key))
            for block in select(b for k in keys if evidence_file(k) and (b := parse(k))):
                yield block.key
        for folder in ("artifacts", "checkpoints"):
            directory = root / folder
            if directory.is_dir() and not directory.is_symlink():
                yield from sorted(
                    key
                    for path in directory.glob("*.json")
                    if evidence_file(key := folder + "/" + path.name)
                )
        blocks = root / "blocks"
        if blocks.is_dir() and not blocks.is_symlink():
            for index in sorted(blocks.glob("*/block.json")):
                name = index.relative_to(root).as_posix()
                if evidence_file(name) and not index.parent.is_symlink():
                    for path in sorted(index.parent.glob("*.parquet")):
                        if evidence_file(key := path.relative_to(root).as_posix()):
                            yield key
                    yield name
        yield "outcome.json"

    def _send(self, key: str) -> None:
        path = self.root / key
        info = path.stat(follow_symlinks=False)
        version = (info.st_ino, info.st_mtime_ns, info.st_size)
        if not stat.S_ISREG(info.st_mode) or self._sent.get(key) == version:
            return
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError("training evidence exceeds hosted view limit")
        if path.resolve().parent != path.parent.absolute():
            return
        with os.fdopen(os.open(path, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return
            body = stream.read(MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            raise ValueError("training evidence exceeds hosted view limit")
        self._post(
            "/api/observations/file",
            body,
            {
                "Content-Type": "application/octet-stream",
                "X-Etude-Key": key,
                "X-Etude-Sha256": sha256(body).hexdigest(),
                "X-Etude-Modified-At": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            },
        )
        if key == "run.json":
            record = RunRecord.model_validate_json(body)
            self._members = tuple(name for name in record.members or () if member_folder(name))
        self._sent[key] = version

    def _post(self, route: str, body: bytes, headers: dict[str, str]) -> None:
        url = urlsplit(self.target.origin)
        assert url.hostname is not None
        connection = HTTPSConnection if url.scheme == "https" else HTTPConnection
        # A single exchange never follows a redirect carrying the grant.
        client = connection(url.hostname, url.port, timeout=5)
        try:
            client.request(
                "POST", route, body, {**headers, "Authorization": "Bearer " + self.target.token}
            )
            with client.getresponse() as response:
                if response.status in (409, 413, 422):
                    raise ValueError(f"hosted view refused observation (HTTP {response.status})")
                if response.status != 200:
                    raise HTTPException(f"hosted view returned HTTP {response.status}")
        finally:
            client.close()

    def _observe(self) -> None:
        progress = read_progress(self.journal)
        done = progress.finished_at is not None
        observation = AttemptObservation(
            producer_seq=self.last.producer_seq if self.last else 1,
            phase="done" if done else "running" if progress.started_at else "setting_up",
            started_at=progress.started_at,
            finished_at=progress.finished_at,
            outcome=progress.outcome,
            cause=progress.cause,
            exit_code=progress.exit_code,
            clocks=progress.clocks,
            tally=progress.tally,
            context=self.target.context,
            console=self.console.collect(self.journal / "log", done),
        )
        if observation == self.last and time.monotonic() < self.heartbeat:
            return
        self.sequence += 1
        observation = observation.model_copy(update={"producer_seq": self.sequence})
        self._post(
            "/api/observations",
            observation.model_dump_json().encode(),
            {"Content-Type": "application/json"},
        )
        self.last = observation
        self.heartbeat = time.monotonic() + 60

    def stopping(self) -> bool:
        # An owned host receives TERM on normal completion, while final evidence is in flight.
        return (
            self.stop.is_set()
            or self.halt.is_set()
            and not (self.journal / "finished.json").exists()
        )

    def run(self) -> None:
        failed = False
        delay = 1
        while not self.stopping():
            final = self.finish.is_set()
            failure: str | None = None
            try:
                try:
                    self._observe()
                except ValueError as error:
                    failure = explain(error)
                for key in self._files():
                    if self.stopping():
                        return
                    try:
                        self._send(key)
                    except FileNotFoundError:
                        continue  # Not sealed yet, or already folded into a wider block.
                    except ValueError as error:
                        # One refused record must not starve later sealed blocks.
                        failure = explain(error)
            except (OSError, ValueError, HTTPException) as error:
                failure = explain(error)
            if failure and not failed:
                print("hosted view: " + failure, file=sys.stderr, flush=True)
            failed = failure is not None
            delay = min(delay * 2, 30) if failed else 1
            if final:
                return
            self.finish.wait(delay)


@contextmanager
def publish_attempt(
    session: Path, attempt: str, run: str, halt: threading.Event, deadline: datetime | None
) -> Iterator[None]:
    private = session / ".view" / (attempt + ".json")
    try:
        target = ViewGrantTarget.model_validate_json(private.read_bytes())
    except FileNotFoundError:
        yield
        return
    except (OSError, ValueError):
        print("hosted view: cannot read attempt grant", file=sys.stderr, flush=True)
        yield
        return
    publisher = AttemptPublisher(
        session.parent.parent / "track" / run, session / "attempts" / attempt, target, halt
    )
    thread = threading.Thread(target=publisher.run, daemon=True, name="etude-view-publish")
    thread.start()
    try:
        yield
    finally:
        publisher.finish.set()
        until = time.monotonic() + min(
            5, max(0, (deadline - datetime.now(UTC)).total_seconds()) if deadline else 5
        )
        while thread.is_alive() and not publisher.stopping() and time.monotonic() < until:
            thread.join(min(0.1, max(0, until - time.monotonic())))
        publisher.stop.set()
