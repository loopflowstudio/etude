"""Standard-library Vast transport, also used by the instance's deadline guardian."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from etude.deadline import call, remaining
from etude.launch.errors import ProviderUnavailable
from etude.launch.measurement import timed

BASE = "https://console.vast.ai/"
MAX_REPLY_BYTES = 8 * 1024 * 1024
Send = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


def send_request(
    method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as reply:
            return reply.status, reply.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""


class InstanceHTTP:
    def __init__(self, key: str, send: Send = send_request) -> None:
        if not key or any(character.isspace() for character in key):
            raise ValueError("a Vast API key is required")
        self.headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "etude-launcher",
        }
        self.send_request = send

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
            status, raw = call(
                lambda: self.send_request(
                    method, url, None if body is None else body.encode(), self.headers
                )
            )
        except (OSError, TimeoutError):
            raise ProviderUnavailable(f"Vast {method} {path}: reply unconfirmed") from None
        if absent_ok and status == 404:
            return None
        if not 200 <= status < 300 or len(raw) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Vast {method} {path}: status {status}")
        return raw

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
