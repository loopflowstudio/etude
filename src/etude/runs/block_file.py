"""Metric block names and sequence coverage, for their writers, readers and delivery.

Names identify the writer and covered sequences without reading block bytes or
loading NumPy. Wider blocks supersede their covered tails and recent blocks.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Under a run's folder. A tail is local to its machine while its writer lives,
# and says so by its suffix, which an uploader can skip without knowing more.
NAME = re.compile(
    r"^metrics/(?:(?P<only>\d{8})-(?P<tail_writer>[0-9a-f]{12})\.tail"
    r"|(?P<level>\d+)-(?P<first>\d{8})-(?P<last>\d{8})-(?P<writer>[0-9a-f]{12})\.blk)$"
)


@dataclass(frozen=True)
class BlockFile:
    key: str  # relative to the run
    writer: str
    level: int  # 0 for a tail
    first: int
    last: int


def tail_key(sequence: int, writer: str) -> str:
    return f"metrics/{sequence:08d}-{writer}.tail"


def block_key(level: int, first: int, last: int, writer: str) -> str:
    return f"metrics/{level}-{first:08d}-{last:08d}-{writer}.blk"


def parse(key: str) -> BlockFile | None:
    """The block a key under a run names, or None when it names something else."""
    found = NAME.match(key)
    if found is None:
        return None
    if found["only"] is not None:
        sequence = int(found["only"])
        return BlockFile(key, found["tail_writer"], 0, sequence, sequence)
    return BlockFile(
        key, found["writer"], int(found["level"]), int(found["first"]), int(found["last"])
    )


def select(files: Iterable[BlockFile]) -> list[BlockFile]:
    """Keep the widest coverage, preferring higher-level permanent copies."""
    ordered = sorted(files, key=lambda f: (f.writer, f.first - f.last, -f.level))
    kept: list[BlockFile] = []
    for file in ordered:
        if not any(
            other.writer == file.writer and other.first <= file.first and file.last <= other.last
            for other in kept
        ):
            kept.append(file)
    return sorted(kept, key=lambda f: (f.writer, f.first))
