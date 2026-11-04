"""Vast's instance-scoped guardian and bootstrap. No third-party dependencies."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from etude.launch.errors import ProviderUnavailable
from etude.launch.providers.bootstrap import run
from etude.launch.providers.vast.http import InstanceHTTP


class GuardianAPI(InstanceHTTP):
    def budget_deadline(self, machine: str, max_usd: Decimal) -> datetime | None:
        raw = self.ask("GET", f"api/v0/instances/{machine}/", absent_ok=True)
        if raw is None:
            return None
        try:
            row = json.loads(raw)["instances"]
            if row is None:
                return None
            price = Decimal(str(row["dph_total"]))
            start = datetime.fromtimestamp(float(row["start_date"]), UTC)
            if not price.is_finite() or price < 0:
                raise ValueError("invalid rate")
            return start + timedelta(hours=float(max_usd / price)) if price else None
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            raise ProviderUnavailable("Vast guardian could not read the rental rate") from None


def _ssh_ready() -> None:
    # ssh_direct has already installed and started SSH before invoking onstart.
    print("SSH managed by Vast: use the instance Connect tab.", flush=True)


def main(argv: list[str]) -> int:
    return run(
        argv[1],
        api=GuardianAPI(os.environ["CONTAINER_API_KEY"]),
        machine=os.environ["CONTAINER_ID"],
        module="etude.launch.providers.vast.instance",
        start_ssh=_ssh_ready,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
