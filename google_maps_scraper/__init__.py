"""Google Maps Scraper - complete business-data scraper over pure HTTP.

Extracts full Google Maps business listings (name, category, address,
phone, website, email, rating, review count, hours, coordinates, plus
code, ...) through the internal ``tbm=map`` endpoint - no browser, no
Selenium, no API key. Datasets:

* ``businesses``  - one CSV row per unique business, enriched with
  place-details deep fields and contact emails from their websites
  (default).
* ``place-urls``  - ``https://maps.google.com/?cid=...`` deep links only.
* ``websites``    - business website URLs embedded in the search response.

Grid mode (``--all``) tiles a location into viewport-scoped cells to beat
Google's ~120-results-per-query cap. The request protocol (endpoints,
``pb`` parameter templates, ``!8i{offset}`` pagination) lives in
:mod:`google_maps_scraper.constants`; parser behaviour is regression-covered
against captured fixtures in ``tests/``.

Production behaviours built in: proxy rotation with escalating cooldowns,
per-proxy rate limiting, retries with backoff, a global circuit breaker,
crash-safe resumable state (SQLite), dedupe on the place cid, and clean
console-only logging with credential masking.
"""

__version__ = "2.0.0"
