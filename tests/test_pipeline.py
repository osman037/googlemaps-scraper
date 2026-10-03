"""End-to-end pipeline check with a stubbed HTTP client (no network).

Run:  python tests/test_pipeline.py
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_maps_scraper import engine, worker as worker_mod  # noqa: E402
from google_maps_scraper.config import Settings  # noqa: E402
from google_maps_scraper.models import PageResult, Verdict  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class FakeClient:
    """MapsClient stand-in replaying the captured fixtures."""

    def __init__(self, *a, **kw):
        pass

    def fetch_search_page(self, job, offset, proxy_url=None):
        # same fixture on every page: page 0 adds 20, page 1 adds 0 -> stop
        return PageResult(verdict=Verdict.OK, text=_fixture("search_page_0.txt"),
                          status_code=200)

    def fetch_place_details(self, ftid, lat, lng, proxy_url=None):
        return PageResult(verdict=Verdict.OK, text=_fixture("place_details.txt"),
                          status_code=200)

    def fetch_reviews(self, ftid, source_path, cursor="", page_size=10,
                      proxy_url=None):
        # gated/empty payload shape - must be tolerated
        return PageResult(verdict=Verdict.OK,
                          text=_fixture("place_reviews.txt"), status_code=200)

    def fetch_website(self, url, proxy_url=None):
        return PageResult(verdict=Verdict.OK,
                          text='<html><a href="mailto:Hi@Example-Clinic.com">'
                               'contact hello@clinic-site.org</a></html>',
                          status_code=200)

    def close(self):
        pass


def main():
    tmp = Path(tempfile.mkdtemp(prefix="gmaps_pipeline_"))
    settings = Settings(
        query="dentist in Austin, TX",
        db_path=tmp / "state.sqlite3",
        output_path=tmp / "businesses.csv",
        reviews_path=tmp / "reviews.csv",
        max_pages=2,
        delay_min=0.0, delay_max=0.0,
        min_interval=0.0,
        workers=1,
        pool_wait_secs=0.0,
        collect_details=True, collect_emails=True, collect_reviews=True,
        emit="businesses",
        proxy_file=tmp / "no-proxies.txt",
        categories_path=tmp / "no-cats.txt",
        locations_dir=tmp / "no-loc",
    )
    with mock.patch.object(worker_mod, "MapsClient", FakeClient):
        code = engine.run(settings)
    assert code == 0, code

    import csv

    with open(settings.output_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 20, f"expected 20 rows, got {len(rows)}"
    first = rows[0]
    assert first["name"] == "ATX Family Dental", first["name"]
    assert first["email"] == "hi@example-clinic.com", first["email"]
    assert first["hours"].startswith("Saturday"), first["hours"]
    assert int(first["review_count"]) > 0, first["review_count"]
    assert first["google_maps_url"].startswith("https://maps.google.com/?cid=")
    assert first["phone"].startswith("(512)")

    # header + 0 review rows (gated payload) - file must exist and be valid
    with open(settings.reviews_path, encoding="utf-8") as fh:
        header = fh.readline().strip()
    assert header.startswith("place_cid"), header

    # resume: nothing pending on a second run
    with mock.patch.object(worker_mod, "MapsClient", FakeClient):
        code = engine.run(settings)
    assert code == 0

    from google_maps_scraper.database import Database
    db = Database(settings.db_path)
    counts = db.counts()
    assert counts["places"] == 20, counts
    assert counts["emails"] == 20, counts
    assert counts["searches_done"] == 1, counts
    db.close()

    # clean CSV export from the DB matches the live file
    out = tmp / "export.csv"
    assert engine.export(settings, out_path=out) == 0
    with open(out, encoding="utf-8") as fh:
        exported = list(csv.DictReader(fh))
    assert len(exported) == 20
    assert exported[0]["email"] == "hi@example-clinic.com"

    print(f"  run: 20 places | emails {counts['emails']}/20 | "
          f"csv + export both valid | resume skipped re-work")
    print("PIPELINE TEST PASSED")


if __name__ == "__main__":
    main()
