"""Publish sealed training evidence from the attempt's host, independently of delivery."""

import os
import stat
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit

from etude.block_file import parse, select
from etude.errors import explain
from etude.observations import MAX_FILE_BYTES, ViewGrantTarget, training_file


class AttemptPublisher:
    def __init__(self, root: Path, target: ViewGrantTarget, halt: threading.Event) -> None:
        self.root = root
        self.target = target
        self.halt = halt
        self.finish = threading.Event()
        self.stop = threading.Event()
        self._sent: dict[str, tuple[int, int, int]] = {}

    def _files(self) -> Iterator[str]:
        yield "run.json"
        metrics = self.root / "metrics"
        if metrics.is_dir() and not metrics.is_symlink():
            keys = ["metrics/" + p.name for p in metrics.iterdir() if not p.is_symlink()]
            yield from sorted(key for key in keys if key.endswith(".json") and training_file(key))
            for block in select(b for k in keys if training_file(k) and (b := parse(k))):
                yield block.key
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
        url = urlsplit(self.target.origin)
        if not url.hostname:
            raise ValueError("missing hosted view origin")
        connection = HTTPSConnection if url.scheme == "https" else HTTPConnection
        # One exchange, with no redirect handling or forwarding of the grant.
        client = connection(url.hostname, url.port, timeout=5)
        try:
            client.request(
                "POST",
                "/api/observations/file",
                body,
                {
                    "Authorization": "Bearer " + self.target.token,
                    "Content-Type": "application/octet-stream",
                    "X-Etude-Key": key,
                    "X-Etude-Sha256": sha256(body).hexdigest(),
                    "X-Etude-Modified-At": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
                },
            )
            with client.getresponse() as response:
                if response.status in (409, 413, 422):
                    raise ValueError(f"hosted view refused {key} (HTTP {response.status})")
                if response.status != 200:
                    raise HTTPException(f"hosted view returned HTTP {response.status}")
        finally:
            client.close()
        self._sent[key] = version

    def run(self) -> None:
        failed = False
        delay = 1
        while not (self.halt.is_set() or self.stop.is_set()):
            final = self.finish.is_set()
            failure: str | None = None
            try:
                for key in self._files():
                    if self.halt.is_set() or self.stop.is_set():
                        return
                    try:
                        self._send(key)
                    except FileNotFoundError:
                        continue  # Not sealed yet, or already folded into a wider block.
                    except ValueError as error:
                        # One refused record must not starve later sealed blocks.
                        failure = explain(error)
            except (OSError, HTTPException) as error:
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
    publisher = AttemptPublisher(session.parent.parent / "track" / run, target, halt)
    thread = threading.Thread(target=publisher.run, daemon=True, name="etude-view-publish")
    thread.start()
    try:
        yield
    finally:
        publisher.finish.set()
        until = time.monotonic() + min(
            5, max(0, (deadline - datetime.now(UTC)).total_seconds()) if deadline else 5
        )
        while thread.is_alive() and not halt.is_set() and time.monotonic() < until:
            thread.join(min(0.1, max(0, until - time.monotonic())))
        publisher.stop.set()
