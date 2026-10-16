"""Retry policy for transient database locking conflicts."""
from __future__ import annotations

import random
from typing import Any


_TRANSIENT_CODES = {1020, 1205, 1206, 1213}


def is_transient_database_conflict(error: BaseException) -> bool:
    """Identify retryable lock/deadlock failures across supported DB drivers."""
    code = None
    try:
        args = getattr(error, "args", ())
        if args:
            code = int(args[0])
    except (TypeError, ValueError):
        pass

    if code in _TRANSIENT_CODES:
        return True

    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "record has changed since last read",
            "deadlock",
            "lock wait timeout",
            "lock request time out",
            "1222",
            "timeout expired",
            "hyt00",
            "40001",
        )
    )


def database_retry_delay(
    attempt: int,
    *,
    base_seconds: float = 0.25,
    max_seconds: float = 2.0,
    jitter_seconds: float = 0.25,
    random_value: float | None = None,
) -> float:
    """Exponential backoff with jitter to prevent retry synchronization."""
    attempt = max(0, int(attempt))
    base = max(0.0, float(base_seconds))
    cap = max(0.0, float(max_seconds))
    jitter = max(0.0, float(jitter_seconds))
    delay = min(cap, base * (2 ** min(attempt, 30)))

    if random_value is None:
        jitter_factor = random.random()
    else:
        jitter_factor = min(1.0, max(0.0, float(random_value)))

    return delay + jitter * jitter_factor
