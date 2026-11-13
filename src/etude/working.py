"""Membership of a carried recipe directory, published after its files.

Storage can retain deleted checkpoint bytes without restoring them into out.
An empty membership is meaningful: the next attempt starts with no working files.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from etude.carriage import within

MEMBERSHIP = "out.json"


class WorkingFiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[str, ...]

    @model_validator(mode="after")
    def portable(self) -> Self:
        if any(not within(key) or not key.startswith("out/") for key in self.files):
            raise ValueError("working files must be within out")
        return self
