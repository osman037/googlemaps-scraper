"""Per-identity rate limiting.

Google throttles per IP, so the correct unit of rate limiting is the proxy
identity (or ``"direct"`` when running without proxies) - not a global
requests/second cap. :class:`RateLimiter` enforces a minimum interval
between consecutive requests *per key*, with jitter so that a pool of
workers does not synchronize into polite-but-identical request waves.

The limiter is deliberately simple (no token bucket): each key may fire at
most once per ``interval`` seconds, which maps directly onto "how hard can
we hammer this one IP before Google gets suspicious".
"""

from __future__ import annotations

import random
import threading
import time


class RateLimiter:
    """Thread-safe minimum-interval limiter, keyed by proxy identity.

    Args:
        interval: Minimum seconds between two requests for the same key.
            ``0`` disables pacing.
        jitter: Fraction of ``interval`` used as random spread (``0.3``
            means each effective interval is 70%..130% of the base).
    """

    def __init__(self, interval: float = 1.5, jitter: float = 0.3) -> None:
        self.interval = max(0.0, interval)
        self.jitter = max(0.0, min(jitter, 1.0))
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def acquire(self, key: str) -> None:
        """Block the caller until ``key`` may fire its next request.

        The reservation timestamp is booked under the lock (so concurrent
        workers queue up correctly), but the sleep happens outside it.
        """
        now = time.monotonic()
        with self._lock:
            earliest = self._next_allowed.get(key, 0.0)
            wait = earliest - now
            if self.interval > 0:
                span = self.interval * (1 + random.uniform(-self.jitter, self.jitter))
                self._next_allowed[key] = max(now, earliest) + max(0.0, span)
            else:
                self._next_allowed[key] = now
        if wait > 0:
            time.sleep(wait)
