"""
Unit tests for retry module.
"""
import pytest
import time
import tenacity.nap as tenacity_nap

from retry import (
    run_with_retry,
    RetryError,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_MULTIPLIER,
    RETRY_MAX_DELAY,
    is_retryable_http_status,
    is_transient_s3_error,
)


def test_run_with_retry_retries_and_sleeps(monkeypatch):
    sleeps = []
    sleep_recorder = lambda s: sleeps.append(s)
    monkeypatch.setattr(tenacity_nap, "sleep", sleep_recorder)
    monkeypatch.setattr(time, "sleep", sleep_recorder)

    calls = {"count": 0}

    @run_with_retry
    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RetryError("transient")
        return "ok"

    result = flaky()

    assert result == "ok"
    assert calls["count"] == 3
    expected = [
        pytest.approx(RETRY_BASE_DELAY),
        pytest.approx(min(RETRY_BASE_DELAY * RETRY_MULTIPLIER, RETRY_MAX_DELAY)),
    ]
    assert sleeps == expected


def test_run_with_retry_exhausts_and_raises(monkeypatch):
    sleeps = []
    sleep_recorder = lambda s: sleeps.append(s)
    monkeypatch.setattr(tenacity_nap, "sleep", sleep_recorder)
    monkeypatch.setattr(time, "sleep", sleep_recorder)

    calls = {"count": 0}

    @run_with_retry
    def always_fails():
        calls["count"] += 1
        raise RetryError("boom")

    with pytest.raises(RetryError):
        always_fails()

    assert calls["count"] == RETRY_MAX_ATTEMPTS
    assert len(sleeps) == RETRY_MAX_ATTEMPTS - 1
    assert sleeps[-1] == pytest.approx(RETRY_MAX_DELAY)


def test_run_with_retry_fail_fast_on_non_retryable(monkeypatch):
    sleeps = []
    sleep_recorder = lambda s: sleeps.append(s)
    monkeypatch.setattr(tenacity_nap, "sleep", sleep_recorder)
    monkeypatch.setattr(time, "sleep", sleep_recorder)

    calls = {"count": 0}

    @run_with_retry
    def bad_call():
        calls["count"] += 1
        raise ValueError("no retry")

    with pytest.raises(ValueError):
        bad_call()

    assert calls["count"] == 1
    assert sleeps == []


def test_is_retryable_http_status():
    assert is_retryable_http_status(500) is True
    assert is_retryable_http_status(503) is True
    assert is_retryable_http_status(429) is True
    assert is_retryable_http_status(404) is False


def test_is_transient_s3_error():
    assert is_transient_s3_error("SlowDown") is True
    assert is_transient_s3_error("RequestTimeout") is True
    assert is_transient_s3_error("AccessDenied") is False
