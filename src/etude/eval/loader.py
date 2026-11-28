"""Checkpoint loading declarations use callables in Python and import paths in queued jobs."""

from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator

from etude.call import Function
from etude.eval.resources import Device
from etude.records import Record

_DEFAULT_DEVICES: tuple[Device, ...] = ("cuda", "metal")


def _normalize_devices(devices: tuple[Device, ...]) -> tuple[Device, ...]:
    # Checked here rather than with `min_length`, so a misspelled device reports
    # one error instead of also calling the tuple empty.
    if not devices:
        raise ValueError("devices must name at least one device")
    # Direct play is always the fallback, so only accelerators affect grouping.
    return tuple(sorted(set(devices) - {"cpu"})) or ("cpu",)


type _Devices = Annotated[tuple[Device, ...], AfterValidator(_normalize_devices)]


class LoaderDeclaration[F](Record):
    load: F | None = None
    build: F | None = None
    devices: _Devices = _DEFAULT_DEVICES
    max_batch: int = Field(default=32, ge=1)
    memory_gb: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class AgentLoader(LoaderDeclaration[Function]):
    """Portable loading declaration. Batching never enters scientific identity."""

    # Build-only records cannot declare devices and must round-trip through JSON.
    devices: _Devices = Field(
        default=_DEFAULT_DEVICES, exclude_if=lambda devices: devices == _DEFAULT_DEVICES
    )

    @model_validator(mode="after")
    def _validate_loading(self) -> Self:
        if self.load is None and "devices" in self.model_fields_set:
            raise ValueError("devices requires a checkpoint loader")
        return self
