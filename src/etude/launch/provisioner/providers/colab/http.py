"""Google Colab OAuth2 transport and Jupyter kernel WebSocket execution.

Account OAuth credentials stay on the launcher and are never sent to a runtime.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

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
_MAX_WS_FRAMES = 64


class ColabCredentials(Record):
    """Google OAuth2 credentials for Colab's backend API.

    Either a direct short-lived `access_token` (from `COLAB_ACCESS_TOKEN`), or
    an authorized-user refresh grant (`client_id`, `client_secret`,
    `refresh_token`), matching `google-colab-cli` and `gcloud` ADC JSON.
    """

    access_token: str | None = None
    expiry: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    token_uri: str = TOKEN_URI

    @classmethod
    def from_authorized_user_json(cls, raw: str) -> ColabCredentials:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Colab OAuth credentials must be a JSON object")
        token = data.get("token")
        expiry_raw = data.get("expiry")
        expiry: datetime | None = None
        if isinstance(expiry_raw, str) and expiry_raw:
            parsed = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
            expiry = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        refresh_token = data.get("refresh_token")
        if not token and not (client_id and client_secret and refresh_token):
            raise ValueError("Colab OAuth credentials lack an access or refresh token")
        return cls(
            access_token=str(token) if isinstance(token, str) and token else None,
            expiry=expiry,
            client_id=str(client_id) if isinstance(client_id, str) and client_id else None,
            client_secret=(
                str(client_secret) if isinstance(client_secret, str) and client_secret else None
            ),
            refresh_token=(
                str(refresh_token) if isinstance(refresh_token, str) and refresh_token else None
            ),
            token_uri=str(data.get("token_uri") or TOKEN_URI),
        )


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


class ColabHTTP:
    def __init__(
        self,
        credentials: ColabCredentials,
        send: Send = send_to_provider,
        *,
        domain: str = COLAB_DOMAIN,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._credentials = credentials
        self._send = send
        self._domain = domain.rstrip("/")
        self._now = now or (lambda: datetime.now(UTC))
        self._token: str | None = credentials.access_token
        self._expiry: datetime | None = credentials.expiry

    def _bearer(self) -> str:
        now = self._now()
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
        try:
            reply = call(
                lambda: self._send(
                    "POST",
                    creds.token_uri,
                    form,
                    {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
            )
        except (OSError, TimeoutError):
            raise ProviderUnavailable("Colab OAuth token refresh failed") from None
        if not 200 <= reply.status < 300 or len(reply.body) > MAX_REPLY_BYTES:
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
        url = f"{self._domain}{path}?{urllib.parse.urlencode(query)}"
        sent_headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "X-Colab-Client-Agent": AGENT,
            **(headers or {}),
        }
        try:
            reply = call(lambda: self._send(method, url, None, sent_headers))
        except (OSError, TimeoutError):
            raise ProviderUnavailable(f"Colab {method} {path}: reply unconfirmed") from None
        if len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Colab {method} {path}: reply too large")
        return reply

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
        body = reply.body
        return body[len(XSSI_PREFIX) :] if body.startswith(XSSI_PREFIX) else body

    def runtime_request(
        self,
        method: str,
        base_url: str,
        path: str,
        proxy_token: str,
        *,
        body: bytes | None = None,
    ) -> bytes:
        url = f"{base_url.rstrip('/')}{path}"
        headers = {
            "Authorization": f"Bearer {proxy_token}",
            "X-Colab-Runtime-Proxy-Token": proxy_token,
            "X-Colab-Client-Agent": AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            reply = call(lambda: self._send(method, url, body, headers))
        except (OSError, TimeoutError):
            raise ProviderUnavailable(f"Colab runtime {method} {path}: reply unconfirmed") from None
        if not 200 <= reply.status < 300 or len(reply.body) > MAX_REPLY_BYTES:
            raise ProviderUnavailable(f"Colab runtime {method} {path}: status {reply.status}")
        return reply.body


def _ws_frame_text(text: str) -> bytes:
    """Encode one masked client-to-server RFC 6455 text frame."""
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    return bytes(header) + masked


def _ws_frame_pong(payload: bytes) -> bytes:
    mask = os.urandom(4)
    header = bytearray([0x8A, 0x80 | len(payload)])
    header.extend(mask)
    masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    return bytes(header) + masked


def _read_exact(sock: socket.socket, buf: bytearray, count: int) -> bytes:
    while len(buf) < count:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("Colab kernel WebSocket closed before acknowledgement")
        buf.extend(chunk)
    out = bytes(buf[:count])
    del buf[:count]
    return out


def _read_ws_frame(sock: socket.socket, buf: bytearray) -> tuple[int, bytes]:
    b1, b2 = _read_exact(sock, buf, 2)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, buf, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, buf, 8))[0]
    if length > MAX_REPLY_BYTES:
        raise ValueError("Colab kernel WebSocket frame exceeded size limit")
    mask = _read_exact(sock, buf, 4) if masked else None
    data = _read_exact(sock, buf, length)
    if mask is not None:
        data = bytes(byte ^ mask[index & 3] for index, byte in enumerate(data))
    return opcode, data


def execute_in_kernel(base_url: str, proxy_token: str, kernel_id: str, code: str) -> None:
    """Submit code to a Colab Jupyter kernel and wait until execution enters `busy`."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host or parsed.scheme not in {"http", "https"}:
        raise ProviderUnavailable("Colab runtime proxy URL is invalid")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    timeout = remaining()
    if timeout is not None and timeout <= 0:
        raise ProviderUnavailable("Colab kernel bootstrap deadline reached")
    ws_session = uuid4().hex
    path = (
        f"{parsed.path.rstrip('/')}/api/kernels/{urllib.parse.quote(kernel_id, safe='')}/channels"
        f"?colab-runtime-proxy-token={urllib.parse.quote(proxy_token, safe='')}"
        f"&session_id={ws_session}"
    )
    ws_key = base64.b64encode(os.urandom(16)).decode()
    sock: socket.socket | None = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        sock = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            if parsed.scheme == "https"
            else raw
        )
        sock.settimeout(timeout)
        host_header = f"{host}:{port}" if parsed.port else host
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {proxy_token}\r\n"
            f"X-Colab-Runtime-Proxy-Token: {proxy_token}\r\n"
            f"X-Colab-Client-Agent: {AGENT}\r\n"
            f"\r\n"
        ).encode()
        sock.sendall(handshake)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise EOFError("Colab kernel WebSocket handshake closed")
            buf.extend(chunk)
        sep = buf.index(b"\r\n\r\n")
        status_line = bytes(buf[:sep]).split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        del buf[: sep + 4]
        if " 101 " not in status_line:
            raise ProviderUnavailable(f"Colab kernel WebSocket rejected: {status_line}")

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
        sock.sendall(_ws_frame_text(json.dumps(request)))

        for _ in range(_MAX_WS_FRAMES):
            left = remaining()
            if left is not None:
                if left <= 0:
                    raise TimeoutError("Colab kernel bootstrap deadline reached")
                sock.settimeout(left)
            opcode, data = _read_ws_frame(sock, buf)
            if opcode == 0x8:
                raise ProviderUnavailable("Colab kernel closed before starting bootstrap")
            if opcode == 0x9 and len(data) < 126:
                sock.sendall(_ws_frame_pong(data))
                continue
            if opcode != 0x1:
                continue
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            parent = message.get("parent_header")
            content = message.get("content")
            if (
                isinstance(parent, dict)
                and parent.get("msg_id") == msg_id
                and message.get("msg_type") == "status"
                and isinstance(content, dict)
                and content.get("execution_state") == "busy"
            ):
                return
        raise ProviderUnavailable("Colab kernel did not acknowledge bootstrap execution")
    except ProviderUnavailable:
        raise
    except (OSError, EOFError, ValueError, TimeoutError):
        raise ProviderUnavailable("Colab kernel bootstrap reply unconfirmed") from None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
