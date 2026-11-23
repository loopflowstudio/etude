"""Advertise the bound view while its owner holds the address file open."""

import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO
from urllib.parse import quote

from etude.home import home


@contextmanager
def advertise(origin: str) -> Iterator[Callable[[], None]]:
    """Publish a locked origin and yield a way to reclaim an unowned address."""
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    address = root / "view"
    with TemporaryDirectory(dir=root) as stage, (Path(stage) / "origin").open("w") as stream:
        stream.write(origin + "\n")
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_EX)

        def publish() -> None:
            # Keep our locked inode even when a newer view replaces the public link.
            link = Path(stage) / "view"
            os.link(stream.name, link)
            os.replace(link, address)

        def reclaim() -> None:
            with _directory_lock(root):
                if _live_origin(address) is None:
                    publish()

        with _directory_lock(root):
            publish()
        try:
            yield reclaim
        finally:
            with _directory_lock(root):
                if address.exists() and address.samefile(stream.name):
                    address.unlink()


@contextmanager
def _directory_lock(root: Path) -> Iterator[None]:
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def run_path(address: str) -> str:
    return "/runs/" + quote(address, safe="/@=")


def _live_origin(address: Path) -> str | None:
    try:
        with address.open() as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return stream.read().strip()
    except FileNotFoundError:
        pass
    return None


def local_origin() -> str | None:
    return _live_origin(home() / "view")


def print_view(run: str, *, origin: str | None = None, file: TextIO | None = None) -> None:
    if not origin:
        origin = (
            os.environ.get("ETUDE_CLIENT_VIEW_URL")
            if os.environ.get("ETUDE_SERVER_LOCAL")
            else local_origin()
        )
    if origin:
        print(f"view: {origin}{run_path(run)}", file=file, flush=True)
