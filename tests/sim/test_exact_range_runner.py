"""Pinned evidence-boundary checks for the INT-9 substrate contract."""

from pathlib import Path

import pytest

from experiments.runners.run_exact_range_player import (
    load_contract,
    resolve_artifact,
)
from manabot.sim.teacher1_evidence import ContractError

CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "experiments"
    / "contracts"
    / "int-9-exact-range-v1.json"
)


def test_contract_pins_world_arena_evidence_and_exclusions() -> None:
    contract, smoke = load_contract(CONTRACT, "smoke")
    _, arena = load_contract(CONTRACT, "arena")

    assert contract["world"] == "w2"
    assert contract["arena"]["id"] == "w2-interactive-belief-v1"
    assert contract["arena"]["required_primary_cell"] == "belief_vs_uniform"
    assert smoke["evidence_class"] == "engineering_smoke_non_admission"
    assert arena["evidence_class"] == "preregistered_arena"
    assert set(contract["required_evidence"]) == {
        "gameplay",
        "calibration",
        "integrity",
        "competencies",
        "systems",
    }
    assert "public_belief_solving" in contract["exclusions"]
    assert "broad_deck_generalization" in contract["exclusions"]


def test_unresolved_likelihood_artifact_fails_closed() -> None:
    contract, _ = load_contract(CONTRACT, "smoke")

    with pytest.raises(ContractError, match="not byte-locked"):
        resolve_artifact(contract, "likelihood_checkpoint")
