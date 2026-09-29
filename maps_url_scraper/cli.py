"""Command line interface.

Commands:

* ``run``      - execute a crawl (or resume an interrupted one)
* ``report``   - stored progress stats (no network)
* ``export``   - regenerate a clean deliverable file from the DB
* ``validate`` - preflight: config problems + pending query count

Examples::

    python main.py validate --states WY --limit-cities 5
    python main.py run --query "dentist in Austin, TX" --max-pages 3
    python main.py run --states TX,CA --workers 6 \\
        --categories data/categories_full.txt
    python main.py export --out delivery_batch1.txt --since 2026-10-01T00:00:00+00:00
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
        prog="maps_url_scraper",
        description="Google Maps URL scraper - pure HTTP, production grade.",
        epilog="See README.md for the full operations guide.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, for_run: bool) -> None:
        p.add_argument("--emit", choices=EMIT_CHOICES, default="place-urls",
                       help="dataset to produce (default: place-urls)")
        p.add_argument("--categories", default="config/categories.txt",
                       help="categories file (one Google category per line)")
        p.add_argument("--proxy-file", default="config/proxies.txt",
                       help="proxy list file; missing file = direct connection")
        p.add_argument("--locations-dir", default="data/locations/states",
                       help="directory with per-state Census JSON files")
        p.add_argument("--out-dir", default="output",
                       help="output/state/logs directory")
        if for_run:
            p.add_argument("--query",
                           help="single search query (bypasses locations)")
            p.add_argument("--states",
                           help="comma-separated state codes, e.g. TX,CA")
            p.add_argument("--limit-cities", type=int, default=0,
                           help="only first N cities per state (testing)")
            p.add_argument("--limit-queries", type=int, default=0,
                           help="only first N pending queries (testing)")
            p.add_argument("--workers", type=int, default=2,
                           help="parallel worker threads (default: 2)")
            p.add_argument("--max-pages", type=int, default=10,
                           help="pages per query, 20 results each (default: 10)")
            p.add_argument("--min-interval", type=float, default=1.5,
                           help="per-proxy minimum seconds between requests")
            p.add_argument("--delay-min", type=float, default=0.8,
                           help="worker page-to-page delay lower bound")
            p.add_argument("--delay-max", type=float, default=2.2,
                           help="worker page-to-page delay upper bound")
            p.add_argument("--request-timeout", type=float, default=45.0,
                           help="per-request timeout in seconds")
            p.add_argument("--collect-websites", action="store_true",
                           help="also store business websites in "
                                "place-urls mode (bonus dataset)")
            p.add_argument("--fresh", action="store_true",
                           help="wipe state and start over")
            p.add_argument("--research", action="store_true",
                           help="re-run queries already marked done")
            p.add_argument("--verbose", action="store_true",
                           help="DEBUG logging on the console too")

    run_p = sub.add_parser("run", help="run (or resume) a crawl")
    add_common(run_p, for_run=True)
    run_p.set_defaults(func=lambda ns: engine.run(Settings.from_args(ns)))

    report_p = sub.add_parser("report", help="show stored progress stats")
    add_common(report_p, for_run=False)
    report_p.set_defaults(func=lambda ns: engine.report(Settings.from_args(ns)))

    export_p = sub.add_parser(
        "export", help="regenerate a clean deliverable file from the DB")
    add_common(export_p, for_run=False)
    export_p.add_argument("--out", help="destination file path")
    export_p.add_argument("--since",
                          help="only rows added at/after this ISO timestamp")
    export_p.set_defaults(func=lambda ns: engine.export(
        Settings.from_args(ns), out_path=ns.out, since=ns.since,
        emit=ns.emit))

    val_p = sub.add_parser(
        "validate", help="preflight: config problems + pending query count")
    add_common(val_p, for_run=True)
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
