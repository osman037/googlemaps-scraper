"""Assert-based parser checks against captured live fixtures.

Run:  python tests/test_parser.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_maps_scraper.parser import (  # noqa: E402
    extract_emails,
    parse_businesses,
    parse_place_details,
    parse_reviews,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def test_search_page():
    text = _load("search_page_0.txt")
    bs = parse_businesses(text, "dentist in Austin, TX")
    assert len(bs) == 20, f"expected 20 records, got {len(bs)}"
    first = bs[0]
    assert first.name == "ATX Family Dental"
    assert first.cid and first.ftid.startswith("0x")
    assert first.maps_url == f"https://maps.google.com/?cid={first.cid}"
    assert first.rating == 4.9
    assert "Dentist" in first.categories
    assert first.address.startswith("1700 S 1st St")
    assert first.phone and first.phone_intl.startswith("+1")
    assert first.website.startswith("https://www.atxfamilydental.com")
    assert 30.24 < first.lat < 30.25 and -97.76 < first.lng < -97.75
    assert first.place_id.startswith("ChIJ")
    for b in bs:
        assert b.phone and b.website and b.address and b.rating, f"incomplete: {b}"
    assert all(b.query == "dentist in Austin, TX" for b in bs)
    print(f"  search page: {len(bs)} records, all core fields present")


def test_place_details():
    text = _load("place_details.txt")
    b = parse_place_details(text, "dentist in Austin, TX")
    assert b is not None and b.name == "ATX Family Dental"
    assert b.review_count > 0, f"review_count={b.review_count}"
    assert b.rating == 4.9
    assert len(b.hours) == 7 and b.hours[0].startswith("Saturday:")
    assert b.plus_code.startswith("66XV+7J")
    assert b.description
    assert b.owner_claimed
    assert b.phone_intl == "+15127173147"
    print(f"  details: {b.review_count} reviews, {len(b.hours)} hour rows, "
          f"plus_code ok")


def test_reviews_empty_payload():
    # Google returns [null,...,true] for gated signed-out sessions -
    # the parser must tolerate it and return nothing
    revs, cursor = parse_reviews(_load("place_reviews.txt"), "42")
    assert revs == [] and cursor == ""
    print("  reviews: gated payload handled (empty, no cursor)")


def test_reviews_synthetic():
    # signature-based extraction on a synthetic batchexecute frame
    import json
    entry = [["http://profile/1", "Jane Doe", None, "avatar"],
             ["A great dentist, gentle and thorough. " * 3,
              "2 weeks ago", None], 5, None, "Zxyz123456789012345678"]
    inner = json.dumps([None, "CURSOR_TOKEN_123456", [entry]])
    line = json.dumps([["wrb.fr", "qv9Egd", inner, None, None, "generic"]])
    body = ")]}'\n\n123\n" + line + "\n25\n[[\"e\",4,null,null,123]]"
    revs, cursor = parse_reviews(body, "42")
    assert len(revs) == 1, f"expected 1 review, got {len(revs)}"
    r = revs[0]
    assert r.reviewer_name == "Jane Doe"
    assert r.text.startswith("A great dentist")
    assert r.date == "2 weeks ago"
    assert r.rating == 5.0
    assert r.place_cid == "42"
    assert cursor == "CURSOR_TOKEN_123456"
    print("  reviews: synthetic frame parsed (name/date/text/rating/cursor)")


def test_extract_emails():
    html = """
    <a href="mailto:Info@Dental-Example.com">mail</a>
    <a href="mailto:noreply@wixpress.com">no</a>
    <img src="ab12cd34ef56aaab@example-assets.com/x.png">
    <img src="https://cdn.example.com/icon-google@2x.png">
    contact us at hello@clinic-site.org or sales@clinic-site.org
    sentry@sentry.io must not appear
    """
    emails = extract_emails(html)
    assert emails == ["info@dental-example.com", "hello@clinic-site.org",
                      "sales@clinic-site.org"], emails
    print("  emails: mailto + text extraction with junk filtering")


def test_malformed_payloads():
    assert parse_businesses(")]}'\nnot json at all") == []
    assert parse_businesses("") == []
    assert parse_place_details(")]}'\n{}") is None
    assert parse_reviews(")]}'\n42\n[[\"e\",4]]") == ([], "")
    assert extract_emails("") == []
    print("  malformed payloads: no crashes")


if __name__ == "__main__":
    test_search_page()
    test_place_details()
    test_reviews_empty_payload()
    test_reviews_synthetic()
    test_extract_emails()
    test_malformed_payloads()
    print("ALL PARSER TESTS PASSED")
