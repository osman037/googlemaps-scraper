"""SQLite persistence: checkpointing, dedupe state and the export source.

The database is the single source of truth. Output files are append-only
mirrors for live monitoring; the clean deliverable is always regenerated
from here via :meth:`Database.iter_businesses` / :meth:`Database.iter_reviews`
(``export`` command), which also makes milestone exports (``--since``) and
crash recovery trivial.

Tables:

* ``searches`` - one row per query with its status. This is what makes
  runs **resumable**: finished queries are skipped, failed ones are
  retried on the next run.
* ``places``   - every unique business seen, keyed by cid (place identity)
  with all extracted fields; deep fields are merged in from the details
  responses and ``email`` from the website enrichment.
* ``reviews``  - best-effort review text, keyed per place.
* ``urls``     - legacy websites mode (``--emit websites``).
* ``blocks``   - block/rate events with the masked proxy, for tuning.

Concurrency: one shared connection guarded by an RLock; WAL journaling plus
batched transactions (one commit per page of ~20 records) keep it fast even
with millions of rows.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Business, Review

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS searches(
    query TEXT PRIMARY KEY, status TEXT, places INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS places(
    cid TEXT PRIMARY KEY, ftid TEXT, name TEXT, place_id TEXT,
    categories TEXT, address TEXT, phone TEXT, phone_intl TEXT,
    website TEXT, email TEXT, rating REAL, review_count INTEGER,
    reviews_url TEXT, lat REAL, lng REAL, plus_code TEXT, hours TEXT,
    price_level TEXT, business_status TEXT, description TEXT,
    neighborhood TEXT, owner_claimed INTEGER, query TEXT, added TEXT);
CREATE INDEX IF NOT EXISTS idx_places_added ON places(added);
CREATE TABLE IF NOT EXISTS reviews(
    place_cid TEXT, review_id TEXT, reviewer_name TEXT, rating REAL,
    text TEXT, date TEXT, owner_reply TEXT, added TEXT,
    PRIMARY KEY(place_cid, review_id));
CREATE INDEX IF NOT EXISTS idx_reviews_added ON reviews(added);
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
        self._migrate()
        # the schema above is always the current one, so stamp it
        self.con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.con.commit()

    def _migrate(self) -> None:
        """Upgrade older state files in place (v1: bare cids table)."""
        with self.lock:
            row = self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='cids'").fetchone()
            if not row:
                return
            # v1 -> v2: carry the identity columns into `places`, then drop
            self.con.execute(
                "INSERT OR IGNORE INTO places(cid, ftid, name, query, added) "
                "SELECT cid, ftid, name, query, added FROM cids")
            self.con.execute("DROP TABLE cids")
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
    def add_businesses(
        self, records: list[Business], query: str
    ) -> list[str]:
        """Insert a page of place records in one transaction.

        Only identity + discovery fields are written here (first-seen
        wins for provenance); deep fields arrive via
        :meth:`update_place_details`.

        Returns:
            The cids that were **newly inserted** - only those need the
            detail/review/enrichment follow-ups and the output file.
        """
        added: list[str] = []
        now = _now()
        with self.lock:
            try:
                for b in records:
                    cur = self.con.execute(
                        "INSERT OR IGNORE INTO places("
                        "cid, ftid, name, place_id, categories, address, phone, "
                        "phone_intl, website, rating, lat, lng, neighborhood, "
                        "query, added) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (b.cid, b.ftid, b.name, b.place_id,
                         ",".join(b.categories), b.address, b.phone,
                         b.phone_intl, b.website, b.rating, b.lat, b.lng,
                         b.neighborhood, query, now),
                    )
                    if cur.rowcount == 1:
                        added.append(b.cid)
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return added

    def update_place_details(self, b: Business) -> None:
        """Merge deep fields (hours, review count, ...) into an existing row."""
        with self.lock:
            self.con.execute(
                "UPDATE places SET review_count = ?, reviews_url = ?, "
                "plus_code = ?, hours = ?, price_level = ?, "
                "business_status = ?, description = ?, owner_claimed = ?, "
                "rating = CASE WHEN ? > 0 THEN ? ELSE rating END, "
                "phone = CASE WHEN ? != '' THEN ? ELSE phone END, "
                "phone_intl = CASE WHEN ? != '' THEN ? ELSE phone_intl END "
                "WHERE cid = ?",
                (b.review_count, b.reviews_url, b.plus_code,
                 "|".join(b.hours), b.price_level, b.business_status,
                 b.description, int(b.owner_claimed),
                 b.rating, b.rating, b.phone, b.phone, b.phone_intl,
                 b.phone_intl, b.cid),
            )
            self.con.commit()

    def update_place_email(self, cid: str, email: str) -> None:
        """Store an enriched contact email for a place."""
        with self.lock:
            self.con.execute(
                "UPDATE places SET email = ? WHERE cid = ?", (email, cid))
            self.con.commit()

    def pending_places(
        self, cids: list[str], need: str
    ) -> list[str]:
        """Filter cids to those whose ``need`` column is still empty.

        ``need`` is ``details`` (any deep field) or ``email``.
        """
        if not cids:
            return []
        column = {"details": "plus_code", "email": "email"}[need]
        marks = ",".join("?" * len(cids))
        with self.lock:
            rows = self.con.execute(
                f"SELECT cid FROM places WHERE cid IN ({marks}) "
                f"AND ({column} IS NULL OR {column} = '')",
                cids,
            ).fetchall()
        return [r[0] for r in rows]

    def get_place(self, cid: str) -> Business | None:
        """Load one stored place row (for detail/review follow-ups)."""
        with self.lock:
            row = self.con.execute(
                "SELECT cid, ftid, name, place_id, categories, address, phone, "
                "phone_intl, website, email, rating, review_count, "
                "reviews_url, lat, lng, plus_code, hours, price_level, "
                "business_status, description, neighborhood, owner_claimed, "
                "query FROM places WHERE cid = ?", (cid,)).fetchone()
        if not row:
            return None
        return Business(
            cid=row[0], ftid=row[1], name=row[2], place_id=row[3],
            categories=[c for c in (row[4] or "").split(",") if c],
            address=row[5], phone=row[6], phone_intl=row[7], website=row[8],
            email=row[9] or "",
            rating=row[10] or 0.0, review_count=row[11] or 0,
            reviews_url=row[12] or "", lat=row[13] or 0.0, lng=row[14] or 0.0,
            plus_code=row[15] or "", hours=(row[16] or "").split("|"),
            price_level=row[17] or "", business_status=row[18] or "",
            description=row[19] or "", neighborhood=row[20] or "",
            owner_claimed=bool(row[21]), query=row[22] or "",
        )

    # ------------------------------------------------------------ reviews
    def add_reviews(self, reviews: list[Review]) -> int:
        """Insert reviews (deduped per place). Returns rows newly added."""
        now = _now()
        added = 0
        with self.lock:
            try:
                for r in reviews:
                    rid = r.review_id or hashlib.sha1(
                        f"{r.reviewer_name}|{r.date}|{r.text[:64]}".encode()
                    ).hexdigest()
                    cur = self.con.execute(
                        "INSERT OR IGNORE INTO reviews("
                        "place_cid, review_id, reviewer_name, rating, text, "
                        "date, owner_reply, added) VALUES(?,?,?,?,?,?,?,?)",
                        (r.place_cid, rid, r.reviewer_name, r.rating, r.text,
                         r.date, r.owner_reply, now),
                    )
                    added += cur.rowcount
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return added

    # ----------------------------------------------------------- websites
    def add_websites(
        self, items: list[tuple[str, str]], query: str
    ) -> list[str]:
        """Insert normalized website URLs; return the newly seen raw URLs."""
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
            for table, key in (("places", "places"), ("reviews", "reviews"),
                               ("urls", "websites"), ("blocks", "blocks")):
                out[key] = self.con.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            out["emails"] = self.con.execute(
                "SELECT COUNT(*) FROM places WHERE email != ''").fetchone()[0]
        return out

    # ------------------------------------------------------------- export
    def iter_businesses(self, since: str | None = None) -> Iterator[tuple]:
        """Stream full place rows (CSV export source), insertion order.

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
                    f"SELECT rowid, cid, ftid, name, place_id, categories, "
                    f"address, phone, phone_intl, website, email, rating, "
                    f"review_count, reviews_url, lat, lng, plus_code, hours, "
                    f"price_level, business_status, description, "
                    f"neighborhood, owner_claimed, query, added "
                    f"FROM places WHERE rowid > ?{where} "
                    "ORDER BY rowid LIMIT 10000",
                    (last_rowid, *params),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last_rowid = row[0]
                yield row[1:]

    def iter_reviews(self, since: str | None = None) -> Iterator[tuple]:
        """Stream review rows for the reviews CSV export."""
        where, params = "", []
        if since:
            where = " AND added >= ?"
            params.append(since)
        last_rowid = 0
        while True:
            with self.lock:
                rows = self.con.execute(
                    f"SELECT rowid, place_cid, reviewer_name, rating, text, "
                    f"date, owner_reply, added FROM reviews "
                    f"WHERE rowid > ?{where} ORDER BY rowid LIMIT 10000",
                    (last_rowid, *params),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                last_rowid = row[0]
                yield row[1:]

    def iter_cids(self, since: str | None = None) -> Iterator[str]:
        """Stream place cids (legacy place-urls txt export), insertion order."""
        where, params = "", []
        if since:
            where = " AND added >= ?"
            params.append(since)
        last_rowid = 0
        while True:
            with self.lock:
                rows = self.con.execute(
                    f"SELECT rowid, cid FROM places WHERE rowid > ?{where} "
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
