"""Inspect the import footprint of a game in a fresh interpreter.

The probe loads only the game and its build hook. It reports through a
temporary file, independent of any console output from imports.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import TypeAdapter

from etude.controller.diagnostics import Issue
from etude.controller.import_probe import ImportProbe
from etude.errors import sanitize
from etude.eval.game import GAME_IMPORT_FIX, Imports


def check_game_imports(games: Iterable[ImportProbe]) -> Iterator[Issue]:
    seen = set()
    for game in games:
        modules = game.modules
        if modules in seen:
            continue
        seen.add(modules)
        if (issue := _check_game_imports(game)) is not None:
            yield issue
            if issue.severity == "error":
                return


def _check_game_imports(probe: ImportProbe) -> Issue | None:
    with tempfile.TemporaryDirectory(prefix="etude-check-") as scratch:
        result = Path(scratch) / "imports.json"
        process = subprocess.run(
            [sys.executable, "-m", "etude.controller.import_probe", str(result)],
            input=probe.model_dump_json().encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if process.returncode or not result.is_file():
            detail = (
                f"process exited with {process.returncode}"
                if process.returncode
                else "process returned no result"
            )
            return Issue(
                file=probe.location.file,
                line=probe.location.line,
                message=f"could not import game module {probe.play.module}: {detail}",
            )
        measured = TypeAdapter(Imports | Issue).validate_json(result.read_bytes())
    if isinstance(measured, Issue):
        if measured.file == probe.checkout:
            return measured.model_copy(
                update={"file": probe.location.file, "line": probe.location.line}
            )
        return measured
    if (warning := measured.warning) is None:
        return None
    return Issue(
        severity="warning",
        file=measured.file or probe.location.file,
        line=measured.line or probe.location.line,
        message=sanitize(warning),
        fix=GAME_IMPORT_FIX,
    )
