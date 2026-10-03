"""Shared data structures passed between the pipeline stages.

Only tiny types live here so that every module can depend on them
without circular imports:

* :class:`Verdict` - the outcome classification of one HTTP response.
* :class:`PageResult` - a classified response returned by the HTTP client.
* :class:`Business` - one business listing extracted by the parser.
* :class:`Review` - one customer review of a business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    """Classification of a single response, driving the retry policy.

    Attributes:
        OK: Valid Google data (response body starts with ``)]}'``).
        BLOCK: Google is refusing this identity - CAPTCHA page, ``/sorry/``
            redirect or a consent wall. The proxy must be cooled down.
        RATE: HTTP 429/503 - throttled. Treated like a block but usually
            recovers faster.
        BAD: A 200 response that is not valid Maps data (transient Google
            hiccup, empty shell, HTML error page). Worth retrying.
        NET: Transport-level failure (DNS, TLS, timeout, connection reset).
    """

    OK = "ok"
    BLOCK = "block"
    RATE = "rate"
    BAD = "bad"
    NET = "net"

    @property
    def is_blockish(self) -> bool:
        """True for verdicts that should trigger proxy cooldown/rotation."""
        return self in (Verdict.BLOCK, Verdict.RATE)


@dataclass(slots=True)
class Job:
    """One unit of crawl work - either a text search or a grid cell.

    Text search jobs carry only ``query`` (``"dentist in Austin, TX"``).
    Grid-cell jobs additionally carry the cell anchor and viewport span so
    the request can target that specific patch of the map (this is how the
    ~120-results-per-query cap is beaten: one job per cell).

    Attributes:
        query: Search text (the category, or the full text query).
        lat / lng: Grid-cell anchor coordinates (``None`` for text jobs).
        span_m: Viewport span in meters for grid-cell jobs.
    """

    query: str
    lat: float | None = None
    lng: float | None = None
    span_m: float | None = None

    @property
    def key(self) -> str:
        """Stable resume-state key for this job."""
        if self.lat is None:
            return self.query
        return f"{self.query} @ {self.lat:.4f},{self.lng:.4f} r{int(self.span_m or 0)}"


@dataclass(slots=True)
class PageResult:
    """Outcome of one page fetch, ready for the worker's policy layer.

    Attributes:
        verdict: Classification of the response.
        text: Response body when ``verdict`` is ``OK`` (else ``None``).
        status_code: HTTP status code, when a response was received.
        error: Transport error description for ``NET`` verdicts.
        url: Final URL after redirects (useful for debugging blocks).
    """

    verdict: Verdict
    text: str | None = None
    status_code: int | None = None
    error: str | None = None
    url: str | None = None


@dataclass(slots=True)
class Business:
    """One business listing extracted from a Maps response.

    ``cid`` is the stable place identity (dedupe key). Search responses
    populate the discovery fields; the place-details response fills in the
    deep fields (hours, review count, plus code, description).

    Attributes:
        cid: Numeric Google customer/place id (decimal string).
        ftid: Raw feature id ``"0xAAA:0xBBB"``.
        name: Business name.
        place_id: Google place id (``ChIJ...``), when present.
        categories: Business category strings, primary first.
        address: Formatted street address.
        phone: Display phone number.
        phone_intl: E.164 phone number (``+1512...``).
        website: Business website URL.
        rating: Average star rating (1.0-5.0).
        review_count: Total number of reviews.
        reviews_url: Link to the reviews listing.
        lat / lng: Coordinates.
        plus_code: Open Location Code (``"66XV+7J Austin, Texas"``).
        hours: ``["Monday: 8 AM–5 PM", ...]`` weekday text.
        price_level: ``$``..``$$$$`` when the category carries one.
        business_status: ``OPERATIONAL`` / ``CLOSED_TEMPORARILY`` /
            ``CLOSED_PERMANENTLY``.
        description: Business-provided description (details only).
        neighborhood: Locality area shown in search results.
        owner_claimed: Owner responded/claimed marker (details only).
        email: Enriched contact email (from the business website).
        query: The search query that surfaced this place (provenance).
    """

    cid: str = ""
    ftid: str = ""
    name: str = ""
    place_id: str = ""
    categories: list[str] = field(default_factory=list)
    address: str = ""
    phone: str = ""
    phone_intl: str = ""
    website: str = ""
    rating: float = 0.0
    review_count: int = 0
    reviews_url: str = ""
    lat: float = 0.0
    lng: float = 0.0
    plus_code: str = ""
    hours: list[str] = field(default_factory=list)
    price_level: str = ""
    business_status: str = ""
    description: str = ""
    neighborhood: str = ""
    owner_claimed: bool = False
    email: str = ""
    query: str = ""

    @property
    def maps_url(self) -> str:
        """Stable shareable deep link for this place."""
        return f"https://maps.google.com/?cid={self.cid}"


@dataclass(slots=True)
class Review:
    """One customer review (best-effort; Google gates review text when
    signed out in some regions - the worker tolerates empty payloads).

    Attributes:
        review_id: Google's unique review id (dedupe key).
        place_cid: Which business this review belongs to.
        reviewer_name: Display name of the reviewer.
        reviewer_url: Profile link, when present.
        rating: Star rating of the review.
        text: Review body.
        date: Relative date string as shown by Google (``"2 weeks ago"``).
        owner_reply: Business owner's reply text, when present.
    """

    review_id: str = ""
    place_cid: str = ""
    reviewer_name: str = ""
    reviewer_url: str = ""
    rating: float = 0.0
    text: str = ""
    date: str = ""
    owner_reply: str = ""
