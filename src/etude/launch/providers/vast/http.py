"""Standard-library Vast transport, also used by the instance's deadline guardian."""

from __future__ import annotations

import json
import urllib.parse

from etude.deadline import call
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed
from etude.launch.providers.http import MAX_REPLY_BYTES, send_to_provider
from etude.launch.storage.transport import Send

BASE = "https://console.vast.ai/"


class InstanceHTTP:
    def __init__(self, key: str, send: Send = send_to_provider) -> None:
        if not key or any(character.isspace() for character in key):
            raise ValueError("a Vast API key is required")
        self._headers = {
            "Authorization": f"Bearer {key}",
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
    ) -> bytes | None:
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            reply = call(
                lambda: self._send(
                    method, url, None if body is None else body.encode(), self._headers
                )
            )
        except (OSError, TimeoutError):
            raise ProviderUnavailable(f"Vast {method} {path}: reply unconfirmed") from None
        if absent_ok and reply.status == 404:
            return None
        if not 200 <= reply.status < 300 or len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Vast {method} {path}: status {reply.status}")
        return reply.body

    def delete_self(self, instance: str) -> None:
        if not instance.isascii() or not instance.isdigit():
            raise ValueError("not a Vast instance id")
        raw = self.ask("DELETE", f"api/v0/instances/{instance}/", absent_ok=True)
        if raw is None:
            return
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ValueError("unconfirmed deletion")
        except (ValueError, TypeError):
            raise ProviderUnavailable("Vast deletion unconfirmed") from None
