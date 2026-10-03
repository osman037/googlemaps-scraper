"""Logging configuration: console only.

Everything the run does is streamed to the terminal: concise INFO lines
(progress, per-query results, warnings) by default, full DEBUG detail with
``--verbose``. Nothing is written to disk — no log files, no log directory —
so the scraper leaves behind only the deliverable file and the state DB.

All output passes through :class:`SecretsFilter`, which scrubs proxy
credentials from every log line, so pasting terminal output for support
never leaks passwords.
"""

from __future__ import annotations

import logging
import re
import sys

_ROOT = "google_maps_scraper"
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


def setup_logging(verbose: bool = False) -> None:
    """Configure the package logger (console sink). Safe to call repeatedly.

    Args:
        verbose: When True, the console shows DEBUG lines as well.
    """
    root = logging.getLogger(_ROOT)
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    console.addFilter(SecretsFilter())
    root.addHandler(console)
