"""The `etude` command: launcher and tracking commands, with reports under `etude reports`.

Each half registers its own commands (`etude.launch.cli`, `etude.train.cli`);
this module joins them, sends launcher commands to a chosen controller, and
turns expected failures into one line on stderr.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import typer

from etude.launch import cli as launch_cli
from etude.launch.commands import Kind, kind
from etude.launch.errors import LauncherError
from etude.runs.errors import EtudeError
from etude.train.cli import app as reports_app
from etude.train.cli import checkpoints, sync

app = launch_cli.app
app.add_typer(reports_app, name="reports")
# Each may reach a bucket, so it is a wait rather than a read.
app.command()(kind(Kind.wait)(checkpoints))
app.command()(kind(Kind.wait)(sync))

# Commands that run on this computer even when a controller is chosen: they read
# local files, or they choose the controller.
_LOCAL = ("launch", "queue", "source", "fetch", "worker", "logs")


def _main(argv: list[str]) -> int:
    try:
        if (
            argv
            and argv[0] not in ("worker", "reports", "checkpoints", "sync")
            and not {"--help", "-h"} & set(argv)
        ):
            # Entry points import only the command being run (AGENTS.md).
            from etude.launch import controller

            if argv == ["--controller-request"]:
                return controller.receive(launch_cli.default())
            local = argv[0] in _LOCAL or argv[:2] in (
                ["controller", "set"],
                ["controller", "unset"],
            )
            if not local and (address := controller.selected()):
                if argv[0] == "controller":
                    print(f"controller: {address}", flush=True)
                return controller.forward(address, argv)
        result = app(args=argv, prog_name="etude", standalone_mode=False)
        return result if isinstance(result, int) else 0
    except typer.Exit as error:
        return int(error.exit_code)
    except Exception as error:
        if isinstance(error, (LauncherError, EtudeError, OSError, ValueError, typer.BadParameter)):
            # A bad flag value names its flag: "Invalid value for '--hidden': ...".
            said = error.format_message() if isinstance(error, typer.BadParameter) else str(error)
            print(f"etude: {said}", file=sys.stderr)
            return 1
        # Typer's public invocation formats usage failures without importing its private parser.
        if hasattr(error, "show") and hasattr(error, "exit_code"):
            error.show()
            raise SystemExit(error.exit_code) from None
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """Profile a whole invocation with `etude --profile FILE COMMAND ...`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--profile":
        return _main(args)
    if len(args) < 3:
        print("etude: --profile needs a file and a command", file=sys.stderr)
        return 1

    import cProfile

    profile = cProfile.Profile()
    try:
        return int(profile.runcall(_main, args[2:]))
    finally:
        profile.dump_stats(args[1])
