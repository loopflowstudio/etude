"""Standard-library RunPod HTTP transport, shared with the independent guardian."""

from __future__ import annotations

import json
import re
import urllib.parse
from decimal import Decimal
from typing import Any

from etude.deadline import call
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed
from etude.launch.providers.http import MAX_REPLY_BYTES, send_to_provider
from etude.launch.storage.transport import Send

BASE = "https://api.runpod.io/v2/"
_ID = re.compile(r"[a-zA-Z0-9_-]{1,200}")


class PodHTTP:
    def __init__(self, key: str, send: Send = send_to_provider) -> None:
        if not key or any(character.isspace() for character in key):
            raise ValueError("a RunPod API key is required")
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # RunPod's front door refuses Python's default agent name with a 403.
            "User-Agent": "etude-launcher",
        }
        self._send = send

    @timed("provider.http.ask")
    def _ask(
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
            reply = call(lambda: self._send(method, url, body, self._headers))
        except TimeoutError:
            raise ProviderUnavailable(
                f"{method} {path}: operation deadline reached; reply unconfirmed"
            ) from None
        except OSError:
            raise ProviderUnavailable(f"{method} {path}: no reply") from None
        if absent_ok and reply.status == 404:
            return None
        if not 200 <= reply.status < 300 or len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"{method} {path}: status {reply.status}")
        try:
            return json.loads(reply.body, parse_float=Decimal) if reply.body else None
        except ValueError:
            raise ProviderUnavailable(f"{method} {path}: unreadable reply") from None

    def delete_self(self, id: str) -> None:
        if not _ID.fullmatch(id):
            raise ValueError("not a provider id")
        self._ask("DELETE", f"pods/{id}", absent_ok=True)
