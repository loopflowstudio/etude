"""Pinned policy likelihoods over viewer-safe public action identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from pathlib import Path
import time
from typing import Any

import numpy as np
from numpy.typing import NDArray
import torch

from manabot.belief.range import ExactHandRange


class PublicActionKind(str, Enum):
    PASS_PRIORITY = "pass_priority"
    COMMIT_DEFINITION = "commit_definition"


@dataclass(frozen=True, slots=True)
class PublicAction:
    """A deliberately small, viewer-safe selected-matchup action alphabet."""

    kind: PublicActionKind
    card_def_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is PublicActionKind.COMMIT_DEFINITION:
            if self.card_def_id is None:
                raise ValueError("commit_definition requires a card_def_id")
        elif self.card_def_id is not None:
            raise ValueError(f"{self.kind.value} cannot carry a card_def_id")

    def to_dict(self) -> dict[str, str | int | None]:
        return {"kind": self.kind.value, "card_def_id": self.card_def_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PublicAction:
        return cls(
            PublicActionKind(str(value["kind"])),
            card_def_id=(
                int(value["card_def_id"])
                if value.get("card_def_id") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class LikelihoodResult:
    likelihoods: NDArray[np.float64]
    legal_action_counts: NDArray[np.int64]
    seconds: float


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FrozenPolicyLikelihood:
    """Counterfactual action likelihoods from one hash-pinned w2 policy.

    The evaluator never accepts the authority's true hand.  Every row begins
    from the same retained public root, installs one hand hypothesis through
    managym's validated exact-hand determinization, and encodes the resulting
    opponent view.  Duplicate physical copies of one definition are grouped
    by summing their legal action probabilities.
    """

    def __init__(
        self,
        checkpoint: str | Path,
        *,
        expected_sha256: str,
        batch_size: int = 256,
        device: str = "cpu",
        counterfactual_seed: int = 0,
    ) -> None:
        path = Path(checkpoint)
        if not path.is_file():
            raise FileNotFoundError(f"frozen likelihood checkpoint is missing: {path}")
        actual_sha256 = file_sha256(path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                "frozen likelihood checkpoint SHA-256 mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.checkpoint = path
        self.checkpoint_sha256 = actual_sha256
        self.batch_size = batch_size
        self.device = torch.device(device)
        self.counterfactual_seed = counterfactual_seed
        from manabot.sim.flat_mc import load_checkpoint_agent

        self.agent, self.obs_space = load_checkpoint_agent(str(path))
        self.agent.to(self.device)
        self.agent.eval()

    def evaluate(
        self,
        root_engine: Any,
        *,
        viewer: int,
        action: PublicAction,
        hand_range: ExactHandRange,
    ) -> LikelihoodResult:
        started = time.perf_counter()
        likelihoods = np.zeros(hand_range.support_size, dtype=np.float64)
        legal_counts = np.zeros(hand_range.support_size, dtype=np.int64)
        encoded_batch: list[dict[str, np.ndarray]] = []
        matching_batch: list[list[int]] = []
        legal_batch: list[int] = []
        row_batch: list[int] = []

        def flush() -> None:
            if not encoded_batch:
                return
            buffers = {
                key: np.stack([encoded[key] for encoded in encoded_batch])
                for key in encoded_batch[0]
            }
            tensors = {
                key: torch.from_numpy(value).to(self.device)
                for key, value in buffers.items()
            }
            with torch.inference_mode():
                logits, _ = self.agent.forward(tensors)
                probabilities = torch.softmax(logits, dim=-1).cpu().numpy()
            for local, row in enumerate(row_batch):
                matching = matching_batch[local]
                if matching:
                    likelihoods[row] = float(probabilities[local, matching].sum())
                    legal_counts[row] = legal_batch[local]
            encoded_batch.clear()
            matching_batch.clear()
            legal_batch.clear()
            row_batch.clear()

        for row, hand in enumerate(hand_range.keys):
            hand_key = tuple(int(value) for value in hand)
            hypothesis = root_engine.clone_env()
            hypothesis.determinize_to_hand(
                hand=hand_range.as_definition_counts(hand_key),
                seed=self.counterfactual_seed,
                perspective=viewer,
            )
            opponent = (viewer + 1) % 2
            raw = hypothesis.observation_for_player(opponent)
            matching, legal_count = _matching_action_indexes(raw, action)
            encoded_batch.append(self.obs_space.encode(raw))
            matching_batch.append(matching)
            legal_batch.append(legal_count)
            row_batch.append(row)
            if len(encoded_batch) >= self.batch_size:
                flush()
        flush()
        return LikelihoodResult(
            likelihoods=likelihoods,
            legal_action_counts=legal_counts,
            seconds=time.perf_counter() - started,
        )


def _matching_action_indexes(raw: Any, observed: PublicAction) -> tuple[list[int], int]:
    card_definitions = {
        int(card.id): int(card.registry_key) for card in raw.agent_cards
    }
    groups: dict[tuple[Any, ...], list[int]] = {}
    for index, option in enumerate(raw.action_space.actions):
        action_type = int(option.action_type)
        focus = tuple(int(value) for value in option.focus)
        if action_type == 2:
            key: tuple[Any, ...] = (PublicActionKind.PASS_PRIORITY.value,)
        elif action_type in (0, 1) and focus:
            definition = card_definitions.get(focus[0])
            if definition is None:
                key = ("unresolved_private_source", action_type)
            else:
                key = (PublicActionKind.COMMIT_DEFINITION.value, definition)
        else:
            # These candidates are public but not evidence in the thinnest
            # selected-matchup alphabet.  They still count in the epsilon
            # denominator and are grouped without physical CardIds.
            key = ("other", action_type, focus, option.declared)
        groups.setdefault(key, []).append(index)

    if observed.kind is PublicActionKind.PASS_PRIORITY:
        observed_key = (PublicActionKind.PASS_PRIORITY.value,)
    else:
        observed_key = (
            PublicActionKind.COMMIT_DEFINITION.value,
            int(observed.card_def_id),
        )
    return groups.get(observed_key, []), len(groups)
