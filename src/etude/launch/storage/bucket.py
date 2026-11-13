"""Bucket journals shared by owned hosts and rented machines."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Concatenate

from etude.carriage import (
    CHECKPOINTS,
    checkpoint_file,
    named_file,
    note_stored,
    recorded_file,
    restore_set,
    resume_file,
)
from etude.launch.logs import snapshot_stream
from etude.launch.records import BootstrapLinks, LauncherError, LogStream, Storage
from etude.launch.schemas import (
    AttemptRecord,
    Bootstrap,
    Delivery,
    Inbox,
    Observation,
    Packet,
    Progress,
    Work,
    WorkerLog,
    progress_from_json,
)
from etude.launch.source import source_archive
from etude.launch.storage.download import Download, download
from etude.launch.storage.s3 import Bucket, Grant, Version
from etude.launch.storage.transport import attribute
from etude.launch.usage import Transfer

# How long a link lasts where no rental bounds it: a session on a machine you
# own, and links the launcher reads itself. A rented session's grants last
# until its deadline instead.
GRANT_SECONDS = 13 * 3600
# Sessions whose last-read versions stay in memory. Logs are the bulk; one
# beyond this is read again, once, when it is next asked about.
SESSIONS_KEPT = 64


def seconds_until(deadline: datetime | None) -> int:
    """How long a grant must last to reach `deadline`, or `GRANT_SECONDS` without one."""
    if deadline is None:
        return GRANT_SECONDS
    return max(1, int((deadline - datetime.now(UTC)).total_seconds()))


def attribute_session[**P, R](
    method: Callable[Concatenate[BucketJournal, str, P], R],
) -> Callable[Concatenate[BucketJournal, str, P], R]:
    @wraps(method)
    def call(self: BucketJournal, session_id: str, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with attribute(session=session_id, purpose="control"):
            return method(self, session_id, *args, **kwargs)

    return call


class BucketJournal:
    """A storage's folder, laid out by run, with each machine's working files apart.

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
        self.prefix, self.bucket = storage.prefix, bucket
        self.kept: dict[str, bytes] = {}
        # The version last read of each object, by session, most recent last.
        self.versions: OrderedDict[str, dict[str, Version]] = OrderedDict()
        self.finished: dict[str, Progress] = {}
        # Staged archives by repository and commit: their key, digest and size.
        self.sources: dict[tuple[str, str], tuple[str, str, int]] = {}

    def session_prefix(self, session_id: str) -> str:
        return f"{self.prefix}/sessions/{session_id}/"

    def run_prefix(self, run: str) -> str:
        return f"{self.prefix}/{run}/"

    def access(self, run: str, seconds: int) -> Grant:
        """What a machine may write for one run: everything under its folder."""
        return self.bucket.grant(self.run_prefix(run), seconds, run)

    def prepare(self, packet: Packet, *, expires_at: datetime | None = None) -> BootstrapLinks:
        """Write a session's instructions and grants that last until `expires_at`.

        `expires_at` defaults to the packet's deadline; without either, grants
        last `GRANT_SECONDS`.
        """
        with attribute(session=packet.session, purpose="control"):
            root = self.session_prefix(packet.session)
            seconds = seconds_until(expires_at or packet.deadline)
            # Grants first: where none can be issued, nothing is written and nothing rented.
            out = self.bucket.grant(root + "out/", seconds, packet.session)
            runs = {work.run: self.access(work.run, seconds) for work in packet.work}
            self.bucket.put(root + "packet.json", packet.model_dump_json().encode(), once=True)
            bootstrap = Bootstrap.model_validate(
                {
                    "packet": packet.model_dump_json(),
                    "out": out,
                    "runs": runs,
                    "inbox_url": self.bucket.get_url(root + "inbox.json", seconds),
                    "fetch": self.fetches(packet.session, packet.work),
                    "restore": self.restore_links(packet.work),
                }
            )
            self.bucket.put(root + "inbox.json", Inbox().model_dump_json().encode())
            self.bucket.put(
                root + "bootstrap.json", bootstrap.model_dump_json(round_trip=True).encode()
            )
            return BootstrapLinks(
                url=self.bucket.get_url(root + "bootstrap.json", seconds),
                log_url=self.bucket.put_url(root + "out/bootstrap.log", seconds),
                worker_log_url=self.bucket.put_url(root + "out/worker.log", seconds),
            )

    @attribute_session
    def started(self, session_id: str) -> bool:
        return self.once(self.session_prefix(session_id) + "out/receipt") is not None

    @attribute_session
    def ended(self, session_id: str) -> datetime | None:
        raw = self.current(session_id, self.session_prefix(session_id) + "out/ended")
        return None if raw is None else datetime.fromisoformat(raw.decode().strip())

    @attribute_session
    def end(self, session_id: str) -> datetime | None:
        # Provider cleanup owns deletion; a bucket alone cannot stop a machine.
        return self.ended(session_id)

    @attribute_session
    def finish(self, session_id: str) -> datetime | None:
        """Tell the session no more work will come; it ends once its work is done."""
        inbox = self.inbox(session_id)
        if not inbox.finish:
            self.bucket.put(
                self.session_prefix(session_id) + "inbox.json",
                inbox.model_copy(update={"finish": True}).model_dump_json(round_trip=True).encode(),
            )
        return self.ended(session_id)

    def stage(self, repo: str, commit: str) -> None:
        """Put a commit's source archive in storage unless it is there; safe to repeat.

        The launcher stages before renting, so no paid machine waits on the upload.
        """
        self.source(repo, commit)

    def source(self, repo: str, commit: str) -> Download:
        if (repo, commit) not in self.sources:
            with attribute(purpose="source"):
                archive = source_archive(repo, commit)
                digest = hashlib.sha256(archive).hexdigest()
                key = f"{self.prefix}/sources/{digest}.tar"
                if key not in self.bucket.list(key):
                    self.bucket.put(key, archive)
            self.sources[repo, commit] = (key, digest, len(archive))
        key, digest, size = self.sources[repo, commit]
        return Download(url=self.bucket.get_url(key, GRANT_SECONDS), sha256=digest, size_bytes=size)

    def fetches(self, session_id: str, works: Sequence[Work]) -> dict[str, Download]:
        """Links to the files this work needs, keyed by where the pod should put them."""
        links: dict[str, Download] = {}
        for work in works:
            if work.repo is not None and work.commit is not None:
                local = f"{session_id}/sources/{work.commit}.tar"
                # A cohort shares inputs; prepare each destination only once.
                if local not in links:
                    links[local] = self.source(work.repo, work.commit)
            for given in work.inputs:
                # A run's file is where its folder was carried.
                key = given.key if given.run is None else self.run_prefix(given.run) + given.key
                # Named by digest, so attempts given the same file share one download.
                links[f"{session_id}/inputs/{given.sha256}"] = Download(
                    url=self.bucket.get_url(key, GRANT_SECONDS),
                    sha256=given.sha256,
                    size_bytes=given.size_bytes,
                )
        return links

    def restore_links(self, works: Sequence[Work]) -> dict[str, str]:
        """Read links to what storage holds of each resumed run's folder, by RUN/KEY.

        The run's declaration and every object under `checkpoints/`; the
        machine reads only what it lacks, and of the checkpoint files only the
        one its latest record names (`etude.carriage.resume_file`). Linking
        reads nothing: one listing per resumed run.
        """
        links: dict[str, str] = {}
        for work in works:
            if not work.resumes:
                continue
            folder = self.run_prefix(work.run)
            with attribute(run=work.run, attempt=work.attempt, purpose="control"):
                kept = self.bucket.list(f"{folder}{CHECKPOINTS}/")
            for key in [folder + "run.json", *kept]:
                links[f"{work.run}/{key[len(folder) :]}"] = self.bucket.get_url(key, GRANT_SECONDS)
        return links

    def held(self, run: str, key: str | None = None) -> dict[str, bytes]:
        """What a machine without storage needs of a run's folder kept here, by key.

        Staged in a folder on this computer, then given as a host holding it
        gives it (`etude.carriage.restore_set`, or with `key` `recorded_file`):
        one listing, the declaration and each checkpoint record read whole, and
        one checkpoint file through this computer's cache, verified against its
        record, so relaying it again reads no more of it. Raises
        `FileNotFoundError` when no record here names `key` or its file is gone.
        """
        folder = self.run_prefix(run)
        with TemporaryDirectory() as staging, attribute(run=run, purpose="control"):
            root = Path(staging)
            (root / CHECKPOINTS).mkdir()
            listed = [name[len(folder) :] for name in self.bucket.list(f"{folder}{CHECKPOINTS}/")]
            note_stored(
                root, [PurePosixPath(name).name for name in listed if checkpoint_file(name)]
            )
            records = [
                name
                for name in listed
                if name.rpartition("/")[0] == CHECKPOINTS and name.endswith(".json")
            ]
            for name in records if key is not None else [*records, "run.json"]:
                if (raw := self.bucket.get(folder + name)) is not None:
                    (root / name).write_bytes(raw)
            wanted = resume_file(root) if key is None else named_file(root, key)
            if wanted is not None and wanted.key in listed:
                with attribute(purpose="checkpoint"):
                    download(
                        Download(
                            url=self.bucket.get_url(folder + wanted.key, GRANT_SECONDS),
                            sha256=wanted.sha256,
                            size_bytes=wanted.size_bytes,
                        ),
                        root / wanted.key,
                        send=self.bucket.send,
                    )
            return restore_set(root) if key is None else recorded_file(root, key)

    def hold(self, run: str, files: dict[str, bytes]) -> None:
        """Nothing to keep: a machine of this journal restores the run from storage."""

    def session_versions(self, session_id: str) -> dict[str, Version]:
        """What is kept of one session's objects, now its most recently asked about."""
        versions = self.versions.setdefault(session_id, {})
        self.versions.move_to_end(session_id)
        while len(self.versions) > SESSIONS_KEPT:
            self.versions.popitem(last=False)
        return versions

    def current(self, session_id: str, key: str) -> bytes | None:
        """A changing object's bytes; storage sends none while they are those last read."""
        versions = self.session_versions(session_id)
        found = self.bucket.get_version(key, versions.get(key))
        if found is None:
            versions.pop(key, None)
            return None
        versions[key] = found
        return found.body

    def once(self, key: str) -> bytes | None:
        """An object that does not change once it exists, read from storage at most once."""
        if key not in self.kept:
            raw = self.bucket.get(key)
            if raw is None:
                return None
            self.kept[key] = raw
        return self.kept[key]

    def pieces(self, session_id: str, keys: Sequence[str]) -> bytes:
        """The pieces of a log its record names, joined; each is read once."""
        versions = self.session_versions(session_id)
        for key in keys:
            if key not in versions:
                found = self.bucket.get_version(key)
                if found is None:
                    raise LauncherError(f"log piece {key} is named by its record but missing")
                versions[key] = found
        return b"".join(versions[key].body for key in keys)

    def worker_log(self, session_id: str) -> bytes | None:
        """The worker's log: the pieces its record names, else the whole log a worker that
        never read its bootstrap, so wrote no receipt, sent by its link."""
        out = self.session_prefix(session_id) + "out/"
        raw = self.current(session_id, out + "worker-log.json")
        if raw is None:
            return (
                None if self.started(session_id) else self.current(session_id, out + "worker.log")
            )
        record = WorkerLog.model_validate_json(raw)
        return self.pieces(session_id, [out + piece for piece in record.pieces()])

    def runs(self, session_id: str) -> dict[str, str]:
        """Each attempt this session was given, to its run."""
        raw = self.once(self.session_prefix(session_id) + "packet.json")
        given = Packet.model_validate_json(raw).work if raw else ()
        added = tuple(entry.work for entry in self.inbox(session_id).work)
        return {work.attempt: work.run for work in (*given, *added)}

    def record(self, session_id: str, run: str, attempt: str) -> AttemptRecord | None:
        with attribute(run=run, attempt=attempt, purpose="control"):
            raw = self.current(session_id, f"{self.run_prefix(run)}attempts/{attempt}.json")
        return None if raw is None else AttemptRecord.model_validate_json(raw)

    @attribute_session
    def observe(self, session_id: str) -> Observation:
        alive = self.ended(session_id) is None
        progress: list[Progress] = []
        for attempt, run in sorted(self.runs(session_id).items()):
            if attempt in self.finished:
                progress.append(self.finished[attempt])
                continue
            record = self.record(session_id, run, attempt)
            if record is None:
                continue  # not started, or its first report has not arrived
            log_bytes = record.log_ends[-1] if record.log_ends else 0
            if record.exited is not None:
                done = progress_from_json(record.exited, attempt, record.started_at)
                if record.delivered:
                    self.finished[attempt] = done  # complete: never read again
                else:
                    done = done.model_copy(update={"delivered": False, "log_bytes": log_bytes})
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
            transfers=self.transfers(session_id),
        )

    def inbox(self, session_id: str) -> Inbox:
        raw = self.current(session_id, self.session_prefix(session_id) + "inbox.json")
        return Inbox.model_validate_json(raw) if raw else Inbox()

    @attribute_session
    def stop(self, session_id: str, attempt: str) -> None:
        inbox = self.inbox(session_id)
        if attempt not in inbox.stop:
            inbox = inbox.model_copy(update={"stop": (*inbox.stop, attempt)})
            self.bucket.put(
                self.session_prefix(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    @attribute_session
    def add(self, session_id: str, work: Work) -> None:
        inbox = self.inbox(session_id)
        if all(entry.work.attempt != work.attempt for entry in inbox.work):
            raw = self.once(self.session_prefix(session_id) + "packet.json")
            packet = Packet.model_validate_json(raw) if raw else None
            deadline = None
            if packet is not None and packet.deadline is not None:
                # The machine delivers for this long after the deadline it was given.
                deadline = packet.deadline + timedelta(seconds=packet.delivery_seconds or 0)
            entry = Delivery.model_validate(
                {
                    "work": work.model_dump_json(),
                    "access": self.access(work.run, seconds_until(deadline)),
                    "fetch": self.fetches(session_id, [work]),
                    "restore": self.restore_links([work]),
                }
            )
            inbox = inbox.model_copy(update={"work": (*inbox.work, entry)})
            self.bucket.put(
                self.session_prefix(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    @attribute_session
    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        if attempt is None:
            raw = (
                self.worker_log(session_id)
                if producer == "worker"
                else self.current(
                    session_id, self.session_prefix(session_id) + f"out/{producer}.log"
                )
            )
            return snapshot_stream(session_id, None, producer, raw, False)
        run = self.runs(session_id).get(attempt)
        # The record names the pieces stored before it, finished or not, so the
        # text and its completion are from one moment.
        record = None if run is None else self.record(session_id, run, attempt)
        if run is None or record is None or record.log_ends is None:
            raw = None
        else:
            with attribute(run=run, attempt=attempt, purpose="result"):
                pieces = [self.run_prefix(run) + piece for piece in record.log_pieces(attempt)]
                raw = self.pieces(session_id, pieces)
        return snapshot_stream(
            session_id,
            attempt,
            producer,
            raw,
            record is not None and record.exited is not None and record.delivered,
        )

    @attribute_session
    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        run = self.runs(session_id).get(attempt)
        record = None if run is None else self.record(session_id, run, attempt)
        if run is None or record is None or record.exited is None or not record.delivered:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        done = progress_from_json(record.exited, attempt, None)
        expected = next((o for o in done.outputs if o.name == name), None)
        if expected is None:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        key = f"{self.run_prefix(run)}attempts/{attempt}/{name}"
        with attribute(run=run, attempt=attempt, purpose="result"):
            return download(
                Download(
                    url=self.bucket.get_url(key, GRANT_SECONDS),
                    sha256=expected.sha256,
                    size_bytes=expected.size_bytes,
                ),
                destination / Path(name).name,
                send=self.bucket.send,
            )

    def transfers(self, session_id: str) -> tuple[Transfer, ...]:
        events: dict[str, Transfer] = {}
        root = self.session_prefix(session_id) + "out/"
        raw = self.current(session_id, root + "usage.jsonl")
        sources = [] if raw is None else raw.decode().splitlines()
        # Bootstrap/early-worker failures precede the courier's usage journal.
        for log in (
            self.current(session_id, root + "bootstrap.log"),
            self.worker_log(session_id),
        ):
            if log is not None:
                sources.extend(
                    line.partition(" ")[2].removeprefix("transfer: ")
                    for line in log.decode(errors="replace").splitlines()
                    if line.partition(" ")[2].startswith("transfer: ")
                )
        for line in sources:
            try:
                event = Transfer.model_validate_json(line)
            except ValueError:
                continue  # interrupted lines are unknown coverage, never zero usage
            events[event.id] = event.model_copy(update={"session": session_id})
        return tuple(events.values())
