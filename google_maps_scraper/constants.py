"""Static constants shared by every module.

This module holds the low-level artifacts of the scraping technique: the
``tbm=map`` request templates, the consent cookies that bypass the EU/EEA
wall, and the compiled regular expressions used to parse Google's
protobuf-as-JSON responses. Everything here was reverse-engineered and
empirically validated against live responses; treat changes to
this file as changes to the scraping protocol itself.
"""

from __future__ import annotations

import re
import time
from urllib.parse import quote, quote_plus

# --------------------------------------------------------------------------
# Request construction
# --------------------------------------------------------------------------

#: Number of results returned per paginated request (fixed by Google).
RESULTS_PER_PAGE = 20

#: The ``pb`` protobuf-style parameter for ``tbm=map`` text searches.
#:
#: ``!1s{query}``  - the search text
#: ``!7i20``       - page size (20 results)
#: ``!8i{offset}`` - pagination offset (0, 20, 40, ...). Verified to work
#:                  far beyond the Maps UI's ~120 result cap.
#: The remaining segments act as field selectors matching what the Maps
#: frontend requests; they were captured from a real browser session.
SEARCH_PB_TEMPLATE = (
    "!1s{query}!7i20!8i{offset}!10b1"
    "!12m6!1m1!18b1!2m1!20e3!6m1!114b1"
    "!17m1!3e1"
    "!20m57!2m2!1i203!2i100!3m2!2i4!5b1"
    "!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3"
    "!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2"
    "!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
    "!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
)

#: Base URL of the search endpoint (``hl``/``gl`` pin language and region).
SEARCH_URL_TEMPLATE = (
    "https://www.google.com/search?tbm=map&hl={lang}&gl={gl}&q={query}&pb={pb}"
)

#: Viewport ``pb`` for grid-cell searches: ``!1d{span}!2d{lng}!3d{lat}`` is
#: the map viewport (span in meters) the results are scoped to, and
#: ``!7i20!8i{offset}`` is the page size / offset exactly like text search.
GRID_PB_TEMPLATE = (
    "!4m12!1m3!1d{span_m}!2d{lng}!3d{lat}"
    "!2m3!1f0!2f0!3f0!3m2!1i1280!2i593!4f13.1"
    "!7i20!8i{offset}!10b1"
    "!12m6!1m1!18b1!2m1!20e3!6m1!114b1"
    "!17m1!3e1"
    "!20m57!2m2!1i203!2i100!3m2!2i4!5b1"
    "!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3"
    "!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2"
    "!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
    "!15m8!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
)


def build_grid_search_url(category: str, lat: float, lng: float, span_m: float,
                          offset: int, lang: str = "en", gl: str = "us") -> str:
    """Build the URL for one grid-cell page: category scoped to a viewport."""
    pb = GRID_PB_TEMPLATE.format(span_m=int(span_m), lng=lng, lat=lat,
                                 offset=offset)
    return SEARCH_URL_TEMPLATE.format(
        lang=lang, gl=gl, query=quote_plus(category), pb=quote(pb, safe=""))

#: Cookies that pre-empt the Google consent wall (validated against both
#: consent.google.com redirects and plain search endpoints).
CONSENT_COOKIES = {
    "SOCS": "CAISHAgBEhJnd3NfMjAyMzAyMjgtMF9SQzEaAmVuIAEaBgiA_LyaBg",
    "CONSENT": "YES+cb",
}

#: Headers sent with every request (curl_cffi adds the browser UA itself).
DEFAULT_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}


def build_search_url(query: str, offset: int, lang: str = "en",
                     gl: str = "us") -> str:
    """Build the fully-encoded URL for one page of a Maps text search.

    Args:
        query: Search text, e.g. ``"dentist in Austin, TX"``.
        offset: Result offset; must be a multiple of ``RESULTS_PER_PAGE``.
        lang / gl: Language and region pinning.

    Returns:
        The complete request URL with the encoded ``pb`` parameter.
    """
    pb = SEARCH_PB_TEMPLATE.replace("{query}", quote_plus(query)).replace(
        "{offset}", str(offset)
    )
    return SEARCH_URL_TEMPLATE.format(
        lang=lang, gl=gl, query=quote_plus(query), pb=quote(pb, safe=""))


# --------------------------------------------------------------------------
# Place details + reviews endpoints
# --------------------------------------------------------------------------

#: ``pb`` for the ``/maps/preview/place`` details endpoint. ``!1s{ftid}``
#: carries the place feature id, ``!4m2!3d{lat}!4d{lng}`` the coordinates.
#: The long selector blocks were captured from a real Maps place-page load;
#: Google rejects the request (HTTP 400) when segments are dropped, so
#: treat this template as one indivisible artifact.
PLACE_DETAIL_PB_TEMPLATE = (
    "!1m6!1s{ftid}!3m1!1d1000!4m2!3d{lat}!4d{lng}!3m1!1e3"
    "!15m111!1m29!4e2!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1"
    "!18m17!3b1!4b1!5b1!6b1!9b1!13b1!14b1!17b1!20b1!21b1!22b1!30b1!32b1"
    "!33m1!1b1!34b1!36e2"
    "!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6!24b1!25b1!26b1!27b1!29b1!30m1"
    "!2b1!36b1!37b1"
    "!39m3!2m2!2i1!3i1!43b1!52b1!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1"
    "!65m5!3m4!1m3!1m2!1i224!2i298"
    "!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1"
    "!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4"
    "!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
    "!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1"
    "!103b1!113b1!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b0"
)

#: Base URL of the place-details endpoint (same record layout as search).
PLACE_DETAIL_URL_TEMPLATE = (
    "https://www.google.com/maps/preview/place?authuser=0&hl={lang}&gl={gl}"
    "&pb={pb}"
)

#: RPC id of the internal reviews endpoint (``MapsWizUi/data/batchexecute``).
REVIEWS_RPCID = "qv9Egd"

#: Host segments of the reviews batchexecute endpoint.
REVIEWS_URL_TEMPLATE = (
    "https://www.google.com/maps/_/MapsWizUi/data/batchexecute"
    "?rpcids={rpcid}&source-path={source_path}&hl={lang}"
    "&_reqid={reqid}&rt=c"
)


def build_place_details_url(ftid: str, lat: float, lng: float, lang: str = "en",
                            gl: str = "us") -> str:
    """Build the place-details request URL for one feature id.

    Note: this endpoint only accepts the pb parameter with literal ``!``
    separators (unlike text search, percent-encoding them yields an empty
    payload), so only the ftid colon is percent-encoded.
    """
    pb = PLACE_DETAIL_PB_TEMPLATE.format(
        ftid=quote(ftid, safe=""), lat=lat, lng=lng)
    return PLACE_DETAIL_URL_TEMPLATE.format(lang=lang, gl=gl, pb=pb)


def build_reviews_request(
    ftid: str, source_path: str, cursor: str = "", page_size: int = 10,
    lang: str = "en",
) -> tuple[str, str]:
    """Build the (url, body) of one reviews batchexecute POST.

    Returns:
        ``(url, urlencoded_body)`` ready for a POST request.
    """
    import json as _json

    inner = _json.dumps([
        [[ftid], None, None, None, None, [None, None, None, [[1], [3]]]],
        [page_size, cursor],
        None, None,
        [None, None, None, None, None, None, 81],
        None, None,
        [None, 1, 1, None, 1, None, 1, None, None, None, None,
         [1, 1, None, [[1]]]],
        None, None,
        [3, 1, None, None, None, [2]],
        None, [1],
    ], separators=(",", ":"))
    freq = _json.dumps([[[REVIEWS_RPCID, inner, None, "generic"]]],
                       separators=(",", ":"))
    url = REVIEWS_URL_TEMPLATE.format(
        rpcid=REVIEWS_RPCID, source_path=quote(source_path, safe=""),
        lang=lang, reqid=int(time.time() * 1000) % 100000)
    return url, "f.req=" + quote(freq)


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

#: A place record inside the response: ``"0xAAA:0xBBB","Place Name",null,["Cat``
#: The trailing ``null,[`` anchors the match to real place records and keeps
#: unrelated hex tokens (review cursors etc.) out of the results.
PLACE_RE = re.compile(
    r'"(0x[0-9a-f]{12,}:0x[0-9a-f]{10,})","([^"]{2,120})",null,\["', re.I
)

#: The website field of a place: ``["https://site.example/","site.example"``
SITE_RE = re.compile(r'\["(https?://[^"]+)","([a-z0-9][a-z0-9.-]*\.[a-z]{2,})"')

#: Contact emails inside a fetched business website (mailto or plain text).
EMAIL_RE = re.compile(
    r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.I)

#: Email local-parts / hosts that are never a human contact address
#: (substring match - platform/framework senders).
EMAIL_JUNK = (
    "sentry", "wixpress", "schema.org", "sentry.io", "godaddy.com",
    "cloudflare", "noreply", "no-reply", "cloudaccess",
)

#: Hosts that are verbatim placeholder/junk domains (exact match).
EMAIL_JUNK_HOSTS = (
    "example.com", "domain.com", "email.com", "test.com", "mysite.com",
    "yourdomain.com", "yourdomain.in", "sentry.io", "schema.org",
)

#: Hosts that are Google infrastructure, never a business website.
GOOGLE_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*"
    r"(google\.[a-z]{2,3}(?:\.[a-z]{2,3})?|gstatic\.com|googleusercontent\.com|"
    r"ggpht\.com|googleapis\.com|youtube\.com|youtu\.be|goo\.gl|blogger\.com|"
    r"blogspot\.com|doubleclick\.net|googleadservices\.com|business\.site)$"
)

#: Substrings that identify Google-owned URLs during website filtering.
GOOGLEISH_SUBSTRINGS = (
    "google.", "gstatic.", "goo.gl", "googleapis.", "googleusercontent.",
    "ggpht.", "blogspot.", "blogger.", "youtu", "doubleclick.",
    "googleadservices.",
)

#: File extensions that mark asset URLs (never a business homepage).
MEDIA_EXT = (".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif",
             ".css", ".js", ".woff", ".woff2", ".ttf")

#: Census place-type suffixes stripped from city names before searching
#: (e.g. ``"Isleton city"`` -> ``"Isleton"``) - searching the raw suffix
#: degrades Google's local results.
CITY_SUFFIXES = (" city", " town", " township", " borough", " village",
                 " municipality", " plantation", " cdp",
                 " census designated place")
