"""Quick proxy provider health-check utility.

Verifies that a rotating residential proxy endpoint is alive and usable for
Google Maps scraping:

* Check 1: sends N requests through the proxy to IP echo services and
  reports how many unique IPs came back (rotation check). Some providers
  block popular echo domains, so a fallback chain of echo services is tried.
* Check 2: sends one real Google Maps search request (the exact URL format
  the scraper builds) and verifies the ``)]}'``-prefixed Maps payload.

Notes:
  * ``verify=False`` is required with providers that intercept TLS at their
    gateway (the certificate served is the provider's, not the target's).
  * Set your key via the ``SCRAPERAPI_KEY`` environment variable, or edit the
    placeholder below. Never commit a live key to a public repository.
  * The example below uses a ScraperAPI-style endpoint; adapt the proxy URL
    format to whichever provider you use.

Run:  python try.py
"""

import json
import os
import random
import time
import warnings
from urllib.parse import quote

import requests
import urllib3

from maps_url_scraper.constants import build_search_url

# ScraperAPI-style gateways intercept TLS -> their certificate is served
# instead of the target's, so certificate verification must be disabled.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "YOUR_SCRAPERAPI_KEY_HERE")
PROXY_URL = f"http://scraperapi:{SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8001"
PROXIES = {"http": PROXY_URL, "https": PROXY_URL}
TOTAL_REQUESTS = 10

# IP echo services - some providers block popular ones, so try them in order.
ECHO_URLS = [
    "http://httpbin.org/ip",
    "https://api.ipify.org?format=json",
    "http://checkip.amazonaws.com/",
    "https://ifconfig.me/ip",
]


def _parse_ip(text: str) -> str:
    """Extract the client IP from an echo response (formats vary)."""
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
        text = data.get("origin") or data.get("ip") or "?"
    # proxy chains can return "1.2.3.4, unknown" - first entry is the client IP
    return text.split(",")[0].strip() or "?"


def get_ip() -> tuple[str, str]:
    """Return ``(ip, echo_host)`` trying each echo service until one works."""
    last_error: Exception | None = None
    for url in ECHO_URLS:
        try:
            r = requests.get(url, proxies=PROXIES, timeout=45, verify=False)
            if r.status_code == 200:
                return _parse_ip(r.text), url.split("/")[2]
            last_error = RuntimeError(f"HTTP {r.status_code} from {url}")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"all echo endpoints failed: {last_error}")


def google_maps_check() -> tuple[int, int, bool]:
    """Send one real Google Maps request and verify the Maps payload."""
    url = build_search_url("dentist in Austin, TX", 0)
    r = requests.get(url, proxies=PROXIES, timeout=90, verify=False,
                     headers={"Accept-Language": "en-US,en;q=0.9"})
    return r.status_code, len(r.text), r.text.startswith(")]}'")


def main() -> None:
    print("Proxy provider health check")
    print("=" * 55)

    ips, sources = [], []
    for i in range(1, TOTAL_REQUESTS + 1):
        try:
            ip, source = get_ip()
            ips.append(ip)
            sources.append(source)
            print(f"{i:2d}. {ip:16} (via {source})")
        except Exception as exc:
            print(f"{i:2d}. FAILED: {type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(random.uniform(0.5, 1.5))

    print("-" * 55)
    unique = set(ips)
    if ips:
        print(f"successful: {len(ips)}/{TOTAL_REQUESTS} | "
              f"unique IPs: {len(unique)} | repeats: {len(ips) - len(unique)}")
        print(f"echo sources used: {sorted(set(sources))}")

    print()
    print("Google Maps check (production request format)")
    print("=" * 55)
    try:
        status, length, marker = google_maps_check()
        print(f"status={status} | bytes={length} | maps-data={marker}")
        if marker:
            print("-> OK: this proxy provider works for Google Maps scraping")
        elif status == 200:
            print("-> HTTP 200 but no Maps payload - inspect the response")
        else:
            print("-> blocked or redirected - check provider settings")
    except Exception as exc:
        print(f"Google check FAILED: {type(exc).__name__}: {str(exc)[:200]}")


if __name__ == "__main__":
    main()
