"""Append-only deliverable files.

Two deliverable shapes exist:

* **CSV** (default) - one row per business (or review), written only for
  newly inserted database rows (the DB is the dedupe authority), guarded
  by a lock, flushed immediately so the file can be watched while the
  crawl runs. The header is (re)written whenever the file is (re)created.
* **TXT** (legacy place-urls mode) - one URL per line.

If a process dies between a DB commit and the file write, the file can end
up missing a few lines - that is why :meth:`google_maps_scraper.engine.export`
regenerates a guaranteed-complete file straight from the database.
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path

#: Columns of the businesses CSV (matches ``Database.iter_businesses``).
BUSINESS_COLUMNS = [
    "name", "category", "address", "phone", "phone_intl", "website",
    "email", "rating", "review_count", "lat", "lng", "plus_code", "hours",
    "price_level", "business_status", "google_maps_url", "place_id",
    "query", "added",
]

#: Columns of the reviews CSV (matches ``Database.iter_reviews``).
REVIEW_COLUMNS = [
    "place_cid", "reviewer_name", "rating", "text", "date", "owner_reply",
    "added",
]


class CsvWriter:
    """Thread-safe append-only CSV file with a stable header row.

    Args:
        path: Target file path (created on first write).
        columns: Header row; also the column order of every written row.
        fresh: Truncate the file instead of appending (``--fresh`` runs).
    """

    def __init__(self, path: str | Path, columns: list[str],
                 fresh: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.columns = columns
        self._lock = threading.Lock()
        needs_header = fresh or not self.path.exists() or \
            self.path.stat().st_size == 0
        self._fh = self.path.open("w" if fresh else "a", newline="",
                                  encoding="utf-8")
        self._csv = csv.writer(self._fh)
        if needs_header:
            self._csv.writerow(columns)
            self._fh.flush()

    def write_rows(self, rows: list[list]) -> int:
        """Append rows (given in ``columns`` order) and flush."""
        if not rows:
            return 0
        with self._lock:
            self._csv.writerows(rows)
            self._fh.flush()
        return len(rows)

    def business_row(self, values: list) -> list:
        """Project one ``iter_businesses`` row into CSV column order."""
        (cid, ftid, name, place_id, categories, address, phone, phone_intl,
         website, email, rating, review_count, reviews_url, lat, lng,
         plus_code, hours, price_level, business_status, description,
         neighborhood, owner_claimed, query, added) = values
        return [name, categories, address, phone, phone_intl, website, email,
                rating or "", review_count or "", lat or "", lng or "",
                plus_code, hours.replace("|", "; ") if hours else "",
                price_level, business_status,
                f"https://maps.google.com/?cid={cid}", place_id, query, added]

    def flush(self) -> None:
        """Flush buffered writes without closing the file."""
        with self._lock:
            try:
                self._fh.flush()
            except Exception:  # pragma: no cover - best effort
                pass

    def close(self) -> None:
        """Flush and close the file handle."""
        with self._lock:
            try:
                self._fh.close()
            except Exception:  # pragma: no cover - best effort
                pass


class OutputWriter:
    """Thread-safe append-only URL file (legacy place-urls mode).

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

    def flush(self) -> None:
        """Flush buffered writes without closing the file."""
        with self._lock:
            try:
                self._fh.flush()
            except Exception:  # pragma: no cover - best effort
                pass

    def close(self) -> None:
        """Flush and close the file handle."""
        with self._lock:
            try:
                self._fh.close()
            except Exception:  # pragma: no cover - best effort
                pass
