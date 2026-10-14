"""Tracking commands keep startup independent of compute and unused dependencies."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_tracking_does_not_import_launcher() -> None:
    root = Path(__file__).resolve().parents[2] / "src/etude/track"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or "", *(f"{node.module}.{alias.name}" for alias in node.names)]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name == "etude.launch" or name.startswith("etude.launch.") for name in names
            ), path


@pytest.mark.parametrize(
    ("command", "count", "forbidden"),
    [
        (["reports", "--help"], 9, ("numpy", "pydantic")),
        (["reports", "write", "--help"], 9, ("numpy", "pydantic")),
        (["reports", "write", "--source", "{directory}"], 16, ("boto3", "etude.track.run")),
        (
            ["sync", "--directory", "{directory}"],
            15,
            ("boto3", "etude.track.report", "etude.track.serve"),
        ),
    ],
)
def test_tracking_command_import_counts(
    tmp_path: Path, command: list[str], count: int, forbidden: tuple[str, ...]
) -> None:
    directory = tmp_path / "files"
    directory.mkdir()
    args = [value.format(directory=directory) for value in command]
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys,json; from etude.cli import main; "
            f"result=main({args!r}); print(json.dumps(sorted(sys.modules))); sys.exit(result)",
        ],
        env={**os.environ, "ETUDE_HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == (
        1 if command[:2] == ["reports", "write"] and "--source" in command else 0
    ), done.stderr
    modules = json.loads(done.stdout.splitlines()[-1])
    loaded = [name for name in modules if name.startswith("etude")]
    # Pin help and execution separately: help builds no records, sync loads no report.
    assert len(loaded) == count, loaded
    assert not [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    # One `etude` command: the launcher contributes only its command table.
    assert {name for name in loaded if name.startswith("etude.launch")} <= {
        "etude.launch",
        "etude.launch.cli",
        "etude.launch.commands",
        "etude.launch.errors",
    }
    assert not (tmp_path / "home" / "launcher.db").exists()
