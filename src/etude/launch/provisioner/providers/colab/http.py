"""Google Colab OAuth2 transport and Jupyter kernel WebSocket execution.

Account OAuth credentials stay on the launcher and are never sent to a runtime.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import AliasChoices, Field
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from etude.deadline import call, remaining
from etude.errors import LauncherError
from etude.launch.errors import ProviderUnavailable
from etude.launch.provisioner.providers.http import MAX_REPLY_BYTES, send_to_provider
from etude.launch.storage.transport import HTTPReply, Send
from etude.spec import Record

COLAB_DOMAIN = "https://colab.research.google.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AGENT = "colab-cli"
XSSI_PREFIX = b")]}'\n"
_REFRESH_MARGIN = timedelta(minutes=5)
_MAX_WS_MESSAGES = 64
# Protocol debug logs contain proxy tokens and the signed links in execute_request.
_WS_LOG = logging.Logger("colab.websocket", level=logging.CRITICAL + 1)


class ColabCredentials(Record):
    """Google OAuth2 credentials for Colab's backend API.

    Either a direct short-lived `access_token` (from `COLAB_ACCESS_TOKEN`), or
    an authorized-user refresh grant (`client_id`, `client_secret`,
    `refresh_token`), matching `google-colab-cli` and `gcloud` ADC JSON.
    """

    access_token: str | None = Field(
        default=None, validation_alias=AliasChoices("access_token", "token")
    )
    expiry: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    token_uri: str = TOKEN_URI

    @classmethod
    def from_authorized_user_json(cls, raw: str) -> ColabCredentials:
        try:
            fields = json.loads(raw)
            if not isinstance(fields, dict):
                raise ValueError
            # Some authorized-user files leave optional strings empty.
            credentials = cls.model_validate(
                {k: v for k, v in fields.items() if isinstance(v, str) and v}
            )
        except ValueError:
            # Pydantic diagnostics may contain the original credential values.
            raise ValueError("invalid Colab OAuth credential fields") from None
        if not credentials.access_token and not (
            credentials.client_id and credentials.client_secret and credentials.refresh_token
        ):
            raise ValueError("Colab OAuth credentials lack an access or refresh token")
        return credentials


def load_credentials() -> ColabCredentials:
    """Resolve Google OAuth credentials from environment or standard local token files.

    Never invokes interactive GUI prompts, so the launcher works identically on
    laptops and headless servers.
    """
    if token := os.environ.get("COLAB_ACCESS_TOKEN", "").strip():
        return ColabCredentials(access_token=token)
    if raw := os.environ.get("COLAB_OAUTH_TOKEN", "").strip():
        try:
            return ColabCredentials.from_authorized_user_json(raw)
        except ValueError as error:
            raise LauncherError(f"COLAB_OAUTH_TOKEN is invalid: {error}") from None
    if file_path := os.environ.get("COLAB_OAUTH_TOKEN_FILE", "").strip():
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise LauncherError(f"COLAB_OAUTH_TOKEN_FILE does not exist: {path}")
        try:
            return ColabCredentials.from_authorized_user_json(path.read_text())
        except (OSError, ValueError) as error:
            raise LauncherError(f"COLAB_OAUTH_TOKEN_FILE is unreadable: {error}") from None
    etude_home = Path(os.environ.get("ETUDE_HOME") or Path.home() / ".etude").expanduser()
    for candidate in (
        etude_home / "colab-token.json",
        Path.home() / ".config" / "colab-cli" / "token.json",
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
    ):
        if candidate.is_file():
            try:
                return ColabCredentials.from_authorized_user_json(candidate.read_text())
            except (OSError, ValueError):
                continue
    raise LauncherError(
        "Colab credentials not found; run "
        "`gcloud auth application-default login "
        "--scopes=openid,https://www.googleapis.com/auth/cloud-platform,"
        "https://www.googleapis.com/auth/userinfo.email,"
        "https://www.googleapis.com/auth/colaboratory` "
        "or supply COLAB_OAUTH_TOKEN / COLAB_OAUTH_TOKEN_FILE / COLAB_ACCESS_TOKEN "
        "or ~/.etude/colab-token.json (mode 0600)"
    )


class _TokenReply(Record):
    access_token: str
    expires_in: int = 3600


def _proxy_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Colab-Runtime-Proxy-Token": token,
        "X-Colab-Client-Agent": AGENT,
    }


class ColabHTTP:
    def __init__(
        self,
        credentials: ColabCredentials,
        send: Send = send_to_provider,
    ) -> None:
        self._credentials = credentials
        self._send = send
        self._token: str | None = credentials.access_token
        self._expiry = credentials.expiry
        if self._expiry is not None and self._expiry.tzinfo is None:
            self._expiry = self._expiry.replace(tzinfo=UTC)

    def _request(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str], operation: str
    ) -> HTTPReply:
        # URLs and bodies may carry credentials. Only the operation belongs in diagnostics.
        try:
            reply = call(lambda: self._send(method, url, body, headers))
        except OSError:
            raise ProviderUnavailable(f"{operation}: reply unconfirmed") from None
        if len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"{operation}: reply too large")
        return reply

    def _bearer(self) -> str:
        now = datetime.now(UTC)
        if self._token is not None and (
            self._expiry is None or self._expiry - now > _REFRESH_MARGIN
        ):
            return self._token
        creds = self._credentials
        if not (creds.client_id and creds.client_secret and creds.refresh_token):
            if self._token is not None:
                return self._token
            raise ProviderUnavailable("Colab OAuth token expired and has no refresh grant")
        form = urllib.parse.urlencode(
            {
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "refresh_token": creds.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        reply = self._request(
            "POST",
            creds.token_uri,
            form,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            "Colab OAuth token refresh",
        )
        if not 200 <= reply.status < 300:
            raise ProviderUnavailable(f"Colab OAuth token refresh: status {reply.status}")
        try:
            refreshed = _TokenReply.model_validate_json(reply.body)
        except ValueError:
            raise ProviderUnavailable("Colab OAuth token refresh returned invalid JSON") from None
        self._token = refreshed.access_token
        self._expiry = now + timedelta(seconds=refreshed.expires_in)
        return self._token

    def ask_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HTTPReply:
        query = {**(params or {}), "authuser": "0"}
        url = f"{COLAB_DOMAIN}{path}?{urllib.parse.urlencode(query)}"
        sent_headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "X-Colab-Client-Agent": AGENT,
            **(headers or {}),
        }
        return self._request(method, url, None, sent_headers, f"Colab {method} {path}")

    def ask(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        absent_ok: bool = False,
    ) -> bytes | None:
        reply = self.ask_raw(method, path, params=params, headers=headers)
        if absent_ok and reply.status == 404:
            return None
        if not 200 <= reply.status < 300:
            raise ProviderUnavailable(f"Colab {method} {path}: status {reply.status}")
        return reply.body.removeprefix(XSSI_PREFIX)

    def start_kernel(self, base_url: str, token: str) -> bytes:
        reply = self._request(
            "POST",
            f"{base_url.rstrip('/')}/api/kernels",
            b'{"name":"python3"}',
            {
                **_proxy_headers(token),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            "Colab runtime POST /api/kernels",
        )
        if not 200 <= reply.status < 300:
            raise ProviderUnavailable(f"Colab runtime POST /api/kernels: status {reply.status}")
        return reply.body


def execute_in_kernel(base_url: str, proxy_token: str, kernel_id: str, code: str) -> None:
    """Submit code to a Colab Jupyter kernel and wait until execution enters `busy`."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host or parsed.scheme not in {"http", "https"}:
        raise ProviderUnavailable("Colab runtime proxy URL is invalid")
    ws_session = uuid4().hex
    path = (
        f"{parsed.path.rstrip('/')}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels"
    )
    query = urllib.parse.urlencode(
        {"colab-runtime-proxy-token": proxy_token, "session_id": ws_session}
    )
    url = urllib.parse.urlunsplit(
        ("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, query, "")
    )
    msg_id = uuid4().hex
    request = {
        "channel": "shell",
        "header": {
            "msg_id": msg_id,
            "username": "etude",
            "session": ws_session,
            "msg_type": "execute_request",
            "version": "5.3",
            "date": datetime.now(UTC).isoformat(),
        },
        "parent_header": {},
        "metadata": {},
        "content": {
            "code": code,
            "silent": True,
            "store_history": False,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        },
    }
    try:
        with connect(
            url,
            additional_headers=_proxy_headers(proxy_token),
            open_timeout=remaining(),
            close_timeout=0,
            max_size=MAX_REPLY_BYTES,
            compression=None,
            proxy=None,
            ping_interval=None,
            logger=_WS_LOG,
        ) as channel:
            call(lambda: channel.send(json.dumps(request)))
            for _ in range(_MAX_WS_MESSAGES):
                data = channel.recv(timeout=remaining())
                if not isinstance(data, str):
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                match message:
                    case {
                        "msg_type": "status",
                        "parent_header": {"msg_id": parent_id},
                        "content": {"execution_state": "busy"},
                    } if parent_id == msg_id:
                        return
        raise ProviderUnavailable("Colab kernel did not acknowledge bootstrap execution")
    except (OSError, WebSocketException, ValueError):
        raise ProviderUnavailable("Colab kernel bootstrap reply unconfirmed") from None
