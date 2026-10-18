"""Executor on this computer: each is one detached session and its directory.

Free, so the price is zero. The process outlives whoever launched it, enforces
its own deadline, and leaves a dated `ended` file when it ends.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from etude.launch.logs import snapshot_stream
from etude.launch.records import BootstrapLinks, LauncherError, LogStream, Output
from etude.launch.schemas import (
    CheckpointRef,
    Observation,
    Packet,
    Progress,
    Work,
    progress_from_json,
)
from etude.launch.source import source_archive
from etude.launch.storage.files import digest, read_result, write
from etude.launch.storage.s3 import read


class DirectoryJournal:
    def __init__(self, root: Path) -> None:
        self._root = root

    def checkpoint(self, ref: CheckpointRef) -> CheckpointRef:
        with tempfile.TemporaryDirectory() as directory:
            path = self.fetch(ref.session, ref.attempt, ref.name, Path(directory))
            return ref.model_copy(update={"content": base64.b64encode(path.read_bytes()).decode()})

    def prepare(
        self, packet: Packet, *, expires_at: datetime | None = None
    ) -> BootstrapLinks | None:
        return None

    def started(self, session_id: str) -> bool:
        return (self._root / session_id / "receipt").exists()

    def ended(self, session_id: str) -> datetime | None:
        note = self._root / session_id / "ended"
        return datetime.fromisoformat(note.read_text()) if note.exists() else None

    def add(self, session_id: str, work: Work) -> None:
        added = self._root / session_id / "work"
        added.mkdir(exist_ok=True)
        if not (added / f"{work.attempt}.json").exists():
            self._place_source(self._root / session_id, work)
            write(added / f"{work.attempt}.json", work.model_dump_json())

    @staticmethod
    def _place_source(home: Path, work: Work) -> None:
        if work.checkpoint is not None:
            ref = work.checkpoint
            target = home.parent / ref.session / "attempts" / ref.attempt / "outputs" / ref.name
            body = (
                read(ref.url) if ref.url else base64.b64decode(ref.content) if ref.content else None
            )
            if body is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(body)
                if digest(target) != ref.sha256:
                    raise LauncherError("checkpoint digest mismatch")
        if work.repo is None or work.commit is None:
            return
        archive = home / "sources" / f"{work.commit}.tar"
        if not archive.exists():
            archive.parent.mkdir(exist_ok=True)
            partial = archive.with_name(archive.name + ".partial")
            partial.write_bytes(source_archive(work.repo, work.commit))
            partial.replace(archive)

    def observe(self, session_id: str) -> Observation:
        home = self._root / session_id
        # Read before the journal, so a dead session's journal is final.
        alive = self._alive(session_id)
        attempts = home / "attempts"
        names = sorted(p.name for p in attempts.iterdir()) if attempts.is_dir() else []
        return Observation(
            alive=alive, progress=tuple(self._progress(attempts / name) for name in names)
        )

    def stop(self, session_id: str, attempt: str) -> None:
        stop = self._root / session_id / "stop"
        stop.mkdir(exist_ok=True)
        (stop / attempt).touch()

    def end(self, session_id: str) -> datetime | None:
        ended = self._root / session_id / "ended"
        if self._alive(session_id):
            os.kill(int(self._receipt(session_id)), signal.SIGTERM)
            for _ in range(100):
                if not self._alive(session_id):
                    break
                time.sleep(0.1)
            else:
                return None
        if not ended.exists():
            write(ended, datetime.now(UTC).isoformat())  # the process is dead without a note
        return datetime.fromisoformat(ended.read_text())

    def logs(
        self, session_id: str, attempt: str | None = None, *, producer: str = "job"
    ) -> LogStream:
        home = self._root / session_id
        log = (
            home / "attempts" / attempt / "log"
            if attempt
            else home / ("session.log" if producer == "worker" else "bootstrap.log")
        )
        raw = log.read_bytes() if log.exists() else None
        closed = (
            (home / "attempts" / attempt / "finished.json").exists()
            if attempt
            else (home / "ended").exists()
        )
        return snapshot_stream(session_id, attempt, producer, raw, closed)

    def fetch(self, session_id: str, attempt: str, name: str, destination: Path) -> Path:
        home = self._root / session_id / "attempts" / attempt
        recorded = progress_from_json(
            json.loads((home / "finished.json").read_text()), attempt, None
        ).outputs
        expected = next((o.sha256 for o in recorded if o.name == name), None)
        if expected is None:
            raise LauncherError(f"attempt {attempt} recorded no output {name!r}")
        destination.mkdir(parents=True, exist_ok=True)
        copy = destination / Path(name).name
        shutil.copyfile(home / "outputs" / name, copy)
        if digest(copy) != expected:
            copy.unlink()
            raise LauncherError(f"output {name!r} no longer matches its recorded digest")
        return copy

    def _receipt(self, session_id: str) -> str:
        return (self._root / session_id / "receipt").read_text()

    def _alive(self, session_id: str) -> bool:
        home = self._root / session_id
        if (home / "ended").exists() or not (home / "receipt").exists():
            return False
        pid = int(self._receipt(session_id))
        try:
            os.waitpid(pid, os.WNOHANG)  # collect it if it was ours and has exited
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _progress(home: Path) -> Progress:
        claim, finished = home / "claim", home / "finished.json"
        started = datetime.fromisoformat(claim.read_text()) if claim.exists() else None
        if not finished.exists():
            result = read_result(home / "outputs")
            checkpoint = result.checkpoint
            outputs: tuple[Output, ...] = ()
            if checkpoint:
                path = home / "outputs" / checkpoint
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.resolve().is_relative_to((home / "outputs").resolve())
                ):
                    outputs = (
                        Output(
                            name=checkpoint, sha256=digest(path), size_bytes=path.stat().st_size
                        ),
                    )
                else:
                    checkpoint = None
            return Progress(
                attempt=home.name,
                started_at=started,
                finished_at=None,
                outcome=None,
                cause=None,
                clocks=result.clocks,
                checkpoint=checkpoint,
                outputs=outputs,
                exit_code=None,
            )
        raw: dict[str, Any] = json.loads(finished.read_text())
        return progress_from_json(raw, home.name, started)
