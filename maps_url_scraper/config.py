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
EMIT_CHOICES = ("place-urls", "websites")

#: Directory containing this package (``maps_url_scraper/maps_url_scraper``).
_PACKAGE_DIR = Path(__file__).resolve().parent
#: Project root (the folder with ``main.py``, ``data/``, ``output/``).
_PROJECT_ROOT = _PACKAGE_DIR.parent


@dataclass(slots=True)
class Settings:
    """All runtime knobs for one scrape run.

    Defaults are tuned for safe continuous operation; see the README for
    scaling guidance (workers vs proxies, depth vs coverage).
    """

    # ---- dataset selection
    emit: str = "place-urls"            # "place-urls" | "websites"
    collect_websites: bool = False      # also store websites in place-urls mode

    # ---- what to scrape
    query: str | None = None            # single-query mode (bypasses locations)
    states: str | None = None           # "TX,CA" | None = all
    limit_cities: int = 0               # first N cities per state (0 = all)
    limit_queries: int = 0              # first N pending queries (0 = all)
    research: bool = False              # re-run already-done queries

    # ---- inputs
    categories_path: Path = field(default_factory=lambda: _PROJECT_ROOT / "config" / "categories.txt")
    proxy_file: Path = field(default_factory=lambda: _PROJECT_ROOT / "config" / "proxies.txt")
    locations_dir: Path = field(
        default_factory=lambda: _PROJECT_ROOT / "data" / "locations" / "states")

    # ---- outputs / state
    out_dir: Path = field(default_factory=lambda: _PROJECT_ROOT / "output")

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
    breaker_window: int = 24
    breaker_threshold: float = 0.35
    breaker_cooldown: float = 180.0

    # ---- housekeeping
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
            # run/validate-only flags are absent on report/export namespaces
            query=getattr(ns, "query", None),
            states=getattr(ns, "states", None),
            limit_cities=getattr(ns, "limit_cities", 0) or 0,
            limit_queries=getattr(ns, "limit_queries", 0) or 0,
            research=getattr(ns, "research", False),
            workers=getattr(ns, "workers", 2),
            max_pages=getattr(ns, "max_pages", 10),
            min_interval=getattr(ns, "min_interval", 1.5),
            delay_min=getattr(ns, "delay_min", 0.8),
            delay_max=getattr(ns, "delay_max", 2.2),
            request_timeout=getattr(ns, "request_timeout", 45.0),
            fresh=getattr(ns, "fresh", False),
            verbose=getattr(ns, "verbose", False),
        )

        def resolve(raw: str) -> Path:
            path = Path(raw).expanduser()
            return path if path.is_absolute() else (_PROJECT_ROOT / path).resolve()

        settings.categories_path = resolve(ns.categories)
        settings.proxy_file = resolve(ns.proxy_file)
        settings.locations_dir = resolve(ns.locations_dir)
        settings.out_dir = resolve(ns.out_dir)
        return settings

    # ------------------------------------------------------------ derived
    @property
    def db_path(self) -> Path:
        """SQLite state file (shared by both emit modes)."""
        return self.out_dir / "state.sqlite3"

    @property
    def log_dir(self) -> Path:
        """Rotating-log directory."""
        return self.out_dir / "logs"

    @property
    def output_path(self) -> Path:
        """Deliverable file matching the selected emit mode."""
        name = "place_urls.txt" if self.emit == "place-urls" else "website_urls.txt"
        return self.out_dir / name

    def proxies(self) -> list[str]:
        """Load (and thereby validate) the proxy list. Empty when no file."""
        if not self.proxy_file.exists():
            return []
        return load_proxies(self.proxy_file)

    # ------------------------------------------------------------ checks
    def validate(self) -> list[str]:
        """Return human-readable problems; empty list means ready to run."""
        problems: list[str] = []
        if self.emit not in EMIT_CHOICES:
            problems.append(f"emit must be one of {EMIT_CHOICES}, got {self.emit!r}")
        if not self.query:
            if not self.locations_dir.exists():
                problems.append(f"locations dir not found: {self.locations_dir}")
            elif not any(self.locations_dir.glob("*.json")):
                problems.append(f"no state JSON files in {self.locations_dir}")
        if not self.categories_path.exists() and not self.query:
            problems.append(f"categories file not found: {self.categories_path}")
        if self.workers < 1:
            problems.append(f"workers must be >= 1, got {self.workers}")
        if self.max_pages < 0:
            problems.append(
                f"max_pages must be >= 0 (0 = unlimited pagination), "
                f"got {self.max_pages}")
        if self.min_interval < 0:
            problems.append(f"min_interval must be >= 0, got {self.min_interval}")
        if self.delay_min > self.delay_max:
            problems.append("delay_min must be <= delay_max")
        # proxies: a malformed file is a hard error (the user thinks they
        # have N proxies but the pool ends up empty -> the run would
        # degrade to the direct connection silently otherwise).
        # An absent file is allowed by design (= direct connection mode).
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
