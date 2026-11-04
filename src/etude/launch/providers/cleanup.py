"""Default machine cleanup supplied by the active provider environment."""

import os
import sys


def command() -> list[str]:
    if os.environ.get("RUNPOD_POD_ID"):
        return [sys.executable, "-m", "etude.launch.providers.runpod.pod", "delete"]
    if os.environ.get("CONTAINER_ID") and os.environ.get("CONTAINER_API_KEY"):
        return [sys.executable, "-m", "etude.launch.providers.vast.instance", "delete"]
    return []
