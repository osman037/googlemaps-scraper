"""Grid mode: split a location into map cells to beat the result cap.

Google Maps returns at most ~120 results per search no matter how many
businesses actually match. The fix: geocode the location once (OpenStreetMap
Nominatim - free, no key), tile its bounding box with overlapping cells, and
run one viewport-scoped search per cell. Overlap + the database's cid
dedupe absorb both the boundary gaps and the duplicate hits.

Usage inside the scraper is via ``--all``; this module also runs standalone
as a cell-math self-check:  ``python -m google_maps_scraper.grid``
"""

from __future__ import annotations

import json
import logging
import math
from urllib.parse import quote
from urllib.request import Request, urlopen

from .models import Job

log = logging.getLogger("google_maps_scraper.grid")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
USER_AGENT = "google-maps-scraper/2.0 (educational; +github)"

#: Hard ceiling so a country-sized location cannot explode into
#: millions of cells silently.
MAX_CELLS = 2000


def geocode(location: str) -> tuple[float, float, list[float]]:
    """Resolve a location name to its center + bounding box.

    Returns:
        ``(lat, lng, [south, north, west, east])``.

    Raises:
        RuntimeError: When the location cannot be resolved.
    """
    url = NOMINATIM_URL.format(q=quote(location))
    req = Request(url, headers={"User-Agent": USER_AGENT,
                                "Accept-Language": "en"})
    with urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
        data = json.loads(resp.read().decode("utf-8"))
    if not data:
        raise RuntimeError(f"location not found: {location!r} (Nominatim)")
    hit = data[0]
    lat, lng = float(hit["lat"]), float(hit["lon"])
    south, north, west, east = (float(x) for x in hit["boundingbox"])
    return lat, lng, [south, north, west, east]


def make_grid(bbox: list[float], cell_km: float, overlap: float = 0.25,
              radius_km: float = 0.0) -> list[Job]:
    """Tile a bounding box with overlapping viewport-scoped cell jobs.

    Args:
        bbox: ``[south, north, west, east]`` (Nominatim order).
        cell_km: Cell edge length in km.
        overlap: Fraction of a cell that overlaps its neighbours.
        radius_km: Optional cap - only cells whose center is within this
            radius of the bbox center are kept (0 = use the full bbox).

    Returns:
        Cell jobs ordered row-major (deterministic => resumable).
    """
    south, north, west, east = bbox
    if radius_km > 0:
        # shrink the bbox to the requested radius around its center
        mid_lat, mid_lng = (south + north) / 2, (west + east) / 2
        dlat = radius_km / 111.32
        dlng = radius_km / (111.32 * max(0.1, math.cos(math.radians(mid_lat))))
        south, north = mid_lat - dlat, mid_lat + dlat
        west, east = mid_lng - dlng, mid_lng + dlng

    step_km = cell_km * (1 - overlap)
    lat_steps = max(1, math.ceil((north - south) * 111.32 / step_km))
    mid_lat = (south + north) / 2
    lng_deg_per_km = 111.32 * max(0.1, math.cos(math.radians(mid_lat)))
    lng_km = (east - west) * lng_deg_per_km
    lng_steps = max(1, math.ceil(lng_km / step_km))

    if lat_steps * lng_steps > MAX_CELLS:
        raise RuntimeError(
            f"grid too large: {lat_steps}x{lng_steps} cells "
            f"({lat_steps * lng_steps} > {MAX_CELLS}). Use --cell-km / "
            "--radius-km to bound the area.")

    span_m = cell_km * 1000 * (1 + overlap)  # viewport slightly wider than a cell
    jobs: list[Job] = []
    for i in range(lat_steps):
        for j in range(lng_steps):
            lat = south + (i + 0.5) * (north - south) / lat_steps
            lng = west + (j + 0.5) * (east - west) / lng_steps
            jobs.append(Job(query="", lat=round(lat, 6), lng=round(lng, 6),
                            span_m=span_m))
    return jobs


if __name__ == "__main__":
    # self-check: cell math invariants on a synthetic Austin-sized bbox
    bbox = [30.0, 30.4, -98.0, -97.5]  # ~44km x ~48km
    for cell_km, expect_min in ((2.0, 150), (5.0, 30)):
        jobs = make_grid(bbox, cell_km)
        assert len(jobs) >= expect_min, (cell_km, len(jobs))
        latv = sorted({j.lat for j in jobs})
        lngv = sorted({j.lng for j in jobs})
        assert abs(latv[0] - bbox[0]) < cell_km / 111.32
        assert all(0 < j.span_m for j in jobs)
        assert len(jobs) == len(latv) * len(lngv)
        print(f"cell_km={cell_km}: {len(jobs)} cells "
              f"({len(latv)}x{len(lngv)}) OK")
    # radius cap (bounds the bbox to ~2r x ~2r, corners included)
    jobs = make_grid(bbox, 2.0, radius_km=5.0)
    assert len(jobs) < 80, len(jobs)
    print(f"radius cap: {len(jobs)} cells OK")
    # job keys are unique and stable
    keys = [j.key for j in jobs]
    assert len(set(keys)) == len(keys)
    print("GRID SELF-CHECK PASSED")
