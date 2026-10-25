"""Call an author's function in a job's own environment, with the job's config.

    python -m etude.call --function train:train

This is the command Etude's typed jobs (`etude.jobs.training`,
`etude.jobs.evaluation`) run, so nobody writes one. The function takes one
Pydantic config. The job's config text, at `ETUDE_CONFIG`, is validated back
into that parameter's type before the call, and the function runs in the source
checkout (`ETUDE_SOURCE_DIR`), which is the working directory. `Function` names
it by import path. It imports only the standard library, Pydantic and Typer, so a job
starts by loading only its own code.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ConfigDict


class Function(BaseModel):
    """A function by import path, as a job finds it in its source checkout."""

    model_config = ConfigDict(frozen=True)

    module: str
    name: str  # its qualified name in the module
    # The folder below the checkout its module is imported from: "" for the
    # checkout itself, "src" for a src layout, a job file's folder for its neighbours.
    path: str = ""

    def __str__(self) -> str:
        return f"{self.module}:{self.name}"

    def load(self, checkout: Path | str | None = None) -> Callable[..., object]:
        """Import the module, from `path` below the checkout, and find the function.

        The checkout is the working directory unless given, as in a job.
        """
        if self.path or checkout is not None:
            folder = Path(checkout or ".") / self.path
            sys.path.insert(0, str(folder.resolve()))
        found: object = importlib.import_module(self.module)
        for part in self.name.split("."):
            found = getattr(found, part)
        if not callable(found):
            raise TypeError(f"{self} is not a function")
        return found


def config_type(function: Callable[..., object]) -> type[BaseModel]:
    """The Pydantic type of the function's one parameter, which its config is validated into."""
    parameters = list(inspect.signature(function).parameters.values())
    if len(parameters) != 1:
        raise TypeError(
            f"{function.__qualname__} must take exactly one parameter, its config; "
            f"it takes {len(parameters)}"
        )
    kind = typing.get_type_hints(function).get(parameters[0].name)
    if not (isinstance(kind, type) and issubclass(kind, BaseModel)):
        raise TypeError(
            f"{function.__qualname__}'s parameter {parameters[0].name!r} must be annotated "
            "with a Pydantic model, the type of its config"
        )
    return kind


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False, rich_markup_mode=None)


@app.command()
def main(
    function: Annotated[str, typer.Option(help="MODULE:NAME, imported from the source checkout")],
    path: Annotated[str, typer.Option(help="the folder below the checkout to import from")] = "",
) -> None:
    """Call FUNCTION with the job's config, validated into its parameter's type."""
    module, colon, name = function.partition(":")
    if not colon:
        raise typer.BadParameter(f"{function!r} is not MODULE:NAME", param_hint="--function")
    called = Function(module=module, name=name, path=path).load()
    given = os.environ.get("ETUDE_CONFIG")
    if not given:
        raise SystemExit("etude.call: ETUDE_CONFIG is not set; the job has no config")
    called(config_type(called).model_validate_json(Path(given).read_text()))


if __name__ == "__main__":
    app()
