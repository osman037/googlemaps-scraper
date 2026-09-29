"""Query plan builder: (category, city) pairs -> Google Maps search strings.

Locations come from the Census TIGERweb exports in
``../data/locations/states/*.json`` (one file per state, with every
incorporated place and CDP; the combined file is ``../data/locations/
us_locations.json``). Categories come from a plain text file (one Google
Maps category per line, ``#`` comments).

The search string format is ``"{category} in {city}, {ST}"``. Census names
carry place-type suffixes (``"Isleton city"``) which are stripped first -
searching them verbatim degrades Google's local results.

Resume logic lives with the caller: :func:`build_queries` accepts the set of
already-``done`` queries and only returns the pending remainder.
"""

from __future__ import annotations

import json
from pathlib import Path

from .constants import CITY_SUFFIXES


def strip_city_suffix(name: str) -> str:
    """Remove a trailing Census place-type suffix from a city name.

    >>> strip_city_suffix("Amador City city")
    'Amador City'
    """
    n = (name or "").strip()
    low = n.lower()
    for suffix in CITY_SUFFIXES:
        if low.endswith(suffix) and len(n) > len(suffix):
            return n[: -len(suffix)].strip()
    return n


def load_categories(path: str | Path) -> list[str]:
    """Load the active categories from a ``categories*.txt`` file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file contains no active (non-comment) lines.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"categories file not found: {p}")
    cats = [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not cats:
        raise ValueError(f"no active categories in {p}")
    return cats


def build_queries(
    locations_dir: str | Path,
    categories_path: str | Path,
    states: str | None = None,
    limit_cities: int = 0,
    done: set[str] | None = None,
) -> list[str]:
    """Build the full pending query list.

    Args:
        locations_dir: Directory with the per-state Census JSON files.
        categories_path: Categories file (see :func:`load_categories`).
        states: Comma-separated state codes filter (``"TX,CA"``); ``None``
            means every state file.
        limit_cities: Only use the first N cities per state (testing).
        done: Queries to skip (already finished in a previous run).

    Returns:
        Pending search strings in deterministic (state, city, category) order.

    Raises:
        FileNotFoundError: If the locations directory has no state files.
    """
    cats = load_categories(categories_path)
    done = done or set()
    files = [
        p for p in sorted(Path(locations_dir).glob("*.json"))
        if p.name not in ("usa_locations.json", "us_locations.json")
    ]
    if not files:
        raise FileNotFoundError(f"no state files in {locations_dir}")
    wanted_states = (
        {s.strip().upper() for s in states.split(",") if s.strip()}
        if states else None
    )

    queries: list[str] = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        code = (data.get("state") or {}).get("code", "").upper()
        if wanted_states and code not in wanted_states:
            continue
        cities = list((data.get("cities") or {}).values())
        if limit_cities:
            cities = cities[:limit_cities]
        for city in cities:
            name = strip_city_suffix(city.get("name") or "")
            if not name:
                continue
            location = f"{name}, {code}"
            for cat in cats:
                query = f"{cat} in {location}"
                if query not in done:
                    queries.append(query)
    return queries
