"""Low-level Google Maps HTTP client.

One :class:`MapsClient` instance belongs to exactly one worker thread and
wraps a ``curl_cffi`` session with Chrome TLS impersonation (plain Python
TLS fingerprints get flagged by Google far more often).

Responsibilities (and non-responsibilities):

* Builds the ``tbm=map`` request (text search or grid-cell viewport) from
  :mod:`google_maps_scraper.constants`.
* Fetches place details, reviews and arbitrary pages (website emails).
* Applies consent cookies and browser-like headers.
* Performs **transport-level** retries with exponential backoff + jitter for
  connection errors, timeouts and 5xx responses - always on the same proxy.
* Classifies every response into a :class:`google_maps_scraper.models.Verdict`.

It deliberately does NOT rotate proxies, sleep between pages or decide what
to do with blocks - that policy lives in :mod:`google_maps_scraper.worker`, so
that request mechanics and crawl policy stay independently testable.
"""

from __future__ import annotations

import logging
import os
import random
import time

from curl_cffi.requests import Session

from .constants import (
    CONSENT_COOKIES,
    DEFAULT_HEADERS,
    RESULTS_PER_PAGE,
    build_grid_search_url,
    build_place_details_url,
    build_reviews_request,
    build_search_url,
)
from .models import Job, PageResult, Verdict

log = logging.getLogger("google_maps_scraper.http")

# Many residential proxy gateways intercept TLS and serve THEIR certificate
# instead of the target's, so certificate verification must be disabled for
# them (this is the provider's documented usage pattern). Set TLS_VERIFY=true
# in the environment when your proxies do NOT intercept TLS.
TLS_VERIFY = os.environ.get("TLS_VERIFY", "false").lower() == "true"


class MapsClient:
    """Thread-bound HTTP client for Google Maps endpoints.

    Args:
        request_timeout: Per-request timeout in seconds.
        transport_retries: Extra attempts for transport errors / 5xx /
            transient ``BAD`` responses (total tries = retries + 1).
        backoff_base: Base delay in seconds for exponential backoff.
        lang / gl: Language and region pinning for all Maps endpoints.
    """

    def __init__(
        self,
        request_timeout: float = 45.0,
        transport_retries: int = 2,
        backoff_base: float = 1.0,
        lang: str = "en",
        gl: str = "us",
    ) -> None:
        self.request_timeout = request_timeout
        self.transport_retries = max(0, transport_retries)
        self.backoff_base = backoff_base
        self.lang = lang
        self.gl = gl
        self._session = Session(impersonate="chrome")

    # ------------------------------------------------------------ search
    def fetch_search_page(
        self, job: Job, offset: int, proxy_url: str | None = None
    ) -> PageResult:
        """Fetch one page of search results (text query or grid cell).

        Args:
            job: The crawl job (text query, or a grid cell with lat/lng).
            offset: Result offset, a multiple of ``RESULTS_PER_PAGE``.
            proxy_url: Proxy to use, or ``None`` for a direct connection.

        Returns:
            A :class:`PageResult`; ``text`` is populated only for
            :attr:`Verdict.OK`.
        """
        if job.lat is None:
            url = build_search_url(job.query, offset, self.lang, self.gl)
        else:
            url = build_grid_search_url(
                job.query, job.lat, job.lng, job.span_m or 2000.0,
                offset, self.lang, self.gl)
        return self._fetch_maps(url, proxy_url)

    # ----------------------------------------------------------- details
    def fetch_place_details(
        self, ftid: str, lat: float, lng: float, proxy_url: str | None = None
    ) -> PageResult:
        """Fetch the place-details payload (hours, review count, ...)."""
        url = build_place_details_url(ftid, lat, lng, self.lang, self.gl)
        return self._fetch_maps(url, proxy_url)

    # ----------------------------------------------------------- reviews
    def fetch_reviews(
        self, ftid: str, source_path: str, cursor: str = "",
        page_size: int = 10, proxy_url: str | None = None,
    ) -> PageResult:
        """POST one reviews batchexecute page (best-effort dataset)."""
        url, body = build_reviews_request(
            ftid, source_path, cursor=cursor, page_size=page_size,
            lang=self.lang)
        return self._fetch_maps(
            url, proxy_url, method="POST", data=body)

    # ---------------------------------------------------------- websites
    def fetch_website(
        self, url: str, proxy_url: str | None = None
    ) -> PageResult:
        """Fetch an arbitrary page (business website for email enrichment).

        Deliberately lenient: any response with a body is ``OK`` because
        website classification rules differ completely from Google's.
        """
        for attempt in range(self.transport_retries + 1):
            try:
                resp = self._session.get(
                    url, impersonate="chrome",
                    headers=DEFAULT_HEADERS,
                    proxies=self._proxies(proxy_url),
                    timeout=self.request_timeout,
                    verify=TLS_VERIFY,
                )
                return PageResult(verdict=Verdict.OK, text=resp.text,
                                  status_code=resp.status_code,
                                  url=str(resp.url))
            except Exception as exc:
                log.debug("website fetch error (attempt %d): %s",
                          attempt + 1, exc)
                self._sleep_backoff(attempt)
        return PageResult(verdict=Verdict.NET, url=url)

    # ------------------------------------------------------------- core
    def _fetch_maps(
        self, url: str, proxy_url: str | None, method: str = "GET",
        data: str | None = None,
    ) -> PageResult:
        """Run one Maps request with transport retries + verdict handling."""
        proxies = self._proxies(proxy_url)
        last_error: str | None = None

        for attempt in range(self.transport_retries + 1):
            try:
                if method == "POST":
                    resp = self._session.post(
                        url, data=data,
                        impersonate="chrome",
                        cookies=CONSENT_COOKIES,
                        headers=DEFAULT_HEADERS,
                        proxies=proxies,
                        timeout=self.request_timeout,
                        verify=TLS_VERIFY,
                    )
                else:
                    resp = self._session.get(
                        url,
                        impersonate="chrome",
                        cookies=CONSENT_COOKIES,
                        headers=DEFAULT_HEADERS,
                        proxies=proxies,
                        timeout=self.request_timeout,
                        verify=TLS_VERIFY,
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

    @staticmethod
    def _proxies(proxy_url: str | None) -> dict | None:
        return {"http": proxy_url, "https": proxy_url} if proxy_url else None

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
    if code in (401, 403):
        # forbidden/unauthorised: a dead proxy key or an IP Google refuses -
        # either way the identity must be rotated immediately, not retried
        return Verdict.BLOCK
    if "/sorry/" in final_url or "consent.google" in final_url:
        return Verdict.BLOCK
    if text.startswith(")]}'"):
        return Verdict.OK
    head = text[:3000].lower()
    if "unusual traffic" in head or "captcha" in head or "not a robot" in head:
        return Verdict.BLOCK
    return Verdict.BAD
