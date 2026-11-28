"""Locate failed declarations in authored code and format model validation errors."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

from pydantic import ValidationError

from etude.call import validation_message
from etude.errors import EtudeError, LauncherError, SourceLocation, function_location, sanitize
from etude.experiment_file import git_checkout
from etude.records import Record


class Issue(Record):
    severity: Literal["error", "warning"] = "error"
    file: Path
    line: int
    message: str
    fix: str | None = None

    @classmethod
    def from_error(cls, error: EtudeError | LauncherError, file: Path) -> Self:
        location = locate(error, file)
        return cls(
            file=location.file,
            line=location.line,
            message=sanitize(str(error)),
            fix=sanitize(error.fix) if error.fix else None,
        )


def declaration_error(
    error: Exception | SystemExit, path: Path, function: Callable[..., object] | None = None
) -> EtudeError | LauncherError:
    """Describe an authored failure while keeping fixes and explicit locations."""
    location = locate(error, path, function)
    if isinstance(error, (LauncherError, EtudeError)):
        error.location = location
        return error
    if isinstance(error, ValidationError):
        message = validation_message(error)
    else:
        detail = error.msg if isinstance(error, SyntaxError) else str(error)
        message = f"{type(error).__name__}: {' '.join(detail.splitlines())}"
    fix = (
        "use named fields, such as RunRequest(job=job, seed=1)"
        if isinstance(error, TypeError) and "positional argument" in str(error)
        else None
    )
    return LauncherError(message, fix=fix, at=location)


def locate(
    error: BaseException, path: Path, function: Callable[..., object] | None = None
) -> SourceLocation:
    """Use the explicit location, the deepest authored frame, or the declaration's def."""
    causes = tuple(_causes(error))
    for cause in causes:
        if isinstance(cause, (LauncherError, EtudeError)) and cause.location is not None:
            return cause.location
        if isinstance(cause, SyntaxError) and cause.filename and cause.lineno:
            return SourceLocation(Path(cause.filename), cause.lineno)
    base = path.resolve()
    directory = base if base.is_dir() else git_checkout(base.parent) or base.parent
    dependencies = (_PACKAGE, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    for cause in reversed(causes):
        for frame in reversed(traceback.extract_tb(cause.__traceback__)):
            if frame.lineno is not None and not frame.filename.startswith("<"):
                file = Path(frame.filename).resolve()
                if not file.is_relative_to(directory):
                    continue
                # A project can keep its interpreter and installed packages in its checkout.
                if any(file.is_relative_to(root) for root in dependencies):
                    continue
                parts = file.relative_to(directory).parts
                if all(not part.startswith(".") and part != "site-packages" for part in parts):
                    return SourceLocation(file, frame.lineno)
    return (function_location(function) if function is not None else None) or SourceLocation(
        path, 1
    )


_PACKAGE = Path(__file__).resolve().parents[1]


def _causes(error: BaseException) -> Iterator[BaseException]:
    while True:
        yield error
        if error.__cause__ is None:
            return
        error = error.__cause__


@contextmanager
def declaration(path: Path, function: Callable[..., object] | None = None) -> Iterator[None]:
    """Carry authored error locations through the shared runtime entry points."""
    try:
        yield
    except (Exception, SystemExit) as error:
        diagnosed = declaration_error(error, path, function)
        if diagnosed is error:
            raise
        raise diagnosed from error
