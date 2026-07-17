from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from manabot.belief.likelihood import (
    FrozenPolicyLikelihood,
    LikelihoodResult,
    PublicAction,
    PublicActionKind,
    _matching_action_indexes,
    file_sha256,
)
from manabot.belief.player import ExactRangePlayer, UniformRangePlayer
from manabot.env import ObservationSpace
from manabot.infra.hypers import AgentHypers, ObservationSpaceHypers
from manabot.model.agent import Agent
from manabot.sim.flat_mc import play_games
from manabot.sim.teacher1_evidence import _fresh_env


class NeutralLikelihood:
    def evaluate(
        self, root_engine, *, viewer: int, action: PublicAction, hand_range
    ) -> LikelihoodResult:
        del root_engine, viewer, action
        return LikelihoodResult(
            likelihoods=np.ones(hand_range.support_size, dtype=np.float64),
            legal_action_counts=np.full(hand_range.support_size, 2, dtype=np.int64),
            seconds=0.0,
        )


def test_public_action_grouping_sums_duplicate_definition_offers() -> None:
    raw = SimpleNamespace(
        agent_cards=[
            SimpleNamespace(id=101, registry_key=7),
            SimpleNamespace(id=102, registry_key=7),
            SimpleNamespace(id=103, registry_key=9),
        ],
        action_space=SimpleNamespace(
            actions=[
                SimpleNamespace(action_type=1, focus=[101], declared=None),
                SimpleNamespace(action_type=1, focus=[102], declared=None),
                SimpleNamespace(action_type=1, focus=[103], declared=None),
                SimpleNamespace(action_type=2, focus=[], declared=None),
            ]
        ),
    )

    matching, legal_count = _matching_action_indexes(
        raw, PublicAction(PublicActionKind.COMMIT_DEFINITION, card_def_id=7)
    )

    assert matching == [0, 1]
    assert legal_count == 3


def test_frozen_likelihood_fails_closed_on_checkpoint_hash(tmp_path: Path) -> None:
    checkpoint = tmp_path / "likelihood.pt"
    checkpoint.write_bytes(b"not the registered model")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        FrozenPolicyLikelihood(checkpoint, expected_sha256="0" * 64)


def test_belief_and_uniform_use_identical_search_path_without_evidence() -> None:
    env = _fresh_env(83)
    viewer = int(env._engine.current_agent_index())
    likelihood = NeutralLikelihood()
    belief = ExactRangePlayer(1, likelihood=likelihood, seed=109)
    uniform = UniformRangePlayer(1, likelihood=likelihood, seed=109)
    belief.start_game(env, viewer)
    uniform.start_game(env, viewer)

    belief_action = belief.act(env, {})
    uniform_action = uniform.act(env, {})

    assert belief_action == uniform_action
    assert np.array_equal(belief.last_scores, uniform.last_scores)
    assert belief.stats.search.simulations == uniform.stats.search.simulations
    assert belief.stats.installed_hand_mismatches == 0
    assert uniform.stats.installed_hand_mismatches == 0


def test_exact_range_player_emits_revision_bound_legal_command() -> None:
    env = _fresh_env(89)
    viewer = int(env._engine.current_agent_index())
    player = ExactRangePlayer(1, likelihood=NeutralLikelihood(), seed=127)
    player.start_game(env, viewer)
    action = player.act(env, {})
    content_hash = str(env.content_pack_manifest()["content_digest"])

    command = player.command_for_action(
        env.last_raw_obs,
        action,
        match_id="int-9-test",
        revision=3,
        content_hash=content_hash,
        asset_manifest_hash=content_hash,
    )

    assert command["match_id"] == "int-9-test"
    assert command["expected_revision"] == 3
    assert command["prompt_id"] == 3
    assert command["offer_id"] == action
    assert player.stats.commands_emitted == 1
    assert player.evidence_stats()["p50_end_to_end_latency_ms"] is not None


def test_player_omits_noop_prompt_fragments_from_public_replay() -> None:
    env = _fresh_env(93)
    viewer = int(env._engine.current_agent_index())
    player = ExactRangePlayer(1, likelihood=NeutralLikelihood(), seed=129)
    player.start_game(env, viewer)

    player.observe_step(env, viewer)

    assert player.tracker is not None
    assert player.tracker.records == []
    assert player.stats.range_updates == 0


def test_opponent_prepare_uses_public_prompt_kind_not_private_action_index() -> None:
    env = _fresh_env(94)
    acting = int(env._engine.current_agent_index())
    viewer = (acting + 1) % 2
    player = ExactRangePlayer(1, likelihood=NeutralLikelihood(), seed=129)
    player.start_game(env, viewer)

    player.prepare_step(env, acting, action=10_000)

    assert player._pending_likelihood_root is not None


def test_player_archives_public_replay_at_game_boundary() -> None:
    env = _fresh_env(95)
    viewer = int(env._engine.current_agent_index())
    player = ExactRangePlayer(1, likelihood=NeutralLikelihood(), seed=130)
    player.start_game(env, viewer)

    player.finish_game(game_index=4, seed=95)

    replay = player.replay_receipts()[0]
    assert replay["game_index"] == 4
    assert replay["seed"] == 95
    assert replay["viewer"] == viewer


def test_player_tracks_an_opponent_pass_from_the_fixed_viewer_boundary() -> None:
    env = _fresh_env(97)
    viewer = int(env._engine.current_agent_index())
    player = ExactRangePlayer(1, likelihood=NeutralLikelihood(), seed=131)
    player.start_game(env, viewer)

    for _ in range(4):
        raw = env.last_raw_obs
        acting = int(raw.agent.player_index)
        pass_action = next(
            index
            for index, option in enumerate(raw.action_space.actions)
            if int(option.action_type) == 2
        )
        player.prepare_step(env, acting, pass_action)
        env.step(pass_action)
        player.observe_step(env, acting)
        if acting != viewer:
            break

    assert acting != viewer
    assert player.stats.action_updates == 1
    assert player.tracker is not None
    assert player.tracker.posterior.card_def_ids == player.tracker.uniform.card_def_ids
    assert player.stats.peak_range_bytes > 0


def test_tiny_matchup_records_calibration_replay_and_system_cost(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "tiny-policy.pt"
    obs_hypers = ObservationSpaceHypers()
    agent_hypers = AgentHypers()
    obs_space = ObservationSpace(obs_hypers)
    agent = Agent(obs_space, agent_hypers)
    torch.save(
        {
            "hypers": {
                "observation_hypers": obs_hypers.model_dump(),
                "agent_hypers": agent_hypers.model_dump(),
            },
            "model_state_dict": agent.state_dict(),
        },
        checkpoint,
    )
    digest = file_sha256(checkpoint)
    common = {
        "sims": 1,
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": digest,
        "rollouts_per_world": 1,
        "max_steps": 200,
        "likelihood_batch_size": 16,
    }
    tiny_deck = {"Mountain": 4, "Raging Goblin": 4}

    result = play_games(
        {"kind": "exact_range", **common},
        {"kind": "uniform_range", **common},
        num_games=1,
        seed=157,
        hero_deck=tiny_deck,
        villain_deck=tiny_deck,
    )

    assert len(result.records) == 1
    assert result.hero_evidence is not None
    assert result.villain_evidence is not None
    assert result.hero_evidence["installed_hand_mismatches"] == 0
    assert result.hero_evidence["calibration"]["points"] > 0
    assert result.hero_evidence["p95_end_to_end_latency_ms"] is not None
    assert result.hero_evidence["peak_rss_bytes"] > 0
    assert result.hero_replays[0]["game_index"] == 0
    assert result.hero_known_truth
