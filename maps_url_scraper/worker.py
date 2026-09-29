"""Crawl worker: query loop plus all per-request crawl policy.

Each worker is one thread with its own :class:`MapsClient`. For every page
it executes the full defensive sequence:

1. pick a healthy proxy from the pool (rotate on failure)
2. per-proxy rate limiting (pacing) + circuit-breaker pause
3. fetch via the HTTP client (transport retries happen inside it)
4. classify + act:
   - ``OK``        -> parse, persist (batched), append new URLs to output
   - ``BLOCK/RATE``-> cool the proxy down, rotate, backoff, retry the page
   - ``BAD``/``NET``-> backoff and retry through a (possibly new) proxy
5. pagination until exhausted (no new places), empty, or ``max_pages``

A page that still fails after ``page_attempts`` fails its query; the query
is requeued up to ``query_attempts`` times (later in the same run, when the
proxy pool has cooled down) and marked ``failed`` in the DB so the next run
retries it automatically.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from .constants import RESULTS_PER_PAGE
from .http_client import MapsClient
from .models import Verdict
from .parser import extract_websites, normalize_url, parse_places
from .proxy_pool import mask_proxy


class Worker(threading.Thread):
    """One crawl thread. Instantiate via :mod:`maps_url_scraper.engine`."""

    def __init__(self, wid: int, ctx) -> None:
        super().__init__(daemon=True, name=f"W{wid}")
        self.wid = wid
        self.ctx = ctx
        self.settings = ctx.settings
        self.log = logging.getLogger(f"maps_url_scraper.worker{wid}")
        self.client = MapsClient(
            request_timeout=self.settings.request_timeout,
            transport_retries=self.settings.transport_retries,
        )
        self._current_proxy: str | None = None

    # -------------------------------------------------------------- loop
    def run(self) -> None:
        """Consume queries from the shared feed until it is exhausted."""
        while not self.ctx.stop.is_set():
            query = self.ctx.next_query()
            if query is None:
                return
            try:
                self._process(query)
            except Exception:  # never let one query kill the worker
                self.log.exception("unexpected error processing %r", query)
                self.ctx.metrics.inc("queries_failed")
                self.ctx.db.record_search(query, "failed", 0)

    # ----------------------------------------------------------- pipeline
    def _process(self, query: str) -> None:
        """Crawl one query through all its pages, then checkpoint it."""
        seen: set[str] = set()
        status = "done"
        for page in range(self.settings.max_pages):
            text = self._fetch_page(query, page)
            if text is None:
                status = "failed"
                break
            new_ftids, page_ftids = self._ingest(query, text, seen)
            seen |= page_ftids
            if not page_ftids or (page > 0 and not new_ftids):
                break  # empty page or exhausted result set
            if page < self.settings.max_pages - 1:
                time.sleep(random.uniform(
                    self.settings.delay_min, self.settings.delay_max))
        self._finish(query, len(seen), status)

    def _fetch_page(self, query: str, page: int) -> str | None:
        """Fetch one page under the full policy; ``None`` = give up page."""
        for attempt in range(1, self.settings.page_attempts + 1):
            if self.ctx.stop.is_set():
                return None
            proxy = self._pick_proxy()
            self.ctx.limiter.acquire(proxy or "direct")
            self.ctx.breaker.wait_if_open(self.ctx.stop)

            result = self.client.fetch_search_page(
                query, page * RESULTS_PER_PAGE, proxy)
            self.ctx.metrics.record(result.verdict)

            if result.verdict is Verdict.OK:
                if proxy:
                    self.ctx.pool.report_ok(proxy)
                self.log.debug(
                    "ok page=%d attempt=%d proxy=%s q=%r",
                    page, attempt, mask_proxy(proxy), query)
                return result.text

            if result.verdict.is_blockish:
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                self.ctx.db.record_block(
                    mask_proxy(proxy), result.verdict.value)
                self.ctx.breaker.record(result.verdict)
                self.log.warning(
                    "%s on proxy=%s (attempt %d/%d) q=%r final_url=%s",
                    result.verdict.value, mask_proxy(proxy), attempt,
                    self.settings.page_attempts, query,
                    (result.url or "")[:120])
                time.sleep(random.uniform(3.0, 6.0))
            else:  # BAD / NET
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                self.log.debug(
                    "%s attempt=%d/%d proxy=%s q=%r err=%s",
                    result.verdict.value, attempt,
                    self.settings.page_attempts, mask_proxy(proxy),
                    query, result.error)
                time.sleep(random.uniform(2.0, 4.0))
        return None

    def _ingest(
        self, query: str, text: str, seen: set[str]
    ) -> tuple[set[str], set[str]]:
        """Parse a page, persist it, and mirror new URLs to the output file.

        Returns:
            ``(new_ftids, page_ftids)`` for the pagination stop conditions.
        """
        records = parse_places(text)
        page_ftids = {r.ftid for r in records}
        new_ftids = page_ftids - seen

        # places are always recorded (place-identity dedupe, works for both
        # emit modes and keeps the DB usable if the mode changes later)
        if self.settings.emit == "place-urls" or self.settings.collect_websites:
            added_cids = self.ctx.db.add_places(
                [(r.cid, r.ftid, r.name) for r in records], query)
            if added_cids:
                self.ctx.writer.write_lines(
                    [f"https://maps.google.com/?cid={cid}" for cid in added_cids])
                self.ctx.metrics.inc("cids", len(added_cids))

        if self.settings.emit == "websites" or self.settings.collect_websites:
            items = []
            for raw in extract_websites(text):
                normalized = normalize_url(raw)
                if normalized:
                    items.append(normalized)
            if items:
                added_raws = self.ctx.db.add_websites(items, query)
                if added_raws:
                    self.ctx.writer.write_lines(added_raws)
                    self.ctx.metrics.inc("urls", len(added_raws))
        return new_ftids, page_ftids

    def _finish(self, query: str, places: int, status: str) -> None:
        """Checkpoint the query and decide whether it should be retried."""
        self.ctx.db.record_search(query, status, places)
        if status == "done":
            self.ctx.metrics.inc("queries_done")
            self.log.info("%3d places | %s", places, query)
            return
        attempts = self.ctx.register_failure(query)
        self.ctx.metrics.inc("queries_failed")
        if attempts < self.settings.query_attempts and not self.ctx.stop.is_set():
            self.ctx.requeue(query)
            self.log.warning(
                "query failed (attempt %d/%d), requeued: %s",
                attempts, self.settings.query_attempts, query)
        else:
            self.log.error(
                "query failed after %d attempts, giving up: %s",
                attempts, query)

    # ------------------------------------------------------------- proxy
    def _pick_proxy(self) -> str | None:
        """Next healthy proxy, waiting out full-pool cooldowns when needed."""
        if not len(self.ctx.pool):
            return None
        for _ in range(60):  # ~5 min worst case, then the page attempt fails
            if self.ctx.stop.is_set():
                return None
            proxy = self.ctx.pool.next(avoid_url=self._current_proxy)
            if proxy is not None:
                self._current_proxy = proxy
                return proxy
            time.sleep(5.0)
        return None
