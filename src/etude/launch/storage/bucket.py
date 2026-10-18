"""Bucket journals shared by owned hosts and rented machines."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from etude.launch.logs import snapshot_stream
from etude.launch.records import BootstrapLinks, LauncherError, LogStream, Storage
from etude.launch.schemas import (
    AttemptRecord,
    Bootstrap,
    BucketAccess,
    CheckpointRef,
    Delivery,
    Inbox,
    Observation,
    Packet,
    Progress,
    RunAccess,
    Work,
    progress_from_json,
)
from etude.launch.source import source_archive
from etude.launch.storage.s3 import Bucket

_GRANT_SECONDS = 13 * 3600


class BucketJournal:
    """A storage's folder, laid out by run, with each machine's working files apart.

        PREFIX/RUN/                     everything about one run, from every attempt
          attempts/ATTEMPT.log          what the job printed
          attempts/ATTEMPT.json         start, progress and how it ended
          attempts/ATTEMPT/NAME         other files the job left in its output directory
          checkpoints/NAME              the checkpoints the job reported
          ...                           whatever the job's tracker keeps for the run
        PREFIX/sessions/SESSION/        one machine's instructions, inbox and notes
          out/receipt, out/ended        written by the machine

    A machine may write only under the folders of the runs it is given, and
    under its own `out/`.
    """

    def __init__(self, storage: Storage, bucket: Bucket) -> None:
        self._prefix, self._bucket = storage.prefix, bucket
        self._kept: dict[str, bytes] = {}
        self._finished: dict[str, Progress] = {}

    def _session(self, session_id: str) -> str:
        return f"{self._prefix}/sessions/{session_id}/"

    def _run(self, run: str) -> str:
        return f"{self._prefix}/{run}/"

    def _access(self, run: str) -> RunAccess:
        """What a machine may write for one run; keys where AWS will issue them."""
        folder = self._run(run)
        keys = self._bucket.grant(folder, _GRANT_SECONDS, run)
        return RunAccess(
            form=self._bucket.post_form(folder, _GRANT_SECONDS),
            bucket=None
            if keys is None
            else BucketAccess(
                name=self._bucket.name,
                region=self._bucket.region,
                host=self._bucket.host,
                keys=keys,
            ),
        )

    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef:
        key = f"{self._run(ref.run)}checkpoints/{ref.name}"
        return ref.model_copy(update={"url": self._bucket.get_url(key, _GRANT_SECONDS)})

    def prepare(self, packet: Packet, *, expires_at: datetime | None = None) -> BootstrapLinks:
        root = self._session(packet.session)
        self._bucket.put(root + "packet.json", packet.model_dump_json().encode(), once=True)
        bootstrap = Bootstrap.model_validate(
            {
                "packet": packet.model_dump_json(),
                "form": self._bucket.post_form(root + "out/", _GRANT_SECONDS),
                "runs": {work.run: self._access(work.run) for work in packet.work},
                "inbox_url": self._bucket.get_url(root + "inbox.json", _GRANT_SECONDS),
                "fetch": self._fetches(packet.session, packet.work),
            }
        )
        self._bucket.put(root + "inbox.json", Inbox().model_dump_json().encode())
        self._bucket.put(
            root + "bootstrap.json", bootstrap.model_dump_json(round_trip=True).encode()
        )
        expires_at = expires_at or packet.deadline
        seconds = (
            _GRANT_SECONDS
            if expires_at is None
            else max(1, min(_GRANT_SECONDS, int((expires_at - datetime.now(UTC)).total_seconds())))
        )
        return BootstrapLinks(
            url=self._bucket.get_url(root + "bootstrap.json", seconds),
            log_url=self._bucket.put_url(root + "out/bootstrap.log", seconds),
            worker_log_url=self._bucket.put_url(root + "out/worker.log", seconds),
        )

    def started(self, session_id: str) -> bool:
        return self._bucket.get(self._session(session_id) + "out/receipt") is not None

    def ended(self, session_id: str) -> datetime | None:
        raw = self._bucket.get(self._session(session_id) + "out/ended")
        return None if raw is None else datetime.fromisoformat(raw.decode().strip())

    def end(self, session_id: str) -> datetime | None:
        # Provider cleanup owns deletion; a bucket alone cannot stop a machine.
        return self.ended(session_id)

    def _fetches(self, session_id: str, works: Sequence[Work]) -> dict[str, str]:
        """Links to the files this work needs, keyed by where the pod should put them."""
        links: dict[str, str] = {}
        for work in works:
            if work.repo is not None and work.commit is not None:
                key = f"{self._session(session_id)}sources/{work.commit}.tar"
                if key not in self._bucket.list(key):
                    self._bucket.put(key, source_archive(work.repo, work.commit))
                links[f"{session_id}/sources/{work.commit}.tar"] = self._bucket.get_url(
                    key, _GRANT_SECONDS
                )
            held = work.checkpoint
            if held is not None:
                key = f"{self._run(held.run)}checkpoints/{held.name}"
                if held.content is not None:
                    key = f"{self._session(session_id)}inputs/{held.sha256}"
                    self._bucket.put(key, base64.b64decode(held.content))
                # Where the machine's worker looks for it, under its own sessions folder.
                local = f"{held.session}/attempts/{held.attempt}/outputs/{held.name}"
                links[local] = held.url or self._bucket.get_url(key, _GRANT_SECONDS)
        return links

    def _once(self, key: str) -> bytes | None:
        """An object that does not change once it exists, read from storage at most once."""
        if key not in self._kept:
            raw = self._bucket.get(key)
            if raw is None:
                return None
            self._kept[key] = raw
        return self._kept[key]

    def _runs(self, session_id: str) -> dict[str, str]:
        """Each attempt this session was given, to its run."""
        raw = self._once(self._session(session_id) + "packet.json")
        given = Packet.model_validate_json(raw).work if raw else ()
        added = tuple(entry.work for entry in self._inbox(session_id).work)
        return {work.attempt: work.run for work in (*given, *added)}

    def _record(self, run: str, attempt: str) -> AttemptRecord | None:
        raw = self._bucket.get(f"{self._run(run)}attempts/{attempt}.json")
        return None if raw is None else AttemptRecord.model_validate_json(raw)

    def observe(self, session_id: str) -> Observation:
        alive = self.ended(session_id) is None
        progress: list[Progress] = []
        for attempt, run in sorted(self._runs(session_id).items()):
            if attempt in self._finished:
                progress.append(self._finished[attempt])
                continue
            record = self._record(run, attempt)
            if record is None:
                continue  # not started, or its first report has not arrived
            if record.finished is not None:
                done = progress_from_json(record.finished, attempt, record.started_at)
                self._finished[attempt] = done
                progress.append(done)
                continue
            outputs = record.result.outputs
            progress.append(
                Progress(
                    attempt=attempt,
                    started_at=record.started_at,
                    finished_at=None,
                    outcome=None,
                    cause=None,
                    clocks=record.result.clocks,
                    checkpoint=record.result.checkpoint if outputs else None,
                    outputs=outputs,
                    exit_code=None,
                )
            )
        return Observation(
            alive=alive, progress=tuple(progress), start_unknown=self.started(session_id)
        )

    def _inbox(self, session_id: str) -> Inbox:
        raw = self._bucket.get(self._session(session_id) + "inbox.json")
        return Inbox.model_validate_json(raw) if raw else Inbox()

    def stop(self, session_id: str, attempt: str) -> None:
        inbox = self._inbox(session_id)
        if attempt not in inbox.stop:
            inbox = inbox.model_copy(update={"stop": (*inbox.stop, attempt)})
            self._bucket.put(
                self._session(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    def add(self, session_id: str, work: Work) -> None:
        inbox = self._inbox(session_id)
        if all(entry.work.attempt != work.attempt for entry in inbox.work):
            entry = Delivery.model_validate(
                {
                    "work": work.model_dump_json(),
                    "access": self._access(work.run),
                    "fetch": self._fetches(session_id, [work]),
                }
            )
            inbox = inbox.model_copy(update={"work": (*inbox.work, entry)})
            self._bucket.put(
                self._session(session_id) + "inbox.json",
                inbox.model_dump_json(round_trip=True).encode(),
            )

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        if attempt is None:
            raw = self._bucket.get(self._session(session_id) + f"out/{producer}.log")
            return snapshot_stream(session_id, None, producer, raw, False)
        run = self._runs(session_id).get(attempt)
        raw = None if run is None else self._bucket.get(f"{self._run(run)}attempts/{attempt}.log")
        record = None if run is None else self._record(run, attempt)
        return snapshot_stream(
            session_id, attempt, producer, raw, record is not None and record.finished is not None
        )

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        run = self._runs(session_id).get(attempt)
        record = None if run is None else self._record(run, attempt)
        if run is None or record is None or record.finished is None:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        done = progress_from_json(record.finished, attempt, None)
        expected = next((o.sha256 for o in done.outputs if o.name == name), None)
        # The checkpoint it reported is kept with the run's; anything else with the attempt.
        folder = "checkpoints/" if name == done.checkpoint else f"attempts/{attempt}/"
        raw = self._bucket.get(f"{self._run(run)}{folder}{name}")
        if expected is None or raw is None:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise LauncherError(f"output {name!r} no longer matches its recorded digest")
        destination.mkdir(parents=True, exist_ok=True)
        copy = destination / Path(name).name
        copy.write_bytes(raw)
        return copy
