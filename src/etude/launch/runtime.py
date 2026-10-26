"""Inspect and select a job's preinstalled Python without modifying dependencies.

The worker executes the stdlib probe with the requested interpreter, validates its
response once, then retains RuntimeInfo and exports ETUDE_PYTHON to the job.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from etude.launch.records import Record, Runtime

PROBE = """import importlib.metadata,json,sys
names=json.loads(sys.argv[1])
versions={name:importlib.metadata.version(name) for name in names}
cuda=None
if sys.argv[2]=='cuda':
    import torch
    cuda=torch.version.cuda
print(json.dumps(dict(python=sys.executable,version=sys.version.split()[0],prefix=sys.prefix,
                     base_prefix=sys.base_prefix,packages=versions,cuda=cuda)))
"""


class RuntimeInfo(Record):
    python: str
    version: str
    prefix: str
    base_prefix: str
    packages: dict[str, str]
    cuda: str | None


def find_python(runtime: Runtime, environment: dict[str, str]) -> str:
    """The interpreter's absolute path; a relative path with `/` is inside ETUDE_PREPARED_DIR."""
    python = runtime.python
    prepared = environment.get("ETUDE_PREPARED_DIR")
    if prepared and "/" in python and not Path(python).is_absolute():
        python = str(Path(prepared) / python)
    executable = shutil.which(python, path=environment.get("PATH", os.defpath))
    if executable is None:
        raise ValueError(f"job interpreter {runtime.python!r} is unavailable")
    return str(Path(executable).absolute())


def select_runtime(runtime: Runtime, body: bytes, environment: dict[str, str]) -> RuntimeInfo:
    """Check exact package/CUDA versions and select the probed interpreter for the job."""
    try:
        info = RuntimeInfo.model_validate_json(body)
    except ValueError:
        raise ValueError("job interpreter returned invalid runtime metadata") from None
    if Path(info.prefix) == Path(sys.prefix):
        raise ValueError("job runtime must be separate from Etude's client environment")
    if runtime.python_version and not (
        info.version == runtime.python_version
        or info.version.startswith(runtime.python_version + ".")
    ):
        raise ValueError(
            f"Python mismatch: expected {runtime.python_version}, found {info.version}"
        )
    for name, version in runtime.packages.items():
        if info.packages.get(name) != version:
            raise ValueError(
                f"{name} mismatch: expected {version}, found {info.packages.get(name)}"
            )
    if runtime.cuda is not None and info.cuda != runtime.cuda:
        raise ValueError(f"CUDA build mismatch: expected {runtime.cuda}, found {info.cuda}")
    environment["ETUDE_PYTHON"] = info.python
    environment["PATH"] = (
        str(Path(info.python).parent) + os.pathsep + environment.get("PATH", os.defpath)
    )
    if info.prefix != info.base_prefix:
        environment["VIRTUAL_ENV"] = info.prefix
    return info
