"""Measure a game's imports in a fresh interpreter using the game process baseline."""

import sys
from pathlib import Path

from etude.call import Function
from etude.errors import SourceLocation
from etude.eval.game import measure_imports
from etude.eval.resources import Resources
from etude.records import Record


class ImportProbe(Record):
    checkout: Path
    play: Function
    build: Function | None
    location: SourceLocation

    @property
    def modules(self) -> tuple[Path, str, str, tuple[str, str] | None]:
        """Import cost depends on the modules, irrespective of which functions a job uses."""
        build = (self.build.module, self.build.path) if self.build is not None else None
        return self.checkout, self.play.module, self.play.path, build


def main() -> None:
    result = Path(sys.argv[1])
    game = ImportProbe.model_validate_json(sys.stdin.buffer.read())
    Resources(scratch=result.parent, artifacts=result.parent).configure(1)
    try:
        with measure_imports(game.play.module) as observe:
            game.play.load(game.checkout)
            if game.build is not None:
                game.build.load(game.checkout)
            payload = observe().model_dump_json()
    except (Exception, SystemExit) as error:
        # This entry point loads diagnostic formatting only after measurement.
        from etude.controller.diagnostics import Issue  # noqa: PLC0415

        payload = Issue.from_error(error, game.checkout).model_dump_json()
    result.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
