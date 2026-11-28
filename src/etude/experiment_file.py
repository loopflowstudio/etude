"""Which Python files are experiment files, recognised by their text without running them.

`etude launch`, `etude report` and a folder's index agree on it: a file
defining `training_jobs()`, `eval_jobs(state)` or `report()` at its top level is the experiment named
for its stem. Standard library only, so neither the launcher nor a report
loads anything more for it.
"""

from __future__ import annotations

import ast
import runpy
import sys
from pathlib import Path

from etude.errors import LauncherError, SourceLocation

# What makes a Python file an experiment's: a top-level training_jobs(), eval_jobs(state) or report().
EXPERIMENT_FUNCTIONS = {"training_jobs", "eval_jobs", "report"}


def experiment_named(path: Path) -> str | None:
    """The experiment a file is, its stem, when it defines one of `EXPERIMENT_FUNCTIONS`.

    Recognised by its text, without running it; a file that does not parse is no experiment.
    """
    try:
        return path.stem if _functions(path).keys() & EXPERIMENT_FUNCTIONS else None
    except SyntaxError:
        return None


def git_checkout(directory: Path) -> Path | None:
    """Find the enclosing checkout, including worktrees, without running Git."""
    directory = directory.resolve()
    return next(
        (folder for folder in (directory, *directory.parents) if (folder / ".git").exists()),
        None,
    )


def _functions(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    return {
        each.name: each.lineno
        for each in tree.body
        if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef)
    }


RENAMED = {"runs": "training_jobs", "training_runs": "training_jobs", "eval": "eval_jobs"}


def execute_file(path: Path) -> dict[str, object]:
    """Check for retired declarations, then execute the file with its siblings importable."""
    defined = _functions(path)
    for old, new in RENAMED.items():
        if old in defined:
            parameters = "state" if new == "eval_jobs" else ""
            raise LauncherError(
                f"rename {old}() to {new}()",
                fix=f"def {new}({parameters}):",
                at=SourceLocation(path, defined[old]),
            )

    path = path.resolve()
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return runpy.run_path(str(path))
