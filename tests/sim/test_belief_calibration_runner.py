"""Evidence-boundary checks for the frozen INT-17 calibration runner."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from experiments.runners.run_belief_calibration import (
    _load_inputs,
    _resolve_locked_file,
    load_contract,
    parse_args,
    preflight,
)
from manabot.sim.teacher1_evidence import ContractError

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "experiments/contracts/int-17-belief-calibration-v1.json"


def test_contract_locks_real_likelihood_selected_trace_and_negative_policy() -> None:
    contract = load_contract(CONTRACT)

    checkpoint = _resolve_locked_file(
        contract["likelihood_checkpoint"], "likelihood checkpoint"
    )

    assert checkpoint.name == "visit_policy_only-seed-197.pt"
    assert contract["likelihood_checkpoint"]["sha256"].startswith("1673a237")
    assert contract["algorithm"]["model_fallback"] == "forbidden"
    assert contract["algorithm"]["truth_access"] == (
        "post_update_authority_audit_only"
    )
    assert contract["cohort"] == {
        "games": 1,
        "game_seed": 0,
        "viewers": [0, 1],
        "commands": 132,
        "commitments": 62,
        "unadmitted_commands": 70,
        "points_per_viewer": 133,
        "total_points": 266,
    }
    assert contract["prediction"]["failure_policy"] == (
        "retain_refutation_without_checkpoint_substitution"
    )
    assert "arena_strength" in contract["exclusions"]
    assert "belief_head_training" in contract["exclusions"]


def test_preflight_reconstructs_the_rul11_identity_and_exact_row_cap() -> None:
    contract = load_contract(CONTRACT)
    trace, _ = _load_inputs(contract)

    result = preflight(contract, trace)

    assert result.to_dict() == contract["preflight"]
    assert result.world_rows == 903_063
    assert result.commitments == 62


def test_checkpoint_hash_mismatch_fails_before_inference(tmp_path: Path) -> None:
    contract = load_contract(CONTRACT)
    tampered = deepcopy(contract["likelihood_checkpoint"])
    checkpoint = tmp_path / "checkpoint.pt"
    checkpoint.write_bytes(b"not the retained checkpoint")
    tampered.update({"path": str(checkpoint), "sha256": "0" * 64})

    with pytest.raises(ContractError, match="SHA-256 mismatch"):
        _resolve_locked_file(tampered, "likelihood checkpoint")


def test_test_only_likelihood_cannot_be_registered(tmp_path: Path) -> None:
    payload = json.loads(CONTRACT.read_text())
    payload["algorithm"]["model_fallback"] = "test-only-likelihood/v1"
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ContractError, match="forbid likelihood fallback"):
        load_contract(path)


def test_runner_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--contract",
                str(CONTRACT),
                "--out-dir",
                "result",
                "--preflight-only",
                "--verify-only",
            ]
        )
