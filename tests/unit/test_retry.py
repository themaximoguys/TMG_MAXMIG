from dataclasses import dataclass

import pytest
import requests

from maxmig.retry import call_with_retry


@dataclass
class FakeResponse:
    status_code: int


def no_jitter(_low: float, _high: float) -> float:
    return 1.0


def test_call_with_retry_returns_immediately_on_success():
    calls = []

    def fn():
        calls.append(1)
        return FakeResponse(200)

    sleeps: list[float] = []
    response = call_with_retry(fn, sleep=sleeps.append, rand=no_jitter)
    assert response.status_code == 200
    assert len(calls) == 1
    assert sleeps == []


def test_call_with_retry_does_not_retry_non_retryable_status():
    calls = []

    def fn():
        calls.append(1)
        return FakeResponse(404)

    sleeps: list[float] = []
    response = call_with_retry(fn, sleep=sleeps.append, rand=no_jitter)
    assert response.status_code == 404
    assert len(calls) == 1
    assert sleeps == []


def test_call_with_retry_retries_retryable_status_then_succeeds():
    responses = iter([FakeResponse(500), FakeResponse(500), FakeResponse(200)])

    def fn():
        return next(responses)

    sleeps: list[float] = []
    response = call_with_retry(fn, sleep=sleeps.append, rand=no_jitter)
    assert response.status_code == 200
    assert sleeps == [2.0, 4.0]  # exponential backoff, base=2, multiplier=2, no jitter


def test_call_with_retry_stops_after_max_attempts_and_returns_last_response():
    def fn():
        return FakeResponse(503)

    sleeps: list[float] = []
    response = call_with_retry(fn, max_attempts=5, sleep=sleeps.append, rand=no_jitter)
    assert response.status_code == 503
    assert sleeps == [2.0, 4.0, 8.0, 16.0]  # 4 sleeps between 5 attempts


def test_call_with_retry_retries_connection_error_then_succeeds():
    attempts = {"n": 0}

    def fn():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise requests.exceptions.ConnectionError("boom")
        return FakeResponse(200)

    sleeps: list[float] = []
    response = call_with_retry(fn, sleep=sleeps.append, rand=no_jitter)
    assert response.status_code == 200
    assert attempts["n"] == 2
    assert sleeps == [2.0]


def test_call_with_retry_raises_after_exhausting_attempts_on_connection_error():
    def fn():
        raise requests.exceptions.ConnectionError("boom")

    sleeps: list[float] = []
    with pytest.raises(requests.exceptions.ConnectionError):
        call_with_retry(fn, max_attempts=3, sleep=sleeps.append, rand=no_jitter)
    assert sleeps == [2.0, 4.0]
