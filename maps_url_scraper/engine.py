"""Engine: wires every component together and runs / reports / exports.

This is the only module that knows about *all* the pieces. ``run`` executes
the full lifecycle:

    validate config -> wipe (optional) -> load proxies -> build pending
    queries -> spawn workers -> monitor progress -> graceful shutdown ->
    final summary

Shutdown is cooperative: Ctrl+C sets a stop event; workers finish their
current page (state is checkpointed per query), then the engine prints the
summary and exits. Nothing is lost - the next run resumes where this one
stopped.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import queries as queries_mod
from .config import Settings
from .circuit_breaker import CircuitBreaker
from .database import Database
from .logsetup import setup_logging
from .metrics import Metrics
from .output import OutputWriter
from .proxy_pool import ProxyPool
from .rate_limiter import RateLimiter
from .worker import Worker

log = logging.getLogger("maps_url_scraper.engine")


@dataclass(slots=True)
class EngineContext:
    """Everything workers share. Engine owns it; workers only read/use it."""

    settings: Settings
    db: Database
    pool: ProxyPool
    limiter: RateLimiter
    breaker: CircuitBreaker
    metrics: Metrics
    writer: OutputWriter
    queries: list[str]
    stop: threading.Event = field(default_factory=threading.Event)
    _qi: int = 0
    _qlock: threading.Lock = field(default_factory=threading.Lock)
    _fails: dict[str, int] = field(default_factory=dict)
    _flock: threading.Lock = field(default_factory=threading.Lock)

    def next_query(self) -> str | None:
        """Pop the next query, or ``None`` when done/stopped."""
        with self._qlock:
            if self.stop.is_set() or self._qi >= len(self.queries):
                return None
            query = self.queries[self._qi]
            self._qi += 1
            return query

    def requeue(self, query: str) -> None:
        """Append a failed query back to the end of the feed."""
        with self._qlock:
            if not self.stop.is_set():
                self.queries.append(query)

    def register_failure(self, query: str) -> int:
        """Count a failed attempt for this query; returns the count."""
        with self._flock:
            self._fails[query] = self._fails.get(query, 0) + 1
            return self._fails[query]


def run(settings: Settings) -> int:
    """Execute one full crawl. Returns a process exit code."""
    setup_logging(settings.log_dir, settings.verbose)

    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("CONFIG PROBLEM: %s", problem)
            log.error("Fix the issues above (see README) and re-run.")
        return 2

    if settings.fresh:
        for path in (settings.db_path, settings.output_path):
            try:
                path.unlink()
                log.info("wiped %s (--fresh)", path.name)
            except FileNotFoundError:
                pass

    db = Database(settings.db_path)
    try:
        return _run_inner(settings, db)
    finally:
        db.close()


def _run_inner(settings: Settings, db: Database) -> int:
    proxy_urls = settings.proxies()
    pool = ProxyPool(proxy_urls)
    log.info("Proxies: %s", len(proxy_urls) or "none (direct connection)")
    if proxy_urls and settings.workers > 2 * len(proxy_urls):
        log.warning(
            "%d workers on %d proxies is aggressive - Google flags IPs that "
            "receive too many requests. 1-2 workers per proxy is safer.",
            settings.workers, len(proxy_urls))

    baseline = db.counts()
    if settings.query:
        pending = [settings.query]
    else:
        pending = queries_mod.build_queries(
            locations_dir=settings.locations_dir,
            categories_path=settings.categories_path,
            states=settings.states,
            limit_cities=settings.limit_cities,
            done=set() if settings.research else db.done_queries(),
        )
        if settings.limit_queries:
            pending = pending[: settings.limit_queries]

    log.info(
        "Mode: %s | pending queries: %d | workers: %d | max pages/query: %d "
        "| per-proxy pacing: %.1fs",
        settings.emit, len(pending), settings.workers,
        settings.max_pages, settings.min_interval)
    if not pending:
        log.info("Nothing pending. Use --research to re-run finished "
                 "queries, or --fresh to wipe state.")
        return 0

    ctx = EngineContext(
        settings=settings,
        db=db,
        pool=pool,
        limiter=RateLimiter(settings.min_interval),
        breaker=CircuitBreaker(
            settings.breaker_window, settings.breaker_threshold,
            settings.breaker_cooldown),
        metrics=Metrics(),
        writer=OutputWriter(settings.output_path, fresh=settings.fresh),
        queries=pending,
    )
    workers = [Worker(i + 1, ctx) for i in range(settings.workers)]

    started = time.monotonic()
    try:
        for worker in workers:
            worker.start()
        while any(worker.is_alive() for worker in workers):
            time.sleep(15)
            log.info(
                "progress %d/%d assigned | %d queries done | +%d places "
                "| +%d urls | %d reqs (%.1f/s) | block rate %.0f%%",
                ctx._qi, len(pending),
                baseline.get("searches_done", 0) + ctx.metrics.get("queries_done"),
                ctx.metrics.get("cids"), ctx.metrics.get("urls"),
                ctx.metrics.get("requests"), ctx.metrics.rps(),
                ctx.metrics.block_rate() * 100)
    except KeyboardInterrupt:
        log.info("Ctrl+C - stopping gracefully (finishing current pages)...")
        ctx.stop.set()
    finally:
        for worker in workers:
            worker.join(timeout=60)
        ctx.writer.close()

    _final_summary(settings, db, ctx, started)
    return 0


def _final_summary(settings: Settings, db: Database, ctx, started: float) -> None:
    counts = db.counts()
    log.info(
        "finished in %.0fs | searches: %s | unique places: %d | unique "
        "websites: %d | blocks: %d",
        time.monotonic() - started,
        {k: v for k, v in counts.items() if k.startswith("searches")},
        counts.get("places", 0), counts.get("websites", 0),
        counts.get("blocks", 0))
    if settings.emit == "place-urls" or settings.collect_websites:
        log.info("deliverable: %s", settings.out_dir / "place_urls.txt")
    if settings.emit == "websites" or settings.collect_websites:
        log.info("deliverable: %s", settings.out_dir / "website_urls.txt")


def report(settings: Settings) -> int:
    """Print stored progress stats (no network)."""
    setup_logging(settings.log_dir, settings.verbose)
    problems = settings.validate()
    if problems and not settings.proxy_file.exists():
        # report must work even on a machine without proxies configured
        problems = [p for p in problems if "proxy" not in p]
    db = Database(settings.db_path)
    try:
        counts = db.counts()
        log.info("state file: %s", settings.db_path)
        log.info("searches: %s",
                 {k: v for k, v in counts.items() if k.startswith("searches")})
        log.info("unique places (cids): %d", counts.get("places", 0))
        log.info("unique websites: %d", counts.get("websites", 0))
        log.info("blocks recorded: %d", counts.get("blocks", 0))
        for path in (settings.db_path, settings.output_path):
            if path.exists():
                log.info("%s: %s bytes", path.name, f"{path.stat().st_size:,}")
        if settings.proxy_file.exists():
            pool = ProxyPool(settings.proxies())
            for entry in pool.snapshot():
                log.info(
                    "proxy %s fails=%d cooling=%.0fs",
                    entry["proxy"], entry["fails"], entry["cooling_for_s"])
    finally:
        db.close()
    return 0


def export(
    settings: Settings,
    out_path: str | Path | None = None,
    since: str | None = None,
    emit: str | None = None,
) -> int:
    """Regenerate a guaranteed-complete deliverable file from the DB.

    Args:
        settings: Runtime settings (paths).
        out_path: Destination file; default ``output/<mode>_export.txt``.
        since: ISO timestamp - only rows added at/after it (milestones).
        emit: Dataset to export; defaults to ``settings.emit``.
    """
    setup_logging(settings.log_dir, settings.verbose)
    emit = emit or settings.emit
    if emit not in ("place-urls", "websites"):
        log.error("emit must be place-urls or websites, got %r", emit)
        return 2
    destination = Path(out_path) if out_path else (
        settings.out_dir / f"{'place_urls' if emit == 'place-urls' else 'website_urls'}_export.txt")
    destination.parent.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    try:
        count = 0
        with destination.open("w", encoding="utf-8") as fh:
            if emit == "place-urls":
                for cid in db.iter_cids(since=since):
                    fh.write(f"https://maps.google.com/?cid={cid}\n")
                    count += 1
            else:
                for raw, _added in db.iter_websites(since=since):
                    fh.write(raw + "\n")
                    count += 1
        log.info("exported %d URLs -> %s%s",
                 count, destination, f" (since {since})" if since else "")
    finally:
        db.close()
    return 0


def validate(settings: Settings) -> int:
    """Preflight check: config problems + pending query count (no network)."""
    setup_logging(settings.log_dir, settings.verbose)
    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("CONFIG PROBLEM: %s", problem)
        return 2
    log.info("config OK | mode=%s | workers=%d | max_pages=%d",
             settings.emit, settings.workers, settings.max_pages)
    db = Database(settings.db_path)
    try:
        pending = queries_mod.build_queries(
            locations_dir=settings.locations_dir,
            categories_path=settings.categories_path,
            states=settings.states,
            limit_cities=settings.limit_cities,
            done=set() if settings.research else db.done_queries(),
        )
        if settings.limit_queries:
            pending = pending[: settings.limit_queries]
        log.info("pending queries: %d", len(pending))
        if pending:
            log.info("first: %r", pending[0])
            log.info("last:  %r", pending[-1])
    except Exception as exc:
        log.error("query plan failed: %s", exc)
        return 2
    finally:
        db.close()
    log.info("ready to run.")
    return 0
