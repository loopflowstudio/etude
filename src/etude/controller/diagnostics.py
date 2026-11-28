"""Locate failed declarations in authored code and format model validation errors."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self

from pydantic import ValidationError
from typer._click.exceptions import UsageError

from etude.errors import EtudeError, LauncherError, SourceLocation, function_location, sanitize
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


def validation_message(error: ValidationError) -> str:
    problem = error.errors(include_input=False, include_context=False, include_url=False)[0]
    field = ".".join(map(str, (error.title, *problem["loc"])))
    return f"{field}: {problem['msg']}"


def locate(
    error: BaseException, path: Path, function: Callable[..., object] | None = None
) -> SourceLocation:
    """Use the explicit location, the deepest authored frame, or the declaration's def."""
    directory = path.resolve().parent
    for cause in _causes(error):
        if isinstance(cause, (LauncherError, EtudeError)) and cause.location is not None:
            return cause.location
        if isinstance(cause, SyntaxError) and cause.filename and cause.lineno:
            return SourceLocation(Path(cause.filename), cause.lineno)
        for frame in reversed(traceback.extract_tb(cause.__traceback__)):
            if frame.lineno is not None and not frame.filename.startswith("<"):
                file = Path(frame.filename).resolve()
                if file.is_relative_to(directory) and not file.is_relative_to(_PACKAGE):
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
    except UsageError:
        raise
    except (LauncherError, EtudeError) as error:
        if error.location is None:
            error.location = locate(error, path, function)
        raise
    except (Exception, SystemExit) as error:
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
        raise LauncherError(message, fix=fix, at=locate(error, path, function)) from error
