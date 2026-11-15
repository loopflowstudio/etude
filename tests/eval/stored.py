"""Released inference-shaped records used to check stored-result compatibility."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from etude.eval.records import Composition, Relative, Seat, Version, as_config, version_of

_SEAT: TypeAdapter[Seat] = TypeAdapter(Seat)


def stored_inference(
    *, source: Sequence[Path | str] = (), config: Mapping[str, object] | BaseModel | None = None
) -> Composition:
    seat = _SEAT.validate_python(
        {
            "config": as_config(config),
            "version": (version_of(source) if source else Version()).model_dump(mode="json"),
        }
    )
    assert isinstance(seat, Composition)
    return seat


def stored_relative(seat: Relative, inference: Composition) -> Composition:
    result = _SEAT.validate_python(
        {**seat.model_dump(mode="json"), "inference": {"config": inference.config}}
    )
    assert isinstance(result, Composition)
    return result
