"""Reconstruct author player records in games and bind their checkpoint leaves.

Stored trees stay portable and readable without author imports. Only build_agents
loads wrapper modules; network model loading remains in the agent process.
"""

import importlib
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from pydantic import BaseModel, JsonValue, TypeAdapter

from etude.call import Function
from etude.eval.agents import Agent, AgentConnection, _RemoteAgent
from etude.eval.records import CheckpointPlayer, Composition, Player, player_checkpoints

_PLAYER: TypeAdapter[Player] = TypeAdapter(Player)
_FIELDS: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _decode(value: JsonValue) -> object:
    if isinstance(value, list):
        return [_decode(each) for each in value]
    if not isinstance(value, dict):
        return value
    if value.get("kind") == "mapping":
        items = _FIELDS.validate_python(value["items"])
        return {key: _decode(each) for key, each in items.items()}
    if value.get("kind") == "composition":
        return _restore(Composition.model_validate(value))
    if value.get("kind") in ("model", "code"):
        return _PLAYER.validate_python(value)
    return {key: _decode(each) for key, each in value.items()}


def _restore(value: object) -> object:
    if isinstance(value, Composition):
        if value.type == "etude:configured":
            return value
        module, name = value.type.split(":", 1)
        kind: object = importlib.import_module(module)
        for part in name.split("."):
            kind = getattr(kind, part)
        if not isinstance(kind, type) or not issubclass(kind, BaseModel):
            raise TypeError(f"player type {value.type} must be a Pydantic record")
        value = kind.model_validate(
            {key: _decode(each) for key, each in value.fields.items()}, by_name=True
        )
    if isinstance(value, BaseModel):
        # Seat validation lowers nested author records. Restore those after validation,
        # preserving the author's tuple/container types and field defaults.
        return value.model_copy(
            update={key: _restore(getattr(value, key)) for key in type(value).model_fields}
        )
    if isinstance(value, tuple):
        return tuple(_restore(each) for each in value)
    if isinstance(value, list):
        return [_restore(each) for each in value]
    if isinstance(value, dict):
        return {key: _restore(each) for key, each in value.items()}
    return value


def build_agents(
    players: Sequence[Player],
    connection: AgentConnection,
    tournament: str,
    match: str,
    build: Function | None,
    checkout: str | None,
) -> Sequence[Agent]:
    hook = (
        None
        if build is None
        else cast(Callable[[object, Mapping[str, Agent]], Agent], build.load(checkout))
    )
    agents: list[Agent] = []
    for player in players:
        leaves: dict[str, Agent] = {
            checkpoint.key: _RemoteAgent(connection, checkpoint, tournament, match, player.key)
            for checkpoint in player_checkpoints(player)
        }
        if isinstance(player, CheckpointPlayer) and not player.config:
            agents.append(leaves[player.checkpoint.key])
        elif hook is None:
            raise ValueError("a composed or code player needs Agents(build=)")
        else:
            agents.append(hook(_restore(player), leaves))
    return agents
