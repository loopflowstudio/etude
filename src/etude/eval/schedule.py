"""The order an eval run plays its matches in, so consecutive matches reuse checkpoints.

A match seats checkpoints, by their `CheckpointKey` (`checkpoints`); code
agents need none. An author's inference runtime, such as Manabot's
`PolicyStore`, keeps the checkpoints used most recently loaded and evicts the
least recently used. `ordered` yields matches in the order `evaluate` plays
them: next, the match that loads the fewest checkpoints not yet used, then the
one reusing the most, then the one whose checkpoints were used most recently.
That ranking by recency (stack distance) suits a runtime of any size, so Etude
models no byte allowance and counts no loads: the runtime owns those.

Ties keep the caller's order, so a queue of code agents plays as given. The
order changes no result: a match's games come from its key and seed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence

from etude.eval.records import CheckpointKey, Match


def checkpoints(match: Match) -> frozenset[CheckpointKey]:
    """The checkpoints a match seats; one checkpoint at two configs is one checkpoint."""
    return frozenset(
        seated.key.checkpoint for seated in match.agents if seated.key.checkpoint is not None
    )


def ordered(matches: Sequence[Match]) -> Iterator[Match]:
    """The matches in the order `evaluate` plays them, one as each worker frees up.

    Each match counts as using its checkpoints once it is yielded. A caller
    cutting its own batches should cut contiguous runs of this order.
    """
    # Matches seating the same checkpoints, each group in the caller's order.
    groups: dict[frozenset[CheckpointKey], deque[tuple[int, Match]]] = {}
    for index, match in enumerate(matches):
        groups.setdefault(checkpoints(match), deque()).append((index, match))
    # Checkpoints by when they were last used, least recent first.
    used: dict[CheckpointKey, None] = {}
    while groups:
        # How many checkpoints were used since each one: its stack distance.
        since = {key: len(used) - 1 - at for at, key in enumerate(used)}

        def cost(group: frozenset[CheckpointKey]) -> tuple[int, ...]:
            warm = [since[key] for key in group if key in since]
            first = groups[group][0][0]
            return (len(group) - len(warm), -len(warm), max(warm, default=0), first)

        group = min(groups, key=cost)
        waiting = groups[group]
        _, match = waiting.popleft()
        if not waiting:
            del groups[group]
        # Seat order, not the `checkpoints` set, whose iteration follows string
        # hashing: every process, and so every share of a hold, sees one order.
        for seated in match.agents:
            if seated.key.checkpoint is not None:
                used.pop(seated.key.checkpoint, None)
                used[seated.key.checkpoint] = None
        yield match
