"""Known-truth calibration kept outside the acting player's information path."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable

import numpy as np

from manabot.belief.range import ExactHandRange, HandKey, RangeError
from manabot.belief.tracker import ExactRangeTracker

_HAND_ZONE = 1  # managym.ZoneEnum.HAND


@dataclass(frozen=True, slots=True)
class CardCalibrationPoint:
    card_def_id: int
    predicted_inclusion: float
    present: bool

    def to_dict(self) -> dict[str, int | float | bool]:
        return {
            "card_def_id": self.card_def_id,
            "predicted_inclusion": self.predicted_inclusion,
            "present": self.present,
        }


@dataclass(frozen=True, slots=True)
class KnownTruthPoint:
    """Authority-only witness scored against one viewer-derived posterior."""

    game_index: int
    step: int
    viewer: int
    transition_sequence: int
    true_hand: HandKey
    true_hand_probability: float
    true_hand_log_loss: float
    true_hand_rank: int | None
    top_hand_mass: float
    effective_range_size: float
    posterior_digest: str
    cards: tuple[CardCalibrationPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_index": self.game_index,
            "step": self.step,
            "viewer": self.viewer,
            "transition_sequence": self.transition_sequence,
            "true_hand": list(self.true_hand),
            "true_hand_probability": self.true_hand_probability,
            "true_hand_log_loss": self.true_hand_log_loss,
            "true_hand_rank": self.true_hand_rank,
            "top_hand_mass": self.top_hand_mass,
            "effective_range_size": self.effective_range_size,
            "posterior_digest": self.posterior_digest,
            "cards": [card.to_dict() for card in self.cards],
        }


def authority_hand_key(engine: Any, tracker: ExactRangeTracker) -> HandKey:
    """Read the opponent hand from authority for evaluation, never inference.

    This function deliberately lives in the audit module.  The player and
    tracker receive neither this observation nor the returned hand key.
    """

    opponent = (tracker.viewer + 1) % 2
    authority_view = engine.observation_for_player(opponent)
    counts = {definition: 0 for definition in tracker.posterior.card_def_ids}
    for card in authority_view.agent_cards:
        if int(card.zone) != _HAND_ZONE:
            continue
        definition = int(card.registry_key)
        if definition not in counts:
            raise RangeError(
                f"authority hand contains definition outside the viewer range: {definition}"
            )
        counts[definition] += 1
    hand = tuple(counts[definition] for definition in tracker.posterior.card_def_ids)
    if sum(hand) != tracker.posterior.hand_size:
        raise RangeError(
            "authority hand size does not match the viewer-safe public hand count"
        )
    return hand


def score_known_truth(
    engine: Any,
    tracker: ExactRangeTracker,
    *,
    game_index: int,
    step: int,
) -> KnownTruthPoint:
    """Score one posterior without feeding the authority witness back to play."""

    posterior = tracker.posterior
    hand = authority_hand_key(engine, tracker)
    probability = posterior.probability(hand)
    inclusions = posterior.inclusion_probabilities()
    cards = tuple(
        CardCalibrationPoint(
            card_def_id=definition,
            predicted_inclusion=float(inclusion),
            present=hand[index] > 0,
        )
        for index, (definition, inclusion) in enumerate(
            zip(posterior.card_def_ids, inclusions)
        )
    )
    return KnownTruthPoint(
        game_index=game_index,
        step=step,
        viewer=tracker.viewer,
        transition_sequence=len(tracker.records),
        true_hand=hand,
        true_hand_probability=probability,
        true_hand_log_loss=(-math.log(probability) if probability > 0.0 else math.inf),
        true_hand_rank=posterior.rank(hand),
        top_hand_mass=float(np.max(posterior.probabilities)),
        effective_range_size=posterior.effective_range_size,
        posterior_digest=posterior.digest,
        cards=cards,
    )


def _ece(points: Iterable[CardCalibrationPoint], bins: int) -> float:
    rows = list(points)
    if not rows:
        return 0.0
    weighted_error = 0.0
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        bucket = [
            row
            for row in rows
            if low <= row.predicted_inclusion < high
            or (index == bins - 1 and row.predicted_inclusion == 1.0)
        ]
        if not bucket:
            continue
        predicted = float(np.mean([row.predicted_inclusion for row in bucket]))
        observed = float(np.mean([row.present for row in bucket]))
        weighted_error += len(bucket) * abs(predicted - observed)
    return weighted_error / len(rows)


def aggregate_known_truth(
    points: Iterable[KnownTruthPoint], *, bins: int = 10
) -> dict[str, Any]:
    """Aggregate hand log loss and per-definition reliability diagnostics."""

    rows = list(points)
    if bins < 1:
        raise ValueError("calibration bins must be positive")
    if not rows:
        return {
            "points": 0,
            "mean_true_hand_log_loss": None,
            "mean_per_card_brier": None,
            "per_card": {},
        }
    card_rows = [card for row in rows for card in row.cards]
    ranks = [row.true_hand_rank for row in rows if row.true_hand_rank is not None]
    definitions = sorted({card.card_def_id for card in card_rows})
    per_card: dict[str, Any] = {}
    for definition in definitions:
        selected = [card for card in card_rows if card.card_def_id == definition]
        per_card[str(definition)] = {
            "points": len(selected),
            "brier": float(
                np.mean(
                    [
                        (card.predicted_inclusion - float(card.present)) ** 2
                        for card in selected
                    ]
                )
            ),
            "ece": _ece(selected, bins),
            "mean_predicted_inclusion": float(
                np.mean([card.predicted_inclusion for card in selected])
            ),
            "observed_inclusion": float(np.mean([card.present for card in selected])),
        }
    return {
        "points": len(rows),
        "mean_true_hand_log_loss": float(
            np.mean([row.true_hand_log_loss for row in rows])
        ),
        "mean_per_card_brier": float(
            np.mean(
                [
                    (card.predicted_inclusion - float(card.present)) ** 2
                    for card in card_rows
                ]
            )
        ),
        "per_card_ece": _ece(card_rows, bins),
        "mean_top_hand_mass": float(np.mean([row.top_hand_mass for row in rows])),
        "median_true_hand_rank": float(np.median(ranks)) if ranks else None,
        "mean_effective_range_size": float(
            np.mean([row.effective_range_size for row in rows])
        ),
        "true_hand_outside_support": sum(row.true_hand_rank is None for row in rows),
        "per_card": per_card,
    }


def score_range_for_hand(
    hand_range: ExactHandRange, hand: HandKey
) -> tuple[float, int | None]:
    """Small public helper used by brute-force calibration tests."""

    return hand_range.log_loss(hand), hand_range.rank(hand)


def viewer_equivalence_audit(
    engine: Any, tracker: ExactRangeTracker, *, first_seed: int, second_seed: int
) -> dict[str, int | bool]:
    """Compare fixed-viewer projections across distinct supported hands."""

    posterior = tracker.posterior
    first_key = tuple(int(value) for value in posterior.keys[0])
    second_key = tuple(int(value) for value in posterior.keys[-1])
    first = engine.clone_env()
    second = engine.clone_env()
    first.determinize_to_hand(
        hand=posterior.as_definition_counts(first_key),
        seed=first_seed,
        perspective=tracker.viewer,
    )
    second.determinize_to_hand(
        hand=posterior.as_definition_counts(second_key),
        seed=second_seed,
        perspective=tracker.viewer,
    )
    first_view = json.loads(first.observation_for_player(tracker.viewer).toJSON())
    second_view = json.loads(second.observation_for_player(tracker.viewer).toJSON())
    exposed = sum(
        int(card["zone"] == _HAND_ZONE)
        for view in (first_view, second_view)
        for card in view["opponent_cards"]
    )
    return {
        "hands_distinct": first_key != second_key,
        "authority_states_distinct": first.state_digest() != second.state_digest(),
        "viewer_projection_mismatches": int(first_view != second_view),
        "opponent_private_cards_exposed": exposed,
    }
