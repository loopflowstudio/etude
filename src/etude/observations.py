"""Attempt-scoped authority and portable training evidence shared by publisher and receiver."""

import base64
import hmac
import re
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field

from etude.block_file import NAME
from etude.errors import EtudeError
from etude.spec import Record

MAX_FILE_BYTES = 32 * 1024 * 1024
_WRITER_RECORD = re.compile(r"metrics/[0-9a-f]{12}\.json")


def training_file(key: str) -> bool:
    return key in ("run.json", "outcome.json") or bool(
        _WRITER_RECORD.fullmatch(key) or NAME.fullmatch(key)
    )


def _origin(value: str) -> str:
    url = urlsplit(value)
    local = url.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or not (url.scheme == "https" or local and url.scheme == "http")
    ):
        raise ValueError("view URL must be an HTTPS origin or loopback HTTP origin")
    return value.rstrip("/")


ViewOrigin = Annotated[str, AfterValidator(_origin)]


class ViewGrant(Record):
    v: Literal[1] = 1
    origin: ViewOrigin
    run: str
    attempt: str
    attempt_number: int = Field(ge=1)
    exp: int

    def sign(self, secret: bytes) -> str:
        payload = base64.urlsafe_b64encode(self.model_dump_json().encode())
        return payload.decode() + "." + hmac.digest(secret, payload, "sha256").hex()

    @classmethod
    def verify(cls, token: str, secret: bytes, origin: str | None) -> "ViewGrant":
        try:
            payload, signature = token.split(".")
            expected = hmac.digest(secret, payload.encode("ascii"), "sha256").hex()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            grant = cls.model_validate_json(base64.urlsafe_b64decode(payload))
        except ValueError:
            raise EtudeError("invalid_view_grant", 401) from None
        if grant.exp <= time.time() or origin is not None and grant.origin != origin.rstrip("/"):
            raise EtudeError("view_grant_refused", 403)
        return grant


class ViewGrantTarget(Record):
    origin: ViewOrigin
    token: str = Field(repr=False)
