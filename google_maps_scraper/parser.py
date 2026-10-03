"""Response parsing: raw Maps payloads -> structured records.

Two payload shapes flow through here and they share one record layout:

* search pages (``tbm=map``) - 20 result records per page, discovery fields
* place details (``/maps/preview/place``) - one record with the deep fields

Both are ``)]}'``-prefixed JSON arrays. Records are located by *signature*
(a long list whose ``[10]`` is a feature id string and ``[11]`` a name), not
by a fixed container path, so Google reshuffling wrapper indices does not
break extraction. Field indices inside a record are read defensively:
when Google moves a field it degrades to empty, never to a crash.

Regex fallback: if the body is not valid JSON we still extract ftid+name
pairs through :data:`google_maps_scraper.constants.PLACE_RE` so pagination
detection keeps working.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .constants import (
    EMAIL_JUNK,
    EMAIL_JUNK_HOSTS,
    EMAIL_RE,
    GOOGLE_HOST_RE,
    GOOGLEISH_SUBSTRINGS,
    MEDIA_EXT,
    PLACE_RE,
    REVIEWS_RPCID,
    SITE_RE,
)
from .models import Business, Review

# Pre-compiled cleanup of JS string escapes inside the response blob.
_UNESCAPE = {
    "\\u003d": "=",
    "\\u0026": "&",
    "\\/": "/",
}

_FTID_RE = re.compile(r"^0x[0-9a-f]{12,}:0x[0-9a-f]{10,}$", re.I)
_PRICE_RE = re.compile(r"^\$[1-4]{1,4}$")
_RELATIVE_DATE_RE = re.compile(
    r"^(?:a|an|\d+)\s+(?:second|minute|hour|day|week|month|year)s?\s+ago$", re.I)


def _get(node, *indices, default=None):
    """Defensively descend ``node`` through ``indices``; default when absent."""
    cur = node
    for i in indices:
        if not isinstance(cur, list) or i >= len(cur):
            return default
        cur = cur[i]
    return cur


def _strip_xssi(text: str) -> str:
    """Drop the ``)]}'`` XSSI prefix and any line noise before the JSON."""
    if text.startswith(")]}'"):
        text = text[4:]
    return text.lstrip("\r\n")


def _load_json_array(text: str):
    """Parse the payload body; returns the decoded object or ``None``."""
    body = _strip_xssi(text)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    # some endpoints wrap the payload: {"c":N,"d":"<json string>"}
    if isinstance(data, dict) and isinstance(data.get("d"), str):
        try:
            return json.loads(data["d"])
        except (json.JSONDecodeError, ValueError):
            return None
    return data


def _is_record(node) -> bool:
    """Signature of one place record: long list, ftid at [10], name at [11]."""
    return (
        isinstance(node, list) and len(node) > 50
        and isinstance(_get(node, 10), str) and bool(_FTID_RE.match(node[10]))
        and isinstance(_get(node, 11), str) and len(node[11]) > 1
    )


def _find_records(node, out: list | None = None) -> list:
    """Collect every place record anywhere in the payload tree."""
    if out is None:
        out = []
    if isinstance(node, list):
        if _is_record(node):
            out.append(node)
        for item in node:
            _find_records(item, out)
    return out


def _record_to_business(rec: list, query: str = "") -> Business | None:
    """Map one record (index-verified layout) onto a :class:`Business`.

    Deep fields (review count, hours, plus code, ...) only exist in the
    details payload; they simply stay empty for search-page records.
    """
    ftid = _get(rec, 10, default="")
    cid = ""
    try:
        cid = str(int(ftid.split(":")[1], 16))
    except (IndexError, ValueError):
        return None
    if cid == "0":
        return None

    b = Business(cid=cid, ftid=ftid, name=_get(rec, 11, default="").strip())
    b.place_id = _get(rec, 78, default="") or _get(rec, 227, 0, 4, default="") or ""
    cats = _get(rec, 13, default=[])
    if isinstance(cats, str):
        cats = [cats]
    b.categories = [c for c in (cats or []) if isinstance(c, str)]

    lines = [x for x in (_get(rec, 2, default=[]) or [])
             if isinstance(x, str)] or None
    b.address = ", ".join(lines) if lines else (
        _get(rec, 39, default="") or _get(rec, 18, default="") or "")

    b.rating = _get(rec, 4, 7, default=0.0) or 0.0
    b.review_count = _get(rec, 4, 8, default=0) or 0
    reviews_block = _get(rec, 4, 3, default=None)
    if isinstance(reviews_block, list) and reviews_block:
        b.reviews_url = reviews_block[0] or ""
        if not b.review_count:
            m = re.search(r"([\d,]+)", _get(rec, 4, 3, 1, default="") or "")
            if m:
                b.review_count = int(m.group(1).replace(",", ""))

    b.lat = _get(rec, 9, 2, default=0.0) or 0.0
    b.lng = _get(rec, 9, 3, default=0.0) or 0.0
    site = _get(rec, 7, 0, default="")
    b.website = site if isinstance(site, str) else ""

    phone = _get(rec, 178, 0, default=None)
    if isinstance(phone, list):
        p0 = _get(phone, 0, default="")
        b.phone = p0 if isinstance(p0, str) else ""
        p3 = _get(phone, 3, default="")
        b.phone_intl = p3 if isinstance(p3, str) else ""

    b.neighborhood = _get(rec, 14, default="") or ""
    desc = _get(rec, 154, 0, default="")
    if isinstance(desc, list):  # occasionally nested: [[text]]
        desc = next((x for x in desc
                     if isinstance(x, str) and len(x) > 20), "")
    b.description = desc if isinstance(desc, str) else ""
    owner = _get(rec, 57, 1, default="")
    b.owner_claimed = bool(owner and "(Owner)" in owner)
    plus = _get(rec, 183, 2, 2, 0, default="")
    b.plus_code = plus if isinstance(plus, str) else ""
    b.hours = _parse_hours(_get(rec, 203, 0, default=None))

    # price level / closure status live in different spots per category -
    # pick them by signature instead of a brittle index
    blob = json.dumps(rec, ensure_ascii=False)
    m = _PRICE_RE.search(blob)
    if m:
        b.price_level = m.group(0)
    if re.search(r'"Permanently closed"', blob):
        b.business_status = "CLOSED_PERMANENTLY"
    elif re.search(r'"Temporarily closed"', blob):
        b.business_status = "CLOSED_TEMPORARILY"
    b.query = query
    return b


def _parse_hours(block) -> list[str]:
    """``["Monday: 8 AM–5 PM", ...]`` from the details hours block."""
    out: list[str] = []
    if not isinstance(block, list):
        return out
    for day in block:
        if not (isinstance(day, list) and day and isinstance(day[0], str)):
            continue
        spans = _get(day, 3, default=None)
        text = ""
        if isinstance(spans, list) and spans and isinstance(spans[0], list):
            text = spans[0][0] if isinstance(spans[0][0], str) else ""
        out.append(f"{day[0]}: {text}" if text else f"{day[0]}: Closed")
    return out


def parse_businesses(text: str, query: str = "") -> list[Business]:
    """Extract every business from one search page.

    Returns:
        One :class:`Business` per unique ftid, in document order. Falls
        back to regex ftid+name extraction when the body is not JSON.
    """
    data = _load_json_array(text)
    if data is None:
        blob = unescape_response(_strip_xssi(text))
        seen: set[str] = set()
        out: list[Business] = []
        for ftid, name in PLACE_RE.findall(blob):
            if ftid in seen:
                continue
            seen.add(ftid)
            try:
                cid = str(int(ftid.split(":")[1], 16))
            except (IndexError, ValueError):
                continue
            if cid == "0":
                continue
            out.append(Business(cid=cid, ftid=ftid, name=name.strip(),
                                query=query))
        return out

    records, seen, out = _find_records(data), set(), []
    for rec in records:
        b = _record_to_business(rec, query)
        if b is None or b.ftid in seen:
            continue
        seen.add(b.ftid)
        out.append(b)
    return out


def parse_place_details(text: str, query: str = "") -> Business | None:
    """Parse one ``/maps/preview/place`` response into a Business."""
    data = _load_json_array(text)
    if data is None:
        return None
    records = _find_records(data)
    return _record_to_business(records[0], query) if records else None


def _looks_like_review(entry: list) -> bool:
    """A review entry: has a relative date string and a 1-5 rating inside."""
    if not isinstance(entry, list):
        return False
    if not any(_RELATIVE_DATE_RE.match(s) for s in _iter_strings(entry)):
        return False
    return any(
        isinstance(x, (int, float)) and not isinstance(x, bool)
        and 1 <= x <= 5
        for x in _iter_numbers(entry)
    )


def _iter_numbers(node):
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_numbers(item)


def parse_reviews(text: str, place_cid: str = "") -> tuple[list[Review], str]:
    """Best-effort parse of one reviews batchexecute response.

    Google gates full review text for signed-out sessions in some regions
    (the payload then carries ``[null, ..., true]``); in that case the
    result is simply empty. Review entries are located by signature
    (relative-date string + 1-5 rating), not fixed indices, because no
    live fixture was available to pin the layout.

    Returns:
        ``(reviews, next_cursor)`` - cursor is ``""`` when exhausted.
    """
    # ponytail: signature-based without a live fixture - re-validate the
    # indices against a real payload the first time a region returns data
    body = _strip_xssi(text)
    reviews: list[Review] = []
    cursor = ""
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            arr = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        for frame in (arr if isinstance(arr, list) else []):
            if not (isinstance(frame, list) and len(frame) > 2
                    and frame[0] == "wrb.fr"
                    and (len(frame) < 2 or frame[1] == REVIEWS_RPCID)):
                continue
            try:
                inner = json.loads(frame[2])
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if not isinstance(inner, list):
                continue
            candidates = _get(inner, 2, default=None)
            if not isinstance(candidates, list):
                candidates = [x for x in inner if isinstance(x, list)]
            for entry in candidates:
                if not _looks_like_review(entry):
                    continue
                r = Review(place_cid=place_cid)
                texts = [x for x in _iter_strings(entry) if len(x) > 40]
                r.text = max(texts, key=len) if texts else ""
                dates = [x for x in _iter_strings(entry)
                         if _RELATIVE_DATE_RE.match(x)]
                r.date = dates[0] if dates else ""
                nums = [x for x in _iter_numbers(entry) if 1 <= x <= 5]
                r.rating = float(nums[0]) if nums else 0.0
                names = [x for x in _iter_strings(entry)
                         if 0 < len(x) <= 40 and not x.startswith("http")
                         and not _RELATIVE_DATE_RE.match(x)]
                r.reviewer_name = names[0] if names else ""
                ids = [x for x in _iter_strings(entry)
                       if len(x) > 20 and x.startswith("Z")]
                r.review_id = ids[0] if ids else ""
                if r.text or r.reviewer_name:
                    reviews.append(r)
            for x in inner:
                if isinstance(x, str) and len(x) > 15 and not x.startswith("http"):
                    cursor = x
    return reviews, cursor


def _iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)


def extract_emails(html: str) -> list[str]:
    """Extract contact emails from a business website's HTML.

    Filters assets/junk (``noreply``, schema.org, sentry, image filenames);
    order-preserving, deduplicated, lower-cased.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in EMAIL_RE.findall(html or ""):
        email = m.strip(".").lower()
        local, _, host = email.partition("@")
        if not host or any(j in email for j in EMAIL_JUNK):
            continue
        if host in EMAIL_JUNK_HOSTS or host.endswith(MEDIA_EXT):
            continue  # placeholder hosts and asset filenames ("icon@2x.png")
        if host.count(".") > 4 or len(local) > 64:
            continue
        if re.match(r"^[0-9a-f]{16,}$", local):  # hash-like image names
            continue
        if email not in seen:
            seen.add(email)
            out.append(email)
    return out


def unescape_response(text: str) -> str:
    """Decode the JS escapes Google injects inside the data blob.

    ``\\/`` (escaped slashes) and ``\\u003d`` (escaped ``=``) appear inside
    URLs; without decoding them, extracted URLs are truncated and unusable.
    """
    for old, new in _UNESCAPE.items():
        text = text.replace(old, new)
    return text


def extract_websites(text: str) -> list[str]:
    """Extract candidate business-website URLs from one response page.

    Only URLs appearing in Google's ``["url","domain"]`` website field are
    returned (booking/app links live elsewhere in the payload and are
    naturally excluded). Google-owned and asset URLs are filtered out.

    Returns:
        Raw URL strings, deduplicated per page, in document order.
    """
    blob = unescape_response(text)
    out: list[str] = []
    seen: set[str] = set()
    for url, _domain in SITE_RE.findall(blob):
        if url in seen:
            continue
        seen.add(url)
        try:
            host = urlparse(url).netloc.lower()
            path = urlparse(url).path.lower()
        except ValueError:
            continue
        # Filter on the HOST only: utm parameters legitimately contain
        # "google" (utm_source=google comes from Google Business Profiles).
        if any(g in host for g in GOOGLEISH_SUBSTRINGS):
            continue
        if path.endswith(MEDIA_EXT):
            continue
        out.append(url.rstrip(".,;:)"))
    return out


def normalize_url(url: str) -> tuple[str, str] | None:
    """Normalize a website URL into its dedupe key.

    The key is ``host + path`` with: lowercase host, ``www.`` stripped,
    query string and fragment dropped (they carry tracking params such as
    ``utm_*`` that would otherwise split one site into many "unique" URLs).

    Returns:
        ``(norm_key, cleaned_raw_url)`` or ``None`` when the URL is not a
        usable business website (Google-owned hosts, asset files, garbage).
    """
    url = url.strip().rstrip(".,;:)")
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    host = (parts.netloc or "").split("@")[-1].split(":")[0].lower().strip()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if not host.rsplit(".", 1)[-1].isalpha():
        return None
    if GOOGLE_HOST_RE.match(host):
        return None
    if parts.path.lower().endswith(MEDIA_EXT):
        return None
    norm = host + parts.path.rstrip("/")
    return norm, url
