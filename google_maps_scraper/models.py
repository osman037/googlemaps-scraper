"""Shared data structures passed between the pipeline stages.

Only three tiny types live here so that every module can depend on them
without circular imports:

* :class:`Verdict` - the outcome classification of one HTTP response.
* :class:`PageResult` - a classified response returned by the HTTP client.
* :class:`PlaceRecord` - one business listing extracted by the parser.
"""

from __future__ import annotations

from dataclasses import dataclass
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
class PlaceRecord:
    """One business listing extracted from a search response page.

    Attributes:
        cid: Numeric Google customer/place id (decimal string). The stable
            identity of the place and the dedupe key for place-URL output.
        ftid: Raw feature id ``"0xAAA:0xBBB"``; ``cid = int(BBB, 16)``.
        name: Business name as shown by Google (for QA/observability).
    """

    cid: str
    ftid: str
    name: str
