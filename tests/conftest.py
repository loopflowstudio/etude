"""Every test keeps Etude's files, including its caches, in its own temporary home."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _etude_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH"))))
    )
    monkeypatch.setenv("ETUDE_HOME", str(tmp_path_factory.mktemp("etude-home")))


@pytest.fixture(scope="session")
def command_python(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A separate offline job environment for the command-driven training fixtures."""
    import subprocess  # noqa: PLC0415 - fixture setup loads only what these jobs need
    import sys  # noqa: PLC0415 - fixture setup
    import sysconfig  # noqa: PLC0415 - fixture setup

    directory = tmp_path_factory.mktemp("command-env") / "venv"
    subprocess.run(
        ["uv", "venv", "--offline", "--python", sys.executable, str(directory)],
        check=True,
        capture_output=True,
    )
    site = directory / "lib/python3.12/site-packages/sitecustomize.py"
    site.write_text(
        f"import site; site.addsitedir({sysconfig.get_paths()['purelib']!r}); site.addsitedir({str(Path(__file__).resolve().parents[1])!r})\n"
    )
    return str(directory / "bin/python")
