"""Pre-install Lium watchdog. Stops this pod's work, never ends provider billing.

The account credential stays on the controller. Provider removal timers cover
controller loss; this process adds a local deadline for bootstrap and execution.
"""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal

from etude.launch.providers.bootstrap import GUARDIAN_MARKER, boot, guard


class Watchdog:
    def budget_deadline(self, machine: str, max_usd: Decimal) -> datetime | None:
        return None

    def delete_self(self, machine: str) -> None:
        # The SSH bootstrap owns a fresh process group. This kills its bootstrap
        # and worker; the worker's own deadline supervises its job groups.
        os.killpg(int(machine), signal.SIGTERM)


def main(argv: list[str]) -> int:
    if argv[1] == "boot":
        return boot("etude.launch.providers.lium.pod", None, None)
    if argv[1] != "guard":
        raise ValueError("expected boot or guard")
    GUARDIAN_MARKER.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    GUARDIAN_MARKER.write_text(datetime.now(UTC).isoformat())
    guard(
        Watchdog(),
        str(os.getpgrp()),
        datetime.fromisoformat(os.environ["ETUDE_DEADLINE"]),
        Decimal(os.environ["ETUDE_MAX_USD"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
