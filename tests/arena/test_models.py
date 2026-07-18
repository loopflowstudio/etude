import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from manabot.arena.models import ArenaContract, PlayerRegistration
from manabot.arena.players import build_player

ROOT = Path(__file__).resolve().parents[2]


def test_checked_contract_has_exact_code_only_base() -> None:
    contract = ArenaContract.model_validate(
        json.loads(
            (ROOT / "experiments/contracts/int-6-skill-arena-v1.json").read_text()
        )
    )
    assert [player.player_id for player in contract.anchors] == [
        "random-v1",
        "scripted-greedy-v1",
        "flat-mc-4-v1",
        "flat-mc-16-v1",
        "flat-mc-64-v1",
    ]
    assert all(player.runner_kind == "code" for player in contract.anchors)
    assert len(contract.schedules["production"].deal_seeds) == 24
    assert contract.promotion.elo_margin == 25.0


def test_dpuct_registration_requires_every_effective_parameter() -> None:
    candidate = json.loads(
        (ROOT / "experiments/candidates/int-6-dpuct-32-w4-v1.json").read_text()
    )
    candidate["player_spec"].pop("c_puct")
    with pytest.raises(ValidationError, match="fully explicit"):
        PlayerRegistration.model_validate(candidate)


def test_checkpoint_candidate_missing_bytes_fails_closed() -> None:
    registration = PlayerRegistration(
        player_id="fixture-checkpoint-v1",
        display_name="fixture",
        role="challenger",
        runner_kind="checkpoint",
        player_spec={"kind": "checkpoint", "deterministic": True},
        compute_class_id="policy-cpu-batch1-v1",
        information_boundary="acting-viewer-history-only-v1",
        world="w2",
        observation_abi_sha256="a" * 64,
        checkpoint_sha256="b" * 64,
        checkpoint_bytes=1,
        parameter_count=1,
        artifact_id="fixture-only",
        player_seed_derivation_id="arena-pair-deal-player-v1",
    )
    with pytest.raises(FileNotFoundError, match="unavailable"):
        build_player(registration, seed=1)
