"""One request to a provider's API. Standard library only: each rental guardian sends with it."""

from __future__ import annotations

import urllib.error
import urllib.request

from etude.deadline import remaining
from etude.launch.storage.transport import HTTPReply

MAX_ERROR_BYTES = 2048
MAX_REPLY_BYTES = 8 * 1024 * 1024


def send_to_provider(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> HTTPReply:
    """Send within the enclosing deadline; raises OSError when no reply came.

    A body longer than `MAX_REPLY_BYTES` comes back one byte over it, for the
    caller to refuse. Error bodies retain only their first 2 KiB for diagnostics.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as reply:  # noqa: S310
            return HTTPReply(status=reply.status, body=reply.read(MAX_REPLY_BYTES + 1))
    except urllib.error.HTTPError as error:
        with error:
            return HTTPReply(status=error.code, body=error.read(MAX_ERROR_BYTES))
