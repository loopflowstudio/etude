"""Embed the main frontier at build time; installed upgrades need no Git."""

from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    # Hatch supplies heterogeneous build metadata at its untyped plugin boundary.
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root) / "src" / "etude"
        # Build hooks run before the client dependencies are installed.
        provenance = run_path(str(root / "launch" / "provenance.py"))
        self._temporary = TemporaryDirectory(prefix="etude-frontier-")
        path = Path(self._temporary.name) / "merged.json"
        path.write_bytes(provenance["frontier"](root))
        destination = (
            "src/" if self.target_name == "sdist" else ""
        ) + "etude/launch/migrations/merged.json"
        build_data["force_include"][str(path)] = destination

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        self._temporary.cleanup()
