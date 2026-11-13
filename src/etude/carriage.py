"""What may leave a run's folder, in what order and as what: one set of rules for both carriers.

A run's folder (`etude.tracking.storage.run_directory`) reaches storage by one of
two carriers: the tracker's shipper (`etude.tracking.delivery`) when a script runs
on its own, and the launcher's courier (`etude.launch.worker.courier`) when it
was launched, so the job's environment never holds bucket credentials. Both
decide here which files are ready (`listing`), their order, the transfer's
purpose, what a writer retired (`retired`), and which objects are never sent
twice (`immutable`): Etude's own blocks, tables and checkpoints, and any
file an author writes under a run's `artifacts/` folder. An eval run's
block (`etude.eval.block`) is sent data first and index last. Each carrier
keeps its own change detection and storage I/O. A fresh machine restores
records, the checkpoint `resume_file` names and every file in `out/`.
Without shared storage, `restore_set` and `put_back` relay that state between
hosts; `recorded_file` selects a single checkpoint or out file for an input.
A checkpoint file Etude copied keeps its name wherever it is stored, so any
address of it gives its key in the run's folder (`checkpoint_key`). A name is
never used twice (`taken`), so no key under `checkpoints/` ever holds two
different files. A checkpoint recorded in place names its key under `out/`.

Standard library only, and it imports neither the launcher nor tracking.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from errno import EEXIST, ENOTEMPTY
from hashlib import sha256
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import Literal

CHECKPOINTS = "checkpoints"


class RecordMismatch(ValueError):
    """Relayed checkpoint bytes that do not match the record naming them."""


# Names of checkpoint files held elsewhere (in storage, on another host) and
# not here, one per line. A dot-name, so no carrier ever sends it.
STORED = f"{CHECKPOINTS}/.stored"
# A complete relay preserves an empty out as well as its files.
OUT_PRESENT = ".out"


def checkpoint_file(key: str) -> bool:
    """A recorded model's bytes: a file under `checkpoints/` that is not its JSON record or a note."""
    folder, _, name = key.rpartition("/")
    return (
        "out" not in PurePosixPath(folder).parts
        and folder.rpartition("/")[2] == CHECKPOINTS
        and not name.endswith(".json")
        and not name.startswith(".")
    )


def checkpoint_file_name(address: str) -> str:
    """A checkpoint file's name, from any address or key of it: its last part.

    A file keeps its name wherever it is stored, so the name is what
    identifies it within its run (`checkpoint_key`).
    """
    return address.rpartition("/")[2]


def checkpoint_key(address: str) -> str:
    """Where a recorded checkpoint's file sits in its run's folder, from any address of it."""
    return f"{CHECKPOINTS}/{checkpoint_file_name(address)}"


ARTIFACTS = "artifacts"
# An eval run's sealed block is a folder `blocks/WRITTEN.WRITER/`; its index is written last.
EVAL_BLOCKS = "blocks"
EVAL_BLOCK_INDEX = "block.json"
# Its blobs, in one pack sent as a file: it may be hundreds of megabytes.
EVAL_BLOCK_PACK = "blobs.zip"


def _eval_block(folder: str) -> bool:
    parent, _, name = folder.rpartition("/")
    return parent.rpartition("/")[2] == EVAL_BLOCKS and not name.startswith(".")


def immutable(key: str) -> bool:
    """Whether an object never changes once written: a metric block or tail, a
    table, a checkpoint, an eval run's block, or an author's file under `artifacts/`.

    A carrier sends it once and never again, so nothing under `checkpoints/`
    is replaced with different bytes. An author writes each file under
    `artifacts/`, at any depth, once; naming it by its content keeps that
    true. The key may sit under any folders.
    """
    if "out" in PurePosixPath(key).parts:
        return False
    folder, _, name = key.rpartition("/")
    kind = folder.rpartition("/")[2]
    return (
        (kind == "metrics" and name.endswith((".blk", ".tail")))
        or (kind == "tables" and name.endswith(".parquet"))
        or checkpoint_file(key)
        or _eval_block(folder)
        or ARTIFACTS in PurePosixPath(folder).parts
    )


def purpose(key: str, recorded: Container[str] = ()) -> Literal["checkpoint", "report"]:
    """What a transfer of this key is for, in usage."""
    return "checkpoint" if key in recorded or checkpoint_file(key) else "report"


def _rank(key: str) -> int:
    # All out files precede records, including checkpoints recorded in place.
    # Ordering needs only paths; carriers read records for verification.
    if key.startswith("out/") or checkpoint_file(key):
        return 0
    if key.endswith("/" + EVAL_BLOCK_INDEX) and _eval_block(key.rpartition("/")[0]):
        return 2
    if immutable(key):
        return 1
    return 3


def _listing(root: Path, *, tail_seconds: float) -> list[str]:
    """Ready files as slash-separated keys, ordered by delivery rank then key.

    Out files share the first rank, so their membership is already in key order.
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


POINTER = "current.json"
# A numbered save file: its state folder (default or named), save folder and path within.
_SAVE_FILE = re.compile(r"(out/state/(?:[^/]+/)?)(save-[0-9]+)/.+")


def save_number(name: str) -> int | None:
    """N of a save folder named `save-N`; None for any other name."""
    match = re.fullmatch(r"save-([0-9]+)", name)
    return None if match is None else int(match[1])


def _save(key: str) -> tuple[str, int] | None:
    """The pointer owning a numbered save file, and that save's number."""
    match = _SAVE_FILE.fullmatch(key)
    if match is None:
        return None
    number = save_number(match[2])
    assert number is not None  # the pattern admits only numbered folders
    return match[1] + POINTER, number


def save_pointer(key: str) -> str | None:
    """The pointer owning a numbered save file, including named state."""
    save = _save(key)
    return None if save is None else save[0]


def state_pointer(key: str) -> bool:
    match = re.fullmatch(r"out/state/(?:([^/]+)/)?current\.json", key)
    return match is not None and (match[1] is None or valid_state_name(match[1]))


def valid_state_name(name: str) -> bool:
    return (
        re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name) is not None
        and save_number(name) is None
        and name != POINTER
    )


def save_name(body: bytes) -> str:
    """Validate the portable pointer once before using it to select or remove files."""
    value = json.loads(body)
    if not isinstance(value, dict) or set(value) != {"save"}:
        raise ValueError("state pointer must name one save")
    name = value["save"]
    if not isinstance(name, str) or save_number(name) is None:
        raise ValueError("state pointer must name a numbered save")
    return name


def _numbers(pointers: Mapping[str, bytes]) -> dict[str, int]:
    """The save each pointer names, by number."""
    return {key: int(save_name(body).removeprefix("save-")) for key, body in pointers.items()}


def _committed(keys: Iterable[str], pointers: Mapping[str, bytes]) -> list[str]:
    """Drop every save file but those the pointers name: orphaned and superseded folders
    never travel. A pointer whose save lacks its manifest among `keys` raises."""
    names = {
        key: key.removesuffix(POINTER) + save_name(body) + "/" for key, body in pointers.items()
    }
    selected = [
        key
        for key in keys
        if (pointer := save_pointer(key)) is None
        or pointer in names
        and key.startswith(names[pointer])
    ]
    present = set(selected)
    for pointer, folder in names.items():
        if folder + "state.json" not in present:
            raise ValueError(f"state pointer {pointer} names an incomplete save")
    return selected


def older_saves(keys: Iterable[str], pointers: Mapping[str, bytes]) -> list[str]:
    """Files strictly below acknowledged pointers; never remove a newer in-flight save."""
    numbers = _numbers(pointers)
    return [
        key
        for key in keys
        if (save := _save(key)) is not None and save[1] < numbers.get(save[0], 0)
    ]


def recovery_files(
    keys: Iterable[str], members: Container[str] | None, pointers: Mapping[str, bytes]
) -> list[str]:
    """State pointers override an older out membership after an interrupted delivery."""
    return _committed(
        (
            key
            for key in keys
            if members is None
            or not key.startswith("out/")
            or key in members
            or state_pointer(key)
            or save_pointer(key) is not None
        ),
        pointers,
    )


@dataclass(frozen=True)
class Publication:
    """One delivery's selected files and frozen pointers, captured before any upload."""

    keys: tuple[str, ...]
    pointers: Mapping[str, bytes]


def publication(root: Path, *, tail_seconds: float) -> Publication:
    # Pointers are read before the listing: a save committed in between waits
    # for the next pass instead of travelling under the pointer before it.
    bodies = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (*root.glob("out/state/current.json"), *root.glob("out/state/*/current.json"))
        if path.is_file()
    }
    keys = _listing(root, tail_seconds=tail_seconds)
    # The listing follows no link, so a linked pointer or state folder selects nothing.
    listed = set(keys)
    pointers = {key: body for key, body in bodies.items() if key in listed and state_pointer(key)}
    selected = _committed(keys, pointers)
    # Pointers follow every save file, including arbitrarily named callback files.
    selected.sort(key=lambda key: (key in pointers, _rank(key), key))
    return Publication(keys=tuple(selected), pointers=pointers)


def listing(root: Path, *, tail_seconds: float) -> list[str]:
    """Ready files in publication order, with only the selected saves."""
    return list(publication(root, tail_seconds=tail_seconds).keys)


def prune_state(root: Path, pointers: Mapping[str, bytes]) -> None:
    """Remove only save folders older than acknowledged pointers, including private files."""
    for key, number in _numbers(pointers).items():
        for path in (root / key).parent.glob("save-*"):
            found = save_number(path.name)
            if found is not None and found < number and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)


def note_retired(root: Path, writer: str, keys: Sequence[str]) -> None:
    """Append keys `writer` replaced to its `.removed` note, once what replaced them exists.

    Only complete lines count (`retired`), so a carrier never needs to know
    block names or bytes.
    """
    if keys:
        with (root / "metrics" / f"{writer}.removed").open("a") as note:
            note.write("".join(key + "\n" for key in keys))
            note.flush()


def retired(root: Path) -> list[str]:
    """Tracking files a writer has replaced, from its `.removed` notes (`note_retired`).

    Read before listing: a file retired during a pass is deleted only once a
    later pass has sent what replaced it. Out absence belongs
    to membership; recipe files are never interpreted as retirement notes.
    """
    lines: list[str] = []
    for note in sorted(root.rglob("*.removed")):
        parts = note.relative_to(root).parts
        if parts[0] != "out" and not any(part.startswith(".") for part in parts):
            lines.extend(
                key for key in note.read_text().split("\n")[:-1] if not key.startswith("out/")
            )
    return lines


@dataclass(frozen=True)
class RecordedFile:
    """A checkpoint file in a run's folder, as the record naming it describes it."""

    key: str
    sha256: str
    size_bytes: int
    # The key of the JSON record that names it.
    record: str


def _recorded(root: Path) -> dict[int, RecordedFile]:
    """By ordinal: the file each checkpoint record names, where Etude stored one.

    Reads the records (`etude.train.records.CheckpointRecord`) as plain JSON.
    A record that only references a model elsewhere names no file here.
    """
    recorded: dict[int, RecordedFile] = {}
    for path in (root / CHECKPOINTS).glob("*.json"):
        try:
            record = json.loads(path.read_bytes())
            ordinal, artifact = int(record["ordinal"]), dict(record["artifact"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        key = record.get("file") or checkpoint_key(str(artifact.get("uri", "")))
        if not isinstance(key, str) or not within(key):
            continue
        sha256, size = artifact.get("sha256"), artifact.get("size_bytes")
        if (
            isinstance(sha256, str)
            and isinstance(size, int)
            and (checkpoint_file(key) or key.startswith("out/"))
        ):
            recorded[ordinal] = RecordedFile(key, sha256, size, f"{CHECKPOINTS}/{path.name}")
    return recorded


def within(key: str) -> bool:
    """A plain relative file path, safe to put under a run's folder."""
    return not key.startswith("/") and not ({"", ".", ".."} & set(key.split("/")))


def recorded_files(root: Path) -> dict[str, RecordedFile]:
    """Checkpoint bytes by their portable key, including references inside out."""
    return {file.key: file for file in _recorded(root).values()}


def resume_file(root: Path) -> RecordedFile | None:
    """The file of the highest-ordinal checkpoint record in `root`, when Etude stored one."""
    recorded = _recorded(root)
    return recorded[max(recorded)] if recorded else None


def named_file(root: Path, key: str) -> RecordedFile | None:
    """The checkpoint file `key` as the record in `root` naming it describes it, when one does."""
    return next((file for file in _recorded(root).values() if file.key == key), None)


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
            if checkpoint_file(checkpoint_key(path.name)) and path.suffix != ".partial"
        }
        if folder.is_dir()
        else set()
    )
    return here | _stored(root)


def restore_set(root: Path) -> dict[str, bytes]:
    """What a later attempt on another host needs of this folder, by key.

    The declaration, every checkpoint record, the file `resume_file` names,
    all of out, and the note of every checkpoint name
    `taken`. Metric blocks and other copied milestones remain in storage.
    """
    keys = [f"{CHECKPOINTS}/{path.name}" for path in sorted((root / CHECKPOINTS).glob("*.json"))]
    if (root / "run.json").is_file():
        keys.append("run.json")
    wanted = resume_file(root)
    if wanted is not None and (root / wanted.key).is_file():
        keys.append(wanted.key)
    snapshot = publication(root, tail_seconds=0)
    keys.extend(key for key in snapshot.keys if key.startswith("out/"))
    files = {
        key: snapshot.pointers[key] if key in snapshot.pointers else (root / key).read_bytes()
        for key in dict.fromkeys(keys)
    }
    if (root / "out").is_dir() or (root / OUT_PRESENT).is_file():
        files[OUT_PRESENT] = b""
    files[STORED] = "".join(name + "\n" for name in sorted(taken(root))).encode()
    return files


def recorded_file(root: Path, key: str) -> dict[str, bytes]:
    """One checkpoint with its record, or an out file, by key.

    What another host needs to be given that one file (`put_back` checks it
    against the record). Raises `FileNotFoundError` when the file is gone or
    a key outside `out/` has no checkpoint record.
    """
    named = named_file(root, key)
    if named is not None:
        return {named.record: (root / named.record).read_bytes(), key: (root / key).read_bytes()}
    if within(key) and key.startswith("out/"):
        return {key: (root / key).read_bytes()}
    raise FileNotFoundError(f"no checkpoint record in {root} names {key}")


def reconcile_out(root: Path, keys: Container[str]) -> None:
    """Remove stale portable files before restoring out in full.

    Private and unfinished files belong to the local producer and stay local.
    Empty directories are removed so a restored file can replace a directory.
    """
    out = root / "out"
    for key in _listing(out, tail_seconds=0):
        if "out/" + key not in keys:
            (out / key).unlink(missing_ok=True)
    for path in sorted(out.rglob("*"), reverse=True):
        if (
            path.is_dir()
            and not path.is_symlink()
            and not any(part.startswith(".") for part in path.relative_to(out).parts)
        ):
            try:
                path.rmdir()
            except OSError as error:
                if error.errno not in (EEXIST, ENOTEMPTY):
                    raise


def put_back(root: Path, files: Mapping[str, bytes]) -> None:
    """Restore relayed files, retaining existing records and replacing out files.

    Records come first, so each checkpoint file is checked against the digest
    of the record that names it; a mismatch raises `RecordMismatch` and writes no file.
    """
    if any(not within(key) for key in files):
        raise RecordMismatch("a restored file must be within the run folder")
    # Only a complete relay includes the checkpoint-name ledger. A single
    # input must never clear unrelated out files.
    if STORED in files:
        reconcile_out(root, files)
    ordered = sorted(files, key=lambda key: (checkpoint_file(key) or key.startswith("out/"), key))
    recorded: dict[str, RecordedFile] | None = None
    for key in ordered:
        target = root / key
        if key == OUT_PRESENT:
            (root / "out").mkdir(parents=True, exist_ok=True)
        if key == STORED:
            note_stored(root, files[key].decode().split("\n")[:-1])
            continue
        if target.exists() and not key.startswith("out/"):
            continue
        if recorded is None and (checkpoint_file(key) or key.startswith("out/")):
            recorded = recorded_files(root)
        named = None if recorded is None else recorded.get(key)
        if named is not None or checkpoint_file(key):
            if (
                named is None
                or named.size_bytes != len(files[key])
                or named.sha256 != sha256(files[key]).hexdigest()
            ):
                raise RecordMismatch(f"{key} does not match the checkpoint record that names it")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(files[key])
        partial.replace(target)
