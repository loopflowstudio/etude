"""Default machine cleanup supplied by the active provider environment."""

import os
import sys


def command() -> list[str]:
    if os.environ.get("RUNPOD_POD_ID"):
        return [sys.executable, "-m", "etude.launch.providers.runpod.pod", "delete"]
    return []
