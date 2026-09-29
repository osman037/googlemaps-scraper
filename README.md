# Google Maps Place URL Scraper

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Browser](<https://img.shields.io/badge/browser-not%20required-success>)

Extract **unique Google Maps place URLs** (`https://maps.google.com/?cid=...`)
for the entire USA — **pure HTTP, no browser, no API key, no Selenium**.
Ship-ready Python package with rotating proxies, retries, rate limiting,
a circuit breaker and resumable state built in.

Works out of the box with **32,079 US cities** (every incorporated place and
Census-designated place, bundled) × **4,044 Google Business Profile
categories** (bundled) — a complete grid for full USA coverage.

## Why this scraper?

- **No browser, no Playwright, no Selenium** — it talks directly to the same
  internal endpoint the Google Maps frontend uses (`tbm=map`), so a page of
  results costs ~1 second instead of ~30.
- **Beats the UI limit** — the Maps UI stops at ~120 results per search;
  this scraper paginates the underlying endpoint far beyond that
  (verified 158+ unique results on a single query).
- **Real URLs, not scraped HTML** — every place becomes a stable,
  permanent `maps.google.com/?cid=` deep link that opens the exact listing.
- **Production-grade by default** — proxy rotation, escalating cooldowns,
  exponential-backoff retries, a global circuit breaker, per-query
  checkpointing, crash-safe resume and rotating log files.
- **Provider-agnostic proxies** — bring any HTTP/SOCKS proxy provider; a
  plain `proxies.txt` is all the configuration there is.

## Output

| Mode                     | File                        | Line format                                          |
| ------------------------ | --------------------------- | ---------------------------------------------------- |
| `place-urls` (default) | `output/place_urls.txt`   | `https://maps.google.com/?cid=5562772415832503806` |
| `websites`             | `output/website_urls.txt` | `https://www.sierracrestdental.com/`               |

Every line is **globally unique** — the same place is never written twice,
no matter how many categories or cities it appears in.

## Quick start

```bash
git clone https://github.com/sonagara-vashram/google-maps-place-url-scraper.git
cd google-maps-place-url-scraper
pip install -r requirements.txt

# 1. (optional but recommended) add proxies
copy config\proxies.txt.example config\proxies.txt   # Windows
# cp config/proxies.txt.example config/proxies.txt   # Linux/macOS

# 2. smoke test - one query, no proxies needed
python main.py run --query "dentist in Austin, TX" --max-pages 3

# 3. real run - one state, 8 parallel workers
python main.py run --states TX --workers 8

# 4. full USA grid - 32k cities x 250 categories
python main.py run --workers 8 --categories config/categories_full.txt
```

That's it. Progress is visible in the console and in
`output/logs/scraper.log`; the deliverable appears in `output/`.

## How it works

```
main.py
  └── maps_url_scraper/
        ├── cli.py            run | report | export | validate
        ├── config.py         every knob, fail-fast validation
        ├── engine.py         lifecycle: workers -> monitor -> summary
        ├── worker.py         crawl policy: proxy pick, pacing, pagination
        ├── http_client.py    curl_cffi + Chrome TLS impersonation
        ├── parser.py         place/website extraction, URL normalisation
        ├── constants.py      the tbm=map protocol (pb template, regexes)
        ├── proxy_pool.py     rotation, cooldowns, credential masking
        ├── rate_limiter.py   per-proxy minimum-interval pacing
        ├── circuit_breaker.py global pause when blocks spike
        ├── database.py       SQLite state: searches, places, urls, blocks
        ├── output.py         append-only deliverable files
        ├── queries.py        locations x categories query plan
        ├── metrics.py        thread-safe counters
        └── logsetup.py       console + rotating file logs
```

The scraper requests

```
https://www.google.com/search?tbm=map&hl=en&gl=us&q={query}&pb={pb}
```

where the `pb` parameter encodes the query, a page size of 20 and an
offset (`!8i20`, `!8i40`, ...). Responses are a `)]}'`-prefixed payload;
each place record carries its feature id (`0xAAA:0xBBB`), display name and
categories, and the place URL is derived as
`https://maps.google.com/?cid={int(0xBBB, 16)}`.

## Proxy configuration

Copy the example and add your proxies, one per line:

```text
# config/proxies.txt
http://user:password@1.2.3.4:8080
socks5://user:password@5.6.7.8:1080
http://user:password@gateway.provider.com:9000
```

- **Residential/ISP proxies** give the best results and keep results
  US-localised.
- No `config/proxies.txt`? The scraper runs through your direct connection
  (fine for a smoke test, risky at scale).
- Blocked proxies are cooled down automatically and rotated out; a proxy
  that keeps failing is replaced for the rest of the run.
- `python try.py` is a one-command health check: it verifies that your
  proxy endpoint rotates IPs and can actually reach Google Maps.

## Configuration

Every knob is a CLI flag (see `python main.py run --help`); the important
ones:

| Flag                   | Default        | Meaning                                               |
| ---------------------- | -------------- | ----------------------------------------------------- |
| `--emit`             | `place-urls` | `place-urls` or `websites` dataset                |
| `--workers`          | `2`          | parallel queries (match roughly to your proxy count)  |
| `--max-pages`        | `10`         | pages per query (20 results each); `0` = unlimited until the last page |
| `--min-interval`     | `1.5`        | seconds between requests on the same proxy            |
| `--states`           | all            | comma-separated state codes, e.g.`TX,CA`            |
| `--limit-cities`     | `0`          | first N cities per state (testing)                    |
| `--fresh`            | off            | wipe state and start over                             |
| `--research`         | off            | re-run queries that already finished                  |
| `--collect-websites` | off            | also store business websites in place-urls mode       |

## Resume, dedupe and durability

- **Resume** — every (category, city) query is checkpointed in
  `output/state.sqlite3` (SQLite WAL). Ctrl+C, crash, reboot: the next run
  continues exactly where the last one stopped.
- **Dedupe** — places are keyed by their cid, so a business found under ten
  categories is stored once. Website URLs are deduplicated on a normalised
  host+path key (query strings and `utm_*` parameters are ignored).
- **Clean exports** — `python main.py export --out delivery.txt` regenerates
  the deliverable straight from the database, guaranteed complete and
  duplicate-free. `--since <ISO-timestamp>` supports milestone deliveries.
- **Monitoring** — `python main.py report` prints stored progress without
  touching the network.

## Data included

- `data/locations/states/*.json` — all 32,079 US places (incorporated
  places, CDPs, consolidated cities) from the US Census Bureau's TIGERweb
  service (public domain). Regenerate any time:
  `python scripts/fetch_usa_locations.py`.
- `config/categories.txt` — 36 starter categories.
- `config/categories_full.txt` — ~250 categories for deep coverage.
  The full Google Business Profile taxonomy has ~4,000 categories; add
  yours one-per-line (the scraper treats each line as one search).

## Scaling honestly

32k cities × N categories × ~4 pages per query adds up fast: the full
grid is millions of requests. Start with one state, measure your block
rate, then scale workers and proxies. Match `--workers` roughly to your
proxy count (1–2 workers per proxy) and keep `--min-interval` at 1.5s or
above — slow and steady beats fast and banned.

## Disclaimer

This project accesses Google through undocumented endpoints and violates
Google's Terms of Service. It is provided for educational purposes.
You are responsible for complying with applicable laws and terms of
service, and for the proxy infrastructure you use. For fully compliant
access, use the official Google Maps Places API.

## License

[MIT](LICENSE) — free to use, modify and ship.
