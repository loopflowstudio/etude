"""Describe the environment shared by an evaluation panel. Execution belongs to EvalRun."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel

from etude.eval.records import (
    Environment,
    as_config,
)
from etude.eval.source import digest_files

__all__ = ("environment",)


KIND = "evaluation"


def environment(
    name: str,
    *,
    files: Sequence[Path | str] = (),
    config: Mapping[str, object] | BaseModel | None = None,
) -> Environment:
    """What matches are played in: its defining files, hashed like code, and its config."""
    return Environment(name=name, files=digest_files(files), config=as_config(config))
