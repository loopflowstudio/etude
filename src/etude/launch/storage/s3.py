"""An S3 bucket through plain HTTPS, signed with AWS Signature Version 4.

Standard library only. It does what a session's records need and no more:
put, get and list objects; make a link that reads one object for a while; and
make a form that lets its holder write under one folder for a while. The link
and the form carry no keys, so a machine can be handed them and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain

from etude.launch.log_read import remaining
from etude.launch.measurement import timed
from etude.launch.records import Record

# Sends one request and returns the status and body. Raises OSError when no reply came.
Send = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
WEEK = 7 * 24 * 3600  # the longest a link or form signed with these keys may last
GRANT_LIMIT = 36 * 3600
PART_BYTES = 64 * 1024**2


class StorageUnavailable(Exception):
    """No usable answer from storage. This never means an object is absent."""


class Denied(StorageUnavailable):
    """Storage refused the keys: they have expired or may not do this."""


def _failed(what: str, status: int) -> StorageUnavailable:
    return (Denied if status == 403 else StorageUnavailable)(f"{what}: status {status}")


class AlreadyThere(Exception):
    """An object that must be written only once has been written before."""


class Keys(Record):
    access_key: str
    secret_key: str
    session_token: str | None = None

    @staticmethod
    def from_environment() -> Keys:
        """The standard AWS variables. Long-lived keys are needed for links that outlast an hour."""
        try:
            return Keys(
                access_key=os.environ["AWS_ACCESS_KEY_ID"],
                secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                session_token=os.environ.get("AWS_SESSION_TOKEN") or None,
            )
        except KeyError as missing:
            raise StorageUnavailable(f"{missing.args[0]} is not set") from None


class PostForm(Record):
    """Where to POST a file, and the fields that must go with it. `key` is the writer's to choose."""

    url: str
    fields: dict[str, str]
    prefix: str  # every key written with this form must start with it


def _send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as reply:  # noqa: S310
            return reply.status, reply.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quote(text: str, safe: str = "") -> str:
    return urllib.parse.quote(text, safe="-_.~" + safe)


class Bucket:
    def __init__(
        self,
        name: str,
        region: str,
        keys: Keys,
        send: Send = _send,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        host: str | None = None,
    ) -> None:
        self.name, self.region, self._keys = name, region, keys
        self._send, self._clock = send, clock
        self.host = host or f"{name}.s3.{region}.amazonaws.com"

    def _signing_key(self, day: str, service: str = "s3") -> bytes:
        key = ("AWS4" + self._keys.secret_key).encode()
        for part in (day, self.region, service, "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return key

    def _signature(self, stamp: str, canonical_request: str, service: str = "s3") -> str:
        scope = f"{stamp[:8]}/{self.region}/{service}/aws4_request"
        to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope, _hash(canonical_request.encode())))
        key = self._signing_key(stamp[:8], service)
        return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    def _credential(self, stamp: str, service: str = "s3") -> str:
        return f"{self._keys.access_key}/{stamp[:8]}/{self.region}/{service}/aws4_request"

    def _stamp(self) -> str:
        return self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    @timed("bucket.ask")
    def _ask(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra: dict[str, str] | None = None,
        service: str = "s3",
    ) -> tuple[int, bytes]:
        """One signed request, to the bucket or, with `service`, to another AWS service."""
        host = self.host if service == "s3" else f"{service}.{self.region}.amazonaws.com"
        stamp = self._stamp()
        headers = {"host": host, "x-amz-content-sha256": _hash(body), "x-amz-date": stamp}
        if self._keys.session_token:
            headers["x-amz-security-token"] = self._keys.session_token
        headers.update({name.lower(): value for name, value in (extra or {}).items()})
        names = sorted(headers)
        text_query = "&".join(
            f"{_quote(name)}={_quote(value)}" for name, value in sorted((query or {}).items())
        )
        canonical = "\n".join(
            (
                method,
                "/" + _quote(key, safe="/"),
                text_query,
                "".join(f"{name}:{headers[name].strip()}\n" for name in names),
                ";".join(names),
                headers["x-amz-content-sha256"],
            )
        )
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._credential(stamp, service)}, "
            f"SignedHeaders={';'.join(names)}, "
            f"Signature={self._signature(stamp, canonical, service)}"
        )
        url = f"https://{host}/{_quote(key, safe='/')}" + (f"?{text_query}" if query else "")
        del headers["host"]
        try:
            return self._send(method, url, body if method in ("PUT", "POST") else None, headers)
        except OSError:
            raise StorageUnavailable(f"{method} {key or '(list)'}: no reply") from None

    def put(self, key: str, body: bytes, *, once: bool = False) -> None:
        """Write an object. With `once`, refuse if it was ever written: raises AlreadyThere."""
        status, _ = self._ask("PUT", key, body=body, extra={"if-none-match": "*"} if once else None)
        if once and status == 412:
            raise AlreadyThere(key)
        if status != 200:
            raise _failed(f"PUT {key}", status)

    def delete(self, key: str) -> None:
        """Remove an object. Removing one that is not there succeeds, as S3 defines it."""
        status, _ = self._ask("DELETE", key)
        if status not in (200, 204):
            raise _failed(f"DELETE {key}", status)

    def put_parts(self, key: str, chunks: Iterable[bytes]) -> None:
        """Write chunks without reading the whole file into memory.

        One chunk is an ordinary put. More are sent as the parts of a multipart
        upload, so every chunk but the last must be at least 5 MiB. A failed
        upload is abandoned, which frees the parts already stored.
        """
        waiting: Iterator[bytes] = iter(chunks)
        first = next(waiting, b"")
        second = next(waiting, None)
        if second is None:
            self.put(key, first)
            return
        status, body = self._ask("POST", key, query={"uploads": ""})
        upload = _text(body, "UploadId") if status == 200 else None
        if upload is None:
            raise _failed(f"start upload of {key}", status)
        try:
            number = 0
            for number, chunk in enumerate(chain((first, second), waiting), start=1):
                status, _ = self._ask(
                    "PUT", key, query={"partNumber": str(number), "uploadId": upload}, body=chunk
                )
                if status != 200:
                    raise _failed(f"PUT {key} part {number}", status)
            # The reply to each part carries its tag in a header this module's
            # `Send` does not return, so the tags are read back in one listing.
            tags: dict[int, str] = {}
            marker = "0"
            while len(tags) < number:
                status, body = self._ask(
                    "GET", key, query={"uploadId": upload, "part-number-marker": marker}
                )
                listed = _parts(body) if status == 200 else {}
                if not listed:
                    raise StorageUnavailable(f"list parts of {key}: status {status}")
                tags |= listed
                marker = str(max(listed))
            done = "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{tags[n]}</ETag></Part>"
                for n in sorted(tags)
            )
            # Without a content type of its own, urllib labels a POST body as a
            # web form, and S3 then refuses the signature (seen against AWS).
            status, body = self._ask(
                "POST",
                key,
                query={"uploadId": upload},
                body=f"<CompleteMultipartUpload>{done}</CompleteMultipartUpload>".encode(),
                extra={"content-type": "application/xml"},
            )
            # S3 can answer 200 and report the failure in the body.
            if status != 200 or b"<Error>" in body:
                raise StorageUnavailable(f"finish upload of {key}: status {status}")
        except StorageUnavailable:
            try:
                self._ask("DELETE", key, query={"uploadId": upload})
            except StorageUnavailable:
                pass  # the bucket's own rule for unfinished uploads is the backstop
            raise

    def grant(self, prefix: str, seconds: int, name: str) -> Keys | None:
        """Temporary keys that can write, send in parts and delete under `prefix` only.

        None when AWS will not issue them: the caller is not an IAM user (keys
        that are themselves temporary cannot ask), or lacks
        `sts:GetFederationToken`. The keys can do no more than the caller can.
        """
        if self._keys.session_token:
            return None
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": f"arn:aws:s3:::{self.name}/{prefix}*",
                }
            ],
        }
        form = {
            "Action": "GetFederationToken",
            "Version": "2011-06-15",
            "Name": ("etude-" + "".join(c for c in name if c.isalnum() or c in "=,.@-"))[:32],
            "DurationSeconds": str(max(900, min(seconds, GRANT_LIMIT))),
            "Policy": json.dumps(policy, separators=(",", ":")),
        }
        try:
            status, body = self._ask(
                "POST",
                "",
                body=urllib.parse.urlencode(form).encode(),
                extra={"content-type": "application/x-www-form-urlencoded"},
                service="sts",
            )
        except StorageUnavailable:
            return None  # the form does not depend on STS answering
        if status != 200:
            return None
        access, secret, token = (
            _text(body, field) for field in ("AccessKeyId", "SecretAccessKey", "SessionToken")
        )
        if not (access and secret and token):
            return None
        return Keys(access_key=access, secret_key=secret, session_token=token)

    def get(self, key: str) -> bytes | None:
        """An object's bytes, or None when storage says it does not exist."""
        status, body = self._ask("GET", key)
        if status == 404:
            return None
        if status != 200:
            raise StorageUnavailable(f"GET {key}: status {status}")
        return body

    def list(self, prefix: str) -> list[str]:
        """Every key under a prefix, in order."""
        keys: list[str] = []
        query = {"list-type": "2", "prefix": prefix}
        for _ in range(1000):
            status, body = self._ask("GET", "", query=query)
            if status != 200:
                raise StorageUnavailable(f"list {prefix}: status {status}")
            try:
                page = ET.fromstring(body)  # noqa: S314
            except ET.ParseError:
                raise StorageUnavailable(f"list {prefix}: unreadable reply") from None
            keys += [node.text or "" for node in page.iter(f"{_NS}Key")]
            more = page.findtext(f"{_NS}NextContinuationToken")
            if page.findtext(f"{_NS}IsTruncated") != "true" or not more:
                return keys
            query["continuation-token"] = more
        raise StorageUnavailable(f"list {prefix}: did not end")

    def put_url(self, key: str, seconds: int) -> str:
        """Write-only capability for exactly one object, without a prefix grant."""
        return self._url(key, seconds, "PUT")

    def get_url(self, key: str, seconds: int) -> str:
        return self._url(key, seconds, "GET")

    def _url(self, key: str, seconds: int, method: str) -> str:
        """A capability for one method and object, valid for `seconds`."""
        stamp = self._stamp()
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": self._credential(stamp),
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(min(seconds, WEEK)),
            "X-Amz-SignedHeaders": "host",
        }
        if self._keys.session_token:
            query["X-Amz-Security-Token"] = self._keys.session_token
        text_query = "&".join(f"{_quote(n)}={_quote(v)}" for n, v in sorted(query.items()))
        path = "/" + _quote(key, safe="/")
        canonical = "\n".join(
            (method, path, text_query, f"host:{self.host}\n", "host", "UNSIGNED-PAYLOAD")
        )
        signature = self._signature(stamp, canonical)
        return f"https://{self.host}{path}?{text_query}&X-Amz-Signature={signature}"

    def post_form(self, prefix: str, seconds: int, max_bytes: int = 5 * 1024**3) -> PostForm:
        """A form that writes objects under `prefix` for `seconds`, and nowhere else."""
        stamp = self._stamp()
        fields = {
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": self._credential(stamp),
            "x-amz-date": stamp,
        }
        if self._keys.session_token:
            fields["x-amz-security-token"] = self._keys.session_token
        expires = self._clock().astimezone(UTC) + timedelta(seconds=min(seconds, WEEK))
        policy = {
            "expiration": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conditions": [
                {"bucket": self.name},
                ["starts-with", "$key", prefix],
                ["content-length-range", 0, max_bytes],
                *({name: value} for name, value in fields.items()),
            ],
        }
        encoded = base64.b64encode(json.dumps(policy).encode()).decode()
        signature = hmac.new(
            self._signing_key(stamp[:8]), encoded.encode(), hashlib.sha256
        ).hexdigest()
        return PostForm(
            url=f"https://{self.host}/",
            fields={**fields, "policy": encoded, "x-amz-signature": signature},
            prefix=prefix,
        )


def _nodes(body: bytes, name: str) -> list[ET.Element]:
    """Every element called `name` in an XML reply, whatever namespace it is in."""
    try:
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError:
        return []
    return [node for node in root.iter() if node.tag.rpartition("}")[2] == name]


def _text(body: bytes, name: str) -> str | None:
    found = _nodes(body, name)
    return (found[0].text or "").strip() or None if found else None


def _parts(body: bytes) -> dict[int, str]:
    """Part number to tag, from one page of a multipart upload's parts."""
    listed: dict[int, str] = {}
    for part in _nodes(body, "Part"):
        fields = {child.tag.rpartition("}")[2]: (child.text or "").strip() for child in part}
        listed[int(fields["PartNumber"])] = fields["ETag"]
    return listed


@timed("bucket.post")
def post(form: PostForm, key: str, body: bytes, send: Send = _send) -> None:
    """Write one object with a form. The key must start with the form's prefix."""
    if not key.startswith(form.prefix):
        raise ValueError(f"{key} is outside {form.prefix}")
    boundary = "etude" + hashlib.sha256(key.encode()).hexdigest()[:24]
    parts: list[bytes] = []
    for name, value in {"key": key, **form.fields}.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="file"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    payload = b"".join(parts) + body + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    try:
        status, _ = send("POST", form.url, payload, headers)
    except OSError:
        raise StorageUnavailable(f"POST {key}: no reply") from None
    if status not in (200, 201, 204):
        raise StorageUnavailable(f"POST {key}: status {status}")


@timed("bucket.read")
def read(url: str, send: Send = _send) -> bytes | None:
    """Follow a reading link. None when storage says the object does not exist."""
    try:
        status, body = send("GET", url, None, {})
    except OSError:
        raise StorageUnavailable("GET by link: no reply") from None
    if status == 404:
        return None
    if status != 200:
        raise StorageUnavailable(f"GET by link: status {status}")
    return body
