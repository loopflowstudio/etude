"""Copy a session's inbox and evidence, independent of its machine supplier.

Courier publishes tracked files in order and final outputs with bounded parallel
I/O. Its invoking thread owns transfer retention, the sent cache and completion.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from etude.cache import Cache
from etude.carriage import (
    OUT_PRESENT,
    RecordedFile,
    checkpoint_file,
    checkpoint_file_name,
    immutable,
    note_stored,
    older_saves,
    prune_state,
    publication,
    purpose,
    reconcile_out,
    recorded_files,
    recovery_files,
    resume_file,
    retired,
    save_pointer,
    state_pointer,
    within,
)
from etude.deadline import limit_time
from etude.launch.environment import startup_timeout
from etude.launch.logging import CLOSED, Console, bounded, explain, log_to, publish, write_log
from etude.launch.records import Diagnostic
from etude.launch.schemas import (
    WORKER_LOG,
    AttemptRecord,
    Bootstrap,
    Fetch,
    Finished,
    Inbox,
    ReadLink,
    Work,
    WorkerLog,
    attempt_log,
    log_piece,
)
from etude.launch.storage.download import Download, StoredContentMismatch, download
from etude.launch.storage.files import RESULT, append_usage, read_result, write
from etude.launch.storage.s3 import (
    PART_BYTES,
    Bucket,
    Denied,
    Grant,
    Version,
    _send,
    post,
    read,
    read_version,
)
from etude.launch.storage.transport import Send, attribute, deliver, observe
from etude.launch.worker.attempts import _input_path, seed_out
from etude.out import MEMBERSHIP, OutFile, OutFiles
from etude.storage_timeout import StorageUnavailable

ROOT = Path("/run/etude/sessions")
# A log piece waits about a twentieth of its log's age, between 15 seconds and
# 5 minutes, before it is sealed: new output reaches storage within that, and
# a 12-hour attempt or session leaves about two hundred pieces, not one per poll.
_PIECE_LEAST_SECONDS = 15.0
_PIECE_MOST_SECONDS = 300.0
# Small independent evidence objects are latency-bound; cap in-flight files.
_OUTPUT_UPLOADS = 8


@dataclass(frozen=True)
class _Upload:
    path: Path
    run: str
    key: str
    size: int
    modified: float


@dataclass(frozen=True)
class _Uploaded:
    file: _Upload
    transfers: tuple[str, ...]
    error: BaseException | None


def _diagnose(operation: str, error: BaseException, attempt: str | None = None) -> None:
    message = bounded(explain(error))
    session = os.environ.get("ETUDE_SESSION")
    diagnostic = Diagnostic(
        id=hashlib.sha256(f"{session}:{attempt}:{operation}:{message}".encode()).hexdigest(),
        observed_at=_now(),
        operation=operation,
        message=message,
        exception=type(error).__name__,
        session=session,
        attempt=attempt,
    )
    write_log("diagnostic", diagnostic.model_dump_json())


def _now() -> datetime:
    return datetime.now(UTC)


def _until_deadline() -> float | None:
    """Seconds left before the rental's deadline (`ETUDE_DEADLINE`), when it has one."""
    deadline = os.environ.get("ETUDE_DEADLINE")
    return None if not deadline else (datetime.fromisoformat(deadline) - _now()).total_seconds()


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    stream.seek(0)
    while chunk := stream.read(PART_BYTES):
        yield chunk


class _Folder:
    """Where one granted folder's files go, and with what this machine may write them."""

    def __init__(self, grant: Grant, send: Send) -> None:
        self.prefix, self.form, self._send = grant.prefix, grant.form, send
        granted = grant.bucket
        self.bucket = (
            None
            if granted is None
            else Bucket(granted.name, granted.region, granted.keys, send, host=granted.host)
        )

    def write(self, key: str, stream: IO[bytes]) -> None:
        """Store one object from an open file, read from its start: in parts with keys."""
        bucket = self.bucket
        if bucket is not None:
            try:
                bucket.put_parts(self.prefix + key, _chunks(stream))
                return
            except Denied:
                if self.form is None:
                    raise  # R2: the keys were all there was
                self.bucket = None  # the keys have run out; the form has not
        assert self.form is not None  # a grant holds keys or a form
        stream.seek(0)
        post(self.form, self.prefix + key, stream.read(), self._send)

    def write_bytes(self, key: str, body: bytes) -> None:
        self.write(key, io.BytesIO(body))

    def delete(self, key: str) -> bool:
        """Remove an object; False when these keys cannot, or no keys were granted."""
        if self.bucket is None:
            return False
        try:
            self.bucket.delete(self.prefix + key)
        except Denied:
            self.bucket = None
            return False
        return True


class _Log:
    """One log as carried, an attempt's or the worker's: where its sealed pieces end, and when."""

    def __init__(self, now: float) -> None:
        self.ends: tuple[int, ...] = ()
        self.opened = now
        self.sealed: float | None = None  # the first output is sent at once

    @property
    def sent(self) -> int:
        return self.ends[-1] if self.ends else 0

    def due(self, now: float) -> bool:
        if self.sealed is None:
            return True
        wait = (now - self.opened) / 20
        return now - self.sealed >= min(_PIECE_MOST_SECONDS, max(_PIECE_LEAST_SECONDS, wait))


class Courier:
    """Carries one session's files between its directory and storage.

    Each attempt's files go to its run's folder (see `BucketJournal`): its log,
    in appended pieces each sent once, and a record of the attempt naming them,
    under `attempts/`, and the run's tracked folder as it is, by the rules the
    tracker's own shipper follows (`etude.carriage`), so every checkpoint the
    tracker records is in storage while the attempt still runs. The worker's
    own log, `worker_log`, goes to the session's `out/` the same way, named by
    `WorkerLog`. Large files go in parts, and replaced tracking files are
    deleted, where the run's grant includes keys; with a form alone, neither
    happens.
    """

    def __init__(
        self,
        home: Path,
        out: Grant,
        inbox_url: str,
        send: Send = _send,
        runs: dict[str, Grant] | None = None,
        worker_log: Path | None = None,
    ) -> None:
        self._home, self._inbox_url, self._send = home, inbox_url, send
        self._worker_log = worker_log
        self._worker_log_carried: _Log | None = None  # from its first output
        self._out = _Folder(out, send)  # the session's own folder
        self._runs = {run: _Folder(grant, send) for run, grant in (runs or {}).items()}
        # What this courier stored or restored, by run and key. Out files and
        # membership by content; other files by their size and mtime then.
        self._sent: dict[tuple[str, str], str | tuple[int, float]] = {}
        self._logs: dict[str, _Log] = {}
        self._delivered: set[str] = set()  # attempts whose completion record is stored
        self._console_position = 0
        self._console = Console("worker output")
        self._inbox: Version | None = None  # the inbox as last read
        self._usage_sent = -1  # bytes of the usage journal last delivered
        self._cache = _cache()
        # Monotonic time of the last pass that left every file delivered, or the
        # courier's start: a stall is reported from here, not from its first failure.
        self._delivered_at = time.monotonic()
        self._pending = False

    def _retain_transfer(self, raw: str) -> None:
        """Keep a transfer in the session's usage journal, not the worker log.

        The log says what changed; usage and diagnostics read this journal.
        """
        append_usage(self._home, raw)

    def _carry_usage(self) -> None:
        path = self._home / "usage.jsonl"
        if path.exists() and path.stat().st_size != self._usage_sent:
            body = path.read_bytes()
            # Delivering the journal is not itself journaled: each delivery would
            # change it and need another. A failed delivery raises to the caller.
            with limit_time(_until_deadline()), observe(lambda raw: None):
                self._out.write_bytes("usage.jsonl", body)
            self._usage_sent = len(body)

    def fetch(self, fetch: Fetch) -> None:
        """Verify transport bytes before exposing inputs, including existing paths."""
        with observe(self._retain_transfer), attribute(session=self._home.name):
            try:
                with limit_time(_until_deadline()):
                    for commit, source in fetch.sources.items():
                        with attribute(purpose="source"):
                            download(
                                source,
                                self._home / "sources" / f"{commit}.tar",
                                send=self._send,
                                cache=self._cache,
                            )
                    for given in fetch.inputs:
                        with attribute(purpose="input"):
                            download(
                                given,
                                self._home / "inputs" / given.sha256,
                                send=self._send,
                                cache=self._cache,
                                # Inputs stay in the cache for this session's attempts to link.
                                holder=self._home.name,
                            )
            finally:
                # Recovery evidence is outside the discretionary input allowance.
                self._carry_usage()

    def release(self) -> None:
        """Let the cache evict this session's inputs; attempts' links keep their bytes."""
        self._cache.release(self._home.name)

    def initialize_out(self, work: Work) -> None:
        """Store a continuation's initial state before allowing its first claim.

        After a claim, retries depend on the child's carried state alone; its
        parent's mutable directory may already have changed.
        """
        inputs = [given for given in work.inputs if given.out is not None]
        if not inputs:
            return
        with (
            observe(self._retain_transfer),
            attribute(session=self._home.name, run=work.run, attempt=work.attempt, purpose="input"),
        ):
            try:
                files = {
                    given.name: _input_path(self._home, self._track_root, given) for given in inputs
                }
                seed_out(self._track_root / work.run / "out", inputs, files)
                self._track(work.run, done=False)
            finally:
                self._carry_usage()

    def restore(self, links: dict[str, ReadLink], works: Sequence[Work]) -> None:
        """Put back what storage holds of a resumed run's folder, before its job starts.

        `links` name objects and lengths by RUN/KEY. Keep existing immutable
        records, replace out files, and verify the latest copied checkpoint.
        A run whose latest attempt ran here keeps its folder, which is newer
        than storage, unless the folder is gone. What is restored counts as
        sent, so it is not uploaded back. Checkpoint names left in storage stay
        taken (`etude.carriage.taken`).
        """
        held = {w.run for w in works if w.here and (self._track_root / w.run).is_dir()}
        by_run: dict[str, dict[str, ReadLink]] = {}
        for name, url in links.items():
            run, _, key = name.partition("/")
            if run not in held:
                by_run.setdefault(run, {})[key] = url
        with observe(self._retain_transfer), attribute(session=self._home.name):
            try:
                with limit_time(_until_deadline()):
                    for run, keys in sorted(by_run.items()):
                        self._restore(run, keys)
            finally:
                self._carry_usage()

    def _restore(self, run: str, keys: dict[str, ReadLink]) -> None:
        folder = self._track_root / run
        # Every checkpoint name storage holds stays taken, restored or not.
        note_stored(folder, [checkpoint_file_name(key) for key in keys if checkpoint_file(key)])
        membership = None
        members = None
        if MEMBERSHIP in keys:
            body = read(keys[MEMBERSHIP].url, self._send)
            if body is None:
                raise StorageUnavailable("out membership is missing")
            membership = OutFiles.model_validate_json(body)
            members = {file.key for file in membership.files}
            ordinary = {name for name in members if save_pointer(name) is None}
            if not ordinary <= keys.keys():
                raise StorageUnavailable("out member is missing")
        pointers: dict[str, bytes] = {}
        for key, link in keys.items():
            if state_pointer(key):
                body = read(link.url, self._send)
                if body is None:
                    raise StorageUnavailable("state pointer is missing")
                pointers[key] = body
        selected = recovery_files(keys, members, pointers)
        keys = {key: keys[key] for key in selected}
        reconcile_out(folder, keys)
        recorded: dict[str, RecordedFile] | None = None
        for key, link in sorted(keys.items()):
            if not within(key):
                raise StorageUnavailable("restore path is outside the run")
            target = folder / key
            if (
                key == MEMBERSHIP
                or checkpoint_file(key)
                or target.exists()
                and not key.startswith("out/")
            ):
                continue
            if recorded is None and key.startswith("out/"):
                recorded = recorded_files(folder)
            named = None if recorded is None else recorded.get(key)
            with attribute(run=run, purpose="checkpoint" if named is not None else "report"):
                body = pointers[key] if key in pointers else read(link.url, self._send)
            if body is None or len(body) != link.size_bytes:
                raise StorageUnavailable(f"restored file {key} is missing or has changed length")
            # Reuse the received bytes for verification and the sent marker;
            # restoring a large out file needs no second read from disk.
            digest = hashlib.sha256(body).hexdigest() if key.startswith("out/") else None
            if named is not None and (len(body) != named.size_bytes or digest != named.sha256):
                raise StorageUnavailable(f"restored file {key} differs from its checkpoint record")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            partial.write_bytes(body)
            partial.replace(target)
            if digest is not None:
                self._sent[run, key] = pointers[key].decode() if key in pointers else digest
            else:
                self._restored(run, key, target)
        if membership is not None:
            (folder / "out").mkdir(parents=True, exist_ok=True)
            (folder / OUT_PRESENT).touch()
            self._sent[run, MEMBERSHIP] = membership.model_dump_json()
        wanted = resume_file(folder)
        if wanted is None or (folder / wanted.key).exists() or wanted.key not in keys:
            return
        with attribute(run=run, purpose="checkpoint"):
            download(
                Download(
                    url=keys[wanted.key].url, sha256=wanted.sha256, size_bytes=wanted.size_bytes
                ),
                folder / wanted.key,
                send=self._send,
            )
        self._restored(run, wanted.key, folder / wanted.key)

    def _restored(self, run: str, key: str, path: Path) -> None:
        stat = path.stat()
        self._sent[run, key] = (stat.st_size, stat.st_mtime)

    @property
    def _track_root(self) -> Path:
        """Where jobs on this machine keep their runs' tracked folders (`ETUDE_TRACK_DIR`)."""
        return self._home.parent.parent / "track"

    def bring_in(self) -> None:
        """Apply the inbox: mark attempts to stop, and hand over work added since.

        A poll asks with the ETag of the inbox last read, so an unchanged inbox
        sends no body, and leaves no record; a changed or failed read keeps its
        transfer, and a change is logged.
        """
        polled: list[str] = []
        try:
            with observe(polled.append), attribute(session=self._home.name):
                found = read_version(self._inbox_url, self._inbox, self._send)
        except (StorageUnavailable, OSError, ValueError) as error:
            for event in polled:
                self._retain_transfer(event)
            _diagnose("inbox read", error)
            raise
        before, self._inbox = self._inbox, found
        unchanged = (found is None and before is None) or (
            found is not None and before is not None and found.body == before.body
        )
        if unchanged:
            return
        for event in polled:
            self._retain_transfer(event)
        if found is None:
            return
        inbox = Inbox.model_validate_json(found.body)
        finish = ", finish when done" if inbox.finish else ""
        write_log("inbox", f"{len(inbox.work)} work, {len(inbox.stop)} to stop{finish}")
        for attempt in inbox.stop:
            (self._home / "stop" / str(attempt)).touch()
        for entry in inbox.work:
            attempt = entry.work.attempt
            target = self._home / "work" / f"{attempt}.json"
            if not target.exists():
                self._runs.setdefault(entry.work.run, _Folder(entry.access, self._send))
                try:
                    # One work's files, so its run's usage counts them.
                    with attribute(run=entry.work.run, attempt=attempt):
                        self.fetch(entry.fetch)  # files precede executable work
                    self.restore(entry.restore, [entry.work])
                    self.initialize_out(entry.work)
                except StoredContentMismatch as error:
                    # Permanent: hand the work over so its setup fails and says why,
                    # rather than asking for the same wrong bytes until the deadline.
                    _diagnose("input verification", error, attempt)
                except (StorageUnavailable, OSError, ValueError) as error:
                    self._inbox = before  # the same inbox still contains un-applied work
                    _diagnose("source fetch", error, attempt)
                    raise
                write(target, entry.work.model_dump_json())
        if inbox.finish:
            (self._home / "finish").touch()  # after any work, so the session runs it first

    def _unsent(self, path: Path, run: str, key: str) -> _Upload | None:
        stat = path.stat()
        sent = self._sent.get((run, key))
        if sent is not None and (immutable(key) or sent == (stat.st_size, stat.st_mtime)):
            return None
        return _Upload(path, run, key, stat.st_size, stat.st_mtime)

    def _upload(self, file: _Upload) -> _Uploaded:
        # Network work may run in a thread. The retaining owner receives its
        # observations later, on the thread that invoked output delivery.
        transfers: list[str] = []
        try:
            with observe(transfers.append), file.path.open("rb") as stream:
                self._runs[file.run].write(file.key, stream)
        except BaseException as error:
            return _Uploaded(file, tuple(transfers), error)
        return _Uploaded(file, tuple(transfers), None)

    def _accept(self, uploaded: _Uploaded) -> None:
        for raw in uploaded.transfers:
            deliver(raw)
        if uploaded.error is not None:
            raise uploaded.error
        file = uploaded.file
        self._sent[file.run, file.key] = (file.size, file.modified)

    def _put(self, path: Path, run: str, key: str) -> None:
        if file := self._unsent(path, run, key):
            self._accept(self._upload(file))

    def _put_out(self, run: str, key: str, recorded: RecordedFile | None) -> OutFile:
        """Send changed out bytes, keeping recorded checkpoints immutable."""
        sent_key = (run, key)
        if recorded is not None and self._sent.get(sent_key) == recorded.sha256:
            return OutFile(key=key, sha256=recorded.sha256, size_bytes=recorded.size_bytes)
        # Keep one open inode through hashing and upload: atomic replacement
        # during the pass cannot mix two saves.
        with (self._track_root / run / key).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            size = os.fstat(stream.fileno()).st_size
            if recorded is not None and digest != recorded.sha256:
                raise StorageUnavailable(f"out checkpoint {key} changed")
            if digest != self._sent.get(sent_key):
                self._runs[run].write(key, stream)
        self._sent[sent_key] = digest
        return OutFile(key=key, sha256=digest, size_bytes=size)

    def _put_outputs(self, outputs: Path, run: str, attempt: str) -> None:
        paths = iter(
            sorted(
                p
                for p in outputs.rglob("*")
                if p.is_file()
                and not p.is_symlink()
                and p.resolve().is_relative_to(outputs.resolve())
                and p.relative_to(outputs).as_posix() != RESULT
            )
        )
        error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=_OUTPUT_UPLOADS) as pool:

            def submit() -> Future[_Uploaded] | None:
                nonlocal error
                if error is not None:
                    return None
                try:
                    for path in paths:
                        name = path.relative_to(outputs).as_posix()
                        if file := self._unsent(path, run, f"attempts/{attempt}/{name}"):
                            return pool.submit(copy_context().run, self._upload, file)
                except BaseException as raised:
                    error = raised
                return None

            pending = {future for _ in range(_OUTPUT_UPLOADS) if (future := submit()) is not None}
            try:
                while pending:
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        try:
                            self._accept(future.result())
                        except BaseException as raised:
                            error = error or raised
                    if error is None:
                        pending.update(
                            future for _ in completed if (future := submit()) is not None
                        )
            except BaseException as raised:
                error = error or raised
            finally:
                # A caller interruption must also retain in-flight observations.
                # Workers share its deadline; no new work starts after failure.
                for future in pending:
                    try:
                        self._accept(future.result())
                    except BaseException as raised:
                        error = error or raised
        if error is not None:
            raise error

    def _clear(self, run: str, removed: list[str]) -> None:
        """Delete what the job retired, but only what this courier uploaded."""
        for key in removed:
            if (run, key) not in self._sent:
                continue
            if not self._runs[run].delete(key):
                return
            del self._sent[run, key]

    def _track(self, run: str, done: bool) -> None:
        """The job's tracked folder for this run, as it is, then what it retired.

        Which files are ready, their order and purpose follow `etude.carriage`.
        Tails wait until the attempt has ended. A retired file is deleted
        once this pass has sent its replacement.
        """
        folder = self._track_root / run
        removed = retired(folder) if folder.is_dir() else []
        recorded = recorded_files(folder)
        out: list[OutFile] = []
        snapshot = publication(folder, tail_seconds=0.0 if done else math.inf)
        acknowledged = {
            key: prior.encode()
            for key in snapshot.pointers
            if isinstance(prior := self._sent.get((run, key)), str)
        }
        for key in snapshot.keys:
            if key == MEMBERSHIP:
                continue  # only this pass can publish its membership
            # Training removes files while this runs. One gone by now is
            # skipped; what replaced it is seen on the next pass.
            try:
                with attribute(purpose=purpose(key, recorded)):
                    if key.startswith("out/"):
                        if key in snapshot.pointers:
                            pointer_body = snapshot.pointers[key]
                            digest = hashlib.sha256(pointer_body).hexdigest()
                            if self._sent.get((run, key)) != pointer_body.decode():
                                self._runs[run].write_bytes(key, pointer_body)
                                self._sent[run, key] = pointer_body.decode()
                            out.append(
                                OutFile(key=key, sha256=digest, size_bytes=len(pointer_body))
                            )
                        else:
                            out.append(self._put_out(run, key, recorded.get(key)))
                    else:
                        self._put(folder / key, run, key)
            except FileNotFoundError:
                if save_pointer(key) is not None:
                    raise  # never publish a pointer after skipping one of its files
                continue
        if (folder / "out").is_dir() or (folder / OUT_PRESENT).is_file():
            body = OutFiles(files=tuple(out)).model_dump_json()
            if self._sent.get((run, MEMBERSHIP)) != body:
                self._runs[run].write_bytes(MEMBERSHIP, body.encode())
                (folder / OUT_PRESENT).touch()
                self._sent[run, MEMBERSHIP] = body
        self._clear(
            run, older_saves((key for owner, key in self._sent if owner == run), acknowledged)
        )
        self._clear(run, removed)
        prune_state(folder, acknowledged)

    def carry_out(self, final: bool = False) -> None:
        """Send changed files before their attempt's completion record, and the worker's log.

        `final` seals every tail now. The session's console is read only as far
        as complete lines; its owner collects the rest once the session exits.
        An attempt that cannot be carried raises after the others, the worker's
        log and usage are: that log is where its failure is told.
        """
        try:
            self._carry_files(final)
        except (StorageUnavailable, OSError, ValueError) as error:
            self._pending = True
            write_log(
                "output delivery",
                f"has not succeeded for {self._stalled_minutes():.1f} minutes"
                f" ({type(error).__name__}); local files retained",
            )
            raise
        # Final delivery belongs to a producer that has already closed its log.
        # Its end receipt confirms recovery; append no output after EOF.
        if self._pending and not final:
            write_log("output delivery", f"recovered after {self._stalled_minutes():.1f} minutes")
        self._pending = False
        self._delivered_at = time.monotonic()

    def _stalled_minutes(self) -> float:
        return (time.monotonic() - self._delivered_at) / 60

    def _carry_files(self, final: bool) -> None:
        failure: Exception | None = None
        with observe(self._retain_transfer), attribute(session=self._home.name):
            self.carry_console()
            attempts = self._home / "attempts"
            for home in sorted(attempts.iterdir()) if attempts.is_dir() else []:
                if not (home / "run").exists():
                    continue  # not set up yet
                try:
                    with attribute(
                        run=(home / "run").read_text(), attempt=home.name, purpose="result"
                    ):
                        self._carry_attempt(home, final)
                except (StorageUnavailable, OSError, ValueError) as error:
                    _diagnose("output delivery", error, home.name)
                    failure = failure or error
            with attribute(purpose="control"):
                self._carry_worker_log(final)
        self._carry_usage()
        if failure is not None:
            raise failure

    def _carry_worker_log(self, final: bool) -> None:
        """Seal the worker's new output as pieces, then the record naming them."""
        log = self._worker_log
        if log is None or not log.exists():
            return
        carried = self._worker_log_carried = self._worker_log_carried or _Log(time.monotonic())
        if not (final or carried.due(time.monotonic())):
            return
        before = carried.ends
        self._seal_pieces(log, self._out, WORKER_LOG, carried)
        if carried.ends != before:
            self._out.write_bytes(
                "worker-log.json", WorkerLog(ends=carried.ends).model_dump_json().encode()
            )

    def carry_console(self, final: bool = False) -> None:
        """Retain the session's console in the worker's log, sanitized line by line.

        `final` reads it to its end, once the session has exited: a last line
        without a newline is kept, and a console that is gone raises.
        """
        console = self._home / "session.log"
        if final or console.exists():
            with console.open("rb") as output:
                output.seek(self._console_position)
                while body := output.read(65536):
                    self._console.write(body)
                    self._console_position += len(body)
            if final:
                self._console.write(b"", final=True)

    def _carry_attempt(self, home: Path, final: bool) -> None:
        """Publish how an attempt ended, then its files, then its completion record."""
        run, attempt = (home / "run").read_text(), home.name
        done = (home / "finished.json").exists()
        claim, outputs = home / "claim", home / "outputs"
        entry = Finished.model_validate_json((home / "finished.json").read_text()) if done else None
        carried = self._logs.get(attempt)
        record = AttemptRecord(
            started_at=datetime.fromisoformat(claim.read_text()) if claim.exists() else None,
            result=read_result(outputs),
            exited=entry,
            log_ends=None if carried is None else carried.ends,
        )
        if done and attempt not in self._delivered:
            # Before any file: what fails below must not hide how the job ended.
            self._note(home, run, attempt, record)
        self._track(run, done or final)
        if (home / "log").exists():
            log = self._logs.setdefault(attempt, _Log(time.monotonic()))
            if done or final or log.due(time.monotonic()):
                self._seal_pieces(home / "log", self._runs[run], attempt_log(attempt), log)
            record = record.model_copy(update={"log_ends": log.ends})
        if done:
            self._put_outputs(outputs, run, attempt)
            record = record.model_copy(update={"delivered": True})
        self._note(home, run, attempt, record)
        if done:
            self._delivered.add(attempt)

    def _note(self, home: Path, run: str, attempt: str, record: AttemptRecord) -> None:
        """Store the attempt's record, when it changed."""
        note = home / "record.json"
        content = record.model_dump_json()
        if not note.exists() or note.read_text() != content:
            write(note, content)
        with attribute(purpose="control"):
            self._put(note, run, f"attempts/{attempt}.json")

    def _seal_pieces(self, path: Path, folder: _Folder, name: str, log: _Log) -> None:
        """Store what was written to `path` since the last piece, as new pieces of `name`.

        A log is only appended to, so the bytes before `log.sent` are stored
        already. A piece is at most one part, so each is one request. A failed
        write leaves the piece unsealed; it is written again whole, under the
        same name, and no record has named it yet.
        """
        with path.open("rb") as stream:
            # Up to what is there now: output printed during the upload waits
            # for the next piece instead of making a small one of its own.
            size = os.fstat(stream.fileno()).st_size
            stream.seek(log.sent)
            while body := stream.read(min(PART_BYTES, size - log.sent)):
                folder.write_bytes(log_piece(name, log.sent), body)
                log.ends = (*log.ends, log.sent + len(body))
                log.sealed = time.monotonic()


def _serve(
    bootstrap_url: str,
    root: Path,
    send: Send,
    sleep: Callable[[float], None],
    every: float,
    worker_log: Path,
    log_url: str,
) -> None:
    process: subprocess.Popen[bytes] | None = None
    courier: Courier | None = None
    deadline: float | None = None
    delivery_seconds: float | None = None
    try:
        if until := os.environ.get("ETUDE_DEADLINE"):
            deadline = time.monotonic() + (datetime.fromisoformat(until) - _now()).total_seconds()
        raw = None
        while raw is None:  # startup allowance and the independent rental guardian both apply
            startup_timeout()
            try:
                with limit_time(startup_timeout()):
                    raw = read(bootstrap_url, send)
            except StorageUnavailable as error:
                _diagnose("bootstrap read", error)
                raise  # GET owns retries; only a missing object remains pending here.
            if raw is None:
                sleep(every)
        write_log("bootstrap read", "received")
        bootstrap = Bootstrap.model_validate_json(raw)
        packet = bootstrap.packet
        delivery_seconds = packet.delivery_seconds
        os.environ["ETUDE_LOG_SECONDS"] = (
            str(packet.log_seconds) if packet.log_seconds is not None else ""
        )
        home = root / packet.session
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (home / "courier-claim").open("x"):
            pass  # a restarted container must not run the session again
        courier = Courier(
            home, bootstrap.out, bootstrap.inbox_url, send, bootstrap.runs, worker_log
        )
        for folder in ("stop", "work"):
            (home / folder).mkdir(exist_ok=True)
        write_log("source fetch", "starting")
        # A packet of one work's files is that run's usage; a cohort's stay shared.
        only = packet.work[0] if len(packet.work) == 1 else None
        with limit_time(startup_timeout()):
            with attribute(
                run=None if only is None else only.run,
                attempt=None if only is None else only.attempt,
            ):
                courier.fetch(bootstrap.fetch)
            courier.restore(bootstrap.restore, packet.work)
            for work in packet.work:
                courier.initialize_out(work)
        write_log("source fetch", "completed")
        write(home / "packet.json", packet.model_dump_json())
        # Persist start intent before user code can run. A lost write reply must not
        # leave running code with no durable evidence that it might have started.
        startup_timeout()
        write_log("receipt upload", "publishing start intent")
        with limit_time(startup_timeout()):
            courier._out.write_bytes("receipt", str(os.getpid()).encode())
        startup_timeout()
        write_log("receipt upload", "confirmed")
        package_root = str(Path(__file__).resolve().parents[3])
        with (home / "session.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "etude", "worker", str(home)],
                cwd=home,
                env={**os.environ, "PYTHONPATH": package_root},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        write(home / "pid", str(process.pid))
        while process.poll() is None:
            for step in (courier.bring_in, courier.carry_out):
                try:
                    with limit_time(_until_deadline()):
                        step()
                except (StorageUnavailable, OSError, ValueError) as error:
                    write_log(
                        "work delivery",
                        f"pending after {type(error).__name__}; see retained diagnostics",
                    )
            sleep(every)
    except BaseException as error:
        _diagnose("worker", error)
        raise
    finally:
        original_error = sys.exception()
        left = _allowance(deadline, delivery_seconds)
        collected, failure = False, None
        try:
            if process is not None:
                assert courier is not None
                _reap(courier, process, left)
                courier.release()
            write_log("cleanup", "finalizing worker output before cleanup")
            write_log("worker", CLOSED)
            collected = True
        except Exception as error:
            failure = error
            _diagnose("final collection", error)
            if courier is not None:
                try:
                    # Keep complete lines already available, without inventing EOF.
                    courier.carry_console()
                except Exception as console_error:
                    _diagnose("console collection", console_error)
            write_log("worker", "collection incomplete; final output unconfirmed")
        try:
            if courier is not None:
                ended = collected and process is not None  # a session ran, and its output is in
                _deliver(courier, sleep, every, left, closed=collected, ended=ended)
            elif log_url:
                publish(worker_log, log_url)  # no grant was read: the log goes whole, by its link
        except Exception as error:
            _diagnose("final delivery", error)
            failure = failure or error
        if failure is not None and original_error is None:
            raise failure


def _cache() -> Cache:
    """This machine's cache: what it holds plus half the disk free when the session began.

    A rental's disk, not a fixed size, decides what fits, so its weights are
    held once rather than staged beside every session.
    """
    root = Cache().root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    held = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
    return Cache(root, max_bytes=held + shutil.disk_usage(root).free // 2)


def _allowance(deadline: float | None, seconds: float | None) -> Callable[[], float | None]:
    """Seconds left of the one monotonic allowance that shutdown and final delivery share:
    `seconds` from now, capped by the rental's deadline."""
    if seconds is not None:
        latest = time.monotonic() + seconds
        deadline = latest if deadline is None else min(latest, deadline)

    def left() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    return left


def _reap(
    courier: Courier, process: subprocess.Popen[bytes], left: Callable[[], float | None]
) -> None:
    """Reap the owned child and collect its console to EOF before this producer closes."""
    if process.poll() is None:
        write_log("worker stop", "requesting session shutdown")
        process.terminate()
        try:
            process.wait(timeout=left())
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=left())
    write_log("worker exit", f"exit status {process.returncode}")
    courier.carry_console(final=True)


def _deliver(
    courier: Courier,
    sleep: Callable[[float], None],
    every: float,
    left: Callable[[], float | None],
    *,
    closed: bool,
    ended: bool,
) -> None:
    """Deliver the session's last files and the tail of its log, then its end receipt.

    A machine may be deleted as soon as its end receipt is seen, so the
    receipt goes last, and only for a session that ran. A failed try is
    diagnosed in the log, which is closed again after it when its producer
    had closed it, and tried again within the allowance; an end receipt
    never sent leaves the end to the machine's cleanup or its deadline.
    """
    note = courier._home / "ended"
    for _ in range(40):
        allowance = left()
        if allowance is not None and allowance <= 0:
            break
        try:
            # S3 transports already share this monotonic request allowance.
            with limit_time(allowance):
                courier.carry_out(final=True)
                if ended:
                    at = note.read_text() if note.exists() else _now().isoformat()
                    courier._out.write_bytes("ended", at.encode())
            return
        except (StorageUnavailable, OSError, ValueError) as error:
            _diagnose("final delivery", error)
            if closed:
                write_log("worker", CLOSED)
            pause = left()
            sleep(every if pause is None else min(every, pause))
    write_log("final delivery", "incomplete; final files or end receipt not confirmed")


def run(
    bootstrap_url: str,
    delete_self: Callable[[], None],
    root: Path = ROOT,
    send: Send = _send,
    sleep: Callable[[float], None] = time.sleep,
    every: float = 3.0,
) -> None:
    session = os.environ.get("ETUDE_SESSION")
    log = root / session / "worker.log" if session else root / "worker.log"
    url = os.environ.get("ETUDE_WORKER_LOG_URL", "")
    with log_to(log):
        try:
            write_log("worker entry", "client ready")
            _serve(bootstrap_url, root, send, sleep, every, log, url)
        finally:
            original_error = sys.exception()
            try:
                delete_self()
            except Exception as error:
                print("cleanup unconfirmed: " + explain(error), file=sys.stderr)
                if original_error is None:
                    raise
