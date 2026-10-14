"""Atomic journal writes, file digests and job-owned progress reads.

Both readers and workers use these file operations; reading a journal never
loads the process that executes attempts.
"""

import hashlib
from pathlib import Path

from etude.launch.schemas import Result

RESULT = "etude-result.json"


def write(path: Path, text: str) -> None:
    """Whole file or no file: readers never see half of it."""
    partial = path.with_name(path.name + ".partial")
    partial.write_text(text)
    partial.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_result(outputs: Path) -> Result:
    """Validate job-owned progress once; unreadable or partial reports mean no progress."""
    try:
        return Result.model_validate_json((outputs / RESULT).read_bytes(), strict=True)
    except (OSError, ValueError):
        return Result()
