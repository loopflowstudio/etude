"""Common-seed S1-S5 competency evidence for arena registrations."""

from __future__ import annotations

from typing import Any

from manabot.verify.competency import (
    SCENARIOS,
    aggregate_scenario_results,
    run_scenario_once,
)

from .models import PlayerRegistration, canonical_sha256
from .players import build_player


def competency_seed(player_id: str, scenario: str, run_seed: int) -> int:
    return int(canonical_sha256([player_id, scenario, run_seed])[:16], 16)


def run_competencies(
    registrations: list[PlayerRegistration],
    *,
    seeds: tuple[int, ...],
    checkpoint_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    checkpoint_paths = checkpoint_paths or {}
    evidence: dict[str, Any] = {"scenario_seeds": list(seeds), "players": {}}
    for registration in registrations:
        player_block: dict[str, Any] = {}
        for scenario_name, scenario in SCENARIOS.items():
            runs = []
            for run_seed in seeds:
                seed = competency_seed(registration.player_id, scenario_name, run_seed)
                player, obs_space = build_player(
                    registration,
                    seed=seed,
                    checkpoint_path=checkpoint_paths.get(registration.player_id),
                )
                result = run_scenario_once(scenario, player, obs_space, seed=seed)
                runs.append({"run_seed": run_seed, "player_seed": seed, **result})
            player_block[scenario_name] = {
                "correct_line": scenario.correct_line,
                "runs": runs,
                "aggregate": aggregate_scenario_results(runs),
            }
        evidence["players"][registration.player_id] = player_block
    return evidence


def competency_noninferiority(
    evidence: dict[str, Any], challenger: str, incumbent: str, *, margin: float
) -> dict[str, Any]:
    result = {}
    for scenario in SCENARIOS:
        challenger_runs = evidence["players"][challenger][scenario]["runs"]
        incumbent_runs = evidence["players"][incumbent][scenario]["runs"]
        differences = [
            float(bool(left["correct"])) - float(bool(right["correct"]))
            for left, right in zip(challenger_runs, incumbent_runs, strict=True)
        ]
        point = sum(differences) / len(differences)
        result[scenario] = {
            "point_difference": point,
            "margin": -margin,
            "point_passed": point >= -margin,
        }
    return result
