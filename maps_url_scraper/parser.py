"""Response parsing: raw ``tbm=map`` payloads -> structured records.

Google returns a ``)]}'``-prefixed JSON array whose strings embed the place
data. Parsing by structure (index navigation) is brittle because Google
shuffles indices; instead we extract only the three things the product needs
via tightly-anchored regular expressions:

* place records  -> :data:`maps_url_scraper.constants.PLACE_RE`
* website fields -> :data:`maps_url_scraper.constants.SITE_RE`

Both regexes were validated against live responses (exactly 20 place records
per page; ~19/20 places carry an embedded website). Keeping the extraction
surface this small is what makes the parser resilient to cosmetic changes.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .constants import (
    GOOGLE_HOST_RE,
    GOOGLEISH_SUBSTRINGS,
    MEDIA_EXT,
    PLACE_RE,
    SITE_RE,
)
from .models import PlaceRecord

# Pre-compiled cleanup of JS string escapes inside the response blob.
_UNESCAPE = {
    "\\u003d": "=",
    "\\u0026": "&",
    "\\/": "/",
}


def unescape_response(text: str) -> str:
    """Decode the JS escapes Google injects inside the data blob.

    ``\\/`` (escaped slashes) and ``\\u003d`` (escaped ``=``) appear inside
    URLs; without decoding them, extracted URLs are truncated and unusable.
    """
    for old, new in _UNESCAPE.items():
        text = text.replace(old, new)
    return text


def parse_places(text: str) -> list[PlaceRecord]:
    """Extract every unique place record from one response page.

    Args:
        text: Raw response body (escaped or unescaped).

    Returns:
        One :class:`PlaceRecord` per unique ftid on the page, in document
        order. The cid is derived from the ftid's second hex component;
        records with a degenerate cid (``0``) are dropped.
    """
    blob = unescape_response(text)
    records: list[PlaceRecord] = []
    seen: set[str] = set()
    for ftid, name in PLACE_RE.findall(blob):
        if ftid in seen:
            continue
        seen.add(ftid)
        try:
            cid = int(ftid.split(":")[1], 16)
        except (IndexError, ValueError):
            continue
        if cid == 0:
            continue
        records.append(PlaceRecord(cid=str(cid), ftid=ftid, name=name.strip()))
    return records


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
