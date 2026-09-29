"""Thread-safe run counters.

Every worker records outcomes into a single :class:`Metrics` instance. The
engine's progress line and the final summary are rendered from it, so live
monitoring never needs to touch the database (COUNT(*) over a multi-million
row table would stall the writers).

Counters used by the pipeline:

* ``requests``            - pages fetched (any verdict)
* ``cids``                - unique place URLs written this run
* ``urls``                - unique website URLs written this run
* ``blocked_pages``       - BLOCK/RATE outcomes
* ``queries_done`` / ``queries_failed``
"""

from __future__ import annotations

import threading
import time

from .models import Verdict


class Metrics:
    """Lock-protected counter bag with cheap derived stats."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._started = time.monotonic()

    def record(self, verdict: Verdict) -> None:
        """Count one page outcome (any verdict)."""
        with self._lock:
            self._counts["requests"] = self._counts.get("requests", 0) + 1
            if verdict.is_blockish:
                self._counts["blocked_pages"] = \
                    self._counts.get("blocked_pages", 0) + 1

    def inc(self, key: str, n: int = 1) -> None:
        """Increment a named counter by ``n``."""
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + n

    def get(self, key: str) -> int:
        """Current value of a counter (0 when untouched)."""
        with self._lock:
            return self._counts.get(key, 0)

    def elapsed(self) -> float:
        """Seconds since the Metrics object was created."""
        return time.monotonic() - self._started

    def rps(self) -> float:
        """Average requests per second over the whole run."""
        elapsed = self.elapsed()
        return self.get("requests") / elapsed if elapsed > 0 else 0.0

    def block_rate(self) -> float:
        """Fraction of requests that were blocked/rate-limited (0..1)."""
        with self._lock:
            total = self._counts.get("requests", 0)
            blocked = self._counts.get("blocked_pages", 0)
        return blocked / total if total else 0.0
