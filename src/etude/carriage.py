"""What may leave a run's folder, in what order and as what: one set of rules for both carriers.

A run's folder (`etude.run.storage.run_directory`) reaches storage by one of
two carriers: the tracker's shipper (`etude.run.delivery`) when a script runs
on its own, and the launcher's courier (`etude.launch.worker.courier`) when it
was launched, so the job's environment never holds bucket credentials. Both
decide here which files are ready (`listing`), their order, the transfer's
purpose, what a writer retired (`retired`), and which objects are never sent
twice (`immutable`): Etude's own blocks, tables and checkpoints, and any
file an author writes under a run's `artifacts/` folder. Each carrier keeps its own change detection and storage
I/O. A later attempt on a fresh machine restores the records and the one
checkpoint file `resume_file` names, from storage or, between owned hosts
without one, as the `restore_set` another host's folder gives (`put_back`);
a continuation's machine is given its parent's one file (`recorded_file`).
A checkpoint file's name is never used twice (`taken`), so no key under
`checkpoints/` ever holds two different files.

Standard library only, and it imports neither the launcher nor tracking.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import Literal

CHECKPOINTS = "checkpoints"


class Mismatch(ValueError):
    """Relayed checkpoint bytes that do not match the record naming them."""


# Names of checkpoint files held elsewhere (in storage, on another host) and
# not here, one per line. A dot-name, so no carrier ever sends it.
STORED = f"{CHECKPOINTS}/.stored"


def checkpoint_file(key: str) -> bool:
    """A recorded model's bytes: a file under `checkpoints/` that is not its JSON record or a note."""
    folder, _, name = key.rpartition("/")
    return (
        folder.rpartition("/")[2] == CHECKPOINTS
        and not name.endswith(".json")
        and not name.startswith(".")
    )


ARTIFACTS = "artifacts"


def immutable(key: str) -> bool:
    """Whether an object never changes once written: a metric block or tail, a
    table, a checkpoint, or an author's file under `artifacts/`.

    A carrier sends it once and never again, so nothing under `checkpoints/`
    is replaced with different bytes. An author writes each file under
    `artifacts/`, at any depth, once; naming it by its content keeps that
    true. The key may sit under any folders.
    """
    folder, _, name = key.rpartition("/")
    kind = folder.rpartition("/")[2]
    return (
        (kind == "metrics" and name.endswith((".blk", ".tail")))
        or (kind == "tables" and name.endswith(".parquet"))
        or checkpoint_file(key)
        or ARTIFACTS in PurePosixPath(folder).parts
    )


def purpose(key: str) -> Literal["checkpoint", "report"]:
    """What a transfer of this key is for, in usage."""
    return "checkpoint" if checkpoint_file(key) else "report"


def _rank(key: str) -> int:
    # Checkpoint files precede their records, and data the indexes and
    # records that describe it, so a reader never finds a record first.
    if checkpoint_file(key):
        return 0
    if immutable(key):
        return 1
    return 2


def listing(root: Path, *, tail_seconds: float) -> list[str]:
    """Every file under `root` ready to carry, as slash-separated keys, in sending order.

    A file still being written (`.open`, `.partial`) waits until it is whole,
    and anything under a folder starting with `.` (a carrier's own markers)
    never leaves. A tail is ready once it is `tail_seconds` old: 0 once its
    writer has ended, `math.inf` while it runs and owns it.
    """
    keys: list[str] = []
    if not root.is_dir():
        return keys
    now = time.time()
    for path in root.rglob("*"):
        name = PurePosixPath(path.relative_to(root).as_posix())
        if any(part.startswith(".") for part in name.parts) or path.suffix in (".open", ".partial"):
            continue
        try:
            stat = path.lstat()  # a link is never followed out of the folder
        except FileNotFoundError:
            continue  # retired since the listing; what replaced it is seen next pass
        if not S_ISREG(stat.st_mode):
            continue
        if path.suffix == ".tail" and tail_seconds > 0 and now - stat.st_mtime < tail_seconds:
            continue
        keys.append(name.as_posix())
    return sorted(keys, key=lambda key: (_rank(key), key))


def retired(root: Path) -> list[str]:
    """Every key a writer has replaced, from its `.removed` notes.

    Read before listing: a file retired during a pass is deleted only once a
    later pass has sent what replaced it.
    """
    lines: list[str] = []
    for note in sorted(root.rglob("*.removed")):
        if not any(part.startswith(".") for part in note.relative_to(root).parts):
            lines += note.read_text().split("\n")[:-1]
    return lines


@dataclass(frozen=True)
class RecordedFile:
    """A checkpoint file in a run's folder, as the record naming it describes it."""

    key: str
    sha256: str
    size_bytes: int


def _recorded(root: Path) -> dict[int, tuple[str, RecordedFile]]:
    """By ordinal: each checkpoint record's key and the file it names, where Etude stored one.

    Reads the records (`etude.train.records.CheckpointRecord`) as plain JSON.
    A record that only references a model elsewhere names no file here.
    """
    recorded: dict[int, tuple[str, RecordedFile]] = {}
    for path in (root / CHECKPOINTS).glob("*.json"):
        try:
            record = json.loads(path.read_bytes())
            ordinal, artifact = int(record["ordinal"]), dict(record["artifact"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        key = f"{CHECKPOINTS}/{PurePosixPath(str(artifact.get('uri', ''))).name}"
        sha256, size = artifact.get("sha256"), artifact.get("size_bytes")
        if isinstance(sha256, str) and isinstance(size, int) and checkpoint_file(key):
            recorded[ordinal] = (f"{CHECKPOINTS}/{path.name}", RecordedFile(key, sha256, size))
    return recorded


def resume_file(root: Path) -> RecordedFile | None:
    """The file of the highest-ordinal checkpoint record in `root`, when Etude stored one."""
    recorded = _recorded(root)
    return recorded[max(recorded)][1] if recorded else None


def _stored(root: Path) -> set[str]:
    note = root / STORED
    return set(note.read_text().split("\n")[:-1]) if note.is_file() else set()


def note_stored(root: Path, names: Iterable[str]) -> None:
    """Remember checkpoint file names held elsewhere, so none is used again here."""
    new = sorted(set(names) - _stored(root))
    if new:
        (root / CHECKPOINTS).mkdir(parents=True, exist_ok=True)
        with (root / STORED).open("a") as note:
            note.write("".join(name + "\n" for name in new))


def taken(root: Path) -> set[str]:
    """Every checkpoint file name this run has used: in its folder here, or held elsewhere.

    A file kept without its record (the writer died between the two) still
    holds its name, so a later checkpoint never takes it and different bytes
    never reach that key, here or in storage.
    """
    folder = root / CHECKPOINTS
    here = (
        {
            path.name
            for path in folder.iterdir()
            if checkpoint_file(f"{CHECKPOINTS}/{path.name}") and path.suffix != ".partial"
        }
        if folder.is_dir()
        else set()
    )
    return here | _stored(root)


def restore_set(root: Path) -> dict[str, bytes]:
    """What a later attempt on another host needs of this folder, by key.

    The declaration, every checkpoint record, the file `resume_file` names,
    and the note of every checkpoint name `taken`. Nothing else: no metrics
    and no other milestone.
    """
    keys = [f"{CHECKPOINTS}/{path.name}" for path in sorted((root / CHECKPOINTS).glob("*.json"))]
    if (root / "run.json").is_file():
        keys.append("run.json")
    wanted = resume_file(root)
    if wanted is not None and (root / wanted.key).is_file():
        keys.append(wanted.key)
    files = {key: (root / key).read_bytes() for key in keys}
    files[STORED] = "".join(name + "\n" for name in sorted(taken(root))).encode()
    return files


def recorded_file(root: Path, key: str) -> dict[str, bytes]:
    """One recorded checkpoint file of this folder with the record that names it, by key.

    What another host needs to be given that one file (`put_back` checks it
    against the record). Raises `FileNotFoundError` when no record here names
    `key` or its file is gone.
    """
    for record, named in _recorded(root).values():
        if named.key == key:
            return {record: (root / record).read_bytes(), key: (root / key).read_bytes()}
    raise FileNotFoundError(f"no checkpoint record in {root} names {key}")


def put_back(root: Path, files: Mapping[str, bytes]) -> None:
    """Write what `restore_set` or `recorded_file` gave into this host's folder, keeping what it has.

    Records come first, so each checkpoint file is checked against the digest
    of the record that names it; a mismatch raises `Mismatch` and writes no file.
    """
    ordered = sorted(files, key=lambda key: (checkpoint_file(key), key))
    for key in ordered:
        target = root / key
        if key == STORED:
            note_stored(root, files[key].decode().split("\n")[:-1])
            continue
        if target.exists():
            continue
        if checkpoint_file(key):
            named = {file.key: file.sha256 for _, file in _recorded(root).values()}
            if named.get(key) != sha256(files[key]).hexdigest():
                raise Mismatch(f"{key} does not match the checkpoint record that names it")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(files[key])
        partial.replace(target)
