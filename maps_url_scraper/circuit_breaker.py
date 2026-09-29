"""Global circuit breaker against full-pool burnout.

When Google starts mass-blocking, individual proxy rotation is not enough:
if the block ratio across the whole run spikes, the correct move is to stop
*everything* for a while, let the heat dissipate, and resume - otherwise a
run can incinerate an entire (expensive) residential proxy pool in minutes.

:class:`CircuitBreaker` watches the outcome ratio of recent pages. If the
block/rate share over the sliding window crosses ``threshold``, it "opens":
every worker pauses in :meth:`wait_if_open` until the cooldown expires, then
the window resets and the crawl resumes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from .models import Verdict

log = logging.getLogger("maps_url_scraper.breaker")


class CircuitBreaker:
    """Ratio-triggered global pause shared by all workers.

    Args:
        window: Number of recent page outcomes kept in the sliding window.
        threshold: Open the breaker when blocked/rate outcomes exceed this
            fraction of the window (0.35 = 35%).
        cooldown: Seconds the breaker stays open once triggered.
        min_samples: Do not evaluate the ratio before this many outcomes
            (prevents a single early block from stopping the run).
    """

    def __init__(
        self,
        window: int = 24,
        threshold: float = 0.35,
        cooldown: float = 180.0,
        min_samples: int = 8,
    ) -> None:
        self._window = max(4, window)
        self._threshold = threshold
        self._cooldown = max(0.0, cooldown)
        self._min_samples = max(2, min(min_samples, self._window))
        self._lock = threading.Lock()
        self._outcomes: deque[bool] = deque(maxlen=self._window)
        self._open_until = 0.0

    def record(self, verdict: Verdict) -> None:
        """Feed one page outcome into the window; open the breaker if needed."""
        with self._lock:
            self._outcomes.append(verdict.is_blockish)
            if len(self._outcomes) < self._min_samples:
                return
            if time.monotonic() < self._open_until:
                return  # already open - cooldown restarts when it elapses
            ratio = sum(self._outcomes) / len(self._outcomes)
            if ratio >= self._threshold:
                self._open_until = time.monotonic() + self._cooldown
                self._outcomes.clear()
                log.warning(
                    "CIRCUIT BREAKER OPEN: %.0f%% of last pages blocked - "
                    "pausing all workers for %.0fs",
                    ratio * 100, self._cooldown,
                )

    def is_open(self) -> bool:
        """True while the breaker forces a global pause."""
        with self._lock:
            return time.monotonic() < self._open_until

    def wait_if_open(self, stop_event: threading.Event) -> None:
        """Block the calling worker while the breaker is open.

        Returns as soon as the breaker closes or ``stop_event`` is set, so
        shutdown is never delayed by more than ~1 second.
        """
        while not stop_event.is_set() and self.is_open():
            time.sleep(1.0)
