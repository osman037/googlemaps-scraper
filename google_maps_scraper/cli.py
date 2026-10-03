"""Command line interface.

Commands:

* ``scrape``   - the one you want: category + location (or a free query)
                 -> businesses.csv with names, phones, websites, emails...
* ``run``      - advanced: crawl the (category x city) grid from state files
* ``report``   - stored progress stats (no network)
* ``export``   - regenerate a clean deliverable file from the DB
* ``validate`` - preflight: config problems + pending job count

Examples::

    python main.py scrape --category "dentist" --location "Austin, TX"
    python main.py scrape --category "dentist" --location "Austin, TX" --all
    python main.py scrape --query "dentists in Austin, TX" --no-emails
    python main.py run --states TX,CA --workers 6
    python main.py export --out delivery.csv --since 2026-10-01T00:00:00+00:00
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import engine
from .config import EMIT_CHOICES, Settings


def build_parser() -> argparse.ArgumentParser:
    """Assemble the CLI argument tree (shared defaults across commands)."""
    parser = argparse.ArgumentParser(
        prog="google_maps_scraper",
        description="Google Maps scraper - pure HTTP, no browser, no API key.",
        epilog="See README.md for the full operations guide.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, for_run: bool) -> None:
        p.add_argument("--emit", choices=EMIT_CHOICES, default="businesses",
                       help="dataset to produce (default: businesses)")
        p.add_argument("--proxy-file", default="config/proxies.txt",
                       help="proxy list file; missing file = direct connection")
        p.add_argument("--direct", action="store_true",
                       help="ignore the proxy file entirely and run on "
                            "your own connection (small tests only)")
        p.add_argument("--db", default=None,
                       help="SQLite state file path "
                            "(default: state.sqlite3 in the project root)")
        if for_run:
            p.add_argument("--workers", type=int, default=2,
                           help="parallel worker threads (default: 2)")
            p.add_argument("--request-timeout", type=float, default=45.0,
                           help="per-request timeout in seconds")
            p.add_argument("--fresh", action="store_true",
                           help="wipe state and start over")
            p.add_argument("--research", action="store_true",
                           help="re-run queries already marked done")
            p.add_argument("--verbose", action="store_true",
                           help="DEBUG logging on the console too")

    def add_output(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out",
                       help="deliverable file path - write the data "
                            "anywhere you like (default: businesses.csv "
                            "in the project root)")
        p.add_argument("--reviews-out",
                       help="reviews CSV path (default: reviews.csv)")

    def add_pacing(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-pages", type=int, default=10,
                       help="pages per query, 20 results each; "
                            "0 = unlimited (default: 10)")
        p.add_argument("--min-interval", type=float, default=1.5,
                       help="per-proxy minimum seconds between requests")
        p.add_argument("--delay-min", type=float, default=8.0,
                       help="worker page-to-page delay lower bound")
        p.add_argument("--delay-max", type=float, default=16.0,
                       help="worker page-to-page delay upper bound")
        p.add_argument("--pool-wait", type=float, default=120.0,
                       help="seconds to wait for a healthy proxy when "
                            "ALL are cooling down; after this the run "
                            "halts (state committed) instead of using "
                            "the real IP (default: 120)")
        p.add_argument("--rotate-per-request", action="store_true",
                       help="acquire a fresh proxy for EVERY request "
                            "(fast mode for gateways that rotate "
                            "residential IPs themselves)")
        p.add_argument("--lang", default="en",
                       help="results language code (default: en)")
        p.add_argument("--gl", default="us",
                       help="results country code (default: us)")

    # ------------------------------------------------------------ scrape
    sc = sub.add_parser(
        "scrape", help="scrape one category+location or a free query")
    add_common(sc, for_run=True)
    add_output(sc)
    add_pacing(sc)
    sc.add_argument("--category",
                    help="business category, e.g. \"dentist\"")
    sc.add_argument("--location",
                    help="location, e.g. \"Austin, TX\"")
    sc.add_argument("--query",
                    help="single free-form search query (bypasses "
                         "category/location)")
    sc.add_argument("--all", dest="all_", action="store_true",
                    help="grid mode: tile the location with cells and "
                         "scrape ALL matching businesses, not just the "
                         "first ~120 results")
    sc.add_argument("--cell-km", type=float, default=2.0,
                    help="grid cell edge in km (default: 2)")
    sc.add_argument("--radius-km", type=float, default=0.0,
                    help="cap the grid to this radius around the location "
                         "center (default: 0 = use the city boundary)")
    sc.add_argument("--no-details", action="store_true",
                    help="skip the place-details follow-up (no hours, "
                         "review counts, plus codes)")
    sc.add_argument("--no-emails", action="store_true",
                    help="skip business-website email extraction")
    sc.add_argument("--reviews", action="store_true",
                    help="also fetch review text (best-effort: Google "
                         "gates review text when signed out in some "
                         "regions)")
    sc.add_argument("--max-reviews", type=int, default=20,
                    help="max review pages*10 per place with --reviews "
                         "(default: 20)")
    sc.add_argument("--collect-websites", action="store_true",
                    help="also store raw website URLs from the payload "
                         "(bonus dataset)")
    sc.set_defaults(func=lambda ns: engine.run(Settings.from_args(ns)))

    # ---------------------------------------------------------------- run
    run_p = sub.add_parser(
        "run", help="advanced: crawl the (category x city) state-file grid")
    add_common(run_p, for_run=True)
    add_output(run_p)
    add_pacing(run_p)
    run_p.add_argument("--states",
                       help="comma-separated state codes, e.g. TX,CA")
    run_p.add_argument("--categories", default="config/categories.txt",
                       help="categories file (one Google category per line)")
    run_p.add_argument("--locations-dir", default="data/locations/states",
                       help="directory with per-state Census JSON files")
    run_p.add_argument("--limit-cities", type=int, default=0,
                       help="only first N cities per state (testing)")
    run_p.add_argument("--limit-queries", type=int, default=0,
                       help="only first N pending queries (testing)")
    run_p.add_argument("--collect-websites", action="store_true",
                       help="also store raw website URLs (bonus dataset)")
    run_p.set_defaults(func=lambda ns: engine.run(Settings.from_args(ns)))

    # ------------------------------------------------------------- report
    report_p = sub.add_parser("report", help="show stored progress stats")
    add_common(report_p, for_run=False)
    report_p.add_argument("--out", help="(unused, kept for flag parity)")
    report_p.set_defaults(func=lambda ns: engine.report(Settings.from_args(ns)))

    # ------------------------------------------------------------- export
    export_p = sub.add_parser(
        "export", help="regenerate a clean deliverable file from the DB")
    add_common(export_p, for_run=False)
    add_output(export_p)
    export_p.add_argument("--since",
                          help="only rows added at/after this ISO timestamp")
    export_p.add_argument("--reviews", action="store_true",
                          help="also export the reviews CSV (businesses "
                               "datasets only)")
    export_p.set_defaults(func=lambda ns: engine.export(
        Settings.from_args(ns), out_path=ns.out, since=ns.since,
        emit=ns.emit, reviews=ns.reviews))

    # ----------------------------------------------------------- validate
    val_p = sub.add_parser(
        "validate", help="preflight: config problems + pending job count")
    add_common(val_p, for_run=True)
    add_pacing(val_p)
    val_p.add_argument("--category", help="business category")
    val_p.add_argument("--location", help="location")
    val_p.add_argument("--query", help="single free-form search query")
    val_p.add_argument("--all", dest="all_", action="store_true",
                       help="count grid cells instead")
    val_p.add_argument("--cell-km", type=float, default=2.0)
    val_p.add_argument("--radius-km", type=float, default=0.0)
    val_p.add_argument("--states", help="comma-separated state codes")
    val_p.add_argument("--categories", default="config/categories.txt")
    val_p.add_argument("--locations-dir", default="data/locations/states")
    val_p.add_argument("--limit-cities", type=int, default=0)
    val_p.add_argument("--limit-queries", type=int, default=0)
    val_p.add_argument("--no-details", action="store_true")
    val_p.add_argument("--no-emails", action="store_true")
    val_p.add_argument("--reviews", action="store_true")
    val_p.add_argument("--max-reviews", type=int, default=20)
    val_p.add_argument("--collect-websites", action="store_true")
    val_p.set_defaults(func=lambda ns: engine.validate(Settings.from_args(ns)))

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - non-tty fallback
        pass
    ns = build_parser().parse_args(argv)
    try:
        return int(ns.func(ns) or 0)
    except KeyboardInterrupt:
        print("\nInterrupted - state is saved; re-run the same command to resume.")
        return 130
