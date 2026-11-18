"""AWS Signature Version 4 credentials and request signing for regional services."""

from __future__ import annotations

import hashlib
import hmac
import os
import urllib.parse
from typing import Literal

from etude.spec import Record
from etude.storage_timeout import StorageUnavailable


def require_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise StorageUnavailable(f"{name} is not set")
    return value


class Keys(Record):
    access_key: str
    secret_key: str
    session_token: str | None = None

    @staticmethod
    def from_environment(provider: Literal["s3", "r2"]) -> Keys:
        """The provider's standard variables: `AWS_*` for S3, `R2_*` for R2.

        Long-lived keys are needed for links that outlast an hour.
        """
        if provider == "r2":
            return Keys(
                access_key=require_environment("R2_ACCESS_KEY_ID"),
                secret_key=require_environment("R2_SECRET_ACCESS_KEY"),
            )
        return Keys(
            access_key=require_environment("AWS_ACCESS_KEY_ID"),
            secret_key=require_environment("AWS_SECRET_ACCESS_KEY"),
            session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
        )


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(text: str, safe: str = "") -> str:
    return urllib.parse.quote(text, safe="-_.~" + safe)


class SignedRequest(Record):
    url: str
    headers: dict[str, str]


class Signer:
    def __init__(self, keys: Keys, region: str) -> None:
        self.keys, self.region = keys, region

    def signing_key(self, day: str, service: str = "s3") -> bytes:
        key = ("AWS4" + self.keys.secret_key).encode()
        for part in (day, self.region, service, "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return key

    def signature(self, stamp: str, canonical_request: str, service: str = "s3") -> str:
        scope = f"{stamp[:8]}/{self.region}/{service}/aws4_request"
        to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope, _hash(canonical_request.encode())))
        key = self.signing_key(stamp[:8], service)
        return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    def credential(self, stamp: str, service: str = "s3") -> str:
        return f"{self.keys.access_key}/{stamp[:8]}/{self.region}/{service}/aws4_request"

    def request(
        self,
        method: str,
        host: str,
        key: str,
        stamp: str,
        service: str,
        body: bytes,
        query: dict[str, str] | None,
        extra: dict[str, str] | None,
    ) -> SignedRequest:
        headers = {"host": host, "x-amz-content-sha256": _hash(body), "x-amz-date": stamp}
        if self.keys.session_token:
            headers["x-amz-security-token"] = self.keys.session_token
        headers.update({name.lower(): value for name, value in (extra or {}).items()})
        names = sorted(headers)
        text_query = "&".join(
            f"{uri_encode(name)}={uri_encode(value)}"
            for name, value in sorted((query or {}).items())
        )
        canonical = "\n".join(
            (
                method,
                "/" + uri_encode(key, safe="/"),
                text_query,
                "".join(f"{name}:{headers[name].strip()}\n" for name in names),
                ";".join(names),
                headers["x-amz-content-sha256"],
            )
        )
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.credential(stamp, service)}, "
            f"SignedHeaders={';'.join(names)}, "
            f"Signature={self.signature(stamp, canonical, service)}"
        )
        url = f"https://{host}/{uri_encode(key, safe='/')}" + (f"?{text_query}" if query else "")
        del headers["host"]
        return SignedRequest(url=url, headers=headers)
