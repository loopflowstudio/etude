"""Immutable client archives and the standard-library boot subset."""

from __future__ import annotations

import io
import zipfile
from itertools import chain
from pathlib import Path

from etude.launch.provenance import frontier

FILES = {
    "__init__.py",
    "errors.py",
    "launch/__init__.py",
    "launch/errors.py",
    "launch/provisioner/__init__.py",
    "launch/provisioner/providers/__init__.py",
    "launch/provisioner/providers/bootstrap.py",
    "launch/provisioner/providers/http.py",
    "launch/provisioner/providers/lium/__init__.py",
    "launch/provisioner/providers/lium/pod.py",
    "launch/provisioner/providers/vast/__init__.py",
    "launch/provisioner/providers/vast/http.py",
    "launch/provisioner/providers/vast/instance.py",
    "launch/provisioner/providers/runpod/__init__.py",
    "launch/provisioner/providers/runpod/http.py",
    "launch/provisioner/providers/runpod/pod.py",
    "launch/environment.py",
    "launch/logging.py",
    "deadline.py",
    "storage_timeout.py",
    "launch/storage/__init__.py",
    "launch/storage/transport.py",
}


def program_archive(*, guardian: bool = False) -> bytes:
    """Stable ZIP bytes, including when an installed client forwards itself."""
    out = io.BytesIO()
    root = Path(__file__).resolve().parents[3]
    migrations = root / "launch" / "migrations"
    paths = (
        (root / name for name in FILES)
        if guardian
        else chain(root.rglob("*.py"), migrations.glob("*.sql"), (migrations / "released.json",))
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            _write(archive, "etude/" + path.relative_to(root).as_posix(), path.read_bytes())
        if not guardian:
            _write(archive, "etude/launch/migrations/merged.json", frontier(root))
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
