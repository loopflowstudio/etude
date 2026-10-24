"""Request signing against the examples Amazon publishes; nothing here reaches the network."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs

import pytest

from etude.launch.records import Storage as Place
from etude.launch.storage.s3 import (
    AlreadyThere,
    Bucket,
    Denied,
    Keys,
    Reply,
    StorageUnavailable,
    open_bucket,
    post,
)

# From "Signature Calculations for the Authorization Header" in the S3 API reference.
KEYS = Keys(
    access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
)
WHEN = datetime(2013, 5, 24, tzinfo=UTC)


class Storage:
    def __init__(self, replies: list[Reply]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        self.asked.append((method, url, body, headers))
        return self.replies.pop(0)


def bucket(storage: Storage) -> Bucket:
    return Bucket(
        "examplebucket",
        "us-east-1",
        KEYS,
        storage,
        clock=lambda: WHEN,
        host="examplebucket.s3.amazonaws.com",
    )


def test_a_get_is_signed_as_amazon_documents() -> None:
    storage = Storage(replies=[Reply(status=200, body=b"hello")])
    assert bucket(storage)._ask("GET", "test.txt", extra={"Range": "bytes=0-9"}) == Reply(
        status=200, body=b"hello"
    )
    ((method, url, _, headers),) = storage.asked
    assert (method, url) == ("GET", "https://examplebucket.s3.amazonaws.com/test.txt")
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_a_listing_is_signed_as_amazon_documents_and_follows_pages() -> None:
    page = (
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<IsTruncated>{more}</IsTruncated>{token}<Contents><Key>{key}</Key></Contents>"
        "</ListBucketResult>"
    )
    first = page.format(
        more="true", token="<NextContinuationToken>t1</NextContinuationToken>", key="a/1"
    )
    storage = Storage(
        replies=[
            Reply(status=200, body=first.encode()),
            Reply(status=200, body=page.format(more="false", token="", key="a/2").encode()),
        ]
    )
    assert bucket(storage).list("a/") == ["a/1", "a/2"]
    assert "continuation-token=t1" in storage.asked[1][1]
    # Amazon's listing example signs max-keys=2&prefix=J; the same request through `_ask`:
    again = Storage(replies=[Reply(status=200, body=b"")])
    bucket(again)._ask("GET", "", query={"max-keys": "2", "prefix": "J"})
    assert again.asked[0][3]["authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def test_a_reading_link_is_signed_as_amazon_documents() -> None:
    assert bucket(Storage(replies=[])).get_url("test.txt", 86400) == (
        "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


def test_put_once_get_and_absence_mean_what_they_say() -> None:
    storage = Storage(replies=[Reply(status=s, body=b"") for s in (200, 412, 404, 503, 503, 503)])
    held = bucket(storage)
    held.put("r/packet.json", b"{}", once=True)
    assert storage.asked[0][3]["if-none-match"] == "*" and storage.asked[0][2] == b"{}"
    with pytest.raises(AlreadyThere):
        held.put("r/packet.json", b"{}", once=True)
    assert held.get("r/missing") is None
    with pytest.raises(StorageUnavailable):  # an error is never read as "absent"
        held.get("r/packet.json")


def test_a_writing_form_is_limited_to_its_folder_and_its_time() -> None:
    form = bucket(Storage(replies=[])).post_form("etude/r1/out/", 3600, max_bytes=1000)
    policy = json.loads(base64.b64decode(form.fields["policy"]))
    assert policy["expiration"] == "2013-05-24T01:00:00Z"
    assert ["starts-with", "$key", "etude/r1/out/"] in policy["conditions"]
    assert ["content-length-range", 0, 1000] in policy["conditions"]
    storage = Storage(replies=[Reply(status=204, body=b"")])
    post(form, "etude/r1/out/log", b"line\n", storage)
    sent = storage.asked[0][2] or b""
    assert b'name="key"\r\n\r\netude/r1/out/log\r\n' in sent and sent.count(b"line\n") == 1
    assert sent.index(b'name="file"') > sent.index(b'name="policy"')  # the file goes last


GRANTED = (
    b'<GetFederationTokenResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
    b"<GetFederationTokenResult><Credentials><SessionToken> token </SessionToken>"
    b"<SecretAccessKey>secret</SecretAccessKey><AccessKeyId>ASIA</AccessKeyId>"
    b"</Credentials></GetFederationTokenResult></GetFederationTokenResponse>"
)


def test_an_s3_grant_asks_sts_for_keys_to_one_folder_and_brings_a_form() -> None:
    storage = Storage([Reply(status=200, body=GRANTED)])
    grant = bucket(storage).grant("etude/r1/out/", 13 * 3600, "r1/with spaces")
    assert grant.bucket is not None and grant.form is not None
    assert grant.bucket.keys == Keys(access_key="ASIA", secret_key="secret", session_token="token")
    assert (grant.prefix, grant.bucket.host) == ("etude/r1/out/", "examplebucket.s3.amazonaws.com")
    ((method, url, body, headers),) = storage.asked
    assert (method, url) == ("POST", "https://sts.us-east-1.amazonaws.com/")
    assert "/us-east-1/sts/aws4_request" in headers["authorization"]
    form = {name: values[0] for name, values in parse_qs((body or b"").decode()).items()}
    assert form["Action"] == "GetFederationToken" and form["DurationSeconds"] == "46800"
    assert form["Name"] == "etude-r1withspaces"
    (statement,) = json.loads(form["Policy"])["Statement"]
    assert statement["Resource"] == "arn:aws:s3:::examplebucket/etude/r1/out/*"
    assert "s3:DeleteObject" in statement["Action"] and "s3:GetObject" not in statement["Action"]


def test_an_s3_grant_is_a_form_alone_when_sts_refuses_or_the_caller_holds_temporary_keys() -> None:
    refused = bucket(Storage([Reply(status=403, body=b"<ErrorResponse/>")])).grant("p/", 3600, "r")
    assert refused.bucket is None and refused.form is not None
    unasked = Storage([])
    borrowed = Bucket(
        "b", "us-east-1", Keys(access_key="ASIA", secret_key="s", session_token="session"), unasked
    )
    alone = borrowed.grant("p/", 3600, "r")
    assert alone.bucket is None and alone.form is not None and unasked.asked == []


def r2(storage: Storage, token: str | None = "cloudflare-token") -> Bucket:
    return Bucket(
        "manabot", "auto", KEYS, storage, clock=lambda: WHEN, account="0123abcd", token=token
    )


def test_an_r2_grant_is_cloudflare_keys_for_one_folder_and_no_form() -> None:
    issued = {"accessKeyId": "temp", "secretAccessKey": "s", "sessionToken": "t"}
    storage = Storage([Reply(status=200, body=json.dumps({"result": issued}).encode())])
    grant = r2(storage).grant("launch/run1/", 13 * 3600, "run1")
    assert grant.form is None and grant.bucket is not None
    assert grant.bucket.keys == Keys(access_key="temp", secret_key="s", session_token="t")
    assert (grant.bucket.host, grant.bucket.region) == (
        "manabot.0123abcd.r2.cloudflarestorage.com",
        "auto",
    )
    ((method, url, body, headers),) = storage.asked
    assert (method, url) == (
        "POST",
        "https://api.cloudflare.com/client/v4/accounts/0123abcd/r2/temp-access-credentials",
    )
    assert headers["Authorization"] == "Bearer cloudflare-token"
    assert json.loads(body or b"") == {
        "bucket": "manabot",
        "parentAccessKeyId": KEYS.access_key,
        "permission": "object-read-write",
        "ttlSeconds": 13 * 3600,
        "prefixes": ["launch/run1/"],
    }
    # The machine's bucket signs for R2's region at R2's host.
    held = Storage([Reply(status=200, body=b"")])
    Bucket(
        grant.bucket.name, grant.bucket.region, grant.bucket.keys, held, host=grant.bucket.host
    ).put("launch/run1/x", b"x")
    assert held.asked[0][1] == "https://manabot.0123abcd.r2.cloudflarestorage.com/launch/run1/x"
    assert "/auto/s3/aws4_request" in held.asked[0][3]["authorization"]


def test_an_r2_grant_that_cannot_be_issued_fails_rather_than_degrading() -> None:
    with pytest.raises(StorageUnavailable, match="R2_API_TOKEN"):
        r2(Storage([]), token=None).grant("p/", 3600, "r")
    with pytest.raises(Denied):
        r2(Storage([Reply(status=403, body=b"{}")])).grant("p/", 3600, "r")
    with pytest.raises(StorageUnavailable, match="unreadable"):
        r2(Storage([Reply(status=200, body=b'{"result": {}}')])).grant("p/", 3600, "r")


def test_one_chunk_is_one_put_and_several_are_a_multipart_upload() -> None:
    single = Storage([Reply(status=200, body=b"")])
    bucket(single).put_parts("a", [b"small"])
    assert [(method, body) for method, _, body, _ in single.asked] == [("PUT", b"small")]

    started = (
        b"<InitiateMultipartUploadResult><UploadId>u1</UploadId></InitiateMultipartUploadResult>"
    )
    storage = Storage(
        [
            Reply(status=200, body=started),
            Reply(status=200, body=b"", headers={"etag": '"t1"'}),
            Reply(status=200, body=b"", headers={"etag": '"t2"'}),
            Reply(status=200, body=b"<Done/>"),
        ]
    )
    bucket(storage).put_parts("big", [b"first", b"second"])
    assert len(storage.asked) == 4  # Start, two acknowledged parts, complete; no listing.
    asked = [(method, url.partition("/big")[2]) for method, url, _, _ in storage.asked]
    assert asked == [
        ("POST", "?uploads="),
        ("PUT", "?partNumber=1&uploadId=u1"),
        ("PUT", "?partNumber=2&uploadId=u1"),
        ("POST", "?uploadId=u1"),
    ]
    # AWS refuses the finishing request's signature if it goes as a web form.
    assert storage.asked[-1][3]["content-type"] == "application/xml"
    finish = (storage.asked[-1][2] or b"").decode()
    assert finish.index('<ETag>"t1"</ETag>') < finish.index('<ETag>"t2"</ETag>')


def test_a_failed_part_abandons_the_upload_and_refused_keys_are_told_apart() -> None:
    started = b"<R><UploadId>u1</UploadId></R>"
    storage = Storage(
        [
            Reply(status=200, body=started),
            Reply(status=200, body=b"", headers={"etag": '"t1"'}),
            Reply(status=500, body=b""),
            Reply(status=204, body=b""),
        ]
    )
    with pytest.raises(StorageUnavailable):
        bucket(storage).put_parts("big", [b"first", b"second"])
    assert (storage.asked[-1][0], storage.asked[-1][1].partition("/big")[2]) == (
        "DELETE",
        "?uploadId=u1",
    )
    held = bucket(Storage([Reply(status=204, body=b""), Reply(status=403, body=b"")]))
    held.delete("gone")  # deleting what is not there is not an error
    with pytest.raises(Denied):
        held.delete("kept")


def test_multipart_failure_does_not_read_the_remaining_checkpoint() -> None:
    read = 0

    def chunks() -> Iterator[bytes]:
        nonlocal read
        for _ in range(100):
            read += 1
            yield b"chunk"

    storage = Storage(
        [
            Reply(status=200, body=b"<R><UploadId>u1</UploadId></R>"),
            Reply(status=500, body=b""),
            Reply(status=204, body=b""),
        ]
    )
    with pytest.raises(StorageUnavailable):
        bucket(storage).put_parts("checkpoint", chunks())
    assert read == 2  # The one-chunk lookahead chooses PUT versus multipart.
    assert len(storage.asked) == 3  # Start, failed first part, abandon.


def test_missing_part_etag_abandons_upload() -> None:
    storage = Storage(
        [
            Reply(status=200, body=b"<R><UploadId>u1</UploadId></R>"),
            Reply(status=200, body=b""),
            Reply(status=204, body=b""),
        ]
    )
    with pytest.raises(StorageUnavailable, match="missing ETag"):
        bucket(storage).put_parts("big", [b"first", b"second"])
    assert storage.asked[-1][0] == "DELETE"


def test_startup_write_capability_is_method_and_object_specific() -> None:
    storage = Storage(replies=[])
    held = bucket(storage)
    write_url = held.put_url("sessions/s/out/bootstrap.log", 300)
    read_url = held.get_url("sessions/s/out/bootstrap.log", 300)
    other_url = held.put_url("sessions/s/out/worker.log", 300)
    write_query = parse_qs(write_url.split("?", 1)[1])
    assert write_query["X-Amz-Expires"] == ["300"]
    assert write_query["X-Amz-SignedHeaders"] == ["host"]
    signature = write_query["X-Amz-Signature"]
    assert signature != parse_qs(read_url.split("?", 1)[1])["X-Amz-Signature"]
    assert signature != parse_qs(other_url.split("?", 1)[1])["X-Amz-Signature"]
    assert storage.asked == []


def test_a_storage_opens_its_providers_bucket_with_keys_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_API_TOKEN"):
        monkeypatch.setenv(name, "fixture")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    r2 = open_bucket(Place(name="r", bucket="manabot", prefix="launch", provider="r2"))
    s3 = open_bucket(Place(name="s", bucket="runs", prefix="launch", region="us-west-2"))
    assert (r2.host, r2.region) == ("manabot.fixture.r2.cloudflarestorage.com", "auto")
    assert (s3.host, s3.region) == ("runs.s3.us-west-2.amazonaws.com", "us-west-2")
    monkeypatch.delenv("R2_ACCOUNT_ID")
    with pytest.raises(StorageUnavailable, match="R2_ACCOUNT_ID"):
        open_bucket(Place(name="r", bucket="manabot", prefix="launch", provider="r2"))
    with pytest.raises(ValueError, match="region"):
        Place(name="r", bucket="manabot", prefix="launch", provider="r2", region="us-west-2")
