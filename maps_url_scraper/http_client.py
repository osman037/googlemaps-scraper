"""Low-level Google Maps HTTP client.

One :class:`MapsClient` instance belongs to exactly one worker thread and
wraps a ``curl_cffi`` session with Chrome TLS impersonation (plain Python
TLS fingerprints get flagged by Google far more often).

Responsibilities (and non-responsibilities):

* Builds the ``tbm=map`` request from :func:`maps_url_scraper.constants.build_search_url`.
* Applies consent cookies and browser-like headers.
* Performs **transport-level** retries with exponential backoff + jitter for
  connection errors, timeouts and 5xx responses - always on the same proxy.
* Classifies every response into a :class:`maps_url_scraper.models.Verdict`.

It deliberately does NOT rotate proxies, sleep between pages or decide what
to do with blocks - that policy lives in :mod:`maps_url_scraper.worker`, so
that request mechanics and crawl policy stay independently testable.
"""

from __future__ import annotations

import logging
import random
import time

from curl_cffi.requests import Session

from .constants import (
    CONSENT_COOKIES,
    DEFAULT_HEADERS,
    RESULTS_PER_PAGE,
    build_search_url,
)
from .models import PageResult, Verdict

log = logging.getLogger("maps_url_scraper.http")


class MapsClient:
    """Thread-bound HTTP client for Google Maps search pages.

    Args:
        request_timeout: Per-request timeout in seconds.
        transport_retries: Extra attempts for transport errors / 5xx /
            transient ``BAD`` responses (total tries = retries + 1).
        backoff_base: Base delay in seconds for exponential backoff.
    """

    def __init__(
        self,
        request_timeout: float = 45.0,
        transport_retries: int = 2,
        backoff_base: float = 1.0,
    ) -> None:
        self.request_timeout = request_timeout
        self.transport_retries = max(0, transport_retries)
        self.backoff_base = backoff_base
        self._session = Session(impersonate="chrome")

    def fetch_search_page(
        self, query: str, offset: int, proxy_url: str | None = None
    ) -> PageResult:
        """Fetch one page of search results.

        Args:
            query: Search text (``"dentist in Austin, TX"``).
            offset: Result offset, a multiple of ``RESULTS_PER_PAGE``.
            proxy_url: Proxy to use, or ``None`` for a direct connection.

        Returns:
            A :class:`PageResult`; ``text`` is populated only for
            :attr:`Verdict.OK`.
        """
        url = build_search_url(query, offset)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        last_error: str | None = None

        for attempt in range(self.transport_retries + 1):
            try:
                resp = self._session.get(
                    url,
                    impersonate="chrome",
                    cookies=CONSENT_COOKIES,
                    headers=DEFAULT_HEADERS,
                    proxies=proxies,
                    timeout=self.request_timeout,
                )
            except Exception as exc:  # transport failure - retry same proxy
                last_error = f"{type(exc).__name__}: {exc}"
                log.debug("transport error (attempt %d): %s", attempt + 1, last_error)
                self._sleep_backoff(attempt)
                continue

            verdict = classify_response(resp)
            if verdict is Verdict.OK:
                return PageResult(
                    verdict=Verdict.OK, text=resp.text,
                    status_code=resp.status_code, url=str(resp.url),
                )

            # BAD responses are often transient Google hiccups - one cheap
            # retry on the same proxy. BLOCK/RATE are policy outcomes: hand
            # them to the worker immediately so it can rotate the proxy.
            if verdict is Verdict.BAD and attempt < self.transport_retries:
                self._sleep_backoff(attempt)
                continue
            return PageResult(
                verdict=verdict, status_code=resp.status_code, url=str(resp.url)
            )

        return PageResult(verdict=Verdict.NET, error=last_error, url=url)

    def _sleep_backoff(self, attempt: int) -> None:
        """Exponential backoff with +-40% jitter after ``attempt`` failures."""
        delay = self.backoff_base * (2 ** attempt) * random.uniform(0.6, 1.4)
        time.sleep(delay)

    def close(self) -> None:
        """Release the underlying session."""
        try:
            self._session.close()
        except Exception:  # pragma: no cover - best effort
            pass


def classify_response(resp) -> Verdict:
    """Classify a raw ``curl_cffi`` response into a :class:`Verdict`.

    The single most important check is the ``)]}'`` XSSI prefix: real Maps
    data always starts with it, so anything else (CAPTCHA page, consent
    redirect, HTML error) is classified accordingly.
    """
    try:
        code = resp.status_code
        final_url = str(resp.url)
        text = resp.text
    except Exception:
        return Verdict.NET
    if code in (429, 503):
        return Verdict.RATE
    if "/sorry/" in final_url or "consent.google" in final_url:
        return Verdict.BLOCK
    if text.startswith(")]}'"):
        return Verdict.OK
    head = text[:3000].lower()
    if "unusual traffic" in head or "captcha" in head or "not a robot" in head:
        return Verdict.BLOCK
    return Verdict.BAD
