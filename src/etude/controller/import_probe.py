"""Measure a game's imports in a fresh interpreter using the game process baseline."""

import sys
from pathlib import Path

from etude.call import Function
from etude.eval.game import measure_imports
from etude.eval.resources import Resources
from etude.records import Record


class ImportProbe(Record):
    checkout: Path
    play: Function
    build: Function | None


def main() -> None:
    result = Path(sys.argv[1])
    game = ImportProbe.model_validate_json(sys.stdin.buffer.read())
    Resources(scratch=result.parent, artifacts=result.parent).configure(1)
    try:
        with measure_imports(game.play.module) as observe:
            game.play.load(game.checkout)
            if game.build is not None:
                game.build.load(game.checkout)
            measured = observe()
    except (Exception, SystemExit) as error:
        # This entry point loads diagnostic formatting only after measurement.
        from etude.controller.diagnostics import Issue, declaration_error  # noqa: PLC0415

        issue = Issue.from_error(declaration_error(error, game.checkout), game.checkout)
        result.write_text(issue.model_dump_json(), encoding="utf-8")
    else:
        result.write_text(measured.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
