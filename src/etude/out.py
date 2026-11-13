"""Out membership and file fingerprints, published after the files.

Storage can retain deleted checkpoint bytes without restoring them into out.
An empty membership is meaningful: the next attempt starts with empty out.
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from etude.carriage import within

MEMBERSHIP = "out.json"


class OutFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def portable(self) -> Self:
        if not within(self.key) or not self.key.startswith("out/"):
            raise ValueError("out file must be within out")
        return self


class OutFiles(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[OutFile, ...]
