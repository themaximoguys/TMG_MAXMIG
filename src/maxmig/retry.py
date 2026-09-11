"""Shared retry/backoff policy for transient Maximo API failures.

Retryable: HTTP 429/500/502/503/504 and requests connection/timeout errors.
Non-retryable (400/401/403/404/409/422, etc.) are returned/raised immediately —
retrying a request that will never succeed just burns the retry budget.
"""

from __future__ import annotations

import random
import time
from typing import Callable

import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MULTIPLIER = 2.0
DEFAULT_MAX_DELAY = 60.0
DEFAULT_JITTER = 0.2


def compute_backoff(
    attempt: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    multiplier: float = DEFAULT_MULTIPLIER,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    rand: Callable[[float, float], float] = random.uniform,
) -> float:
    """Exponential backoff with +/-jitter, capped at max_delay. attempt is 1-based."""
    delay = min(base_delay * (multiplier ** (attempt - 1)), max_delay)
    factor = rand(1 - jitter, 1 + jitter)
    return delay * factor


def call_with_retry(
    fn: Callable[[], requests.Response],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    multiplier: float = DEFAULT_MULTIPLIER,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter: float = DEFAULT_JITTER,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[float, float], float] = random.uniform,
) -> requests.Response:
    """Call `fn()` (expected to perform one HTTP request), retrying on transient failure."""
    def wait(attempt: int) -> None:
        sleep(compute_backoff(attempt, base_delay, multiplier, max_delay, jitter, rand))

    for attempt in range(1, max_attempts + 1):
        try:
            response = fn()
        except requests.exceptions.RequestException:
            if attempt == max_attempts:
                raise
            wait(attempt)
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
            wait(attempt)
            continue

        return response

    raise AssertionError("unreachable: every attempt above returns or raises")
