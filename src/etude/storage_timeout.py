"""Storage socket allowances shared by tracking and the standard-library launcher.

A dead connection must release the uploader. Socket limits bound inactivity,
not the duration of a progressing download; whole-body writes need extra time
because urllib sends their bytes in one blocking operation.
"""

import math
import os


class StorageUnavailable(Exception):
    """No usable answer from storage. This never means an object is absent."""


def _positive(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


def socket_timeout(size_bytes: int = 0) -> float:
    """Seconds for one socket operation; callers apply any enclosing deadline.

    ETUDE_STORAGE_TIMEOUT_SECONDS defaults to 60. Whole-body uploads add their
    size divided by ETUDE_STORAGE_BYTES_PER_SECOND (default 1 MiB/s).
    """
    seconds = _positive("ETUDE_STORAGE_TIMEOUT_SECONDS", 60.0)
    rate = _positive("ETUDE_STORAGE_BYTES_PER_SECOND", 1024.0**2)
    return seconds + size_bytes / rate


def storage_environment() -> dict[str, str]:
    """Forward explicit, validated storage limits to a remote worker's parent."""
    socket_timeout()
    return {
        name: value
        for name in ("ETUDE_STORAGE_TIMEOUT_SECONDS", "ETUDE_STORAGE_BYTES_PER_SECOND")
        if (value := os.environ.get(name)) is not None
    }
