"""Lium controller transport. Account credentials never enter the bootstrap."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from etude.deadline import call, remaining
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed

BASE = "https://lium.io/api/"
_MAX_REPLY_BYTES = 8 * 1024 * 1024
Send = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


def _send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as reply:
            return reply.status, reply.read(_MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""


class LiumHTTP:
    def __init__(self, key: str, send: Send = _send) -> None:
        if not key or any(character.isspace() for character in key):
            raise ValueError("a Lium API key is required")
        self._headers = {
            "X-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "etude-launcher",
        }
        self._send = send

    @timed("provider.http.ask")
    def ask(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        params: dict[str, str] | None = None,
        absent_ok: bool = False,
        idempotency_key: str | None = None,
    ) -> bytes | None:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            status, raw = call(
                lambda: self._send(
                    method,
                    url,
                    None if body is None else body.encode(),
                    {
                        **self._headers,
                        **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
                    },
                )
            )
        except (OSError, TimeoutError):
            raise ProviderUnavailable(f"Lium {method} {path}: reply unconfirmed") from None
        if absent_ok and status == 404:
            return None
        if not 200 <= status < 300 or len(raw) > _MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Lium {method} {path}: status {status}")
        return raw
