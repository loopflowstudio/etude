"""Registered training state, stored as immutable folders with a pointer written last.

SavedState is the portable manifest; State owns one namespace's live objects and
save cadence. Framework imports happen only when objects actually need them.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, JsonValue

from etude.carriage import save_name, save_number, valid_state_name, within


class SavedState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    at: dict[str, float]
    wall_ms: int
    seed: int
    commit: str | None
    extra: dict[str, JsonValue] | None
    files: dict[str, str]


class StatePointer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    save: str


class StateObject(Protocol):
    def state_dict(self) -> object: ...

    def load_state_dict(self, state: object) -> object: ...


class _Torch(Protocol):
    def save(self, state: object, path: Path) -> None: ...

    def load(self, path: Path, *, map_location: str, weights_only: bool) -> object: ...


def _torch() -> _Torch:
    # Optional dependency: callback saves and the core client need no framework.
    try:
        module = import_module("torch")
    except ImportError as error:
        raise ImportError("registered objects require etude-sdk[train]") from error
    # PyTorch's untyped serialization boundary stays here, outside Run's API.
    return cast(_Torch, module)


def _object(value: object) -> StateObject:
    if not callable(getattr(value, "state_dict", None)) or not callable(
        getattr(value, "load_state_dict", None)
    ):
        raise TypeError("registered state needs state_dict() and load_state_dict()")
    # The two callable methods are checked once at registration, before training.
    return cast(StateObject, value)


def state_folder(out: Path, name: str | None) -> Path:
    if name is not None and not valid_state_name(name):
        raise ValueError("state name must be one portable name, outside numbered saves")
    return out / "state" if name is None else out / "state" / name


@dataclass(frozen=True)
class RestoredState:
    path: Path
    record: SavedState


def saved_states(out: Path) -> list[SavedState]:
    """The manifest of every namespace's committed save under a run's out."""
    state = out / "state"
    folders = (state, *(path for path in state.glob("*/") if valid_state_name(path.name)))
    return [found.record for folder in folders if (found := read_state(folder)) is not None]


def read_state(folder: Path) -> RestoredState | None:
    pointer = folder / "current.json"
    if not pointer.is_file():
        return None
    selected = folder / save_name(pointer.read_bytes())
    saved = SavedState.model_validate_json((selected / "state.json").read_bytes())
    for file in saved.files.values():
        if not within(file) or not (selected / file).is_file():
            raise ValueError("saved state names a missing or nonportable file")
    return RestoredState(path=selected, record=saved)


class State:
    """One registered namespace: its live objects, extra counts and last save."""

    def __init__(
        self, folder: Path, objects: Mapping[str, object], extra: BaseModel | None
    ) -> None:
        for name in objects:
            if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", name) is None:
                raise ValueError("object name must be one portable name")
        self.folder = folder
        self.objects = {name: _object(value) for name, value in objects.items()}
        self.extra = extra
        self.export: Callable[[Path], None] | None = None
        self.saved_at = time.monotonic()
        self.saved_step = 0.0
        self.last_at: dict[str, float] | None = None
        if self.objects:
            _torch()  # refuse an unavailable serializer at registration

    def restore(self, *, seed: int) -> Path | None:
        found = read_state(self.folder)
        if found is None:
            return None
        path, saved = found.path, found.record
        self.last_at = saved.at
        if saved.seed != seed:
            raise ValueError(f"saved seed was {saved.seed}, is {seed}")
        for name, value in self.objects.items():
            if name not in saved.files:
                raise ValueError(f"saved state has no object {name!r}")
            value.load_state_dict(
                _torch().load(path / saved.files[name], map_location="cpu", weights_only=True)
            )
        if self.extra is not None:
            if saved.extra is None:
                raise ValueError("saved state has no extra counts")
            loaded = type(self.extra).model_validate(saved.extra)
            for name in type(self.extra).model_fields:
                setattr(self.extra, name, getattr(loaded, name))
        return path

    def write(self, *, at: dict[str, float], wall_ms: int, seed: int, commit: str | None) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        number = 1 + max(
            (save_number(path.name) or 0 for path in self.folder.glob("save-*")), default=0
        )
        target = self.folder / f"save-{number:08d}"
        with TemporaryDirectory(prefix=".save-", dir=self.folder) as temporary:
            staged = Path(temporary)
            files: dict[str, str] = {}
            if self.export is not None:
                self.export(staged)
            for name, value in self.objects.items():
                files[name] = name + ".pt"
                _torch().save(value.state_dict(), staged / files[name])
            saved = SavedState(
                at=at,
                wall_ms=wall_ms,
                seed=seed,
                commit=commit,
                extra=None if self.extra is None else self.extra.model_dump(mode="json"),
                files=files,
            )
            (staged / "state.json").write_text(saved.model_dump_json())
            for path in staged.rglob("*"):
                if path.is_symlink():
                    raise ValueError("a save must be self-contained, without symbolic links")
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            staged.rename(target)
        pointer = self.folder / "current.json.partial"
        with pointer.open("w") as stream:
            stream.write(StatePointer(save=target.name).model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        pointer.replace(self.folder / "current.json")
        self.saved_at = time.monotonic()
        self.last_at = at
