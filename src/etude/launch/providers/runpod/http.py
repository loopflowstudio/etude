"""Standard-library RunPod HTTP transport, shared with the independent guardian."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from etude.deadline import call, remaining
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed

BASE = "https://api.runpod.io/v2/"
ID = re.compile(r"[a-zA-Z0-9_-]{1,200}")
MAX_REPLY_BYTES = 8 * 1024 * 1024

# Sends one request and returns the status and body. Raises OSError when no reply came.
Send = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


def send_request(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as reply:  # noqa: S310
            return reply.status, reply.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""


class PodHTTP:
    def __init__(self, key: str, send: Send = send_request) -> None:
        if not key or any(character.isspace() for character in key):
            raise ValueError("a RunPod API key is required")
        self.headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # RunPod's front door refuses Python's default agent name with a 403.
            "User-Agent": "etude-launcher",
        }
        self.send_request = send

    @timed("provider.http.ask")
    def ask(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        absent_ok: bool = False,
    ) -> Any:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        body = None if payload is None else json.dumps(payload).encode()
        try:
            status, raw = call(lambda: self.send_request(method, url, body, self.headers))
        except TimeoutError:
            raise ProviderUnavailable(
                f"{method} {path}: operation deadline reached; reply unconfirmed"
            ) from None
        except OSError:
            raise ProviderUnavailable(f"{method} {path}: no reply") from None
        if absent_ok and status == 404:
            return None
        if not 200 <= status < 300 or len(raw) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"{method} {path}: status {status}")
        try:
            return json.loads(raw, parse_float=Decimal) if raw else None
        except ValueError:
            raise ProviderUnavailable(f"{method} {path}: unreadable reply") from None

    def delete_self(self, id: str) -> None:
        if not ID.fullmatch(id):
            raise ValueError("not a provider id")
        self.ask("DELETE", f"pods/{id}", absent_ok=True)
