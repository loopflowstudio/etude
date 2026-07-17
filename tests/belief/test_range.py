from __future__ import annotations

import math

import numpy as np
import pytest

from manabot.belief.range import ExactHandRange, RangeError


def test_interactive_opening_range_has_exact_support_and_mass() -> None:
    counts = (12, 12, 6, 6, 4, 4, 6, 4, 3, 3)
    hand_range = ExactHandRange.uniform(range(10), counts, 7)

    assert hand_range.support_size == 10_832
    assert math.isclose(float(hand_range.probabilities.sum()), 1.0)
    assert hand_range.allocated_bytes > 0
    assert 1.0 <= hand_range.effective_range_size <= hand_range.support_size


def test_combinatorial_mass_matches_physical_subsets() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (2, 2), 2)

    assert hand_range.support_size == 3
    assert hand_range.probability((2, 0)) == pytest.approx(1 / 6)
    assert hand_range.probability((1, 1)) == pytest.approx(4 / 6)
    assert hand_range.probability((0, 2)) == pytest.approx(1 / 6)


def test_unknown_draw_is_exact_hypergeometric_convolution() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (2, 1), 1)
    drawn = hand_range.draw_unknown()

    assert drawn.hand_size == 2
    assert drawn.unseen_counts == (2, 1)
    assert drawn.probability((2, 0)) == pytest.approx(1 / 3)
    assert drawn.probability((1, 1)) == pytest.approx(2 / 3)


def test_known_exit_conditions_then_maps_the_hand() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (2, 2), 2)
    removed = hand_range.remove_known(10)

    assert removed.hand_size == 1
    assert removed.unseen_counts == (1, 2)
    assert removed.probability((1, 0)) == pytest.approx(1 / 5)
    assert removed.probability((0, 1)) == pytest.approx(4 / 5)


def test_known_return_is_deterministic() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (1, 2), 1)
    returned = hand_range.add_known(10)

    assert returned.hand_size == 2
    assert returned.unseen_counts == (2, 2)
    assert returned.probability((2, 0)) == pytest.approx(1 / 3)
    assert returned.probability((1, 1)) == pytest.approx(2 / 3)


def test_action_floor_preserves_behavioral_zero_but_not_illegal_hand() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (1, 1), 1)
    updated = hand_range.condition_action(
        likelihoods=(0.0, 0.0), legal_action_counts=(2, 0), epsilon=0.1
    )

    assert updated.support_size == 1
    assert updated.probability((0, 1)) == pytest.approx(1.0)


def test_sampling_is_reproducible_and_returns_supported_hands() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (2, 2), 2)

    first = hand_range.sample(20, seed=71)
    second = hand_range.sample(20, seed=71)

    assert first == second
    assert all(hand_range.probability(hand) > 0.0 for hand in first)


def test_range_digest_rank_and_inclusion_are_replay_stable() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (2, 2), 2)
    rebuilt = ExactHandRange.uniform((10, 20), (2, 2), 2)

    assert hand_range.digest == rebuilt.digest
    assert hand_range.normalization_error < 1e-12
    assert hand_range.rank((1, 1)) == 1
    assert hand_range.rank((9, 9)) is None
    assert hand_range.inclusion_probabilities() == pytest.approx((5 / 6, 5 / 6))


def test_impossible_transition_fails_closed() -> None:
    hand_range = ExactHandRange.uniform((10,), (1,), 1)

    with pytest.raises(RangeError, match="empty hidden library"):
        hand_range.draw_unknown()
    with pytest.raises(RangeError, match="illegal in every hypothesis"):
        hand_range.condition_action([0.0], [0], epsilon=0.1)


def test_normalization_rejects_mutated_weights() -> None:
    hand_range = ExactHandRange.uniform((10, 20), (1, 1), 1)

    with pytest.raises(RangeError, match="not normalized"):
        ExactHandRange(
            hand_range.card_def_ids,
            hand_range.unseen_counts,
            hand_range.hand_size,
            hand_range.keys.copy(),
            np.zeros_like(hand_range.log_weights),
        )
