"""Lium controller transport. Account credentials never enter the bootstrap."""

from __future__ import annotations

import urllib.parse

from etude.deadline import call
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed
from etude.launch.providers.http import MAX_REPLY_BYTES, send_to_provider
from etude.launch.storage.transport import Send

BASE = "https://lium.io/api/"


class LiumHTTP:
    def __init__(self, key: str, send: Send = send_to_provider) -> None:
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
            reply = call(
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
        if absent_ok and reply.status == 404:
            return None
        if not 200 <= reply.status < 300 or len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Lium {method} {path}: status {reply.status}")
        return reply.body
