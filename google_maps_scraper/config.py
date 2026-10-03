"""Runtime configuration.

Every tunable knob lives in one validated :class:`Settings` object, built
from CLI arguments by :meth:`Settings.from_args`. Nothing else in the
package reads ``sys.argv`` or hard-codes paths, so the scraper can also be
driven programmatically (tests, schedulers) by constructing ``Settings``
directly.

``Settings.validate()`` implements fail-fast: any problem (missing
categories file, unreadable proxy file, nonsense worker count) is reported
as a plain-English list before a single request is made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .proxy_pool import load_proxies

#: Output dataset selectors.
EMIT_CHOICES = ("businesses", "place-urls", "websites")

#: Directory containing this package (``google_maps_scraper/google_maps_scraper``).
_PACKAGE_DIR = Path(__file__).resolve().parent
#: Project root (the folder with ``main.py``, ``config/``, ``data/``).
PROJECT_ROOT = _PACKAGE_DIR.parent


@dataclass(slots=True)
class Settings:
    """All runtime knobs for one scrape run.

    Defaults are tuned for safe continuous operation; see the README for
    scaling guidance (workers vs proxies, depth vs coverage).
    """

    # ---- dataset selection
    emit: str = "businesses"            # "businesses" | "place-urls" | "websites"
    collect_websites: bool = False      # also store websites in other modes
    rotate_per_request: bool = False    # fresh proxy identity for EVERY request
                                        # (fast mode - gateways like ScraperAPI
                                        # rotate residential IPs themselves)

    # ---- enrichment depth (businesses mode; each is a follow-up request
    #      per newly found place through the normal proxy policy)
    collect_details: bool = True        # hours, review count, plus code, ...
    collect_emails: bool = True         # fetch the business website for emails
    collect_reviews: bool = False       # review text; gated signed-out in some
                                        # regions (best-effort, degrades silently)
    max_reviews: int = 20               # review-text cap per place

    # ---- locale
    lang: str = "en"                    # hl parameter
    gl: str = "us"                      # gl parameter

    # ---- what to scrape
    query: str | None = None            # single text query (bypasses locations)
    category: str | None = None         # scrape: business category
    location: str | None = None         # scrape: "Austin, TX" (text or grid)
    grid: bool = False                  # --all: split the location into cells
    cell_km: float = 2.0                # grid cell edge (smaller = more thorough)
    radius_km: float = 0.0              # grid radius cap; 0 = use the geocoded
                                        # city boundary (bbox) as-is
    states: str | None = None           # "TX,CA" | None = all
    limit_cities: int = 0               # first N cities per state (0 = all)
    limit_queries: int = 0              # first N pending queries (0 = all)
    research: bool = False              # re-run already-done queries

    # ---- inputs
    categories_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "categories.txt")
    proxy_file: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "proxies.txt")
    locations_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "locations" / "states")

    # ---- outputs / state (user-customizable via --out / --db)
    output_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "businesses.csv")
    reviews_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "reviews.csv")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "state.sqlite3")

    # ---- concurrency & pacing
    workers: int = 2
    max_pages: int = 10                 # pages per query (20 results each); 0 = unlimited
    min_interval: float = 1.5           # per-proxy minimum seconds between requests
    delay_min: float = 8.0              # human-like page-to-page gap (same IP):
    delay_max: float = 16.0             # randomised band, never a fixed interval

    # ---- resilience
    request_timeout: float = 45.0
    transport_retries: int = 2
    page_attempts: int = 4              # attempts per page (with proxy rotation)
    query_attempts: int = 3             # requeues per failed query per run
    pool_wait_secs: float = 120.0       # wait this long for a healthy proxy
                                        # when ALL are cooling; then halt the
                                        # run (never fall back to the real IP)
    breaker_window: int = 24
    breaker_threshold: float = 0.35
    breaker_cooldown: float = 180.0

    # ---- housekeeping
    direct: bool = False                # ignore the proxy file entirely
                                        # (small local tests on your own IP)
    fresh: bool = False
    verbose: bool = False

    @classmethod
    def from_args(cls, ns) -> "Settings":
        """Build a Settings from an argparse namespace, resolving paths.

        Relative paths are resolved against the *project root* (the folder
        containing ``main.py``), not the current working directory, so cron
        jobs and IDE run-configurations behave identically.
        """
        settings = cls(
            emit=ns.emit,
            collect_websites=getattr(ns, "collect_websites", False),
            rotate_per_request=getattr(ns, "rotate_per_request", False),
            collect_details=not getattr(ns, "no_details", False),
            collect_emails=not getattr(ns, "no_emails", False),
            collect_reviews=getattr(ns, "reviews", False),
            max_reviews=getattr(ns, "max_reviews", 20) or 0,
            lang=getattr(ns, "lang", "en") or "en",
            gl=getattr(ns, "gl", "us") or "us",
            # run/validate-only flags are absent on report/export namespaces
            query=getattr(ns, "query", None),
            category=getattr(ns, "category", None),
            location=getattr(ns, "location", None),
            grid=bool(getattr(ns, "all_", False) or getattr(ns, "grid", False)),
            cell_km=getattr(ns, "cell_km", 2.0),
            radius_km=getattr(ns, "radius_km", 0.0) or 0.0,
            states=getattr(ns, "states", None),
            limit_cities=getattr(ns, "limit_cities", 0) or 0,
            limit_queries=getattr(ns, "limit_queries", 0) or 0,
            research=getattr(ns, "research", False),
            workers=getattr(ns, "workers", 2),
            max_pages=getattr(ns, "max_pages", 10),
            min_interval=getattr(ns, "min_interval", 1.5),
            delay_min=getattr(ns, "delay_min", 8.0),
            delay_max=getattr(ns, "delay_max", 16.0),
            request_timeout=getattr(ns, "request_timeout", 45.0),
            pool_wait_secs=getattr(ns, "pool_wait", 120.0),
            direct=getattr(ns, "direct", False),
            fresh=getattr(ns, "fresh", False),
            verbose=getattr(ns, "verbose", False),
        )

        def resolve(raw: str) -> Path:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

        settings.categories_path = resolve(
            getattr(ns, "categories", None) or "config/categories.txt")
        settings.proxy_file = resolve(ns.proxy_file)
        settings.locations_dir = resolve(
            getattr(ns, "locations_dir", None) or "data/locations/states")
        settings.db_path = resolve(getattr(ns, "db", None) or "state.sqlite3")
        out = getattr(ns, "out", None)
        if out:
            settings.output_path = resolve(out)
        elif settings.emit == "place-urls":
            settings.output_path = PROJECT_ROOT / "place_urls.txt"
        reviews_out = getattr(ns, "reviews_out", None)
        if reviews_out:
            settings.reviews_path = resolve(reviews_out)
        return settings

    def proxies(self) -> list[str]:
        """Load (and thereby validate) the proxy list. Empty when no file."""
        if self.direct or not self.proxy_file.exists():
            return []
        return load_proxies(self.proxy_file)

    # ------------------------------------------------------------ checks
    def validate(self) -> list[str]:
        """Return human-readable problems; empty list means ready to run."""
        problems: list[str] = []
        if self.emit not in EMIT_CHOICES:
            problems.append(f"emit must be one of {EMIT_CHOICES}, got {self.emit!r}")
        if self.grid:
            if not self.location:
                problems.append("grid mode (--all) needs --location")
            if not self.category:
                problems.append("grid mode (--all) needs --category")
            if self.cell_km <= 0:
                problems.append(f"cell-km must be > 0, got {self.cell_km}")
        elif not self.query:
            if self.category and not self.location:
                problems.append("--category needs --location (or use --query)")
            if self.location and not self.category:
                problems.append("--location needs --category (or use --query)")
            if not self.category and not self.location:
                # legacy full-grid mode: state files + categories must exist
                if not self.locations_dir.exists():
                    problems.append(
                        f"locations dir not found: {self.locations_dir}")
                elif not any(self.locations_dir.glob("*.json")):
                    problems.append(
                        f"no state JSON files in {self.locations_dir}")
                if not self.categories_path.exists():
                    problems.append(
                        f"categories file not found: {self.categories_path}")
        if self.workers < 1:
            problems.append(f"workers must be >= 1, got {self.workers}")
        if self.max_pages < 0:
            problems.append(
                f"max_pages must be >= 0 (0 = unlimited pagination), "
                f"got {self.max_pages}")
        if self.max_reviews < 0:
            problems.append(f"max-reviews must be >= 0, got {self.max_reviews}")
        if self.min_interval < 0:
            problems.append(f"min_interval must be >= 0, got {self.min_interval}")
        if self.pool_wait_secs < 0:
            problems.append(f"pool_wait must be >= 0, got {self.pool_wait_secs}")
        if self.delay_min > self.delay_max:
            problems.append("delay_min must be <= delay_max")
        # proxies: a malformed file is a hard error (the user thinks they
        # have N proxies but the pool ends up empty -> the run would
        # degrade to the direct connection silently otherwise).
        # An absent file is allowed by design (= direct connection mode).
        if self.direct:
            return problems  # --explicitly no proxies - skip the file check
        if self.proxy_file.exists():
            try:
                proxies = self.proxies()
            except Exception as exc:
                problems.append(f"proxy file unreadable: {exc}")
            else:
                if not proxies:
                    problems.append(
                        f"proxy file exists but contains no usable proxies: "
                        f"{self.proxy_file}")
        return problems
