"""A job's preparation, kept on this machine and reused while its key is unchanged.

A `Preparation` writes one directory, `ETUDE_HOME/prepared/KEY/`, built in place
because a virtual environment is not relocatable. Its key is the SHA256 of a
`Key`: the content of the declared source paths, the command, the declared
inputs' digests, the job environment (hashed, never written out) and the
machine's platform. Beside it:

    KEY/        what the command wrote; $ETUDE_PREPARED_DIR
    KEY.json    the `Receipt`, written last and atomically: the only "prepared" fact
    KEY.lock    exclusive while building or evicting; shared while an attempt uses it

An attempt finds one of three things. With a receipt whose every manifest entry
verifies, the directory is *reused* and the command does not run. Without a
receipt, anything left by an interrupted build is deleted and it is *built*.
A manifest mismatch is *rejected*: the directory is deleted and built again.
Files the manifest does not list, such as bytecode a run writes, are ignored.
The shared lock pins the directory for the attempt, so eviction, least recently
used first beyond half the disk available to preparations, skips it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal

from etude.launch.records import Input, Preparation, Record, Variable
from etude.launch.schemas import Prepared
from etude.launch.storage.files import digest, write


class Entry(Record):
    """One file or link the command wrote, as it was when the build finished."""

    path: str  # relative to the prepared directory
    size_bytes: int = 0
    sha256: str | None = None  # a regular file's
    link: str | None = None  # a symbolic link's target, as written


class Key(Record):
    """Everything that decides what a preparation writes."""

    command: str
    paths: dict[str, str]  # source path: SHA256 of its content
    inputs: dict[str, str]  # input name: SHA256-SIZE
    environment: str  # SHA256 of the job's own variables
    machine: dict[str, str]

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class Receipt(Record):
    key: Key
    size_bytes: int
    manifest: tuple[Entry, ...]


def home() -> Path:
    return Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude") / "prepared"


def key(
    preparation: Preparation, source: Path, inputs: tuple[Input, ...], job: tuple[Variable, ...]
) -> Key:
    """The key of `preparation` for this source and machine; raises for a path the source lacks."""
    given = {i.name: i for i in inputs}
    variables = json.dumps(sorted([v.name, v.value] for v in job))
    return Key(
        command=preparation.command,
        paths={path: _tree(source, path) for path in sorted(preparation.paths)},
        inputs={
            name: f"{given[name].sha256}-{given[name].size_bytes}"
            for name in sorted(preparation.inputs)
        },
        environment=hashlib.sha256(variables.encode()).hexdigest(),
        machine=_machine(),
    )


def _machine() -> dict[str, str]:
    """The platform a build depends on; the kernel release is left out, as it varies by host."""
    libc, version = platform.libc_ver()
    facts = {
        "system": platform.system(),
        "machine": platform.machine(),
        "libc": f"{libc} {version}",
    }
    try:
        release = Path("/etc/os-release").read_text()
    except OSError:
        release = ""
    for line in release.splitlines():
        name, _, value = line.partition("=")
        if name in ("ID", "VERSION_ID"):
            facts[name.lower()] = value.strip('"')
    if platform.system() == "Darwin":
        facts["macos"] = platform.mac_ver()[0]
    return facts


def _tree(source: Path, path: str) -> str:
    """The SHA256 of a file, or of a folder's file names and contents."""
    root = source / path
    if root.is_file():
        return digest(root)
    if not root.is_dir():
        raise ValueError(f"prepare path {path!r} is not in the job's source")
    summary = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        name = file.relative_to(root).as_posix()
        value = os.readlink(file) if file.is_symlink() else digest(file)
        summary.update(f"{name}\0{value}\n".encode())
    return summary.hexdigest()


def _manifest(directory: Path) -> tuple[Entry, ...]:
    entries = []
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if path.is_symlink():
            entries.append(Entry(path=name, link=os.readlink(path)))
        elif path.is_file():
            entries.append(Entry(path=name, size_bytes=path.stat().st_size, sha256=digest(path)))
    return tuple(entries)


def _mismatch(directory: Path, manifest: tuple[Entry, ...]) -> str | None:
    """The first entry that no longer matches what the build wrote, or None."""
    for entry in manifest:
        path = directory / entry.path
        if entry.link is not None:
            if not path.is_symlink() or os.readlink(path) != entry.link:
                return f"{entry.path} link changed"
        elif path.is_symlink() or not path.is_file():
            return f"{entry.path} missing"
        elif path.stat().st_size != entry.size_bytes or digest(path) != entry.sha256:
            return f"{entry.path} digest mismatch"
    return None


def _remove(directory: Path) -> None:
    """Delete a prepared directory, read-only folders included."""
    if not directory.exists():
        return
    for folder in [
        directory,
        *(p for p in directory.rglob("*") if p.is_dir() and not p.is_symlink()),
    ]:
        folder.chmod(folder.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(directory)


def _seal(directory: Path) -> None:
    """Make what the build wrote read-only, so a run does not change it by accident."""
    for path in [*directory.rglob("*"), directory]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


@contextmanager
def _lock(path: Path, waiting: Callable[[], bool]) -> Iterator[IO[str] | None]:
    """An exclusive lock, or None once `waiting` says to give up."""
    with path.open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting():
                    yield None
                    return
                time.sleep(0.1)
        yield lock


Build = Callable[[Path], bool]  # runs the command into a directory; whether it succeeded


@contextmanager
def prepare(
    found: Key, build: Build, waiting: Callable[[], bool], log: Callable[[str], None]
) -> Iterator[tuple[Path, Prepared] | None]:
    """The prepared directory for `found`, reused or built, pinned while the caller uses it.

    Yields None when the wait for another attempt's build was given up, and a
    `Prepared` with no size when the build failed.
    """
    root = home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = found.digest
    directory, receipt = root / name, root / f"{name}.json"
    with _lock(root / f"{name}.lock", waiting) as lock:
        if lock is None:
            yield None
            return
        began = time.monotonic()
        cause = None
        if receipt.exists():
            try:
                kept: Receipt | None = Receipt.model_validate_json(receipt.read_text())
            except ValueError:
                kept = None
            cause = "receipt unreadable" if kept is None else _mismatch(directory, kept.manifest)
            if kept is not None and cause is None:
                os.utime(receipt)  # recently used
                done = Prepared(
                    key=name,
                    outcome="reused",
                    seconds=time.monotonic() - began,
                    size_bytes=kept.size_bytes,
                )
                log(f"prepare: reused {name[:12]} (verified {done.seconds:.1f} s)")
                fcntl.flock(lock, fcntl.LOCK_SH)
                yield directory, done
                return
            log(f"prepare: rejected {name[:12]} ({cause})")
            receipt.unlink()
        _remove(directory)  # an interrupted or rejected build's leftovers
        directory.mkdir()
        outcome: Literal["built", "rejected"] = "rejected" if cause else "built"
        began = time.monotonic()
        if not build(directory):
            _remove(directory)
            yield directory, Prepared(key=name, outcome=outcome, seconds=0, cause=cause)
            return
        _seal(directory)
        manifest = _manifest(directory)
        size = sum(entry.size_bytes for entry in manifest)
        seconds = time.monotonic() - began
        write(
            receipt,
            Receipt(key=found, size_bytes=size, manifest=manifest).model_dump_json(),
        )
        log(f"prepare: built {name[:12]} ({seconds:.1f} s, {size} bytes)")
        fcntl.flock(lock, fcntl.LOCK_SH)
        evict(root, shutil.disk_usage(root).free // 2 + _kept(root) // 2, log)
        yield (
            directory,
            Prepared(key=name, outcome=outcome, seconds=seconds, size_bytes=size, cause=cause),
        )


def _kept(root: Path) -> int:
    return sum(Receipt.model_validate_json(p.read_text()).size_bytes for p in root.glob("*.json"))


def evict(root: Path, allowance: int, log: Callable[[str], None]) -> None:
    """Delete least recently used prepared directories beyond `allowance` bytes.

    A directory an attempt is using holds its lock shared, and is kept.
    """
    receipts = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime)
    sizes = {p: Receipt.model_validate_json(p.read_text()).size_bytes for p in receipts}
    used = sum(sizes.values())
    for receipt in receipts:
        if used <= allowance:
            return
        name = receipt.name.removesuffix(".json")
        with (root / f"{name}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # in use
            receipt.unlink()
            _remove(root / name)
            used -= sizes[receipt]
            log(f"prepare: evicted {name[:12]} ({sizes[receipt]} bytes)")
    if used > allowance:
        log(f"prepare: {used - allowance} bytes over allowance; the rest is in use")
