"""
Fixed-window rate limiter for VR Services API calls.

A single shared instance (API_RATE_LIMITER) is imported by every module that
makes outbound SOAP requests. Allows up to 30 calls per 1-second window across
all endpoints combined. Calls beyond the limit block until the window resets.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("api_ingest.rate_limiter")


class FixedWindowRateLimiter:
    def __init__(self, rate: int, window: float = 1.0) -> None:
        self._rate = rate          # max calls per window
        self._window = window      # window size in seconds
        self._count = 0            # calls made in current window
        self._window_start = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a call is permitted within the current window."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._window_start

                if elapsed >= self._window:
                    # Window rolled over — log the calls made in the previous window
                    if self._count > 0:
                        log.debug(
                            "Rate limiter window closed: %d/%d calls made in the last %.2fs",
                            self._count, self._rate, elapsed,
                        )
                    self._window_start = now
                    self._count = 0

                if self._count < self._rate:
                    self._count += 1
                    log.debug(
                        "API call permitted: %d/%d in current window",
                        self._count, self._rate,
                    )
                    return

                # Window not yet over and limit reached — block until next window
                wait = self._window - elapsed
                log.warning(
                    "Rate limit reached: %d/%d calls in current window — "
                    "throttling, waiting %.3fs for next window",
                    self._count, self._rate, wait,
                )

            time.sleep(wait)


# Shared singleton — import this in every module that calls the VR Services API
API_RATE_LIMITER = FixedWindowRateLimiter(rate=30, window=1.0)
