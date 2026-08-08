"""Per-channel rate limiter (latest-sample)."""

from __future__ import annotations

import time
from typing import Optional


class RateGate:
    def __init__(self, max_hz: float) -> None:
        if max_hz <= 0:
            raise ValueError("max_hz must be positive")
        self._min_period = 1.0 / float(max_hz)
        self._last_send: Optional[float] = None

    def allow(self, now: Optional[float] = None) -> bool:
        t = time.monotonic() if now is None else now
        if self._last_send is None or (t - self._last_send) >= self._min_period:
            self._last_send = t
            return True
        return False
