"""Advertise the bound view while its owner holds the address file open."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO
from urllib.parse import quote

from etude.launch.source import home


@contextmanager
def advertise(origin: str) -> Iterator[None]:
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    address = root / "view"
    # Publish the locked inode whole. A crash releases the lock. A new view
    # may take over the address before the previous owner has finished.
    with TemporaryDirectory(dir=root) as stage, (Path(stage) / "view").open("w") as stream:
        stream.write(origin + "\n")
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_EX)
        with _directory_lock(root):
            os.replace(stream.name, address)
        try:
            yield
        finally:
            with _directory_lock(root):
                if address.exists() and address.stat().st_ino == os.fstat(stream.fileno()).st_ino:
                    address.unlink()


@contextmanager
def _directory_lock(root: Path) -> Iterator[None]:
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def print_view(run: str, *, file: TextIO | None = None) -> None:
    try:
        with (home() / "view").open() as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                print(
                    f"view: {stream.read().strip()}/runs/{quote(run, safe='/@=')}",
                    file=file,
                    flush=True,
                )
    except FileNotFoundError:
        pass
