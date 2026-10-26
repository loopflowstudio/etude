"""A job's preparation, kept on this machine and reused while its key is unchanged.

A `Preparation` writes one directory, `ETUDE_HOME/prepared/KEY/`, built in place
because a virtual environment is not relocatable. Its key is the SHA256 of a
`Key`: the content of the declared source paths, the command, the declared
inputs' digests, the job environment (hashed, never written out) and the
machine's platform. Beside it:

    KEY/        what the command wrote; $ETUDE_PREPARED_DIR
    KEY.json    the `Receipt`, written last and atomically: the only "prepared" fact
    KEY.lock    exclusive while building or evicting; shared while an attempt uses it

With `Preparation.carry`, a build also gets `ETUDE_HOME/carried/CARRY/`
($ETUDE_CARRIED_DIR), keyed by the declared paths and the platform alone
(`Key.carry`), so a new command or input still finds it. It has no receipt:
it is a cache the command checks, such as uv's. Kept here, it is used as it
is; otherwise it is restored from the bucket (`etude.launch.worker.carried`)
or starts empty. `CARRY.lock` is exclusive while a build uses it.

An attempt finds one of three things. With a receipt whose every manifest entry
verifies, the directory is *reused* and the command does not run. Without a
receipt, anything left by an interrupted build is deleted and it is *built*.
A manifest mismatch is *rejected*: the directory is deleted and built again.
Files the manifest does not list, such as bytecode a run writes, are ignored.
The shared lock pins the directory for the attempt, so eviction, least recently
used first beyond half the disk available to preparations and carried
directories together, skips it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import tarfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal, NamedTuple

from etude.launch.logging import explain
from etude.launch.records import Input, Preparation, Record, Variable
from etude.launch.schemas import Prepared
from etude.launch.storage.files import digest, write
from etude.launch.storage.s3 import StorageUnavailable


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
        return _digest(self.model_dump(mode="json"))

    @property
    def carry(self) -> str:
        """The key of the carried directory: the declared paths and the platform alone."""
        return _digest({"paths": self.paths, "machine": self.machine})


def _digest(fields: dict[str, object]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class Receipt(Record):
    key: Key
    size_bytes: int
    manifest: tuple[Entry, ...]


def home() -> Path:
    return _etude() / "prepared"


def carried_home() -> Path:
    return _etude() / "carried"


def _etude() -> Path:
    return Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude")


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
def lock(path: Path, waiting: Callable[[], bool]) -> Iterator[IO[str] | None]:
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
    with lock(root / f"{name}.lock", waiting) as held:
        if held is None:
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
                fcntl.flock(held, fcntl.LOCK_SH)
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
        fcntl.flock(held, fcntl.LOCK_SH)
        evict(shutil.disk_usage(root).free, log)
        yield (
            directory,
            Prepared(key=name, outcome=outcome, seconds=seconds, size_bytes=size, cause=cause),
        )


class Carried(NamedTuple):
    """The carried directory a build uses, and where its contents came from."""

    directory: Path
    kept: bool  # already on this machine, used as it was
    restored_bytes: int | None  # the bucket's archive, restored into it


Restore = Callable[[Path], int | None]  # fills a directory; the bytes restored, or None


@contextmanager
def carry(
    key: str, restore: Restore, waiting: Callable[[], bool], log: Callable[[str], None]
) -> Iterator[Carried | None]:
    """The carried directory for `key`, held exclusively while the caller builds with it.

    Kept on this machine, it is used as it is; otherwise `restore` fills it. A
    restore that fails is logged and the directory starts empty, as the build
    does not depend on it. Yields None when the wait for another build was given up.
    """
    root = carried_home()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = root / key
    with lock(root / f"{key}.lock", waiting) as held:
        if held is None:
            yield None
            return
        os.utime(root / f"{key}.lock")  # recently used
        if directory.is_dir():
            yield Carried(directory, kept=True, restored_bytes=None)
            return
        restored = None
        try:
            restored = restore(directory)
        except (StorageUnavailable, OSError, ValueError, tarfile.TarError) as error:
            log(f"carry: refused {key[:12]} ({explain(error).splitlines()[0]})")
            _remove(directory)
        if restored is not None:
            log(f"prepare: restored {key[:12]} ({restored} bytes)")
        directory.mkdir(exist_ok=True)
        yield Carried(directory, kept=False, restored_bytes=restored)


class _Kept(NamedTuple):
    """A prepared or carried directory on this machine, as eviction sees it."""

    directory: Path
    lock: Path
    receipt: Path | None  # a prepared directory's; a carried one has none
    size_bytes: int
    used: float  # when last used, as a file modification time


def _kept() -> list[_Kept]:
    kept = []
    root = home()
    for receipt in root.glob("*.json"):
        name = receipt.name.removesuffix(".json")
        size = Receipt.model_validate_json(receipt.read_text()).size_bytes
        kept.append(
            _Kept(root / name, root / f"{name}.lock", receipt, size, receipt.stat().st_mtime)
        )
    carried = carried_home()
    for directory in (p for p in carried.glob("*") if p.is_dir()):
        size = sum(
            p.stat().st_size for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
        )
        lock_file = carried / f"{directory.name}.lock"
        used = lock_file.stat().st_mtime if lock_file.exists() else directory.stat().st_mtime
        kept.append(_Kept(directory, lock_file, None, size, used))
    return kept


def evict(free_bytes: int, log: Callable[[str], None]) -> None:
    """Delete least recently used prepared and carried directories beyond half of what
    they and the disk's `free_bytes` together hold.

    A directory an attempt is using holds its lock, and is kept.
    """
    kept = sorted(_kept(), key=lambda entry: entry.used)
    used = sum(entry.size_bytes for entry in kept)
    allowance = (free_bytes + used) // 2
    for entry in kept:
        if used <= allowance:
            return
        with entry.lock.open("a") as held:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # in use
            if entry.receipt is not None:
                entry.receipt.unlink()
            _remove(entry.directory)
            used -= entry.size_bytes
            log(f"prepare: evicted {entry.directory.name[:12]} ({entry.size_bytes} bytes)")
    if used > allowance:
        log(f"prepare: {used - allowance} bytes over allowance; the rest is in use")
