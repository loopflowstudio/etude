"""Immutable client archives and the standard-library boot subset."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

FILES = {
    "__init__.py",
    "launch/__init__.py",
    "launch/errors.py",
    "launch/measurement.py",
    "launch/providers/__init__.py",
    "launch/providers/runpod/__init__.py",
    "launch/providers/runpod/http.py",
    "launch/providers/runpod/pod.py",
    "launch/environment.py",
}


def program_archive(*, guardian: bool = False) -> bytes:
    """Stable ZIP bytes, including when an installed client forwards itself."""
    out = io.BytesIO()
    root = Path(__file__).resolve().parents[2]
    paths = (root / name for name in FILES) if guardian else root.rglob("*.py")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            _write(archive, "etude/" + path.relative_to(root).as_posix(), path.read_bytes())
        package = root / "_package"
        if not package.exists():
            package = root.parents[1]
        for name in () if guardian else ("pyproject.toml", "uv.lock"):
            body = (package / name).read_text()
            if name == "pyproject.toml":
                body = body.split("[tool.hatch.build.targets.wheel.force-include]")[0].rstrip()
                body = body.replace('packages = ["src/etude"]', 'packages = ["etude"]')
                # The archive carries code, not the project page the README makes.
                body = body.replace('readme = "README.md"\n', "")
                body += '\n[tool.hatch.build.targets.wheel.force-include]\n"pyproject.toml" = "etude/_package/pyproject.toml"\n"uv.lock" = "etude/_package/uv.lock"\n'
            _write(archive, name, body)
    return out.getvalue()


def _write(archive: zipfile.ZipFile, name: str, body: bytes | str) -> None:
    # ZipInfo's fixed timestamp keeps digests independent of file mtimes.
    entry = zipfile.ZipInfo(name)
    entry.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(entry, body)
