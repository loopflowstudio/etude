"""Importable author records for composable-player fixtures."""

from typing import Literal

from pydantic import JsonValue

from etude.eval.records import Seat
from etude.records import Record


class Ensemble(Record):
    members: tuple[Seat, ...]
    options: dict[str, JsonValue] = {}


class Tagged(Record):
    kind: Literal["model"] = "model"
    over: Seat
