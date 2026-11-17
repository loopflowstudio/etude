"""Stop a rental process group at its deadline. Its host owns machine deletion."""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal

from etude.launch.provisioner.providers.bootstrap import GUARDIAN_MARKER, boot, guard


class Watchdog:
    def budget_deadline(self, machine: str, max_usd: Decimal) -> datetime | None:
        return None

    def delete_self(self, machine: str) -> None:
        # The bootstrap owns its process group. The worker supervises job groups.
        os.killpg(int(machine), signal.SIGTERM)


def main(argv: list[str]) -> int:
    if argv[1] == "boot":
        return boot("etude.launch.provisioner.providers.watchdog", None, None)
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
