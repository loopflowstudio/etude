"""Advance a small panel of variable-length games together."""

from collections.abc import AsyncIterator
from itertools import islice
from typing import cast

from etude.checkpoints import CheckpointRef
from etude.eval.agents import Forward, ForwardError, ModelForward
from etude.eval.game import MatchStream
from etude.eval.records import Match
from etude.eval.result import Tables


def load(saved: CheckpointRef) -> ModelForward:
    bias = int(saved.path.read_text())

    def forward(inputs: list[object]) -> list[object]:
        # Run all ready rows together, then return one output batch per input.
        batches = [cast(tuple[int, ...], batch) for batch in inputs]
        scores = iter(turn + bias for batch in batches for turn in batch)
        return [list(islice(scores, len(batch))) for batch in batches]

    return forward


async def play_many(matches: MatchStream) -> AsyncIterator[tuple[Match, Tables | Exception]]:
    slots: list[tuple[Match, Forward, int]] = []
    while True:
        for match, agents in await matches.take(matches.target_slots - len(slots)):
            slots.append((match, cast(Forward, agents[0]), 1 + match.seed % 3))
        if not slots:
            return
        # This panel seats the same checkpoint in every match.
        forward = slots[0][1]
        try:
            [output] = await forward([tuple(left for _, _, left in slots)], rows=len(slots))
        except ForwardError as error:
            for match, _, _ in slots:
                yield match, error
            slots.clear()
            continue
        ready, slots = slots, []
        for (match, agent, left), score in zip(ready, cast(list[int], output), strict=True):
            if left > 1:
                slots.append((match, agent, left - 1))
            else:
                yield match, {"scores": [{"seat": 0, "score": score}, {"seat": 1, "score": -score}]}
