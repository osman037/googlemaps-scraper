"""Append-only deliverable files (one URL per line).

The deliverable the client sees is a plain text file with exactly one URL per
line and nothing else. Writes happen only for **newly inserted** database
rows (the DB is the dedupe authority), are guarded by a lock, and are flushed
immediately so the file can be tailed while the crawl runs.

If a process dies between a DB commit and the file write, the file can end
up missing a few lines - that is why :meth:`maps_url_scraper.engine.export`
regenerates a guaranteed-complete file straight from the database for
delivery.
"""

from __future__ import annotations

import threading
from pathlib import Path


class OutputWriter:
    """Thread-safe append-only URL file.

    Args:
        path: Target file path (created on first write).
        fresh: Truncate the file instead of appending (``--fresh`` runs).
    """

    def __init__(self, path: str | Path, fresh: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("w" if fresh else "a", encoding="utf-8")

    def write_lines(self, lines: list[str]) -> int:
        """Append lines and flush; returns the number of lines written."""
        if not lines:
            return 0
        with self._lock:
            self._fh.write("\n".join(lines) + "\n")
            self._fh.flush()
        return len(lines)

    def close(self) -> None:
        """Flush and close the file handle."""
        with self._lock:
            try:
                self._fh.close()
            except Exception:  # pragma: no cover - best effort
                pass
