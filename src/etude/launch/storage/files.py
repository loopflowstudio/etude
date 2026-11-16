"""Journal writes, file fingerprints, output selection and job-owned progress reads.

Both readers and workers use these file operations; reading a journal never
loads the process that executes attempts.
"""

import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from etude.errors import LauncherError
from etude.launch.records import Output
from etude.out import OutFile, OutFiles
from etude.records import RunProgress

RESULT = "etude-result.json"


def write(path: Path, text: str) -> None:
    """Whole file or no file: readers never see half of it."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text)
    partial.replace(path)


def append_usage(session: Path, raw: str) -> None:
    """Keep one transfer in the session's usage journal, which its courier delivers.

    The courier and the session's attempts both append; each line is written whole.
    """
    with (session / "usage.jsonl").open("a") as stream:
        stream.write(raw + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_result(outputs: Path) -> RunProgress:
    """Validate job-owned progress once; unreadable or partial reports mean no progress."""
    try:
        return RunProgress.model_validate_json((outputs / RESULT).read_bytes(), strict=True)
    except (OSError, ValueError):
        return RunProgress()


def select_output(
    run: str, name: str, outputs: Sequence[Output], read_out: Callable[[], OutFiles | None]
) -> Output | OutFile:
    """Prefer attempt outputs; read out membership only when needed, and list names on absence."""
    for output in outputs:
        if output.name == name:
            return output
    membership = read_out()
    members = () if membership is None else membership.files
    for member in members:
        if member.key == f"out/{name}":
            return member
    names = list(
        dict.fromkeys([o.name for o in outputs] + [m.key.removeprefix("out/") for m in members])
    )
    available = ", ".join(names[:20]) or "(none)"
    if len(names) > 20:
        available += f", and {len(names) - 20} more"
    raise LauncherError(f"run {run} has no output {name!r}; it has: {available}")
