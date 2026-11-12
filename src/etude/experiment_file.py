"""Which Python files are experiment files, recognised by their text without running them.

`etude launch`, `etude report` and a folder's index agree on it: a file
defining `eval(state)` or `report()` at its top level is the experiment named
for its stem. Standard library only, so neither the launcher nor a report
loads anything more for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

# What makes a Python file an experiment's: a top-level eval(state) or report().
EXPERIMENT_FUNCTIONS = ("eval", "report")


def experiment_named(path: Path) -> str | None:
    """The experiment a file is, its stem, when it defines one of `EXPERIMENT_FUNCTIONS`.

    Recognised by its text, without running it; a file that does not parse is no experiment.
    """
    return path.stem if defined_functions(path) & set(EXPERIMENT_FUNCTIONS) else None


def defined_functions(path: Path) -> frozenset[str]:
    """The functions a file defines at its top level, by its text; none if it does not parse."""
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except SyntaxError:
        return frozenset()
    return frozenset(
        each.name for each in tree.body if isinstance(each, ast.FunctionDef | ast.AsyncFunctionDef)
    )
