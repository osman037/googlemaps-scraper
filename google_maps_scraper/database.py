"""SQLite persistence: checkpointing, dedupe state and the export source.

The database is the single source of truth. Output text files are append-only
mirrors for live monitoring; the clean deliverable is always regenerated from
here via :meth:`Database.iter_cids` / :meth:`Database.iter_websites`
(``export`` command), which also makes milestone exports (``--since``) and
crash recovery trivial.

Tables:

* ``searches`` - one row per (category, city) query with its status. This is
  what makes runs **resumable**: finished queries are skipped, failed ones
  are retried on the next run.
* ``cids``     - every unique place seen, keyed by cid (place identity).
  Names are kept for QA; the deliverable contains only URLs.
* ``urls``     - every unique normalized business website (websites mode).
* ``blocks``   - block/rate events with the masked proxy, for tuning.

Concurrency: one shared connection guarded by an RLock; WAL journaling plus
batched transactions (one commit per page of ~20 records) keep it fast even
with tens of millions of rows.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS searches(
    query TEXT PRIMARY KEY, status TEXT, places INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS cids(
    cid TEXT PRIMARY KEY, ftid TEXT, name TEXT, query TEXT, added TEXT);
CREATE INDEX IF NOT EXISTS idx_cids_added ON cids(added);
CREATE TABLE IF NOT EXISTS urls(
    norm TEXT PRIMARY KEY, raw TEXT, cid TEXT, query TEXT, added TEXT);
CREATE INDEX IF NOT EXISTS idx_urls_added ON urls(added);
CREATE TABLE IF NOT EXISTS blocks(
    ts TEXT, proxy TEXT, reason TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thread-safe wrapper around the scraper's SQLite state file."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(_SCHEMA)
        self.con.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.con.commit()

    # ----------------------------------------------------------- searches
    def done_queries(self) -> set[str]:
        """Queries with status ``done`` (skipped on the next run)."""
        with self.lock:
            rows = self.con.execute(
                "SELECT query FROM searches WHERE status = 'done'").fetchall()
        return {r[0] for r in rows}

    def record_search(self, query: str, status: str, places: int) -> None:
        """Insert/update the checkpoint row for one query."""
        with self.lock:
            self.con.execute(
                "INSERT INTO searches(query, status, places, updated) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(query) DO UPDATE SET "
                "status = excluded.status, places = excluded.places, "
                "updated = excluded.updated",
                (query, status, places, _now()),
            )
            self.con.commit()

    # ------------------------------------------------------------- places
    def add_places(
        self, rows: list[tuple[str, str, str]], query: str
    ) -> list[str]:
        """Insert a page of place records in one transaction.

        Args:
            rows: ``(cid, ftid, name)`` tuples from :func:`parse_places`.
            query: The search query that surfaced them (provenance).

        Returns:
            The cids that were **newly inserted** - only those belong in the
            output file, everything else is a duplicate across queries/runs.
        """
        added: list[str] = []
        now = _now()
        with self.lock:
            try:
                for cid, ftid, name in rows:
                    cur = self.con.execute(
                        "INSERT OR IGNORE INTO cids(cid, ftid, name, query, added) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (cid, ftid, name, query, now),
                    )
                    if cur.rowcount == 1:
                        added.append(cid)
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return added

    # ----------------------------------------------------------- websites
    def add_websites(
        self, items: list[tuple[str, str]], query: str
    ) -> list[str]:
        """Insert normalized website URLs; return the newly seen raw URLs.

        Args:
            items: ``(norm_key, raw_url)`` tuples from :func:`normalize_url`.
            query: Provenance (search query that surfaced the page).
        """
        added: list[str] = []
        now = _now()
        with self.lock:
            try:
                for norm, raw in items:
                    cur = self.con.execute(
                        "INSERT OR IGNORE INTO urls(norm, raw, cid, query, added) "
                        "VALUES(?, ?, '', ?, ?)",
                        (norm, raw, query, now),
                    )
                    if cur.rowcount == 1:
                        added.append(raw)
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return added

    # -------------------------------------------------------------- blocks
    def record_block(self, proxy_display: str, reason: str) -> None:
        """Log a block/rate event (``proxy_display`` must already be masked)."""
        with self.lock:
            self.con.execute(
                "INSERT INTO blocks(ts, proxy, reason) VALUES(?, ?, ?)",
                (_now(), proxy_display, reason),
            )
            self.con.commit()

    # -------------------------------------------------------------- stats
    def counts(self) -> dict[str, int]:
        """Row counts for the report/summary commands."""
        with self.lock:
            searches = self.con.execute(
                "SELECT status, COUNT(*) FROM searches GROUP BY status").fetchall()
            out: dict[str, int] = {"searches_done": 0, "searches_failed": 0}
            for status, n in searches:
                out[f"searches_{status}"] = n
            for table, key in (("cids", "places"), ("urls", "websites"),
                               ("blocks", "blocks")):
                out[key] = self.con.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out

    # ------------------------------------------------------------- export
    def iter_cids(self, since: str | None = None) -> Iterator[str]:
        """Stream all (or newly-added-since) place cids, insertion order.

        Paginates by rowid in batches so a multi-million-row export never
        loads into memory at once; each batch read holds the lock only
        briefly, letting writers proceed between batches.
        """
        where, params = "", []
        if since:
            where = " AND added >= ?"
            params.append(since)
        last_rowid = 0
        while True:
            with self.lock:
                rows = self.con.execute(
                    f"SELECT rowid, cid FROM cids WHERE rowid > ?{where} "
                    "ORDER BY rowid LIMIT 10000",
                    (last_rowid, *params),
                ).fetchall()
            if not rows:
                return
            for rowid, cid in rows:
                last_rowid = rowid
                yield cid

    def iter_websites(self, since: str | None = None) -> Iterator[tuple[str, str]]:
        """Stream ``(raw_url, added)`` website rows, same batching as above."""
        where, params = "", []
        if since:
            where = " AND added >= ?"
            params.append(since)
        last_rowid = 0
        while True:
            with self.lock:
                rows = self.con.execute(
                    f"SELECT rowid, raw, added FROM urls WHERE rowid > ?{where} "
                    "ORDER BY rowid LIMIT 10000",
                    (last_rowid, *params),
                ).fetchall()
            if not rows:
                return
            for rowid, raw, added in rows:
                last_rowid = rowid
                yield raw, added

    def close(self) -> None:
        """Flush and close the connection."""
        with self.lock:
            self.con.close()
