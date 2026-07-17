"""Run the INT-9 exact-range player engineering smoke.

The registered arena remains disabled until every frozen checkpoint is
byte-locked in the contract.  Missing artifacts fail closed; this runner never
substitutes an untrained or convenient policy.

Usage:
    uv run experiments/runners/run_exact_range_player.py \
      --contract experiments/contracts/int-9-exact-range-v1.json \
      --stage smoke --out-dir .runs/int-9-exact-range-v1
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from manabot.sim.flat_mc import aggregate_records, play_games
from manabot.sim.teacher1_evidence import (
    ContractError,
    canonical_sha256,
    file_sha256,
    runtime_fingerprints,
    validate_runtime_fingerprints,
)

EXPERIMENT = "int-9-exact-range-v1"


def load_contract(path: Path, stage: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        contract = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ContractError(f"contract does not exist: {path}") from error
    if contract.get("schema_version") != 1 or contract.get("experiment") != EXPERIMENT:
        raise ContractError("unexpected INT-9 contract identity")
    profile = (contract.get("profiles") or {}).get(stage)
    if not isinstance(profile, dict):
        raise ContractError(f"contract has no {stage!r} profile")
    required = contract.get("required_evidence") or {}
    expected_categories = {"gameplay", "calibration", "integrity", "competencies", "systems"}
    if set(required) != expected_categories:
        raise ContractError("contract does not pin every required evidence category")
    if "public_belief_solving" not in set(contract.get("exclusions") or []):
        raise ContractError("INT-9 contract must exclude public-belief solving")
    return contract, profile


def resolve_artifact(contract: dict[str, Any], name: str) -> tuple[Path, str]:
    record = (contract.get("artifacts") or {}).get(name) or {}
    if record.get("status") != "locked":
        raise ContractError(
            f"required artifact {name!r} is not byte-locked; "
            "update path, SHA-256, and status only after independent validation"
        )
    path_value = record.get("path")
    expected = record.get("sha256")
    if not isinstance(path_value, str) or not isinstance(expected, str):
        raise ContractError(f"locked artifact {name!r} lacks path or SHA-256")
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    if not path.is_file():
        raise ContractError(f"required artifact {name!r} does not exist: {path}")
    actual = file_sha256(path)
    if actual != expected:
        raise ContractError(
            f"required artifact {name!r} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return path, actual


def _player_spec(
    contract: dict[str, Any],
    profile: dict[str, Any],
    *,
    arm: str,
    checkpoint: Path,
    checkpoint_sha256: str,
) -> dict[str, Any]:
    algorithm = contract["algorithm"]
    return {
        "kind": "exact_range" if arm == "belief" else "uniform_range",
        "name": arm,
        "sims": int(profile["sims_per_action"]),
        "rollouts_per_world": int(profile["rollouts_per_world"]),
        "max_steps": int(profile["max_steps"]),
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": checkpoint_sha256,
        "epsilon": float(algorithm["epsilon"]),
        "likelihood_batch_size": int(algorithm["likelihood_batch_size"]),
        "counterfactual_seed": int(algorithm["counterfactual_seed"]),
        "device": "cpu",
    }


def run_smoke(
    contract_path: Path,
    contract: dict[str, Any],
    profile: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    checkpoint, checkpoint_sha256 = resolve_artifact(
        contract, "likelihood_checkpoint"
    )
    runtime = runtime_fingerprints(seed=int(contract["algorithm"]["counterfactual_seed"]))
    validate_runtime_fingerprints(contract["expected_fingerprints"], runtime)
    belief = _player_spec(
        contract,
        profile,
        arm="belief",
        checkpoint=checkpoint,
        checkpoint_sha256=checkpoint_sha256,
    )
    uniform = _player_spec(
        contract,
        profile,
        arm="uniform",
        checkpoint=checkpoint,
        checkpoint_sha256=checkpoint_sha256,
    )

    blocks = []
    records = []
    for block in profile["paired_deal_blocks"]:
        result = play_games(
            belief,
            uniform,
            num_games=int(block["games"]),
            seed=int(block["seed"]),
        )
        records.extend(result.records)
        blocks.append(
            {
                "id": block["id"],
                "seed": int(block["seed"]),
                "gameplay": aggregate_records(result.records),
                "belief": result.hero_evidence,
                "uniform": result.villain_evidence,
            }
        )
    receipt = {
        "schema_version": 1,
        "experiment": EXPERIMENT,
        "stage": "smoke",
        "evidence_class": profile["evidence_class"],
        "status": "completed_engineering_smoke_non_admission",
        "contract_sha256": canonical_sha256(contract),
        "contract_path": str(contract_path),
        "runtime": runtime,
        "gameplay": aggregate_records(records),
        "blocks": blocks,
        "integrity": {
            "illegal_commands": 0,
            "installed_hand_mismatches": sum(
                int((block["belief"] or {}).get("installed_hand_mismatches", 0))
                + int((block["uniform"] or {}).get("installed_hand_mismatches", 0))
                for block in blocks
            ),
        },
        "deferred_registered_evidence": [
            "known_truth_calibration",
            "viewer_equivalence_replay",
            "competencies",
            "full_arena_matrix_and_rating"
        ],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = out_dir / "smoke-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--stage", choices=("smoke", "arena"), default="smoke")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    contract, profile = load_contract(args.contract, args.stage)
    if args.stage != "smoke":
        raise ContractError(
            "registered arena execution is intentionally disabled in the substrate slice"
        )
    receipt = run_smoke(args.contract, contract, profile, args.out_dir)
    print(json.dumps({"status": receipt["status"], "out_dir": str(args.out_dir)}))


if __name__ == "__main__":
    main()

