"""Out membership and file fingerprints, published after the files.

Storage can retain deleted checkpoint bytes without restoring them into out.
An empty membership is meaningful: the next attempt starts with empty out.
"""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from etude.carriage import recovery_files, save_pointer, state_pointer, within
from etude.storage_timeout import StorageUnavailable

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


@dataclass(frozen=True)
class Recovery:
    """What storage holds of a run's folder that a restoration takes, chosen before file reads."""

    keys: list[str]
    pointers: dict[str, bytes]
    membership: OutFiles | None


def recovery(listed: Collection[str], read: Callable[[str], bytes | None]) -> Recovery:
    """Select a run's restorable keys from its listing, reading membership and state pointers.

    Membership limits out to its files; pointers then add the saves they name,
    which an interrupted delivery may have stored after an older membership.
    `read` returns None for an absent object. A listed membership or pointer
    that cannot be read, or a member storage no longer lists, raises
    `StorageUnavailable`.
    """
    membership = None
    members = None
    if MEMBERSHIP in listed:
        body = read(MEMBERSHIP)
        if body is None:
            raise StorageUnavailable("out membership is missing")
        membership = OutFiles.model_validate_json(body)
        members = {file.key for file in membership.files}
        if any(name not in listed for name in members if save_pointer(name) is None):
            raise StorageUnavailable("out member is missing")
    pointers: dict[str, bytes] = {}
    for key in listed:
        if state_pointer(key):
            body = read(key)
            if body is None:
                raise StorageUnavailable("state pointer is missing")
            pointers[key] = body
    return Recovery(recovery_files(listed, members, pointers), pointers, membership)
