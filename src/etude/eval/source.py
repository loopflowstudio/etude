"""The source files that identify an environment, or version a player's code: `digest_files`.

A file is named by its path below its root, so one file has one name on every
machine. Inside a git checkout the root is the checkout and only tracked files
count, so caches and scratch output beside the code never change an identity.
A launched job's source is an extracted commit with no `.git`; there the root
is `ETUDE_SOURCE_DIR`, which the launcher's worker sets to it (or the working
directory when unset), and every file below a given path counts.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from etude.run.errors import EtudeError
from etude.run.records import Digest

# Set by the launcher's worker to the source a job runs from, and the commit it is.
SOURCE_DIR = "ETUDE_SOURCE_DIR"
SOURCE_COMMIT = "ETUDE_COMMIT"


def source_commit(paths: Sequence[Path | str]) -> str | None:
    """The git commit the first path's source is at: its checkout's HEAD, or a launched
    job's commit. None outside both.

    Uncommitted edits do not change it; `digest_files` of the same paths does.
    """
    given_source = os.environ.get(SOURCE_DIR)
    source = Path(given_source).resolve() if given_source else None
    first = Path(paths[0]).resolve()
    root = _checkout(first, source)
    if root is None:
        within = source is not None and first.is_relative_to(source)
        return os.environ.get(SOURCE_COMMIT) if within else None
    found = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return found.stdout.strip() or None if found.returncode == 0 else None


def digest_files(paths: Sequence[Path | str]) -> Digest:
    """The digest of every file at or below `paths`, by name and content.

    Raises `EtudeError("identity_file_missing")` for a path that does not
    exist, and `EtudeError("identity_path_without_files")` for one with no
    file at or below it, or none tracked inside a git checkout.
    """
    digest = sha256()
    named = _named(paths)
    for name in sorted(named):
        data = named[name].read_bytes()
        digest.update(f"{name}\0{len(data)}\0".encode() + data)
    return digest.hexdigest()


def _named(paths: Sequence[Path | str]) -> dict[str, Path]:
    """Each file by its name below its root; one `git ls-files` per checkout."""
    given_source = os.environ.get(SOURCE_DIR)
    source = Path(given_source).resolve() if given_source else None
    # Outside a checkout, names are below the source root, or the working directory.
    base = source or Path.cwd().resolve()
    checkouts: dict[Path, list[Path]] = {}
    found: dict[str, Path] = {}
    for given in map(Path, paths):
        if not given.exists():
            raise EtudeError("identity_file_missing", 422) from FileNotFoundError(given)
        resolved = given.resolve()
        root = _checkout(resolved, source)
        if root is not None:
            checkouts.setdefault(root, []).append(resolved)
            continue
        files = (
            sorted(p for p in resolved.rglob("*") if p.is_file()) if given.is_dir() else [resolved]
        )
        if not files:
            raise EtudeError("identity_path_without_files", 422) from ValueError(given)
        for path in files:
            name = path.relative_to(base) if path.is_relative_to(base) else path
            found[name.as_posix()] = path
    for root, given_paths in checkouts.items():
        below_root = [path.relative_to(root).as_posix() for path in given_paths]
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", *below_root],
            check=True,
            capture_output=True,
        ).stdout.decode()
        tracked = [name for name in listed.split("\0") if name and (root / name).is_file()]
        for path in given_paths:
            below = path.relative_to(root).as_posix()
            mine = [n for n in tracked if path == root or n == below or n.startswith(below + "/")]
            if not mine:
                raise EtudeError("identity_path_without_files", 422) from ValueError(path)
            found.update((name, root / name) for name in mine)
    return found


def _checkout(path: Path, source: Path | None) -> Path | None:
    """The git checkout holding `path`, unless the source root comes first."""
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
        if parent == source:
            return None
    return None
