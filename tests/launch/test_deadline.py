"""Customer budgets compose; blocking provider I/O cannot stop rental enforcement."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

from etude.launch.deadline import call, limit_time, remaining
from etude.launch.providers.runpod.pod import GuardianAPI, guard


def test_nested_operations_spend_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0
    monkeypatch.setattr("etude.launch.deadline.time.monotonic", lambda: elapsed)
    assert remaining() is None
    with limit_time(900):
        elapsed = 125
        with limit_time(None):
            assert remaining() == 775
        with limit_time(1000):
            assert remaining() == 775
        with limit_time(10):
            assert remaining() == 10
        elapsed = 901
        with pytest.raises(TimeoutError):
            call(lambda: pytest.fail("expired operation must never start"))
    assert remaining() is None


def test_wait_expiry_leaves_one_in_flight_operation_without_replay() -> None:
    release, finished = Event(), Event()
    calls = 0

    def request() -> str:
        nonlocal calls
        calls += 1
        release.wait()
        finished.set()
        return "confirmed"

    try:
        with limit_time(0.1), pytest.raises(TimeoutError, match="unconfirmed"):
            call(request)
        assert calls == 1 and not finished.is_set()
    finally:
        release.set()
        assert finished.wait(5)
    assert calls == 1


@pytest.mark.parametrize("expired", [False, True])
def test_guardian_deletes_despite_stalled_budget_read(expired: bool) -> None:
    release, finished = Event(), Event()
    reads = 0
    deletions: list[str] = []

    class Provider(GuardianAPI):
        def budget_deadline(self, pod: str, max_usd: Decimal) -> datetime | None:
            nonlocal reads
            reads += 1
            release.wait()
            finished.set()
            return None

        def delete_self(self, pod: str) -> None:
            deletions.append(pod)

    try:
        guard(
            Provider("fixture"),
            "owned-pod",
            datetime.now(UTC) + timedelta(seconds=-1 if expired else 0.1),
            Decimal(1),
        )
        assert deletions == ["owned-pod"] and reads == (0 if expired else 1)
    finally:
        release.set()
        if not expired:
            assert finished.wait(5)
