"""Logging configuration.

Two sinks with different jobs:

* **Console** - what a human watching the run needs: concise INFO lines
  (progress, per-query results, warnings). ``--verbose`` upgrades it to DEBUG.
* **Rotating file** - the full DEBUG trail (every verdict, retry and page
  outcome) in ``output/logs/scraper.log``, rotated at 10 MB with 5 backups.
  This is what you attach when diagnosing "why did my run stall at 3 AM".

All output passes through :class:`SecretsFilter`, which scrubs proxy
credentials from every log line, so pasting logs for support never leaks
passwords.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_ROOT = "maps_url_scraper"
_MASK_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


class SecretsFilter(logging.Filter):
    """Scrub ``scheme://user:password@`` patterns from log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = _MASK_RE.sub(r"\1***@", message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(log_dir: str | Path, verbose: bool = False) -> None:
    """Configure the package loggers. Safe to call multiple times.

    Args:
        log_dir: Directory for the rotating log file (created if missing).
        verbose: When True, the console shows DEBUG lines as well.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(_ROOT)
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    console.addFilter(SecretsFilter())
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "scraper.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"))
    file_handler.addFilter(SecretsFilter())
    root.addHandler(file_handler)
