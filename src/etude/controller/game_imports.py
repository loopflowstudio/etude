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

from etude.call import Function
from etude.controller.diagnostics import Issue, declaration
from etude.errors import EtudeError, LauncherError, SourceLocation, sanitize
from etude.eval.game import GAME_IMPORT_FIX, Imports, _match_type, measure_imports
from etude.eval.resources import Resources
from etude.records import Record


class GameImport(Record):
    file: Path
    location: SourceLocation
    checkout: str | None
    play: Function
    build: Function | None
    streaming: bool

    @property
    def modules(self) -> tuple[str | None, str, str, tuple[str, str] | None]:
        build = (self.build.module, self.build.path) if self.build is not None else None
        return self.checkout, self.play.module, self.play.path, build


class _Request(Record):
    game: GameImport
    paths: tuple[str, ...]


def check_game_imports(games: Iterable[GameImport]) -> Iterator[Issue]:
    seen = set()
    for game in games:
        if game.modules in seen:
            continue
        seen.add(game.modules)
        if (issue := _check_game_imports(game)) is not None:
            yield issue
            if issue.severity == "error":
                return


def _check_game_imports(game: GameImport) -> Issue | None:
    request = _Request(game=game, paths=tuple(sys.path))
    with tempfile.TemporaryDirectory(prefix="etude-check-") as scratch:
        result = Path(scratch) / "imports.json"
        process = subprocess.run(
            [sys.executable, "-m", "etude.controller.game_imports", str(result)],
            input=request.model_dump_json().encode(),
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
                file=game.location.file,
                line=game.location.line,
                message=f"could not import game module {game.play.module}: {detail}",
            )
        measured = TypeAdapter(Imports | Issue).validate_json(result.read_bytes())
    if isinstance(measured, Issue):
        return measured
    if (warning := measured.warning) is None:
        return None
    return Issue(
        severity="warning",
        file=measured.file or game.location.file,
        line=measured.line or game.location.line,
        message=sanitize(warning),
        fix=GAME_IMPORT_FIX,
    )


def _measure_game_imports(request: _Request, result: Path) -> None:
    game = request.game
    sys.path[:] = request.paths
    Resources(scratch=result.parent, artifacts=result.parent).configure(1)
    measured: Imports | Issue
    try:
        with declaration(game.file), measure_imports(game.play.module) as observe:
            play = game.play.load(game.checkout)
            _match_type(play, streaming=game.streaming)
            if game.build is not None:
                game.build.load(game.checkout)
            measured = observe()
    except (EtudeError, LauncherError) as error:
        measured = Issue.from_error(error, game.file)
    result.write_bytes(measured.model_dump_json().encode())


if __name__ == "__main__":
    _measure_game_imports(_Request.model_validate_json(sys.stdin.buffer.read()), Path(sys.argv[1]))
