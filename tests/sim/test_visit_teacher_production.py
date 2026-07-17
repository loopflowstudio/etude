"""Production contract and decision checks for the INT-4 iteration."""

from pathlib import Path

import pytest

from experiments.runners.run_visit_teacher_production import (
    _admission,
    _control_receipt,
    _execute_job,
    _load_contracts,
    _production_runtime,
    _validate_production_runtime,
)
from manabot.env import ObservationSpace
from manabot.infra.hypers import AgentHypers
from manabot.model.agent import Agent
from manabot.sim.distill import save_bc_checkpoint
from manabot.sim.teacher1_evidence import ContractError, canonical_sha256, file_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONTRACT = (
    REPO_ROOT / "experiments/contracts/int-4-visit-teacher-iteration-v1.json"
)
PRODUCTION_CONTRACT = (
    REPO_ROOT / "experiments/contracts/int-4-visit-teacher-production-v1.json"
)
SCENARIOS = [
    "s1_counter_the_bomb",
    "s2_hold_the_wipe",
    "s3_bolt_the_threat",
    "s4_race_vs_block",
    "s5_hold_up_quench",
]


def _job(result: dict) -> dict:
    return {"result": result}


def _cell(*, wins: int, games: int = 48, draws: int = 0) -> dict:
    return {
        "num_games": float(games),
        "wins": float(wins),
        "win_rate": wins / games,
        "draws_or_caps": float(draws),
    }


def _profile() -> dict:
    return {"training_seeds": [197, 419, 887]}


def _production() -> dict:
    return {
        "competencies": {"scenarios": SCENARIOS},
        "admission_gates": {
            "median_win_rate": 0.55,
            "winning_training_seeds": 2,
            "per_seed_win_rate": 0.5,
            "competency_max_regression": 0.1,
        },
    }


def _arena(
    *, search_wins: list[int], policy_only_wins: list[int], policy_value_wins: list[int]
) -> dict:
    competencies = {}
    for scenario in SCENARIOS:
        block = {"control-policy-value": {"correct_rate": 0.5}}
        for seed in _profile()["training_seeds"]:
            block[f"visit-policy-value-{seed}"] = {"correct_rate": 0.5}
            block[f"visit-policy-value-search-{seed}"] = {"correct_rate": 0.5}
        competencies[scenario] = block
    per_seed = {}
    for index, seed in enumerate(_profile()["training_seeds"]):
        # The searched candidate is the villain in this paired cell.
        student_wins = 48 - search_wins[index]
        per_seed[str(seed)] = {
            "round_robin": {
                "student-vs-student+search": _job(_cell(wins=student_wins))
            },
            "controls": {
                "student-vs-control-policy_only": _job(
                    _cell(wins=policy_only_wins[index])
                ),
                "student-vs-control-policy_value": _job(
                    _cell(wins=policy_value_wins[index])
                ),
            },
        }
    return {"per_seed": per_seed, "competencies": _job(competencies)}


def test_production_contract_binds_frozen_iteration_and_runtime() -> None:
    contract, _, _, _, production, _ = _load_contracts(
        BASE_CONTRACT, PRODUCTION_CONTRACT
    )
    runtime = _production_runtime(seed=int(contract["runtime_seed"]))
    _validate_production_runtime(production, runtime)
    assert production["base_iteration"]["profile"] == "iteration"
    assert production["calibration"]["max_realized_p50_gap"] == 0.1
    assert production["competencies"]["runs_per_agent_scenario"] == 100


def test_control_receipt_rejects_wrong_hash_before_loading(tmp_path: Path) -> None:
    path = tmp_path / "wrong.pt"
    path.write_bytes(b"not a checkpoint")
    with pytest.raises(ContractError, match="SHA-256"):
        _control_receipt(
            path,
            {
                "arm": "policy_only",
                "sha256": "0" * 64,
                "deterministic": True,
            },
        )


def test_control_receipt_binds_checkpoint_arm_and_model(tmp_path: Path) -> None:
    path = tmp_path / "policy-value.pt"
    obs_space = ObservationSpace()
    agent = Agent(obs_space, AgentHypers())
    save_bc_checkpoint(agent, obs_space, path, extra={"arm": "policy_value"})
    receipt = _control_receipt(
        path,
        {
            "arm": "policy_value",
            "sha256": file_sha256(path),
            "deterministic": True,
        },
    )
    assert receipt["arm"] == "policy_value"
    assert receipt["parameter_count"] > 0
    with pytest.raises(ContractError, match="metadata arm"):
        _control_receipt(
            path,
            {
                "arm": "policy_only",
                "sha256": file_sha256(path),
                "deterministic": True,
            },
        )


def test_admission_prefers_student_search_when_complete_gates_pass() -> None:
    arena = _arena(
        search_wins=[27, 26, 28],
        policy_only_wins=[30, 30, 30],
        policy_value_wins=[30, 30, 30],
    )
    result = _admission(arena, _profile(), _production())
    assert result["decision"] == "admit_student_search"
    assert result["disposition"] == "continue"


def test_admission_falls_back_to_student_then_honest_failure() -> None:
    student = _arena(
        search_wins=[20, 21, 22],
        policy_only_wins=[27, 26, 28],
        policy_value_wins=[28, 27, 26],
    )
    assert _admission(student, _profile(), _production())["decision"] == "admit_student"

    failed = _arena(
        search_wins=[20, 21, 22],
        policy_only_wins=[20, 21, 22],
        policy_value_wins=[21, 20, 22],
    )
    result = _admission(failed, _profile(), _production())
    assert result["decision"] == "prototype_failure"
    assert result["disposition"] == "revise"


def test_completed_child_job_is_reused_only_for_exact_inputs(tmp_path: Path) -> None:
    job = {"kind": "play_cell", "hero": {"kind": "random"}}
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    output = jobs / "cached.result.json"
    output.write_text(
        "{\n"
        f'  "input_sha256": "{canonical_sha256(job)}",\n'
        '  "result": {"cached": true}\n'
        "}\n"
    )
    assert _execute_job(tmp_path, "cached", job)["result"] == {"cached": True}

    output.write_text('{"input_sha256": "wrong", "result": {}}\n')
    with pytest.raises(ContractError, match="different inputs"):
        _execute_job(tmp_path, "cached", job)


def test_spawned_matchup_job_records_result_and_resources(tmp_path: Path) -> None:
    result = _execute_job(
        tmp_path,
        "random-mirror",
        {
            "kind": "play_cell",
            "hero": {"kind": "random"},
            "villain": {"kind": "random"},
            "blocks": [{"id": "focused", "seed": 17, "games": 2}],
        },
    )
    assert result["result"]["num_games"] == 2
    assert result["result"]["draws_or_caps"] == 0
    assert result["resources"]["wall_seconds"] > 0
    assert result["resources"]["peak_rss_bytes"] > 0
