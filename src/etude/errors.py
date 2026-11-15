"""Run failures and sanitized diagnostics, with standard-library-only dependencies."""

import os
import re
import subprocess
import traceback
from pathlib import Path

__all__ = ("EtudeError", "LauncherError")


class EtudeError(Exception):
    """A refusal with a short code a caller can match on."""

    def __init__(self, code: str, status: int = 409, *, detail: str | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code if detail is None else f"{code}: {detail}")


class LauncherError(Exception):
    """The request cannot be carried out as asked."""


class RetryRequested(Exception):
    """Fail this evaluation and leave unfinished matches eligible for the next pass."""


def sanitize(text: str) -> str:
    for name, value in os.environ.items():
        if (
            value
            and len(value) >= 4
            and any(
                part in name.upper()
                for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "URL", "CREDENTIAL")
            )
        ):
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://[^\s<>\"']+", "[redacted URL]", text)
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    return re.sub(
        r"(?i)((?:authorization|password|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )


def explain(error: BaseException) -> str:
    """Never include subprocess argv or traceback locals in retained errors."""
    if isinstance(error, subprocess.TimeoutExpired):
        detail = f"timed out after {error.timeout} seconds"
        stderr = error.stderr
    elif isinstance(error, subprocess.CalledProcessError):
        detail = f"exit status {error.returncode}"
        stderr = error.stderr
    else:
        detail, stderr = str(error), None
    if stderr:
        detail += "\n" + (stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr)
    if not isinstance(error, (OSError, ValueError, subprocess.SubprocessError)):
        frames = traceback.extract_tb(error.__traceback__)[-3:]
        if frames:
            detail += "\n" + " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno} {frame.name}" for frame in frames
            )
    return sanitize(f"{type(error).__name__}: {detail}")
