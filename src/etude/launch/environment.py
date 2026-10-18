"""Install the transferred private package in a digest-specific environment.

Only the public dependencies are downloaded. This module and the guardian use
only the standard library, so neither depends on a successful installation.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from etude.launch.logging import explain, write_log


def install(code: bytes) -> Path:
    base = Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude") / "programs"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = base / hashlib.sha256(code).hexdigest()
    root.mkdir(exist_ok=True, mode=0o700)
    with (root / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (root / "ready").exists():
            uv = shutil.which("uv")
            if uv is None:
                raise RuntimeError("uv is required on the machine; put uv on its login shell PATH")
            with zipfile.ZipFile(io.BytesIO(code)) as archive:
                archive.extractall(root)
            write_log("client install", "starting uv sync")
            try:
                subprocess.run(
                    [
                        uv,
                        "sync",
                        "--frozen",
                        "--no-dev",
                        "--no-editable",
                        "--python",
                        sys.executable,
                    ],
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
                    },
                    timeout=120,
                    check=True,
                )
            except subprocess.SubprocessError as error:
                write_log("client install", explain(error))
                raise
            write_log("client install", "completed")
            (root / "ready").touch()
    return root
