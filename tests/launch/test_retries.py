"""Count physical storage reads and preserve permanent failures and caller deadlines."""

from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self

import pytest

from etude.launch.deadline import limit_time, remaining
from etude.launch.logging import log_to
from etude.launch.storage.s3 import Bucket, Reply, StorageUnavailable, read
from etude.launch.worker.courier import _run
from tests.launch.test_storage_s3 import KEYS


class Transport:
    def __init__(self, replies: list[tuple[int, bytes] | OSError]) -> None:
        self.replies = replies
        self.requests = 0

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        self.requests += 1
        reply = self.replies.pop(0)
        if isinstance(reply, OSError):
            raise reply
        return Reply(status=reply[0], body=reply[1])


@pytest.mark.parametrize("linked", [True, False])
def test_transient_reads_recover_with_one_retry_owner(tmp_path: Path, linked: bool) -> None:
    transport = Transport([OSError("must-not-retain-this-text"), (503, b""), (200, b"checkpoint")])
    path = tmp_path / "log"
    with log_to(path):
        result = (
            read("https://storage.invalid/object?token=private", transport)
            if linked
            else Bucket("b", "r", KEYS, transport).get("object")
        )
    assert result == b"checkpoint" and transport.requests == 3
    text = path.read_text()
    assert "attempt 1/3" in text and "attempt 2/3" in text
    assert "private" not in text and "must-not-retain" not in text


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_replies_are_not_retried(status: int) -> None:
    transport = Transport([(status, b"")])
    if status == 404:
        assert read("https://storage.invalid", transport) is None
    else:
        with pytest.raises(StorageUnavailable, match=str(status)):
            read("https://storage.invalid", transport)
    assert transport.requests == 1


def test_exhausted_reads_remain_failures_and_writes_are_not_replayed() -> None:
    transport = Transport([(429, b""), (502, b""), (504, b"")])
    with pytest.raises(StorageUnavailable, match="504"):
        read("https://storage.invalid", transport)
    assert transport.requests == 3
    replies: list[tuple[int, bytes] | OSError] = [(503, b""), OSError("reply lost")]
    for reply in replies:
        writing = Transport([reply])
        with pytest.raises(StorageUnavailable):
            Bucket("b", "r", KEYS, writing).put("object", b"data")
        assert writing.requests == 1


def test_retry_backoff_and_nested_reads_share_callers_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("etude.launch.deadline.time.monotonic", lambda: now[0])
    monkeypatch.setattr("etude.launch.storage.s3.time.sleep", sleep)
    transport = Transport([(503, b""), (503, b"")])
    with limit_time(0.15):
        with limit_time(100):
            assert remaining() == 0.15
            with pytest.raises(StorageUnavailable, match="deadline"):
                read("https://storage.invalid", transport)
    assert transport.requests == 2 and now[0] == 0.15


def test_bootstrap_retries_cannot_renew_startup_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    start = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(start.timestamp() + now[0], tz)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    requests: list[float] = []

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        left = remaining()
        assert left is not None
        requests.append(left)
        now[0] += 0.01
        return Reply(status=503, body=b"")

    monkeypatch.setattr("etude.launch.environment.datetime", Clock)
    monkeypatch.setattr("etude.launch.deadline.time.monotonic", lambda: now[0])
    monkeypatch.setattr("etude.launch.storage.s3.time.sleep", sleep)
    monkeypatch.setenv("ETUDE_STARTUP_DEADLINE", (start + timedelta(seconds=0.2)).isoformat())
    monkeypatch.delenv("ETUDE_DEADLINE", raising=False)
    with log_to(tmp_path / "log"), pytest.raises(StorageUnavailable, match="deadline"):
        _run("https://storage.invalid/bootstrap", tmp_path, send, sleep, 0.1)
    assert len(requests) == 2 and max(requests) <= 0.2
    assert not list(tmp_path.glob("*/receipt"))


@pytest.mark.parametrize("status", [403, 503])
def test_bootstrap_does_not_add_an_outer_retry_loop(tmp_path: Path, status: int) -> None:
    transport = Transport([(status, b"")] * 3)
    with log_to(tmp_path / "log"), pytest.raises(StorageUnavailable, match=str(status)):
        _run("https://storage.invalid/bootstrap", tmp_path, transport, lambda seconds: None, 0.1)
    assert transport.requests == (1 if status == 403 else 3)


def test_retry_diagnostics_leave_result_stdout_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ETUDE_LOG_PATH", raising=False)
    transport = Transport([(503, b""), (200, b"result")])
    body = read("https://storage.invalid", transport)
    assert body is not None
    print(body.decode())
    captured = capsys.readouterr()
    assert captured.out == "result\n"
    assert "attempt 1/3" in captured.err and transport.requests == 2
