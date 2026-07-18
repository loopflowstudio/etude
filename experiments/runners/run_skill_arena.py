#!/usr/bin/env python3
"""Freeze and challenge the world-pinned manabot skill arena."""

from __future__ import annotations

import argparse
from itertools import combinations
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from manabot.arena.competency import run_competencies
from manabot.arena.match import play_cell
from manabot.arena.models import (
    ArenaContract,
    PlayerRegistration,
    canonical_sha256,
    file_sha256,
)
from manabot.arena.profile import summarize_profiles
from manabot.arena.rating import bootstrap_population, fit_population, payoff_matrix
from manabot.arena.replay import read_trace, replay_games
from manabot.sim.teacher1_evidence import (
    REPO_ROOT,
    runtime_fingerprints,
    source_bundle_sha256,
)


class ArenaError(RuntimeError):
    """The closed arena contract or artifact failed validation."""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def manifest_digest(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    return canonical_sha256(unsigned)


def source_digest(paths: tuple[str, ...] | list[str]) -> str:
    return source_bundle_sha256([REPO_ROOT / path for path in paths])


def registration_source_paths(registration: PlayerRegistration) -> list[str]:
    kind = registration.player_spec["kind"]
    if kind == "scripted_greedy":
        return ["manabot/arena/players.py"]
    if kind == "determinized_puct":
        return [
            "manabot/sim/flat_mc.py",
            "manabot/sim/mcts.py",
            "manabot/sim/search_branch.py",
            "managym/src/python/bindings.rs",
        ]
    return ["manabot/sim/flat_mc.py"]


def validate_registration_source(registration: PlayerRegistration) -> None:
    if registration.runner_kind != "code":
        return
    actual = source_digest(registration_source_paths(registration))
    if registration.source_sha256 != actual:
        raise ArenaError(f"source drift for {registration.player_id}: {actual}")


def preflight_contract(path: Path) -> tuple[ArenaContract, str, dict[str, Any]]:
    raw = load_json(path)
    contract = ArenaContract.model_validate(raw)
    contract_sha = file_sha256(path)
    actual_source = source_digest(contract.source_paths)
    if actual_source != contract.source_sha256:
        raise ArenaError(f"arena source drift: {actual_source}")
    actual_runtime = runtime_fingerprints()
    mismatches = {
        key: {"expected": expected, "actual": actual_runtime.get(key)}
        for key, expected in contract.runtime.items()
        if actual_runtime.get(key) != expected
    }
    if mismatches:
        raise ArenaError("runtime drift: " + json.dumps(mismatches, sort_keys=True))
    for registration in contract.anchors:
        validate_registration_source(registration)
    return contract, contract_sha, actual_runtime


def preflight_candidate(
    path: Path, checkpoint_path: Path | None
) -> tuple[PlayerRegistration, dict[str, str]]:
    registration = PlayerRegistration.model_validate(load_json(path))
    if registration.role != "challenger":
        raise ArenaError("candidate registration role must be challenger")
    checkpoint_paths: dict[str, str] = {}
    if registration.runner_kind == "code":
        if checkpoint_path is not None:
            raise ArenaError("code candidate rejects --candidate-checkpoint")
        validate_registration_source(registration)
    else:
        if checkpoint_path is None or not checkpoint_path.is_file():
            raise ArenaError("checkpoint candidate bytes are unavailable")
        if file_sha256(checkpoint_path) != registration.checkpoint_sha256:
            raise ArenaError("checkpoint candidate SHA-256 mismatch")
        if checkpoint_path.stat().st_size != registration.checkpoint_bytes:
            raise ArenaError("checkpoint candidate byte-size mismatch")
        checkpoint_paths[registration.player_id] = str(checkpoint_path)
    return registration, checkpoint_paths


def append_ledger(out_dir: Path, stage: str, payload: dict[str, Any]) -> None:
    path = out_dir / "resource-ledger.jsonl"
    previous = None
    if path.exists() and path.read_text().splitlines():
        previous = json.loads(path.read_text().splitlines()[-1])["event_sha256"]
    unsigned = {
        "stage": stage,
        "recorded_unix": time.time(),
        "previous_event_sha256": previous,
        "payload": payload,
    }
    event = {**unsigned, "event_sha256": canonical_sha256(unsigned)}
    with path.open("a") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def artifact_receipts(out_dir: Path, names: list[str]) -> dict[str, Any]:
    return {
        name: {"path": name, "sha256": file_sha256(out_dir / name)} for name in names
    }


def finalize_manifest(out_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = {**manifest, "manifest_sha256": manifest_digest(manifest)}
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def report_markdown(
    *,
    fit: Any,
    promotion: dict[str, Any],
    matrix: dict[str, Any],
    replay: dict[str, Any],
) -> str:
    lines = [
        "# Manabot Skill Arena",
        "",
        f"Disposition: **{promotion['disposition']}**",
        f"Reason: `{promotion.get('reason')}`",
        "",
        "## Ratings",
        "",
    ]
    for player, rating in sorted(fit.ratings.items(), key=lambda item: -item[1]):
        lines.append(f"- `{player}`: {rating:.1f} Elo")
    lines.extend(
        [
            "",
            f"Seat-0 effect: {fit.seat0_elo:.1f} Elo",
            f"Log loss: {fit.log_loss:.6f}",
            f"Payoff cells: {len(matrix)}",
            f"Replay passed: {replay.get('passed', False)}",
            "",
        ]
    )
    return "\n".join(lines)


def rating_payload(
    rows: list[dict[str, Any]], schedule: Any
) -> tuple[Any, dict[str, Any]]:
    fit = fit_population(rows)
    bootstrap = bootstrap_population(
        rows,
        replicates=schedule.bootstrap_replicates,
        seed=schedule.bootstrap_seed,
    )
    payload = {
        "model": "seat-aware-gaussian-map-bradley-terry-v1",
        "prior_elo_std": 400.0,
        "ratings": fit.ratings,
        "seat0_elo": fit.seat0_elo,
        "converged": fit.converged,
        "iterations": fit.iterations,
        "gradient_norm": fit.gradient_norm,
        "hessian_condition": fit.hessian_condition,
        "log_loss": fit.log_loss,
        "residual_rows": list(fit.rows),
        "bootstrap": bootstrap,
    }
    return fit, payload


def promotion_payload(
    candidate: PlayerRegistration | None, rows: list[dict[str, Any]], *, smoke: bool
) -> dict[str, Any]:
    integrity_passed = all(
        row.get("replay_passed")
        and not any(int(value) for value in row["integrity"].values())
        for row in rows
    )
    if not integrity_passed:
        return {"disposition": "invalid_integrity", "reason": "zero_tolerance_gate"}
    if smoke:
        return {
            "disposition": "engineering_smoke_non_promotion",
            "reason": "fixture_or_smoke_profile",
        }
    if candidate is None:
        return {
            "disposition": "rated_not_promotion_eligible",
            "reason": "anchor_freeze",
        }
    return {
        "disposition": "rated_not_promotion_eligible",
        "reason": "incumbent_not_in_cohort",
        "candidate": candidate.player_id,
        "compute_class_id": candidate.compute_class_id,
    }


def verify_manifest(
    out_dir: Path, contract: ArenaContract, contract_sha: str
) -> dict[str, Any]:
    manifest = load_json(out_dir / "manifest.json")
    if manifest.get("manifest_sha256") != manifest_digest(manifest):
        raise ArenaError("manifest digest mismatch")
    if manifest.get("contract_sha256") != contract_sha:
        raise ArenaError("manifest contract mismatch")
    if manifest.get("arena_key") != contract.key.model_dump():
        raise ArenaError("manifest arena key mismatch")
    for receipt in manifest["artifacts"].values():
        path = out_dir / receipt["path"]
        if not path.is_file() or file_sha256(path) != receipt["sha256"]:
            raise ArenaError(f"artifact digest mismatch: {path}")
    replay_receipts = []
    for path in sorted((out_dir / "traces").glob("*.commands.jsonl.gz")):
        replay_receipts.append(replay_games(read_trace(path)).to_dict())
    if replay_receipts and not all(receipt["passed"] for receipt in replay_receipts):
        raise ArenaError("Command replay mismatch")
    rows = read_jsonl(out_dir / "matches.jsonl")
    fit = fit_population(rows)
    stored_rating = load_json(out_dir / "rating.json")
    if (
        fit.ratings != stored_rating["ratings"]
        or fit.seat0_elo != stored_rating["seat0_elo"]
    ):
        raise ArenaError("rating recomputation mismatch")
    return {
        "verified": True,
        "manifest_sha256": manifest["manifest_sha256"],
        "games": len(rows),
        "replay": replay_receipts,
    }


def freeze_anchors(args: argparse.Namespace) -> None:
    contract, contract_sha, runtime = preflight_contract(args.contract)
    profile = contract.profiles[args.profile]
    schedule = contract.schedules[args.profile]
    if args.out_dir.exists():
        raise ArenaError("output directory already exists")
    args.out_dir.mkdir(parents=True)
    append_ledger(
        args.out_dir,
        "preflight",
        {"contract_sha256": contract_sha, "profile": args.profile},
    )
    rows: list[dict[str, Any]] = []
    traces = []
    replays = []
    for first, second in combinations(contract.anchors, 2):
        cell_rows, trace, replay = play_cell(
            key=contract.key,
            player_a=first,
            player_b=second,
            deal_seeds=schedule.deal_seeds,
            out_dir=args.out_dir,
        )
        rows.extend(cell_rows)
        traces.append(trace)
        replays.append(replay)
    write_jsonl(args.out_dir / "matches.jsonl", rows)
    write_json(
        args.out_dir / "players.json",
        [player.model_dump() for player in contract.anchors],
    )
    competencies = run_competencies(
        list(contract.anchors), seeds=schedule.competency_seeds
    )
    write_json(args.out_dir / "competencies.json", competencies)
    fit, rating = rating_payload(rows, schedule)
    matrix = payoff_matrix(rows)
    profile_payload = summarize_profiles(rows)
    promotion = promotion_payload(None, rows, smoke=profile.disposition != "production")
    replay_payload = {
        "passed": all(item["passed"] for item in replays),
        "cells": replays,
    }
    write_json(args.out_dir / "replay.json", replay_payload)
    write_json(args.out_dir / "rating.json", rating)
    write_json(args.out_dir / "payoff-matrix.json", matrix)
    write_json(args.out_dir / "profile.json", profile_payload)
    write_json(args.out_dir / "promotion.json", promotion)
    (args.out_dir / "report.md").write_text(
        report_markdown(
            fit=fit, promotion=promotion, matrix=matrix, replay=replay_payload
        )
    )
    append_ledger(args.out_dir, "complete", {"games": len(rows), "cells": len(traces)})
    names = [
        "resource-ledger.jsonl",
        "players.json",
        "matches.jsonl",
        "replay.json",
        "competencies.json",
        "profile.json",
        "rating.json",
        "payoff-matrix.json",
        "promotion.json",
        "report.md",
    ]
    manifest = finalize_manifest(
        args.out_dir,
        {
            "schema_version": 1,
            "kind": "anchor-freeze",
            "profile": args.profile,
            "contract_sha256": contract_sha,
            "arena_key": contract.key.model_dump(),
            "runtime": runtime,
            "traces": traces,
            "artifacts": artifact_receipts(args.out_dir, names),
        },
    )
    print(
        json.dumps(
            {
                "state": "complete",
                "manifest": str(args.out_dir / "manifest.json"),
                "manifest_sha256": manifest["manifest_sha256"],
                "disposition": promotion["disposition"],
            },
            sort_keys=True,
        )
    )


def challenge(args: argparse.Namespace) -> None:
    contract, contract_sha, runtime = preflight_contract(args.contract)
    candidate, checkpoint_paths = preflight_candidate(
        args.candidate, args.candidate_checkpoint
    )
    anchor_dir = args.anchor_artifact.parent
    anchor_verification = verify_manifest(anchor_dir, contract, contract_sha)
    if args.verify:
        verification = verify_manifest(args.out_dir, contract, contract_sha)
        print(json.dumps(verification, sort_keys=True))
        return
    profile = contract.profiles[args.profile]
    schedule = contract.schedules[args.profile]
    if args.out_dir.exists():
        raise ArenaError("output directory already exists")
    args.out_dir.mkdir(parents=True)
    if candidate.runner_kind == "checkpoint":
        retained = args.out_dir / "checkpoints" / f"{candidate.player_id}.pt"
        retained.parent.mkdir(parents=True)
        shutil.copyfile(args.candidate_checkpoint, retained)
        checkpoint_paths[candidate.player_id] = str(retained)
    append_ledger(
        args.out_dir,
        "preflight",
        {
            "contract_sha256": contract_sha,
            "anchor_manifest_sha256": anchor_verification["manifest_sha256"],
            "candidate_sha256": candidate.identity_sha256,
        },
    )
    new_rows: list[dict[str, Any]] = []
    traces = []
    replays = []
    for anchor in contract.anchors:
        cell_rows, trace, replay = play_cell(
            key=contract.key,
            player_a=candidate,
            player_b=anchor,
            deal_seeds=schedule.deal_seeds,
            out_dir=args.out_dir,
            checkpoint_paths=checkpoint_paths,
        )
        new_rows.extend(cell_rows)
        traces.append(trace)
        replays.append(replay)
    anchor_rows = read_jsonl(anchor_dir / "matches.jsonl")
    rows = anchor_rows + new_rows
    write_jsonl(args.out_dir / "matches.jsonl", rows)
    write_json(
        args.out_dir / "players.json",
        [player.model_dump() for player in (*contract.anchors, candidate)],
    )
    anchor_competencies = load_json(anchor_dir / "competencies.json")
    candidate_competencies = run_competencies(
        [candidate], seeds=schedule.competency_seeds, checkpoint_paths=checkpoint_paths
    )
    competencies = anchor_competencies
    competencies["players"].update(candidate_competencies["players"])
    write_json(args.out_dir / "competencies.json", competencies)
    fit, rating = rating_payload(rows, schedule)
    matrix = payoff_matrix(rows)
    profile_payload = summarize_profiles(new_rows)
    promotion = promotion_payload(
        candidate, new_rows, smoke=profile.disposition != "production"
    )
    replay_payload = {
        "passed": all(item["passed"] for item in replays),
        "cells": replays,
        "anchor_verification": anchor_verification,
    }
    write_json(args.out_dir / "replay.json", replay_payload)
    write_json(args.out_dir / "rating.json", rating)
    write_json(args.out_dir / "payoff-matrix.json", matrix)
    write_json(args.out_dir / "profile.json", profile_payload)
    write_json(args.out_dir / "promotion.json", promotion)
    (args.out_dir / "report.md").write_text(
        report_markdown(
            fit=fit, promotion=promotion, matrix=matrix, replay=replay_payload
        )
    )
    append_ledger(
        args.out_dir,
        "complete",
        {"new_games": len(new_rows), "combined_games": len(rows)},
    )
    names = [
        "resource-ledger.jsonl",
        "players.json",
        "matches.jsonl",
        "replay.json",
        "competencies.json",
        "profile.json",
        "rating.json",
        "payoff-matrix.json",
        "promotion.json",
        "report.md",
    ]
    if candidate.runner_kind == "checkpoint":
        names.append(f"checkpoints/{candidate.player_id}.pt")
    manifest = finalize_manifest(
        args.out_dir,
        {
            "schema_version": 1,
            "kind": "challenge",
            "profile": args.profile,
            "contract_sha256": contract_sha,
            "arena_key": contract.key.model_dump(),
            "runtime": runtime,
            "anchor_manifest_sha256": anchor_verification["manifest_sha256"],
            "candidate": candidate.model_dump(),
            "traces": traces,
            "artifacts": artifact_receipts(args.out_dir, names),
        },
    )
    print(
        json.dumps(
            {
                "state": "complete",
                "manifest": str(args.out_dir / "manifest.json"),
                "manifest_sha256": manifest["manifest_sha256"],
                "candidate_elo": fit.ratings[candidate.player_id],
                "disposition": promotion["disposition"],
                "reason": promotion.get("reason"),
            },
            sort_keys=True,
        )
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze-anchors")
    freeze.add_argument("--contract", type=Path, required=True)
    freeze.add_argument("--out-dir", type=Path, required=True)
    freeze.add_argument(
        "--profile", choices=("smoke", "production"), default="production"
    )
    challenge_parser = subparsers.add_parser("challenge")
    challenge_parser.add_argument("--contract", type=Path, required=True)
    challenge_parser.add_argument("--anchor-artifact", type=Path, required=True)
    challenge_parser.add_argument("--candidate", type=Path, required=True)
    challenge_parser.add_argument("--candidate-checkpoint", type=Path)
    challenge_parser.add_argument("--out-dir", type=Path, required=True)
    challenge_parser.add_argument(
        "--profile", choices=("smoke", "production"), default="production"
    )
    challenge_parser.add_argument("--verify", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "freeze-anchors":
            freeze_anchors(args)
        else:
            challenge(args)
    except (ArenaError, ValueError, FileNotFoundError) as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error": type(error).__name__,
                    "message": str(error),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
