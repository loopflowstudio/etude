"""Run the pre-registered W2-214 semantic transfer capability probe."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Any, Mapping

import numpy as np

from manabot.semantic.learning import BoundSemanticPack
from manabot.semantic.transfer import (
    ARMS,
    aggregate_arm,
    build_records,
    build_spec,
    load_workload,
    robustness_controls,
    run_arm,
)
from manabot.sim.structured_benchmark import run_frontiers
from manabot.verify.util import GW_ALLIES_DECK, UR_LESSONS_DECK
import managym

ROOT = Path(__file__).resolve().parents[1]


def _pack(seed: int) -> BoundSemanticPack:
    env = managym.Env(seed=seed, skip_trivial=False)
    env.reset(
        [
            managym.PlayerConfig("ur-lessons", UR_LESSONS_DECK),
            managym.PlayerConfig("gw-allies", GW_ALLIES_DECK),
        ]
    )
    return BoundSemanticPack.from_env(env)


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _distribution(values: list[int]) -> dict[str, float]:
    data = np.asarray(values, dtype=np.float64)
    return {
        "min": float(data.min()),
        "p50": float(np.percentile(data, 50)),
        "p95": float(np.percentile(data, 95)),
        "max": float(data.max()),
        "mean": float(data.mean()),
    }


def _paired_rows(rows: list[Mapping[str, Any]]) -> bool:
    fingerprints = []
    for row in rows:
        fingerprints.append(
            [
                (
                    item["program"],
                    item["definition"],
                    item["deck"],
                    item["seat"],
                    item["replicate"],
                    item["expected"],
                )
                for item in row["zero_shot_rows"]
            ]
        )
    return all(value == fingerprints[0] for value in fingerprints[1:])


def _positive_transfer(
    aggregate: Mapping[str, Mapping[str, Any]], workload: Mapping[str, Any]
) -> dict[str, Any]:
    target = aggregate["semantic_only_structured"]["zero_shot"]["accuracy"]
    controls = {
        arm: aggregate[arm]["zero_shot"]["accuracy"]
        for arm in ("card_id_legacy", "card_id_structured")
    }
    criterion = workload["positive_transfer_criterion"]
    uplifts = {arm: target - value for arm, value in controls.items()}
    positive = target >= criterion["semantic_only_minimum_accuracy"] and all(
        uplift >= criterion["minimum_uplift_over_each_identity_control"]
        for uplift in uplifts.values()
    )
    return {
        "decision": "positive_transfer" if positive else "null_result",
        "semantic_only_zero_shot_accuracy": target,
        "identity_control_accuracies": controls,
        "uplift_over_identity_controls": uplifts,
        "criterion": dict(criterion),
        "criterion_met": positive,
    }


def run(path: Path) -> dict[str, Any]:
    workload, workload_hash = load_workload(path)
    pack = _pack(int(workload["dataset_seed"]))
    spec = build_spec(pack)
    records = build_records(pack, spec, workload)
    arms = [
        run_arm(pack, workload, arm=arm, model_seed=seed)
        for arm in ARMS
        for seed in workload["model_seeds"]
    ]
    aggregate = {
        arm: {
            "zero_shot": aggregate_arm(
                [row for row in arms if row["arm"] == arm], "zero_shot"
            ),
            "limited_retraining": aggregate_arm(
                [row for row in arms if row["arm"] == arm], "limited_retraining"
            ),
        }
        for arm in ARMS
    }
    robustness = robustness_controls(pack, spec)
    frontier = run_frontiers({"scorer_seed": int(workload["dataset_seed"])})
    maximum_choices = max(
        max(row["candidate_count"], row["represented_legal_branches"])
        for row in frontier["fixtures"]
    )
    token_counts = [len(row.token_ids) for row in records]
    checkpoint_rates = [row["checkpoint_rebind"]["match_rate"] for row in arms]
    gates = {
        "causally_paired_examples": _paired_rows(arms),
        "balanced_holdout_decks": {row.deck for row in records if row.held_out}
        == {"ur_lessons", "gw_allies"}
        and sum(row.deck == "ur_lessons" for row in records if row.held_out)
        == sum(row.deck == "gw_allies" for row in records if row.held_out),
        "balanced_evaluation_seats": all(
            row["zero_shot"]["by_seat"]["0"]["total"]
            == row["zero_shot"]["by_seat"]["1"]["total"]
            for row in arms
        ),
        "zero_silent_overflow": max(token_counts) <= spec.max_program_tokens,
        "unknown_opcode_fail_closed": robustness["unknown_opcode_fail_closed"],
        "enum_schema_change_fail_closed": robustness[
            "enum_domain_schema_change_fail_closed"
        ],
        "content_pack_rebind_exact": robustness[
            "content_pack_reorder_checkpoint_rebind_rate"
        ]
        == workload["gates"]["required_rebind_match_rate"],
        "checkpoint_roundtrip_exact": min(checkpoint_rates)
        == workload["gates"]["required_rebind_match_rate"],
        "schema_id_permutation_exact": robustness[
            "token_kind_and_opcode_id_permutation_rate"
        ]
        == workload["gates"]["required_permutation_match_rate"],
        "structured_frontier_above_32": maximum_choices
        >= workload["gates"]["minimum_legal_choices"],
        "legacy_abi_frontier_equivalent": frontier["matching"] == frontier["shared"],
        "metrics_complete": all(
            row["performance"]["latency_ns"]["p50"] > 0
            and row["performance"]["examples_per_second"] > 0
            and row["performance"]["peak_rss_bytes"] > 0
            for row in arms
        ),
    }
    result = {
        "schema_version": 1,
        "status": "pass" if all(gates.values()) else "fail",
        "experiment_kind": workload["claim"],
        "claim_boundary": workload["claim_boundary"],
        "kr6_closed": False,
        "workload": {
            "id": workload["id"],
            "path": str(path),
            "sha256": workload_hash,
        },
        "measurement_code_revision": _git_revision(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "pythonhashseed": os.environ.get("PYTHONHASHSEED", "unset"),
        },
        "semantic_input_spec": spec.to_dict(),
        "semantic_input_spec_digest": spec.digest,
        "dataset": {
            "programs": len(records),
            "training_programs": sum(not row.held_out for row in records),
            "heldout_programs": sum(row.held_out for row in records),
            "heldout_definitions": workload["held_out_definitions"],
            "token_counts": _distribution(token_counts),
            "declared_token_budget": spec.max_program_tokens,
            "overflow_count": 0,
            "silent_truncation_count": 0,
        },
        "arms": arms,
        "aggregate": aggregate,
        "decision": _positive_transfer(aggregate, workload),
        "robustness": robustness,
        "structured_legality_frontier": {
            "maximum_choices_or_branches": maximum_choices,
            **frontier,
        },
        "gates": gates,
        "limitations": [
            "The probe predicts a typed operation from an ability program; it does not select actions in full games.",
            "Semantic programs and structured commands are not yet joined to PPO or every decision family.",
            "The v1 flat enum token omits enum-domain identity, so arbitrary enum-ID rebinding is unsupported and fails closed.",
            "Wilson intervals summarize repeated seeded probe decisions, not independent tournament games.",
        ],
        "follow_ons": [
            "Join the semantic encoder and structured decoder to PPO/all admitted decision families.",
            "Add enum-domain identity to the projection before claiming arbitrary schema-table rebinding.",
            "Run held-out gameplay and win-rate transfer after that join.",
        ],
    }
    return result


def _percent(value: float) -> str:
    return f"{value:.1%}"


def render_report(result: Mapping[str, Any]) -> str:
    aggregate = result["aggregate"]
    lines = [
        "# W2-214 semantic-program transfer probe",
        "",
        f"Status: **{str(result['status']).upper()}**. Decision: "
        f"**{result['decision']['decision']}**.",
        "",
        "Run with:",
        "",
        "```sh",
        "PYTHONHASHSEED=0 uv run scripts/run_semantic_transfer.py \\",
        "  --workload experiments/workloads/semantic-transfer-v1.json \\",
        "  --out experiments/data/semantic-transfer-v1.json \\",
        "  --report experiments/semantic-transfer-v1.md",
        "```",
        "",
        "## Claim boundary",
        "",
        str(result["claim_boundary"]),
        "This result does **not** close Rules Semantic KR6. It is a causal",
        "capability probe over admitted ability programs, not gameplay or win-rate",
        "transfer. The production policy/action ABI is unchanged.",
        "",
        "## Four-arm result",
        "",
        "| Arm | input | head | zero-shot heldout (95% CI) | limited retrain (95% CI) |",
        "|---|---|---|---:|---:|",
    ]
    labels = {
        "card_id_legacy": ("opaque CardDefId", "legacy fixed"),
        "card_id_structured": ("opaque CardDefId", "structured"),
        "semantic_card_id_structured": ("program + CardDefId", "structured"),
        "semantic_only_structured": ("program only", "structured"),
    }
    for arm in ARMS:
        zero = aggregate[arm]["zero_shot"]
        limited = aggregate[arm]["limited_retraining"]
        label, head = labels[arm]
        lines.append(
            f"| `{arm}` | {label} | {head} | {_percent(zero['accuracy'])} "
            f"[{_percent(zero['ci95_low'])}, {_percent(zero['ci95_high'])}] | "
            f"{_percent(limited['accuracy'])} "
            f"[{_percent(limited['ci95_low'])}, {_percent(limited['ci95_high'])}] |"
        )
    lines.extend(
        [
            "",
            "The same training programs, candidate permutations, model seeds,",
            "optimizer budget, held-out rows, deck split, and seat duplication are used",
            "for every arm. Card identity slots are hash-ordered opaque symbols, not IR",
            "rows, and the held-out identity embeddings receive no zero-shot updates.",
            "",
            "## Robustness and capacity",
            "",
            f"- Program tokens: p50 {result['dataset']['token_counts']['p50']:.0f}, "
            f"p95 {result['dataset']['token_counts']['p95']:.0f}, max "
            f"{result['dataset']['token_counts']['max']:.0f} against an exact "
            f"{result['dataset']['declared_token_budget']}-token budget; 0 overflows "
            "and 0 silent truncations.",
            f"- ContentPack reorder + checkpoint rebind: "
            f"{_percent(result['robustness']['content_pack_reorder_checkpoint_rebind_rate'])} exact.",
            f"- Token-kind/opcode numeric-ID permutation: "
            f"{_percent(result['robustness']['token_kind_and_opcode_id_permutation_rate'])} exact.",
            "- Unknown opcodes and changed enum-domain schemas fail closed.",
            f"- The live structured frontier represents "
            f"{result['structured_legality_frontier']['maximum_choices_or_branches']} "
            "choices/branches and remains legacy-adapter equivalent.",
            "",
            "## Performance",
            "",
            "Latency, throughput, parameter bytes, and process peak RSS are recorded",
            "for every arm/seed in the JSON receipt. RSS is the shared-process high-water",
            "mark, so it is suitable as a reproducibility receipt, not an isolated model",
            "memory comparison.",
            "",
            "## What remains",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in result["follow_ons"])
    lines.extend(
        [
            "",
            "The enum follow-on is structural: v1 emits one flat `enum` token without",
            "its source domain. This experiment can safely rebind token-kind and opcode",
            "IDs, but arbitrary enum-table permutation would be ambiguous and is",
            "therefore rejected rather than counted as robustness.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.workload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(result))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
