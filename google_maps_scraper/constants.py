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
    "https://www.google.com/search?tbm=map&hl=en&gl=us&q={query}&pb={pb}"
)

#: Cookies that pre-empt the Google consent wall (validated against both
#: consent.google.com redirects and plain search endpoints).
CONSENT_COOKIES = {
    "SOCS": "CAISHAgBEhJnd3NfMjAyMzAyMjgtMF9SQzEaAmVuIAEaBgiA_LyaBg",
    "CONSENT": "YES+cb",
}

#: Headers sent with every request (curl_cffi adds the browser UA itself).
DEFAULT_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}


def build_search_url(query: str, offset: int) -> str:
    """Build the fully-encoded URL for one page of a Maps text search.

    Args:
        query: Search text, e.g. ``"dentist in Austin, TX"``.
        offset: Result offset; must be a multiple of ``RESULTS_PER_PAGE``.

    Returns:
        The complete request URL with the encoded ``pb`` parameter.
    """
    pb = SEARCH_PB_TEMPLATE.replace("{query}", quote_plus(query)).replace(
        "{offset}", str(offset)
    )
    return SEARCH_URL_TEMPLATE.format(query=quote_plus(query), pb=quote(pb, safe=""))


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
