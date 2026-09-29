import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

STATE_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/"
    "services/TIGERweb/State_County/MapServer"
)

PLACE_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/"
    "services/TIGERweb/"
    "Places_CouSub_ConCity_SubMCD/MapServer"
)

# Same layer configuration as your existing script.
LAYERS = {
    "states": (STATE_URL, 0),
    "incorporated": (PLACE_URL, 4),
    "cdp": (PLACE_URL, 5),
    "consolidated": (PLACE_URL, 3),
}

US_FIPS = set("""
01 02 04 05 06 08 09 10 11 12 13
15 16 17 18 19 20 21 22 23 24 25
26 27 28 29 30 31 32 33 34 35 36
37 38 39 40 41 42 44 45 46 47 48
49 50 51 53 54 55 56
""".split())

# Data lands in the shared project data folder:
#   data/locations/states/<fips>_<state>.json   (per-state files)
#   data/locations/us_locations.json            (combined, built at the end)
LOCATIONS_DIR = Path(__file__).resolve().parents[1] / "data" / "locations"
OUTPUT = LOCATIONS_DIR / "states"
OUTPUT.mkdir(parents=True, exist_ok=True)

BATCH_SIZE = 100

session = requests.Session()
retry = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry)
)


def request_json(url, params):
    response = session.get(
        url, params=params, timeout=90
    )
    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data


def save_json(path, data):
    """Atomic save to avoid corrupt JSON."""
    temp = path.with_suffix(".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data, f,
            ensure_ascii=False,
            indent=2
        )

    temp.replace(path)


def fetch_ids(base_url, layer_id, where):
    url = f"{base_url}/{layer_id}/query"

    data = request_json(url, {
        "where": where,
        "returnIdsOnly": "true",
        "f": "json"
    })

    if "objectIds" not in data:
        raise RuntimeError(
            f"Object IDs missing: {url}"
        )

    return sorted(data["objectIds"] or [])


def fetch_batch(base_url, layer_id, ids):
    url = f"{base_url}/{layer_id}/query"

    data = request_json(url, {
        "objectIds": ",".join(map(str, ids)),
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json"
    })

    if data.get("exceededTransferLimit"):
        raise RuntimeError(
            "Batch truncated. Reduce BATCH_SIZE."
        )

    features = data.get("features", [])

    oid_field = (
        data.get("objectIdFieldName")
        or "OBJECTID"
    )

    records = {
        int(f["attributes"][oid_field]):
        f["attributes"]
        for f in features
    }

    missing = set(ids) - set(records)

    if missing:
        raise RuntimeError(
            f"Missing IDs: {sorted(missing)[:10]}"
        )

    return records


def get_states():
    base, layer = LAYERS["states"]
    ids = fetch_ids(base, layer, "1=1")

    states = []

    for i in range(0, len(ids), BATCH_SIZE):
        records = fetch_batch(
            base, layer, ids[i:i+BATCH_SIZE]
        )

        for attrs in records.values():
            fips = str(
                attrs.get("GEOID", "")
            ).zfill(2)

            if fips not in US_FIPS:
                continue

            states.append({
                "name": attrs["NAME"],
                "code": attrs.get("STUSAB"),
                "fips": fips
            })

    if {s["fips"] for s in states} != US_FIPS:
        raise RuntimeError(
            "State completeness check failed"
        )

    return sorted(
        states, key=lambda s: s["name"]
    )


def state_filename(state):
    safe_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        state["name"]
    )

    return OUTPUT / (
        f'{state["fips"]}_{safe_name}.json'
    )


def process_state(state):
    path = state_filename(state)

    if path.exists():
        with path.open(
            encoding="utf-8"
        ) as f:
            result = json.load(f)
    else:
        result = {
            "state": state,
            "cities": {},
            "progress": {},
            "complete": False
        }

    if result.get("complete"):
        print(
            f'SKIPPED: {state["name"]} '
            '(already complete)'
        )
        return

    print(f'\nProcessing: {state["name"]}')

    # Fetch each place type independently.
    for place_type in (
        "incorporated",
        "cdp",
        "consolidated"
    ):
        base, layer = LAYERS[place_type]

        where = (
            f"STATE='{state['fips']}'"
        )

        ids = fetch_ids(
            base, layer, where
        )

        progress = result["progress"].setdefault(
            place_type, {
                "completed_ids": [],
                "expected_count": len(ids)
            }
        )

        progress["expected_count"] = len(ids)

        completed = set(
            progress["completed_ids"]
        )

        pending = [
            oid for oid in ids
            if oid not in completed
        ]

        print(
            f"  {place_type}: "
            f"{len(completed)}/{len(ids)} "
            "already saved"
        )

        for start in range(
            0, len(pending), BATCH_SIZE
        ):
            batch_ids = pending[
                start:start+BATCH_SIZE
            ]

            records = fetch_batch(
                base, layer, batch_ids
            )

            for oid, attrs in records.items():
                geoid = attrs.get("GEOID")

                if not geoid:
                    raise RuntimeError(
                        "Place GEOID missing"
                    )

                key = str(geoid)

                if key not in result["cities"]:
                    result["cities"][key] = {
                        "name": attrs.get("NAME"),
                        "geoid": key,
                        "types": [],
                        "latitude": attrs.get(
                            "CENTLAT"
                        ),
                        "longitude": attrs.get(
                            "CENTLON"
                        )
                    }

                city = result["cities"][key]

                if place_type not in city["types"]:
                    city["types"].append(
                        place_type
                    )

            completed.update(batch_ids)

            progress["completed_ids"] = sorted(
                completed
            )

            result["total_places"] = len(
                result["cities"]
            )

            result["updated_at"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            # Save immediately after every batch.
            save_json(path, result)

            print(
                f"  {place_type}: "
                f"{len(completed)}/{len(ids)} "
                "saved"
            )

            time.sleep(0.2)

        if set(ids) != completed:
            raise RuntimeError(
                f"Incomplete: {place_type}"
            )

    result["complete"] = True
    result["total_places"] = len(
        result["cities"]
    )

    save_json(path, result)

    print(
        f'COMPLETED: {state["name"]} | '
        f'{result["total_places"]} places'
    )


def combine_states(states):
    combined = {
        "source": "US Census TIGERweb",
        "total_states": len(states),
        "states": {}
    }

    for state in states:
        path = state_filename(state)

        with path.open(encoding="utf-8") as f:
            data = json.load(f)

        if not data.get("complete"):
            raise RuntimeError(
                f'Incomplete state: {state["name"]}'
            )

        combined["states"][state["code"]] = {
            "name": state["name"],
            "fips": state["fips"],
            "cities": sorted(
                data["cities"].values(),
                key=lambda x: (
                    x["name"] or "",
                    x["geoid"]
                )
            )
        }

    combined["total_places"] = sum(
        len(s["cities"])
        for s in combined["states"].values()
    )

    save_json(
        LOCATIONS_DIR / "us_locations.json",
        combined
    )

    print(
        "\nFINAL JSON CREATED:",
        combined["total_places"],
        "places"
    )


def main():
    states = get_states()

    print("Total states:", len(states))

    for state in states:
        process_state(state)

    combine_states(states)


if __name__ == "__main__":
    main()