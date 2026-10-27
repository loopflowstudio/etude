"""Players keep their identities across the rename from agents, and old records still load."""

from __future__ import annotations

from hashlib import sha256

import pytest
from pydantic import ValidationError

from etude.eval.records import (
    Benchmark,
    CheckpointKey,
    CheckpointRef,
    Code,
    Environment,
    Inference,
    Match,
    Matchup,
    Model,
    RoundRobin,
    TournamentRun,
)
from etude.run.records import ArtifactRef

DIGEST = "0" * 64
WORLD = Environment(name="world", files="1" * 64)
U500 = CheckpointRef(
    key=CheckpointKey(run="ppo-1", name="updates-500.pt"),
    artifact=ArtifactRef(uri="s3://b/updates-500.pt", sha256=DIGEST),
)
SEARCH = Inference(config={"sims": 64})
HEURISTIC = Code(name="heuristic")

# Written by etude-sdk at 2eb32d5, before players were named.
OLD_MODEL = (
    '{"key":{"checkpoint":{"run":"ppo-1","name":"updates-500.pt","address":null},"code":null,'
    '"recorded":null,"config":{"sims":64}},"name":"updates-500.pt","artifact":{"uri":'
    '"s3://b/updates-500.pt","sha256":"' + DIGEST + '","size_bytes":null,"version":null,'
    '"state":"unverified","media_type":"application/octet-stream"},"version":{"commit":null,'
    '"source":null}}'
)
OLD_CODE = (
    '{"key":{"checkpoint":null,"code":"heuristic","recorded":null,"config":{}},'
    '"name":"heuristic","artifact":null,"version":{"commit":null,"source":null}}'
)
OLD_MATCH = (
    '{"agents":[' + OLD_MODEL + "," + OLD_CODE + '],"groups":{"all":[0,1]},'
    '"conditions":{"deal":1},"games":8}'
)
OLD_BENCHMARK = (
    '{"tournament":{"name":"ladder","environment":{"name":"world","files":"' + "1" * 64 + '",'
    '"config":{}},"matchups":[{"name":"direct vs heuristic","players":[{"config":{"sims":64},'
    '"version":{"commit":null,"source":null}},' + OLD_CODE + '],"conditions":[{"deal":1}],'
    '"games":8,"tags":[]}]},"checkpoints":[],"entrants":[],"share":[0,1]}'
)
OLD_ROUND_ROBIN = (
    '{"tournament":{"name":"rr","environment":{"name":"world","files":"' + "1" * 64 + '",'
    '"config":{}},"conditions":[{"deal":1}],"games":2},"checkpoints":[],"entrants":['
    + OLD_MODEL
    + ","
    + OLD_CODE
    + '],"share":[0,1]}'
)


def test_renaming_changes_no_match_key_or_matchup_definition() -> None:
    match = Match(players=(SEARCH.model(U500), HEURISTIC), conditions={"deal": 1}, games=8)
    # Pinned at 2eb32d5: a changed key would replay every stored match.
    assert match.key == "a64354de99bed9ed3bfc72755e7af6774877e28a383306be8e512561e6c793c8"
    matchup = Matchup(
        name="direct vs heuristic", players=(SEARCH, HEURISTIC), conditions=({"deal": 1},), games=8
    )
    assert matchup.definition == "9185c1177e55b1aa269d7d099aaa4a02d2a013dc780343081b531dba66c1ea91"


def test_a_match_stored_with_agents_loads_as_the_same_players() -> None:
    old = Match.model_validate_json(OLD_MATCH)
    now = Match(players=(SEARCH.model(U500), HEURISTIC), conditions={"deal": 1}, games=8)
    assert old == now
    assert Match.model_validate_json(now.model_dump_json()) == now
    assert "agents" not in now.model_dump_json()


def test_tournament_files_stored_with_agents_load_as_players() -> None:
    benchmark = TournamentRun.model_validate_json(OLD_BENCHMARK)
    matchup = Matchup(
        name="direct vs heuristic", players=(SEARCH, HEURISTIC), conditions=({"deal": 1},), games=8
    )
    assert benchmark.tournament == Benchmark(name="ladder", environment=WORLD, matchups=(matchup,))
    held = TournamentRun.model_validate_json(OLD_ROUND_ROBIN)
    assert isinstance(held.tournament, RoundRobin)
    assert held.entrants == (SEARCH.model(U500), HEURISTIC)


def test_etude_sdk_0_2_players_load_and_equal_no_player_since() -> None:
    sha = sha256(b"weights").hexdigest()
    old = Match.model_validate(
        {
            "agents": [
                {"name": "updates-100.pt", "kind": "checkpoint", "artifact": sha,
                 "uri": "s3://models/updates-100.pt", "config": {"search": 0}},
                {"name": "heuristic", "kind": "code", "artifact": None, "uri": None,
                 "code": DIGEST, "config": {}},
            ]
        }
    )  # fmt: skip
    model, code = old.players
    placeholder = Matchup.model_validate(
        {"name": "vs heuristic", "players": [{"config": {"search": 0}, "code": DIGEST},
                                             {"name": "heuristic", "kind": "code", "code": DIGEST}],
         "conditions": [{"deal": 1}], "games": 2}
    )  # fmt: skip
    assert placeholder.players[0] == Inference.model_validate(
        {"config": {"search": 0}, "version": {"source": DIGEST}}
    )
    assert isinstance(placeholder.players[1], Code)
    assert isinstance(model, Model) and isinstance(code, Code)
    assert model.inference.config == {"search": 0}
    assert model.checkpoint.artifact.uri == "s3://models/updates-100.pt"
    now = Inference(config={"search": 0}).model(
        CheckpointRef(
            key=CheckpointKey(run="ppo-1", name="updates-100.pt"),
            artifact=ArtifactRef(uri="s3://models/updates-100.pt", sha256=sha),
        )
    )
    assert model.key != now.key and code.key != HEURISTIC.key


def test_a_model_needs_its_checkpoint_digest() -> None:
    with pytest.raises(ValidationError, match="model_checkpoint_requires_digest"):
        CheckpointRef(key=U500.key, artifact=ArtifactRef(uri="s3://b/updates-500.pt"))
