"""Bucket journals shared by owned hosts and rented machines."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Concatenate

from etude.carriage import (
    CHECKPOINTS,
    checkpoint_file,
    checkpoint_file_name,
    named_file,
    note_stored,
    older_saves,
    purpose,
    recorded_file,
    recorded_files,
    recovery_files,
    restore_set,
    resume_file,
    save_pointer,
    state_pointer,
)
from etude.launch.logging import parse_log_line
from etude.launch.logs import snapshot_stream
from etude.launch.records import BootstrapLinks, LauncherError, LogStream, Storage
from etude.launch.schemas import (
    AttemptRecord,
    Bootstrap,
    Delivery,
    Fetch,
    Inbox,
    Observation,
    Packet,
    Progress,
    ReadLink,
    Work,
    WorkerLog,
)
from etude.launch.source import source_archive
from etude.launch.storage.download import Download, download
from etude.launch.storage.s3 import Bucket, Grant, Version
from etude.launch.storage.transport import attribute
from etude.launch.usage import Transfer
from etude.out import MEMBERSHIP, OutFiles
from etude.storage_timeout import StorageUnavailable

# How long a link lasts where no rental bounds it: a session on a machine you
# own, and links the launcher reads itself. A rented session's grants last
# until its deadline instead.
_GRANT_SECONDS = 13 * 3600
# Sessions whose last-read versions stay in memory. Logs are the bulk; one
# beyond this is read again, once, when it is next asked about.
_SESSIONS_KEPT = 64


def _seconds_until(deadline: datetime | None) -> int:
    """How long a grant must last to reach `deadline`, or `_GRANT_SECONDS` without one."""
    if deadline is None:
        return _GRANT_SECONDS
    return max(1, int((deadline - datetime.now(UTC)).total_seconds()))


def _attribute_session[**P, R](
    method: Callable[Concatenate[BucketJournal, str, P], R],
) -> Callable[Concatenate[BucketJournal, str, P], R]:
    @wraps(method)
    def call(self: BucketJournal, session_id: str, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with attribute(session=session_id, purpose="control"):
            return method(self, session_id, *args, **kwargs)

    return call


class BucketJournal:
    """A storage's folder, laid out by run, with each machine's session files apart.

        PREFIX/RUN/                     everything about one run, from every attempt
          attempts/ATTEMPT.log/OFFSET   what the job printed, in pieces by first byte
          attempts/ATTEMPT.json         start, progress, how it ended, and its pieces
          attempts/ATTEMPT/NAME         files the job left in its output directory
          run.json  checkpoints/  ...   the run's tracked folder, as the tracker keeps it
        PREFIX/sessions/SESSION/        one machine's instructions, inbox and notes
          out/receipt, out/ended        written by the machine
          out/worker.log/OFFSET         the worker's log, in pieces by first byte
          out/worker-log.json           where they end (`WorkerLog`)
        PREFIX/sources/SHA256.tar       a commit's source archive, named by its digest

    A machine may write only under the folders of the runs it is given, and
    under its own `out/`.

    Polling never re-reads bytes this journal holds: an object that cannot
    change (a log piece, a packet) is read once, and a changing one (records,
    the inbox, the machine's own logs) is asked for with the ETag of the
    version last read, so storage sends its body only when it has changed.
    """

    def __init__(self, storage: Storage, bucket: Bucket) -> None:
        self._prefix, self._bucket = storage.prefix, bucket
        self._kept: dict[str, bytes] = {}
        # The version last read of each object, by session, most recent last.
        self._versions: OrderedDict[str, dict[str, Version]] = OrderedDict()
        self._finished: dict[str, Progress] = {}
        # Staged archives by repository and commit: their key, digest and size.
        self._sources: dict[tuple[str, str], tuple[str, str, int]] = {}
        self._state_cleaned: dict[str, dict[str, str]] = {}

    def _session_prefix(self, session_id: str) -> str:
        return f"{self._prefix}/sessions/{session_id}/"

    def _run_prefix(self, run: str) -> str:
        return f"{self._prefix}/{run}/"

    def _run_grant(self, run: str, seconds: int) -> Grant:
        """What a machine may write for one run: everything under its folder."""
        return self._bucket.grant(self._run_prefix(run), seconds, run)

    def prepare(self, packet: Packet, *, expires_at: datetime | None = None) -> BootstrapLinks:
        """Write a session's instructions and grants that last until `expires_at`.

        `expires_at` defaults to the packet's deadline; without either, grants
        last `_GRANT_SECONDS`.
        """
        with attribute(session=packet.session, purpose="control"):
            root = self._session_prefix(packet.session)
            seconds = _seconds_until(expires_at or packet.deadline)
            # Grants first: where none can be issued, nothing is written and nothing rented.
            out = self._bucket.grant(root + "out/", seconds, packet.session)
            runs = {work.run: self._run_grant(work.run, seconds) for work in packet.work}
            self._bucket.put(root + "packet.json", packet.model_dump_json().encode(), once=True)
            bootstrap = Bootstrap(
                packet=packet,
                out=out,
                runs=runs,
                inbox_url=self._bucket.get_url(root + "inbox.json", seconds),
                fetch=self._fetch(packet.work),
                restore=self._restore(packet.work),
            )
            self._bucket.put(root + "inbox.json", Inbox().model_dump_json().encode())
            self._bucket.put(
                root + "bootstrap.json", bootstrap.model_dump_json(round_trip=True).encode()
            )
            return BootstrapLinks(
                url=self._bucket.get_url(root + "bootstrap.json", seconds),
                log_url=self._bucket.put_url(root + "out/bootstrap.log", seconds),
                worker_log_url=self._bucket.put_url(root + "out/worker.log", seconds),
            )

    @_attribute_session
    def started(self, session_id: str) -> bool:
        return self._once(self._session_prefix(session_id) + "out/receipt") is not None

    @_attribute_session
    def ended(self, session_id: str) -> datetime | None:
        raw = self._current(session_id, self._session_prefix(session_id) + "out/ended")
        return None if raw is None else datetime.fromisoformat(raw.decode().strip())

    @_attribute_session
    def end(self, session_id: str) -> datetime | None:
        # Provider cleanup owns deletion; a bucket alone cannot stop a machine.
        return self.ended(session_id)

    @_attribute_session
    def finish(self, session_id: str) -> datetime | None:
        """Tell the session no more work will come; it ends once its work is done."""
        inbox = self._inbox(session_id)
        if not inbox.finish:
            self._bucket.put(
                self._session_prefix(session_id) + "inbox.json",
                inbox.model_copy(update={"finish": True}).model_dump_json(round_trip=True).encode(),
            )
        return self.ended(session_id)

    def stage(self, repo: str, commit: str) -> None:
        """Put a commit's source archive in storage unless it is there; safe to repeat.

        The launcher stages before renting, so no paid machine waits on the upload.
        """
        self._source(repo, commit)

    def _source(self, repo: str, commit: str) -> Download:
        if (repo, commit) not in self._sources:
            with attribute(purpose="source"):
                archive = source_archive(repo, commit)
                digest = hashlib.sha256(archive).hexdigest()
                key = f"{self._prefix}/sources/{digest}.tar"
                if key not in self._bucket.list(key):
                    self._bucket.put(key, archive)
            self._sources[repo, commit] = (key, digest, len(archive))
        key, digest, size = self._sources[repo, commit]
        return Download(
            url=self._bucket.get_url(key, _GRANT_SECONDS), sha256=digest, size_bytes=size
        )

    def _fetch(self, works: Sequence[Work]) -> Fetch:
        """Links to the files this work needs."""
        sources: dict[str, Download] = {}
        inputs: dict[str, Download] = {}
        for work in works:
            # A cohort shares sources and inputs; link each only once.
            if work.repo is not None and work.commit is not None and work.commit not in sources:
                sources[work.commit] = self._source(work.repo, work.commit)
            for given in work.inputs:
                # A run's file is where its folder was carried.
                key = given.key if given.run is None else self._run_prefix(given.run) + given.key
                # Named by digest, so attempts given the same file share one download.
                inputs[given.sha256] = Download(
                    url=self._bucket.get_url(key, _GRANT_SECONDS),
                    sha256=given.sha256,
                    size_bytes=given.size_bytes,
                )
        return Fetch(sources=sources, inputs=tuple(inputs.values()))

    def _restore(self, works: Sequence[Work]) -> dict[str, ReadLink]:
        """One listing per resumed run: records, checkpoints and all out files."""
        links: dict[str, ReadLink] = {}
        for work in works:
            if not work.resumes:
                continue
            folder = self._run_prefix(work.run)
            with attribute(run=work.run, attempt=work.attempt, purpose="control"):
                kept = self._bucket.objects(folder)
            for key, size in kept.items():
                relative = key[len(folder) :]
                if relative in ("run.json", MEMBERSHIP) or relative.startswith(
                    ("checkpoints/", "out/")
                ):
                    links[f"{work.run}/{relative}"] = ReadLink(
                        url=self._bucket.get_url(key, _GRANT_SECONDS), size_bytes=size
                    )
        return links

    def held(self, run: str, key: str | None = None) -> dict[str, bytes]:
        """What a machine without storage needs of a run's folder kept here, by key.

        Staged in a folder on this computer, then given as a host holding it
        gives it (`etude.carriage.restore_set`, or with `key` `recorded_file`):
        one listing, the declaration, checkpoint records and out files.
        A copied checkpoint passes through the verified cache. With `key`,
        only that file and the checkpoint records are read. Missing or damaged
        out files raise `StorageUnavailable`; an absent selected checkpoint
        raises `FileNotFoundError`.
        """
        folder = self._run_prefix(run)
        with TemporaryDirectory() as staging, attribute(run=run, purpose="control"):
            root = Path(staging)
            (root / CHECKPOINTS).mkdir()
            objects = self._bucket.objects(folder)
            listed = {name[len(folder) :]: size for name, size in objects.items()}
            note_stored(
                root, [checkpoint_file_name(name) for name in listed if checkpoint_file(name)]
            )
            records = [
                name
                for name in listed
                if name.rpartition("/")[0] == CHECKPOINTS and name.endswith(".json")
            ]
            for name in records if key is not None else [*records, "run.json"]:
                if (raw := self._bucket.get(folder + name)) is not None:
                    (root / name).write_bytes(raw)
            members: set[str] | None = None
            if key is None and MEMBERSHIP in listed:
                raw = self._bucket.get(folder + MEMBERSHIP)
                if raw is None:
                    raise StorageUnavailable("out membership is missing")
                members = {file.key for file in OutFiles.model_validate_json(raw).files}
                (root / "out").mkdir(exist_ok=True)
                ordinary = {name for name in members if save_pointer(name) is None}
                if not ordinary <= listed.keys():
                    raise StorageUnavailable("out member is missing")
            recorded = recorded_files(root)
            pointers: dict[str, bytes] = {}
            if key is None:
                for name in listed:
                    if state_pointer(name):
                        raw = self._bucket.get(folder + name)
                        if raw is None:
                            raise StorageUnavailable("state pointer is missing")
                        pointers[name] = raw
            selected = recovery_files(listed, members, pointers) if key is None else [key]
            for name in selected:
                size = listed[name]
                if not name.startswith("out/") or key not in (None, name):
                    continue
                with attribute(purpose=purpose(name, recorded)):
                    raw = pointers[name] if name in pointers else self._bucket.get(folder + name)
                if raw is None or len(raw) != size:
                    raise StorageUnavailable(
                        f"restored file {name} is missing or has changed length"
                    )
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            wanted = resume_file(root) if key is None else named_file(root, key)
            if (
                wanted is not None
                and wanted.key in listed
                and not (root / wanted.key).exists()
                and (key is not None or not wanted.key.startswith("out/"))
            ):
                with attribute(purpose="checkpoint"):
                    download(
                        Download(
                            url=self._bucket.get_url(folder + wanted.key, _GRANT_SECONDS),
                            sha256=wanted.sha256,
                            size_bytes=wanted.size_bytes,
                        ),
                        root / wanted.key,
                        send=self._bucket.send,
                    )
            return restore_set(root) if key is None else recorded_file(root, key)

    def hold(self, run: str, files: dict[str, bytes]) -> None:
        """Nothing to keep: a machine of this journal restores the run from storage."""

    def _session_versions(self, session_id: str) -> dict[str, Version]:
        """What is kept of one session's objects, now its most recently asked about."""
        versions = self._versions.setdefault(session_id, {})
        self._versions.move_to_end(session_id)
        while len(self._versions) > _SESSIONS_KEPT:
            self._versions.popitem(last=False)
        return versions

    def _current(self, session_id: str, key: str) -> bytes | None:
        """A changing object's bytes; storage sends none while they are those last read."""
        versions = self._session_versions(session_id)
        found = self._bucket.get_version(key, versions.get(key))
        if found is None:
            versions.pop(key, None)
            return None
        versions[key] = found
        return found.body

    def _once(self, key: str) -> bytes | None:
        """An object that does not change once it exists, read from storage at most once."""
        if key not in self._kept:
            raw = self._bucket.get(key)
            if raw is None:
                return None
            self._kept[key] = raw
        return self._kept[key]

    def _pieces(self, session_id: str, keys: Sequence[str]) -> bytes:
        """The pieces of a log its record names, joined; each is read once."""
        versions = self._session_versions(session_id)
        for key in keys:
            if key not in versions:
                found = self._bucket.get_version(key)
                if found is None:
                    raise LauncherError(f"log piece {key} is named by its record but missing")
                versions[key] = found
        return b"".join(versions[key].body for key in keys)

    def _worker_log(self, session_id: str) -> bytes | None:
        """The worker's log: the pieces its record names, else the whole log a worker that
        never read its bootstrap, so wrote no receipt, sent by its link."""
        out = self._session_prefix(session_id) + "out/"
        raw = self._current(session_id, out + "worker-log.json")
        if raw is None:
            return (
                None if self.started(session_id) else self._current(session_id, out + "worker.log")
            )
        record = WorkerLog.model_validate_json(raw)
        return self._pieces(session_id, [out + piece for piece in record.pieces()])

    def _runs(self, session_id: str) -> dict[str, str]:
        """Each attempt this session was given, to its run."""
        raw = self._once(self._session_prefix(session_id) + "packet.json")
        given = Packet.model_validate_json(raw).work if raw else ()
        added = tuple(entry.work for entry in self._inbox(session_id).work)
        return {work.attempt: work.run for work in (*given, *added)}

    def _record(self, session_id: str, run: str, attempt: str) -> AttemptRecord | None:
        with attribute(run=run, attempt=attempt, purpose="control"):
            raw = self._current(session_id, f"{self._run_prefix(run)}attempts/{attempt}.json")
        return None if raw is None else AttemptRecord.model_validate_json(raw)

    def _clear_state(self, session_id: str, run: str) -> None:
        """Use controller keys to retire saves an upload-only courier cannot delete.

        One conditional membership read per observed run; list state only when a
        changed membership contains a pointer. A missing acknowledgement never
        authorizes deletion. Errors leave the next observation free to retry.
        """
        folder = self._run_prefix(run)
        with attribute(run=run, purpose="control"):
            body = self._current(session_id, folder + MEMBERSHIP)
            if body is None:
                return
            membership = OutFiles.model_validate_json(body)
            marks = {file.key: file.sha256 for file in membership.files if state_pointer(file.key)}
            if self._state_cleaned.get(run) == marks:
                return
            if marks:
                pointers: dict[str, bytes] = {}
                for name in marks:
                    raw = self._bucket.get(folder + name)
                    if raw is None:
                        raise StorageUnavailable("state pointer is missing")
                    pointers[name] = raw
                keys = [key[len(folder) :] for key in self._bucket.list(folder + "out/state/")]
                for key in older_saves(keys, pointers):
                    self._bucket.delete(folder + key)
            self._state_cleaned[run] = marks

    @_attribute_session
    def observe(self, session_id: str) -> Observation:
        alive = self.ended(session_id) is None
        progress: list[Progress] = []
        runs = self._runs(session_id)
        for run in sorted(set(runs.values())):
            self._clear_state(session_id, run)
        for attempt, run in sorted(runs.items()):
            if attempt in self._finished:
                progress.append(self._finished[attempt])
                continue
            record = self._record(session_id, run, attempt)
            if record is None:
                continue  # not started, or its first report has not arrived
            log_bytes = record.log_ends[-1] if record.log_ends else 0
            if record.exited is not None:
                if record.delivered:
                    done = record.exited.progress(attempt, record.started_at)
                    self._finished[attempt] = done  # complete: never read again
                else:
                    done = record.exited.progress(
                        attempt, record.started_at, delivered=False, log_bytes=log_bytes
                    )
                progress.append(done)
                continue
            progress.append(
                Progress(
                    attempt=attempt,
                    started_at=record.started_at,
                    finished_at=None,
                    outcome=None,
                    cause=None,
                    clocks=record.result.clocks,
                    outputs=(),
                    exit_code=None,
                    tally=record.result.tally,
                    log_bytes=log_bytes,
                )
            )
        return Observation(
            alive=alive,
            progress=tuple(progress),
            start_unknown=self.started(session_id),
            transfers=self._transfers(session_id),
        )

    def _inbox(self, session_id: str) -> Inbox:
        raw = self._current(session_id, self._session_prefix(session_id) + "inbox.json")
        return Inbox.model_validate_json(raw) if raw else Inbox()

    @_attribute_session
    def stop(self, session_id: str, attempt: str) -> None:
        inbox = self._inbox(session_id)
        if attempt not in inbox.stop:
            inbox = inbox.model_copy(update={"stop": (*inbox.stop, attempt)})
            self._bucket.put(
                self._session_prefix(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    @_attribute_session
    def add(self, session_id: str, work: Work) -> None:
        inbox = self._inbox(session_id)
        if all(entry.work.attempt != work.attempt for entry in inbox.work):
            raw = self._once(self._session_prefix(session_id) + "packet.json")
            packet = Packet.model_validate_json(raw) if raw else None
            deadline = None
            if packet is not None and packet.deadline is not None:
                # The machine delivers for this long after the deadline it was given.
                deadline = packet.deadline + timedelta(seconds=packet.delivery_seconds or 0)
            entry = Delivery(
                work=work,
                access=self._run_grant(work.run, _seconds_until(deadline)),
                fetch=self._fetch([work]),
                restore=self._restore([work]),
            )
            inbox = inbox.model_copy(update={"work": (*inbox.work, entry)})
            self._bucket.put(
                self._session_prefix(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    @_attribute_session
    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        if attempt is None:
            raw = (
                self._worker_log(session_id)
                if producer == "worker"
                else self._current(
                    session_id, self._session_prefix(session_id) + f"out/{producer}.log"
                )
            )
            return snapshot_stream(session_id, None, producer, raw, False)
        run = self._runs(session_id).get(attempt)
        # The record names the pieces stored before it, finished or not, so the
        # text and its completion are from one moment.
        record = None if run is None else self._record(session_id, run, attempt)
        if run is None or record is None or record.log_ends is None:
            raw = None
        else:
            with attribute(run=run, attempt=attempt, purpose="result"):
                pieces = [self._run_prefix(run) + piece for piece in record.log_pieces(attempt)]
                raw = self._pieces(session_id, pieces)
        return snapshot_stream(
            session_id,
            attempt,
            producer,
            raw,
            record is not None and record.exited is not None and record.delivered,
        )

    @_attribute_session
    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        run = self._runs(session_id).get(attempt)
        record = None if run is None else self._record(session_id, run, attempt)
        if run is None or record is None or record.exited is None or not record.delivered:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        expected = next((o for o in record.exited.outputs if o.name == name), None)
        if expected is None:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        key = f"{self._run_prefix(run)}attempts/{attempt}/{name}"
        with attribute(run=run, attempt=attempt, purpose="result"):
            return download(
                Download(
                    url=self._bucket.get_url(key, _GRANT_SECONDS),
                    sha256=expected.sha256,
                    size_bytes=expected.size_bytes,
                ),
                destination / Path(name).name,
                send=self._bucket.send,
            )

    def _transfers(self, session_id: str) -> tuple[Transfer, ...]:
        events: dict[str, Transfer] = {}
        root = self._session_prefix(session_id) + "out/"
        raw = self._current(session_id, root + "usage.jsonl")
        sources = [] if raw is None else raw.decode().splitlines()
        # Bootstrap/early-worker failures precede the courier's usage journal.
        for log in (
            self._current(session_id, root + "bootstrap.log"),
            self._worker_log(session_id),
        ):
            if log is not None:
                lines = (parse_log_line(line) for line in log.decode(errors="replace").splitlines())
                sources.extend(
                    line.message
                    for line in lines
                    if line is not None and line.operation == "transfer"
                )
        for line in sources:
            try:
                event = Transfer.model_validate_json(line)
            except ValueError:
                continue  # interrupted lines are unknown coverage, never zero usage
            events[event.id] = event.model_copy(update={"session": session_id})
        return tuple(events.values())
