"""Engine: wires every component together and runs / reports / exports.

This is the only module that knows about *all* the pieces. ``run`` executes
the full lifecycle:

    validate config -> wipe (optional) -> load proxies -> build pending
    jobs -> spawn workers -> monitor progress -> graceful shutdown ->
    final summary

Shutdown is cooperative: Ctrl+C sets a stop event; workers finish their
current page (state is checkpointed per job), then the engine prints the
summary and exits. Nothing is lost - the next run resumes where this one
stopped.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import grid as grid_mod
from . import queries as queries_mod
from .config import PROJECT_ROOT, Settings
from .circuit_breaker import CircuitBreaker
from .database import Database
from .logsetup import setup_logging
from .metrics import Metrics
from .models import Job
from .output import (
    BUSINESS_COLUMNS,
    REVIEW_COLUMNS,
    CsvWriter,
    OutputWriter,
)
from .proxy_pool import ProxyPool
from .rate_limiter import RateLimiter
from .worker import Worker

log = logging.getLogger("google_maps_scraper.engine")


@dataclass(slots=True)
class EngineContext:
    """Everything workers share. Engine owns it; workers only read/use it."""

    settings: Settings
    db: Database
    pool: ProxyPool
    limiter: RateLimiter
    breaker: CircuitBreaker
    metrics: Metrics
    queries: list[Job]
    business_writer: CsvWriter | None = None
    review_writer: CsvWriter | None = None
    urls_writer: OutputWriter | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    _qi: int = 0
    _qlock: threading.Lock = field(default_factory=threading.Lock)
    _fails: dict[str, int] = field(default_factory=dict)
    _flock: threading.Lock = field(default_factory=threading.Lock)

    def next_job(self) -> Job | None:
        """Pop the next job, or ``None`` when done/stopped."""
        with self._qlock:
            if self.stop.is_set() or self._qi >= len(self.queries):
                return None
            job = self.queries[self._qi]
            self._qi += 1
            return job

    def requeue(self, job: Job) -> None:
        """Append a failed job back to the end of the feed."""
        with self._qlock:
            if not self.stop.is_set():
                self.queries.append(job)

    def register_failure(self, key: str) -> int:
        """Count a failed attempt for this job; returns the count."""
        with self._flock:
            self._fails[key] = self._fails.get(key, 0) + 1
            return self._fails[key]

    def writer_paths(self) -> list[Path]:
        """Every deliverable file the writers currently hold open."""
        paths: list[Path] = []
        for w in (self.business_writer, self.review_writer, self.urls_writer):
            if w is not None:
                paths.append(w.path)
        return paths

    def close_writers(self) -> None:
        """Flush and close every open deliverable file."""
        for w in (self.business_writer, self.review_writer, self.urls_writer):
            if w is not None:
                w.close()

    def halt(self, reason: str) -> None:
        """Emergency stop: end the run gracefully, exactly once.

        Workers exit, then the engine's normal shutdown path takes over:
        the writers are closed and the process exits 0. All state is
        already checkpointed per job in the database, so the run resumes
        from where it stopped on the next invocation.
        """
        with self._qlock:
            already = self.stop.is_set()
            self.stop.set()
        if not already:
            log.critical("HALTING RUN: %s", reason)


def _make_writers(settings: Settings) -> tuple[CsvWriter | None,
                                               CsvWriter | None,
                                               OutputWriter | None]:
    """Open the deliverable writers matching the selected dataset."""
    if settings.emit == "place-urls":
        return None, None, OutputWriter(settings.output_path,
                                        fresh=settings.fresh)
    business = CsvWriter(settings.output_path, BUSINESS_COLUMNS,
                         fresh=settings.fresh)
    review = CsvWriter(settings.reviews_path, REVIEW_COLUMNS,
                       fresh=settings.fresh)
    urls = None
    if settings.emit == "websites" or settings.collect_websites:
        urls = OutputWriter(PROJECT_ROOT / "website_urls.txt",
                            fresh=settings.fresh)
    return business, review, urls


def _pending_jobs(settings: Settings, db: Database) -> list[Job]:
    """Build the pending job list for one run (bounded by --limit-queries)."""
    limit = settings.limit_queries or 0
    done = set() if settings.research else db.done_queries()

    if settings.query:  # explicit single text query
        jobs = [Job(settings.query)]
    elif settings.grid:  # viewport-scoped cells over the location
        lat, lng, bbox = grid_mod.geocode(settings.location)
        log.info("geocoded %r -> center %.4f,%.4f (bbox %s)",
                 settings.location, lat, lng,
                 [round(x, 3) for x in bbox])
        jobs = grid_mod.make_grid(bbox, settings.cell_km,
                                  radius_km=settings.radius_km)
        for job in jobs:
            job.query = settings.category or ""
    elif settings.category and settings.location:
        jobs = [Job(f"{settings.category} in {settings.location}")]
    else:  # legacy full grid: state files x categories
        jobs = []
        for query in queries_mod.iter_queries(
            locations_dir=settings.locations_dir,
            categories_path=settings.categories_path,
            states=settings.states,
            limit_cities=settings.limit_cities,
            done=done,
        ):
            jobs.append(Job(query))
            if limit and len(jobs) >= limit:
                return jobs
        return jobs

    jobs = [j for j in jobs if j.key not in done]
    if limit:
        jobs = jobs[:limit]
    return jobs


def run(settings: Settings) -> int:
    """Execute one full crawl. Returns a process exit code."""
    setup_logging(settings.verbose)

    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("CONFIG PROBLEM: %s", problem)
        log.error("Fix the issues above (see README) and re-run.")
        return 2

    if settings.fresh:
        for path in (settings.db_path, settings.output_path,
                     settings.reviews_path):
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
    pending = _pending_jobs(settings, db)

    enrich = [name for name, on in (
        ("details", settings.collect_details and settings.emit != "place-urls"),
        ("emails", settings.collect_emails and settings.emit != "place-urls"),
        ("reviews", settings.collect_reviews and settings.emit != "place-urls"),
    ) if on]
    log.info(
        "Mode: %s | pending jobs: %d | workers: %d | max pages/job: %d "
        "| per-proxy pacing: %.1fs | enrichment: %s",
        settings.emit, len(pending), settings.workers,
        settings.max_pages, settings.min_interval,
        ", ".join(enrich) or "none")
    if not pending:
        log.info("Nothing pending. Use --research to re-run finished "
                 "jobs, or --fresh to wipe state.")
        return 0

    business_writer, review_writer, urls_writer = _make_writers(settings)
    ctx = EngineContext(
        settings=settings,
        db=db,
        pool=pool,
        limiter=RateLimiter(settings.min_interval),
        breaker=CircuitBreaker(
            settings.breaker_window, settings.breaker_threshold,
            settings.breaker_cooldown),
        metrics=Metrics(),
        queries=pending,
        business_writer=business_writer,
        review_writer=review_writer,
        urls_writer=urls_writer,
    )
    workers = [Worker(i + 1, ctx) for i in range(settings.workers)]

    started = time.monotonic()
    try:
        for worker in workers:
            worker.start()
        while any(worker.is_alive() for worker in workers):
            time.sleep(15)
            log.info(
                "progress %d/%d assigned | %d jobs done | +%d places "
                "| +%d reviews | +%d emails | %d reqs (%.1f/s) "
                "| block rate %.0f%%",
                ctx._qi, len(pending),
                baseline.get("searches_done", 0) + ctx.metrics.get("queries_done"),
                ctx.metrics.get("places"), ctx.metrics.get("reviews"),
                ctx.metrics.get("emails"),
                ctx.metrics.get("requests"), ctx.metrics.rps(),
                ctx.metrics.block_rate() * 100)
    except KeyboardInterrupt:
        log.info("Ctrl+C - stopping gracefully (finishing current pages)...")
        ctx.stop.set()
    finally:
        ctx.stop.set()
        # workers poll next_job(), so no poison-pill queue is needed:
        # stop.set() makes every worker exit, then we drain the shutdown
        # path - writer flush/close and the final summary
        for worker in workers:
            worker.join(timeout=60)
        ctx.close_writers()

    _final_summary(settings, db, started)
    return 0


def _write_final_files(settings: Settings, db: Database) -> tuple[int, int]:
    """Rewrite the deliverables from the DB (complete, dedupe-free).

    The live CSV mirror only gains a row once a place's enrichment
    finishes, and re-found places are never re-mirrored - so right
    before the summary the files are regenerated from the database,
    which is the dedupe authority.
    """
    n_places = 0
    if settings.emit == "place-urls":
        with settings.output_path.open("w", encoding="utf-8") as fh:
            for cid in db.iter_cids():
                fh.write(f"https://maps.google.com/?cid={cid}\n")
                n_places += 1
        return n_places, 0

    with settings.output_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(BUSINESS_COLUMNS)
        for row in db.iter_businesses():
            w.writerow(_business_csv_row(row))
            n_places += 1
    n_reviews = 0
    with settings.reviews_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(REVIEW_COLUMNS)
        for row in db.iter_reviews():
            w.writerow(list(row))
            n_reviews += 1
    return n_places, n_reviews


def _final_summary(settings: Settings, db: Database, started: float) -> None:
    n_places, n_reviews = _write_final_files(settings, db)
    counts = db.counts()
    log.info(
        "finished in %.0fs | searches: %s | unique places: %d "
        "(%d with emails) | reviews: %d | blocks: %d",
        time.monotonic() - started,
        {k: v for k, v in counts.items() if k.startswith("searches")},
        counts.get("places", 0), counts.get("emails", 0),
        counts.get("reviews", 0), counts.get("blocks", 0))
    if settings.emit == "place-urls":
        log.info("deliverable: %s (%d urls)", settings.output_path, n_places)
    else:
        log.info("deliverable: %s (%d businesses) (+ %s, %d reviews)",
                 settings.output_path, n_places,
                 settings.reviews_path, n_reviews)
    if settings.collect_websites and settings.emit != "websites":
        log.info("bonus websites file: %s", PROJECT_ROOT / "website_urls.txt")


def report(settings: Settings) -> int:
    """Print stored progress stats (no network)."""
    setup_logging(settings.verbose)
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
        log.info("unique places: %d (%d with emails)",
                 counts.get("places", 0), counts.get("emails", 0))
        log.info("reviews: %d", counts.get("reviews", 0))
        log.info("unique websites: %d", counts.get("websites", 0))
        log.info("blocks recorded: %d", counts.get("blocks", 0))
        for path in (settings.db_path, settings.output_path,
                     settings.reviews_path):
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
    reviews: bool = False,
) -> int:
    """Regenerate a guaranteed-complete deliverable file from the DB.

    Args:
        settings: Runtime settings (paths).
        out_path: Destination file; default ``businesses_export.csv``
            (or the legacy txt names for the legacy datasets).
        since: ISO timestamp - only rows added at/after it (milestones).
        emit: Dataset to export; defaults to ``settings.emit``.
        reviews: Also write the reviews export alongside.
    """
    setup_logging(settings.verbose)
    emit = emit or settings.emit
    if emit not in ("businesses", "place-urls", "websites"):
        log.error("emit must be one of %s, got %r", EMITS, emit)
        return 2
    if out_path:
        destination = Path(out_path)
    elif emit == "businesses":
        destination = PROJECT_ROOT / "businesses_export.csv"
    elif emit == "place-urls":
        destination = PROJECT_ROOT / "place_urls_export.txt"
    else:
        destination = PROJECT_ROOT / "website_urls_export.txt"
    destination.parent.mkdir(parents=True, exist_ok=True)

    db = Database(settings.db_path)
    try:
        count = 0
        if emit == "businesses":
            with destination.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(BUSINESS_COLUMNS)
                for row in db.iter_businesses(since=since):
                    w.writerow(_business_csv_row(row))
                    count += 1
            log.info("exported %d businesses -> %s%s",
                     count, destination, f" (since {since})" if since else "")
            if reviews:
                rpath = destination.parent / \
                    (destination.stem + "_reviews" + destination.suffix)
                rcount = 0
                with rpath.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(REVIEW_COLUMNS)
                    for row in db.iter_reviews(since=since):
                        w.writerow(list(row))
                        rcount += 1
                log.info("exported %d reviews -> %s", rcount, rpath)
            return 0

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


EMITS = ("businesses", "place-urls", "websites")


def _business_csv_row(row: tuple) -> list:
    """Project one ``iter_businesses`` row into CSV column order."""
    (cid, ftid, name, place_id, categories, address, phone, phone_intl,
     website, email, rating, review_count, reviews_url, lat, lng,
     plus_code, hours, price_level, business_status, description,
     neighborhood, owner_claimed, query, added) = row
    return [name, categories, address, phone, phone_intl, website, email,
            rating or "", review_count or "", lat or "", lng or "",
            plus_code, hours.replace("|", "; ") if hours else "",
            price_level, business_status,
            f"https://maps.google.com/?cid={cid}", place_id, query, added]


def validate(settings: Settings) -> int:
    """Preflight check: config problems + pending job count (no network)."""
    setup_logging(settings.verbose)
    problems = settings.validate()
    if problems:
        for problem in problems:
            log.error("CONFIG PROBLEM: %s", problem)
        return 2
    log.info("config OK | mode=%s | workers=%d | max_pages=%d",
             settings.emit, settings.workers, settings.max_pages)
    db = Database(settings.db_path)
    try:
        if settings.grid:
            lat, lng, bbox = grid_mod.geocode(settings.location)
            n = len(grid_mod.make_grid(bbox, settings.cell_km,
                                       radius_km=settings.radius_km))
            log.info("pending jobs: %d grid cells around %.4f,%.4f",
                     n, lat, lng)
        else:
            pending = _pending_jobs(settings, db)
            log.info("pending jobs: %d", len(pending))
            if pending:
                log.info("first: %r", pending[0].key)
                log.info("last:  %r", pending[-1].key)
    except Exception as exc:
        log.error("query plan failed: %s", exc)
        return 2
    finally:
        db.close()
    log.info("ready to run.")
    return 0
