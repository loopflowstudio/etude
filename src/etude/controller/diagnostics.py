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
    def from_error(cls, error: Exception | SystemExit, file: Path) -> Self:
        diagnosed = _declaration_error(error, file)
        location = diagnosed.location
        assert location is not None
        return cls(
            file=location.file,
            line=location.line,
            message=sanitize(str(diagnosed)),
            fix=sanitize(diagnosed.fix) if diagnosed.fix else None,
        )


def _declaration_error(
    error: Exception | SystemExit, path: Path, function: Callable[..., object] | None = None
) -> EtudeError | LauncherError:
    """Describe an authored failure while keeping fixes and explicit locations."""
    if isinstance(error, (LauncherError, EtudeError)):
        if error.location is None:
            error.location = _locate(error, path, function)
        return error
    if isinstance(error, ValidationError):
        message = validation_message(error)
    else:
        detail = error.msg if isinstance(error, SyntaxError) else str(error)
        message = f"{type(error).__name__}: {' '.join(detail.splitlines())}"
    # Etude's records are Pydantic models; only their constructors refuse positional
    # arguments with this text, so an author's own arity mistake gets no invented fix.
    fix = (
        "pass fields by name, such as TrainingJob(name='base', ...)"
        if isinstance(error, TypeError) and str(error).startswith("BaseModel.__init__()")
        else None
    )
    return LauncherError(message, fix=fix, at=_locate(error, path, function))


def _locate(
    error: BaseException, path: Path, function: Callable[..., object] | None = None
) -> SourceLocation:
    """Use the explicit location, the deepest authored frame, or the declaration's def."""
    chain = tuple(causes(error))
    for cause in chain:
        if isinstance(cause, (LauncherError, EtudeError)) and cause.location is not None:
            return cause.location
        if isinstance(cause, SyntaxError) and cause.filename and cause.lineno:
            return SourceLocation(Path(cause.filename), cause.lineno)
    base = path.resolve()
    directory = base if base.is_dir() else git_checkout(base.parent) or base.parent
    dependencies = (_PACKAGE, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    for cause in reversed(chain):
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


def causes(error: BaseException) -> Iterator[BaseException]:
    """Follow explicit causes once each, including when an author raises an error from itself."""
    seen: set[int] = set()
    cause: BaseException | None = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        yield cause
        cause = cause.__cause__


@contextmanager
def declaration(path: Path, function: Callable[..., object] | None = None) -> Iterator[None]:
    """Carry authored error locations through the shared runtime entry points."""
    try:
        yield
    except (Exception, SystemExit) as error:
        diagnosed = _declaration_error(error, path, function)
        if diagnosed is error:
            raise
        raise diagnosed from error
