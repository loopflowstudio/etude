"""Copy a session's inbox and evidence, independent of who supplies its machine."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import IO

from pydantic import JsonValue

from etude.launch.log_read import read_deadline
from etude.launch.logging import (
    CLOSED,
    Console,
    bounded,
    explain,
    log_to,
    publish,
    start_publication,
    write_log,
)
from etude.launch.records import Diagnostic, Output
from etude.launch.schemas import AttemptRecord, Bootstrap, Inbox, Result, RunAccess
from etude.launch.storage.files import RESULT, read_result, write
from etude.launch.storage.s3 import (
    PART_BYTES,
    Bucket,
    Denied,
    PostForm,
    Send,
    StorageUnavailable,
    _send,
    post,
    read,
)

ROOT = Path("/run/etude/sessions")
_LOG_EVERY_SECONDS = 15.0


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


def _chunks(stream: IO[bytes]) -> Iterator[bytes]:
    stream.seek(0)
    while chunk := stream.read(PART_BYTES):
        yield chunk


class _Run:
    """Where one run's files go, and with what this machine may write them."""

    def __init__(self, access: RunAccess, send: Send) -> None:
        self.form = access.form
        granted = access.bucket
        self.bucket = (
            None
            if granted is None
            else Bucket(granted.name, granted.region, granted.keys, send, host=granted.host)
        )


class Courier:
    """Carries one session's files between its directory and storage.

    Each attempt's files go to its run's folder (see `BucketJournal`): the log
    and a record of the attempt under `attempts/`, the checkpoint it reports
    under `checkpoints/`, and the job's tracking folder for the run as it is.
    Large files go in parts, and replaced tracking files are deleted, where the
    run's access includes keys; with the form alone, neither happens.
    """

    def __init__(
        self,
        home: Path,
        form: PostForm,
        inbox_url: str,
        send: Send = _send,
        runs: dict[str, RunAccess] | None = None,
    ) -> None:
        self._home, self._form, self._inbox_url, self._send = home, form, inbox_url, send
        self._runs = {run: _Run(access, send) for run, access in (runs or {}).items()}
        self._sent: dict[str, tuple[int, float]] = {}
        self._checkpoint_sent: dict[str, str] = {}
        self._log_sent_at: dict[str, float] = {}
        self._console_position = 0
        self._console = Console("worker output")

    def fetch(self, links: dict[str, str]) -> None:
        """Download files the work needs to where the session process looks for them."""
        for path, link in links.items():
            target = self._home.parent / path
            if target.exists():
                continue
            body = read(link, self._send)
            if body is None:
                raise StorageUnavailable(f"{path} is not in storage")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".partial")
            partial.write_bytes(body)
            partial.replace(target)

    def bring_in(self) -> None:
        """Apply the inbox: mark attempts to stop, and hand over work added since."""
        try:
            raw = read(self._inbox_url, self._send)
        except (StorageUnavailable, OSError, ValueError) as error:
            _diagnose("inbox read", error)
            raise
        if raw is None:
            return
        inbox = Inbox.model_validate_json(raw)
        for attempt in inbox.stop:
            (self._home / "stop" / str(attempt)).touch()
        for entry in inbox.work:
            attempt = entry.work.attempt
            target = self._home / "work" / f"{attempt}.json"
            if not target.exists():
                self._runs.setdefault(entry.work.run, _Run(entry.access, self._send))
                try:
                    self.fetch(entry.fetch)  # files precede executable work
                except (StorageUnavailable, OSError, ValueError) as error:
                    _diagnose("source fetch", error, attempt)
                    raise
                write(target, entry.work.model_dump_json())

    def _write(self, run: str, key: str, stream: IO[bytes]) -> None:
        """Store one object under the run's folder from an open file, read from its start."""
        place = self._runs[run]
        if place.bucket is not None:
            try:
                place.bucket.put_parts(place.form.prefix + key, _chunks(stream))
                return
            except Denied:
                place.bucket = None  # the keys have run out; the form has not
        stream.seek(0)
        post(place.form, place.form.prefix + key, stream.read(), self._send)

    def _put(self, path: Path, run: str, key: str) -> None:
        stat = path.stat()
        seen = (stat.st_size, stat.st_mtime)
        if self._sent.get(f"{run}/{key}") != seen:
            with path.open("rb") as stream:
                self._write(run, key, stream)
            self._sent[f"{run}/{key}"] = seen

    def _clear(self, run: str, removed: list[str]) -> None:
        """Delete what the job retired, but only what this courier uploaded."""
        place = self._runs[run]
        for key in removed:
            if place.bucket is None:
                return
            if f"{run}/{key}" not in self._sent:
                continue
            try:
                place.bucket.delete(place.form.prefix + key)
            except Denied:
                place.bucket = None
                return
            del self._sent[f"{run}/{key}"]

    def _track(self, run: str, done: bool) -> None:
        """The job's tracking folder for this run, as it is, then what it retired.

        A file still being written (`.open`, `.partial`) waits until it is
        whole, and one the job keeps to itself while it runs (`.tail`, and
        anything under a folder whose name starts with `.`) until it has ended.
        A `.removed` file lists paths, relative to the folder, that the job has
        replaced; they are deleted once this pass has sent their replacements.
        """
        folder = self._home.parent.parent / "track" / run
        if not folder.is_dir():
            return
        # Read the removal notes BEFORE listing uploads. A file retired during
        # this pass cannot be deleted before its replacement has been sent.
        removed = [
            line
            for note in sorted(folder.rglob("*.removed"))
            for line in note.read_text().split("\n")[:-1]
        ]
        for path in sorted(folder.rglob("*")):
            name = path.relative_to(folder)
            # Training removes files while this runs. One gone by now is
            # skipped; what replaced it is seen on the next pass.
            try:
                if (
                    S_ISREG(path.stat().st_mode)
                    and not path.is_symlink()
                    and not any(part.startswith(".") for part in name.parts)
                    and path.suffix not in (".open", ".partial")
                    and (done or path.suffix != ".tail")
                ):
                    self._put(path, run, name.as_posix())
            except FileNotFoundError:
                continue
        self._clear(run, removed)

    def carry_out(self, final: bool = False) -> None:
        """Send what changed. An attempt's files go before the record that it finished."""
        self._carry_console(final)
        attempts = self._home / "attempts"
        for home in sorted(attempts.iterdir()) if attempts.is_dir() else []:
            if not (home / "run").exists():
                continue  # not set up yet
            try:
                self._carry_attempt(home, final)
            except (StorageUnavailable, OSError, ValueError) as error:
                _diagnose("output delivery", error, home.name)
                raise

    def _carry_console(self, final: bool) -> None:
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
        """Publish an attempt's files before its completion record."""
        run, attempt = (home / "run").read_text(), home.name
        done = (home / "finished.json").exists()
        claim = home / "claim"
        record = AttemptRecord(
            started_at=datetime.fromisoformat(claim.read_text()) if claim.exists() else None
        )
        outputs = home / "outputs"
        if (outputs / RESULT).exists():
            reported = read_result(outputs)
            checkpoint = reported.checkpoint
            result = Result(clocks=reported.clocks)
            if checkpoint:
                path = outputs / checkpoint
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to(outputs.resolve())
                ):
                    # Digest and upload through one open file, so both see the
                    # same bytes even when training replaces the file meanwhile.
                    with path.open("rb") as stream:
                        hashed, size = hashlib.sha256(), 0
                        for chunk in _chunks(stream):
                            hashed.update(chunk)
                            size += len(chunk)
                        digest = hashed.hexdigest()
                        key = f"checkpoints/{checkpoint}"
                        if self._checkpoint_sent.get(f"{run}/{key}") != digest:
                            self._write(run, key, stream)
                            self._checkpoint_sent[f"{run}/{key}"] = digest
                        # Tracking may hold this same snapshot in its run
                        # folder. Count it as delivered before scanning there.
                        stat = os.fstat(stream.fileno())
                        self._sent[f"{run}/{key}"] = (stat.st_size, stat.st_mtime)
                    result = Result(
                        clocks=reported.clocks,
                        checkpoint=checkpoint,
                        outputs=(Output(name=checkpoint, sha256=digest, size_bytes=size),),
                    )
            record = record.model_copy(update={"result": result})
        self._track(run, done or final)
        log_due = time.monotonic() - self._log_sent_at.get(attempt, 0.0) > _LOG_EVERY_SECONDS
        if (home / "log").exists() and (done or final or log_due):
            self._put(home / "log", run, f"attempts/{attempt}.log")
            self._log_sent_at[attempt] = time.monotonic()
        if done:
            reported_checkpoint = record.result.checkpoint
            for path in sorted(
                p
                for p in outputs.rglob("*")
                if p.is_file()
                and not p.is_symlink()
                and p.resolve().is_relative_to(outputs.resolve())
            ):
                name = path.relative_to(outputs).as_posix()
                if name == RESULT or name == reported_checkpoint:
                    continue  # the record carries the first; the run's checkpoints the second
                kept = outputs / reported_checkpoint if reported_checkpoint else None
                if kept is not None and kept.exists() and path.samefile(kept):
                    continue  # the same file under the job's own name
                self._put(path, run, f"attempts/{attempt}/{name}")
            finished: dict[str, JsonValue] = json.loads((home / "finished.json").read_text())
            record = record.model_copy(update={"finished": finished})
        note = home / "record.json"
        content = record.model_dump_json()
        if not note.exists() or note.read_text() != content:
            write(note, content)
        self._put(note, run, f"attempts/{attempt}.json")


def _run(
    bootstrap_url: str,
    root: Path,
    send: Send,
    sleep: Callable[[float], None],
    every: float,
) -> None:
    process: subprocess.Popen[bytes] | None = None
    courier: Courier | None = None
    deadline: float | None = None
    try:
        if until := os.environ.get("ETUDE_DEADLINE"):
            deadline = time.monotonic() + (datetime.fromisoformat(until) - _now()).total_seconds()
        raw = None
        while raw is None:  # the guardian bounds how long this can go on
            try:
                raw = read(bootstrap_url, send)
            except StorageUnavailable as error:
                _diagnose("bootstrap read", error)
            if raw is None:
                sleep(every)
        write_log("bootstrap read", "received")
        bootstrap = Bootstrap.model_validate_json(raw)
        packet = bootstrap.packet
        home = root / packet.session
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (home / "courier-claim").open("x"):
            pass  # a restarted container must not run the session again
        courier = Courier(home, bootstrap.form, bootstrap.inbox_url, send, bootstrap.runs)
        for folder in ("stop", "work"):
            (home / folder).mkdir(exist_ok=True)
        write_log("source fetch", "starting")
        courier.fetch(bootstrap.fetch)
        write_log("source fetch", "completed")
        write(home / "packet.json", packet.model_dump_json())
        # Persist start intent before user code can run. A lost write reply must not
        # leave running code with no durable evidence that it might have started.
        write_log("receipt upload", "publishing start intent")
        post(courier._form, courier._form.prefix + "receipt", str(os.getpid()).encode(), send)
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
        write(home / "receipt", str(process.pid))
        while process.poll() is None:
            for step in (courier.bring_in, courier.carry_out):
                try:
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
        try:
            if process is not None:
                assert courier is not None
                _finish_session(courier, process, sleep, every, deadline)
            write_log("cleanup", "finalizing worker output before cleanup")
            write_log("worker", CLOSED)
        except Exception as error:
            _diagnose("final collection", error)
            if courier is not None:
                try:
                    # Keep complete lines already available, without inventing EOF.
                    courier._carry_console(final=False)
                except Exception as console_error:
                    _diagnose("console collection", console_error)
            write_log("worker", "collection incomplete; final output unconfirmed")
            if original_error is None:
                raise


def _finish_session(
    courier: Courier,
    process: subprocess.Popen[bytes],
    sleep: Callable[[float], None],
    every: float,
    deadline: float | None,
) -> None:
    """Reap the owned child and collect EOF before declaring this producer closed."""
    # Share the existing forty-pass allowance across shutdown and final delivery.
    # The provider's absolute deadline can shorten it; the guardian never waits here.
    latest = time.monotonic() + 40 * every
    deadline = latest if deadline is None else min(latest, deadline)
    if process.poll() is None:
        write_log("worker stop", "requesting session shutdown")
        process.terminate()
        try:
            # The session gives its job two five-second signal/reap windows.
            process.wait(timeout=min(10.0, max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
    write_log("worker exit", f"exit status {process.returncode}")
    courier._carry_console(final=True)
    ended = courier._home / "ended"
    for _ in range(40):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            # S3 transports already share this monotonic request allowance.
            with read_deadline(left):
                courier.carry_out(final=True)
                note = ended.read_text() if ended.exists() else _now().isoformat()
                post(courier._form, courier._form.prefix + "ended", note.encode(), courier._send)
            write_log("final delivery", "completed")
            return
        except (StorageUnavailable, OSError) as error:
            _diagnose("final delivery", error)
            sleep(min(every, max(0.0, deadline - time.monotonic())))
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
    publishing = start_publication(log, url) if url else None
    with log_to(log):
        try:
            write_log("worker entry", "client ready")
            _run(bootstrap_url, root, send, sleep, every)
        finally:
            if publishing is not None:
                publishing.set()
                publish(log, url)
            original_error = sys.exception()
            try:
                delete_self()
            except Exception as error:
                print("cleanup unconfirmed: " + explain(error), file=sys.stderr)
                if original_error is None:
                    raise
