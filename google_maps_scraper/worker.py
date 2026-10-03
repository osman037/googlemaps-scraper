"""Crawl worker: job loop plus all per-request crawl policy.

Each worker is one thread with its own :class:`MapsClient`. The proxy (IP)
strategy is **reuse-first**, mirroring how a human browses:

1. one healthy proxy is LEASED for an entire job - every page of that
   job comes from the same IP (consistent identity)
2. pages are spaced with human-like randomised gaps
   (``--delay-min`` .. ``--delay-max`` seconds)
3. on a block/rate-limit the burnt proxy is cooled down and the job
   continues on a fresh identity; transient failures retry on the same IP
4. ``OK``        -> parse, persist (batched), mirror to the output file
5. pagination until exhausted (no new places), empty, or ``max_pages``

After the search pages of a job the worker enriches each **newly found**
place (all through the same proxy policy):

* details   - ``/maps/preview/place``: hours, review count, plus code,
              description, business status (``--no-details`` to skip)
* reviews   - best-effort review text via the internal batchexecute
              endpoint; Google gates review text for signed-out sessions
              in some regions, in which case this silently yields nothing
              (``--reviews`` to enable, ``--max-reviews`` caps the depth)
* emails    - fetch the business website and extract the contact email
              (``--no-emails`` to skip)

A page that still fails after ``page_attempts`` fails its job; the job
is requeued up to ``query_attempts`` times (later in the same run, when the
proxy pool has cooled down) and marked ``failed`` in the DB so the next run
retries it automatically.

If EVERY proxy is cooling down and the wait budget (``--pool-wait``)
expires, the worker raises :class:`ProxiesExhausted` and the run halts
gracefully (state committed by the engine). It never falls back to the
host's real IP - a datacenter IP hammering Google gets flagged fast, and
the next scheduled run resumes exactly where this one stopped.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from urllib.parse import quote

from .constants import RESULTS_PER_PAGE
from .http_client import MapsClient
from .models import Job, PageResult, Verdict
from .parser import (
    extract_emails,
    extract_websites,
    normalize_url,
    parse_businesses,
    parse_place_details,
    parse_reviews,
)
from .proxy_pool import mask_proxy


class ProxiesExhausted(RuntimeError):
    """Every proxy is cooling down and the wait budget ran out.

    Raised instead of silently falling back to the host's real IP: the run
    halts, the engine checkpoints state, and the next run resumes from the
    database.
    """
class Worker(threading.Thread):
    """One crawl thread. Instantiate via :mod:`google_maps_scraper.engine`."""

    def __init__(self, wid: int, ctx) -> None:
        super().__init__(daemon=True, name=f"W{wid}")
        self.wid = wid
        self.ctx = ctx
        self.settings = ctx.settings
        self.log = logging.getLogger(f"google_maps_scraper.worker{wid}")
        self.client = MapsClient(
            request_timeout=self.settings.request_timeout,
            transport_retries=self.settings.transport_retries,
            lang=self.settings.lang,
            gl=self.settings.gl,
        )
        self._current_proxy: str | None = None

    # -------------------------------------------------------------- loop
    def run(self) -> None:
        """Consume jobs from the shared feed until it is exhausted."""
        while not self.ctx.stop.is_set():
            job = self.ctx.next_job()
            if job is None:
                return
            try:
                self._process(job)
            except ProxiesExhausted as exc:
                # emergency stop: no real-IP fallback; the engine's shutdown
                # path flushes the writers - state is already in the DB
                self.ctx.halt(str(exc))
                return
            except Exception:  # never let one job kill the worker
                self.log.exception("unexpected error processing %r", job.key)
                self.ctx.metrics.inc("queries_failed")
                self.ctx.db.record_search(job.key, "failed", 0)

    # ----------------------------------------------------------- pipeline
    def _process(self, job: Job) -> None:
        """Crawl one job through its pages, enrich, then checkpoint it.

        Two proxy strategies:

        * default (sticky) - one proxy is LEASED for the whole job, every
          page from the same IP with human-like gaps (IP-reuse mode)
        * ``rotate_per_request`` - a FRESH proxy identity for every single
          request (fast mode for gateways that rotate residential IPs
          themselves, e.g. ScraperAPI)

        ``max_pages <= 0`` means UNLIMITED pagination: the job runs until
        the last page (no new places / empty page) before moving on.
        """
        seen: set[str] = set()
        new_places: list = []  # Businesses first seen in this job
        job_places: dict = {}  # every business seen this job, ftid -> record
        status = "done"
        rotate = self.settings.rotate_per_request
        proxy = None if rotate else self._pick_proxy()
        capped = self.settings.max_pages > 0
        page = 0
        while not capped or page < self.settings.max_pages:
            text, proxy = self._fetch_page(job, page, proxy)
            if text is None:
                status = "failed"
                break
            new, page_ftids, records = self._ingest(job, text, seen)
            new_places.extend(new)
            job_places.update({b.ftid: b for b in records})
            seen |= page_ftids
            if not page_ftids or (page > 0 and not new):
                break  # empty page or exhausted result set - last page reached
            # human-like gap on the SAME ip before the next page
            if not capped or page < self.settings.max_pages - 1:
                time.sleep(random.uniform(
                    self.settings.delay_min, self.settings.delay_max))
            page += 1
        self._finish(job, len(seen), status)
        if status == "done" and not self.ctx.stop.is_set():
            # --research: also enrich re-found places that are still
            # missing details/emails (backfill after interrupted runs)
            targets = (list(job_places.values())
                       if self.settings.research else new_places)
            self._enrich(targets)

    def _fetch_page(
        self, job: Job, page: int, proxy: str | None
    ) -> tuple[str | None, str | None]:
        """Fetch one search page under the full policy; rotate only on blocks.

        In ``rotate_per_request`` mode every attempt picks a fresh identity
        instead of keeping the leased one.

        Returns:
            ``(page_text, proxy)`` - the proxy may differ from the input one
            when a block forced a rotation. ``None`` text = page gave up.
        """
        rotate = self.settings.rotate_per_request
        for attempt in range(1, self.settings.page_attempts + 1):
            if self.ctx.stop.is_set():
                return None, proxy
            if rotate:
                proxy = self._pick_proxy()  # fresh identity, every request
            self.ctx.limiter.acquire(proxy or "direct")
            self.ctx.breaker.wait_if_open(self.ctx.stop)

            result = self.client.fetch_search_page(
                job, page * RESULTS_PER_PAGE, proxy)
            self.ctx.metrics.record(result.verdict)

            if result.verdict is Verdict.OK:
                if proxy:
                    self.ctx.pool.report_ok(proxy)
                self.log.debug(
                    "ok page=%d attempt=%d proxy=%s job=%r",
                    page, attempt, mask_proxy(proxy), job.key)
                return result.text, proxy

            if result.verdict.is_blockish:
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                self.ctx.db.record_block(
                    mask_proxy(proxy), result.verdict.value)
                self.ctx.breaker.record(result.verdict)
                self.log.warning(
                    "%s on proxy=%s (attempt %d/%d) job=%r final_url=%s",
                    result.verdict.value, mask_proxy(proxy), attempt,
                    self.settings.page_attempts, job.key,
                    (result.url or "")[:120])
                proxy = self._pick_proxy()  # fresh identity, same job
                time.sleep(random.uniform(3.0, 6.0))
            else:  # BAD / NET - transient, retry on the same proxy
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                self.log.debug(
                    "%s attempt=%d/%d proxy=%s job=%r err=%s",
                    result.verdict.value, attempt,
                    self.settings.page_attempts, mask_proxy(proxy),
                    job.key, result.error)
                time.sleep(random.uniform(2.0, 4.0))
        return None, proxy

    # ------------------------------------------------------------ ingest
    def _ingest(self, job: Job, text: str,
                seen: set[str]) -> tuple[list, set, list]:
        """Parse a page, persist it, and mirror new URLs to the output file.

        Returns:
            ``(new_businesses, page_ftids, records)`` for the pagination
            stop conditions and the enrichment targets. The dedupe
            authority is the database - the same place under ten
            categories is stored once.
        """
        records = parse_businesses(text, job.query)
        new: list = []
        if records:
            added_cids = set(self.ctx.db.add_businesses(records, job.key))
            new = [b for b in records if b.cid in added_cids]
            if self.settings.emit == "place-urls" and added_cids:
                self.ctx.urls_writer.write_lines(
                    [f"https://maps.google.com/?cid={c}" for c in added_cids])
            self.ctx.metrics.inc("places", len(added_cids))

        if self.settings.emit == "websites" or self.settings.collect_websites:
            items = []
            for raw in extract_websites(text):
                normalized = normalize_url(raw)
                if normalized:
                    items.append(normalized)
            if items:
                added_raws = self.ctx.db.add_websites(items, job.key)
                if added_raws:
                    self.ctx.urls_writer.write_lines(added_raws)
                    self.ctx.metrics.inc("urls", len(added_raws))
        return new, {b.ftid for b in records}, records

    # ---------------------------------------------------------- enrich
    def _enrich(self, new_places: list) -> None:
        """Deep-fetch details / reviews / emails for newly found places."""
        s = self.settings
        legacy = s.emit == "place-urls"
        cids = [b.cid for b in new_places]
        detail_cids = set() if legacy or not s.collect_details else set(
            self.ctx.db.pending_places(cids, "details"))
        email_cids = set() if legacy or not s.collect_emails else set(
            self.ctx.db.pending_places(cids, "email"))
        for b in new_places:
            if self.ctx.stop.is_set():
                return
            try:
                if b.cid in detail_cids and b.ftid:
                    self._fetch_details(b)
                if s.collect_reviews and not legacy and b.ftid:
                    self._fetch_reviews(b)
                if b.cid in email_cids and b.website:
                    self._fetch_email(b)
                if not legacy:
                    self._write_business_row(b.cid)
            except Exception:  # enrichment is best-effort, never fatal
                self.log.exception("enrichment failed for %r", b.name)

    def _fetch_details(self, b) -> None:
        """Fetch and merge the deep fields of one place."""
        result = self._fetch_guarded(
            lambda p: self.client.fetch_place_details(b.ftid, b.lat, b.lng, p))
        if result is None:
            return
        details = parse_place_details(result.text or "", b.query)
        if details is None:
            return
        # ponytail: Google randomly serves a reduced details payload (no
        # review count, single hour row); one retry usually gets the full
        # one - drop the retry if it ever degrades the block rate
        if details.review_count == 0 and len(details.hours) <= 1 \
                and not self.ctx.stop.is_set():
            retry = self._fetch_guarded(
                lambda p: self.client.fetch_place_details(
                    b.ftid, b.lat, b.lng, p))
            if retry is not None and retry.text:
                full = parse_place_details(retry.text, b.query)
                if full is not None and (full.review_count
                                         or len(full.hours) > 1):
                    details = full
        details.cid = b.cid  # details payloads carry the same identity
        self.ctx.db.update_place_details(details)
        self.ctx.metrics.inc("details")

    def _fetch_reviews(self, b) -> None:
        """Fetch review pages for one place (best-effort, capped)."""
        source_path = f"/maps/place/{quote(b.name or b.ftid, safe='')}/"
        if b.lat and b.lng:
            source_path += f"@{b.lat:.7f},{b.lng:.7f},17z"
        cursor, pages = "", 0
        while pages * 10 < self.settings.max_reviews:
            if self.ctx.stop.is_set():
                return
            result = self._fetch_guarded(
                lambda p: self.client.fetch_reviews(
                    b.ftid, source_path, cursor=cursor,
                    page_size=min(10, self.settings.max_reviews - pages * 10),
                    proxy_url=p))
            if result is None:
                return
            reviews, cursor = parse_reviews(result.text or "", b.cid)
            if reviews:
                self.ctx.db.add_reviews(reviews)
                self.ctx.review_writer.write_rows([[
                    r.place_cid, r.reviewer_name, r.rating or "", r.text,
                    r.date, r.owner_reply, "",
                ] for r in reviews])
                self.ctx.metrics.inc("reviews", len(reviews))
            if not cursor or not reviews:
                return  # exhausted, or gated (empty payload) - stop early
            pages += 1

    def _fetch_email(self, b) -> None:
        """Fetch the business website and store its contact email."""
        result = self._fetch_guarded(
            lambda p: self.client.fetch_website(b.website, p))
        if result is None or not result.text:
            return
        emails = extract_emails(result.text)
        if emails:
            self.ctx.db.update_place_email(b.cid, emails[0])
            self.ctx.metrics.inc("emails")

    def _write_business_row(self, cid: str) -> None:
        """Mirror one enriched place row into the businesses CSV."""
        place = self.ctx.db.get_place(cid)
        if place is not None:
            row = self.ctx.business_writer.business_row([
                place.cid, place.ftid, place.name, place.place_id,
                ",".join(place.categories), place.address, place.phone,
                place.phone_intl, place.website, place.email, place.rating,
                place.review_count, place.reviews_url, place.lat, place.lng,
                place.plus_code, "|".join(place.hours), place.price_level,
                place.business_status, place.description, place.neighborhood,
                int(place.owner_claimed), place.query, "",
            ])
            self.ctx.business_writer.write_rows([row])

    def _fetch_guarded(self, fetch):
        """Run one non-search fetch under the full crawl policy.

        ``fetch`` is a callable receiving the proxy URL and returning a
        :class:`PageResult`. Blocks rotate the proxy; transient failures
        retry; a page that keeps failing returns ``None`` (best-effort).
        """
        rotate = self.settings.rotate_per_request
        proxy = self._current_proxy
        for attempt in range(1, self.settings.page_attempts + 1):
            if self.ctx.stop.is_set():
                return None
            if rotate or proxy is None:
                proxy = self._pick_proxy()
            self.ctx.limiter.acquire(proxy or "direct")
            self.ctx.breaker.wait_if_open(self.ctx.stop)
            result: PageResult = fetch(proxy)
            self.ctx.metrics.record(result.verdict)
            if result.verdict is Verdict.OK:
                if proxy:
                    self.ctx.pool.report_ok(proxy)
                return result
            if result.verdict.is_blockish:
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                self.ctx.db.record_block(
                    mask_proxy(proxy), result.verdict.value)
                self.ctx.breaker.record(result.verdict)
                proxy = self._pick_proxy()
                time.sleep(random.uniform(3.0, 6.0))
            else:
                if proxy:
                    self.ctx.pool.report_failure(proxy)
                time.sleep(random.uniform(2.0, 4.0))
        return None

    def _finish(self, job: Job, places: int, status: str) -> None:
        """Checkpoint the job and decide whether it should be retried."""
        self.ctx.db.record_search(job.key, status, places)
        if status == "done":
            self.ctx.metrics.inc("queries_done")
            self.log.info("%3d places | %s", places, job.key)
            return
        attempts = self.ctx.register_failure(job.key)
        self.ctx.metrics.inc("queries_failed")
        if attempts < self.settings.query_attempts and not self.ctx.stop.is_set():
            self.ctx.requeue(job)
            self.log.warning(
                "job failed (attempt %d/%d), requeued: %s",
                attempts, self.settings.query_attempts, job.key)
        else:
            self.log.error(
                "job failed after %d attempts, giving up: %s",
                attempts, job.key)

    # ------------------------------------------------------------- proxy
    def _pick_proxy(self) -> str | None:
        """Next healthy proxy; wait out cooldowns, never fall back to real IP.

        Returns ``None`` only when NO proxies are configured (explicit
        direct-connection mode). When the pool has proxies but every one of
        them is cooling down, wait up to ``pool_wait_secs`` for a recovery;
        if the wait expires, raise :class:`ProxiesExhausted` so the run
        halts cleanly instead of hitting Google from the host's own IP.
        """
        if not len(self.ctx.pool):
            return None
        deadline = time.monotonic() + self.settings.pool_wait_secs
        while True:
            if self.ctx.stop.is_set():
                raise ProxiesExhausted(
                    "stop requested while waiting for a healthy proxy")
            proxy = self.ctx.pool.next(avoid_url=self._current_proxy)
            if proxy is None and self._current_proxy is not None:
                # the only healthy proxy may be the one we wanted to avoid
                # for rotation - reusing it beats going direct
                proxy = self.ctx.pool.next()
            if proxy is not None:
                self._current_proxy = proxy
                return proxy
            if time.monotonic() >= deadline:
                raise ProxiesExhausted(
                    f"all {len(self.ctx.pool)} proxies cooling down for over "
                    f"{self.settings.pool_wait_secs:.0f}s - halting instead "
                    "of using the real IP")
            time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))
