"""Google Maps URL scraper - production package.

Scrapes Google Maps listings via the pure-HTTP ``tbm=map`` endpoint (no
browser) and emits unique URL datasets:

* ``place-urls``  - ``https://maps.google.com/?cid=...`` deep links
  (one per unique place, deduplicated on the place cid).
* ``websites``    - business website URLs embedded in the search response
  (deduplicated on the normalized URL).

The request protocol (endpoint, ``pb`` parameter template, ``!8i{offset}``
pagination) is captured in :mod:`maps_url_scraper.constants` and regression
covered by the parser's deterministic behaviour. This package is the
hardened, modular production implementation with proxy rotation, per-proxy
rate limiting, retries with backoff, a global circuit breaker, resumable
state, and structured logging.
"""

__version__ = "1.0.0"
