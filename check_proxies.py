"""Simple proxy checker: verify your proxy list before a real run.

For every proxy in the file it reports the exit IP, response latency and
(optionally with ``--maps``) whether that proxy can actually reach the
Google Maps search endpoint the scraper uses.

No API keys, no accounts - just plain proxy URLs, one per line:

    http://user:pass@1.2.3.4:8080
    socks5://user:pass@gateway.example.com:1080

Run:
    python check_proxies.py                          # checks config/proxies.txt
    python check_proxies.py --proxy-file my.txt      # custom list
    python check_proxies.py --proxy socks5://u:p@host:1080
    python check_proxies.py --maps                   # + Google Maps probe
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time

from curl_cffi.requests import Session

from google_maps_scraper.constants import build_search_url
from google_maps_scraper.proxy_pool import load_proxies, mask_proxy

# IP echo services - some proxies block popular ones, so try them in order.
ECHO_URLS = (
    "https://api.ipify.org?format=json",
    "http://checkip.amazonaws.com/",
    "https://ifconfig.me/ip",
)


def _parse_ip(text: str) -> str:
    """Extract the exit IP from an echo response (formats vary)."""
    text = text.strip()
    if text.startswith("{"):
        try:
            text = json.loads(text).get("ip") or "?"
        except json.JSONDecodeError:
            return "?"
    return text.split(",")[0].strip() or "?"


def check_proxy(proxy: str, timeout: float, maps: bool) -> dict:
    """Test one proxy: exit IP + latency, optionally a Maps reachability probe."""
    result = {"proxy": proxy, "ip": "", "latency": 0.0, "ok": False,
              "maps": None, "error": ""}
    proxies = {"http": proxy, "https": proxy}
    try:
        start = time.perf_counter()
        with Session(impersonate="chrome") as session:
            for url in ECHO_URLS:
                try:
                    resp = session.get(url, proxies=proxies, timeout=timeout)
                    if resp.status_code == 200:
                        result["ip"] = _parse_ip(resp.text)
                        break
                except Exception:
                    continue
            result["latency"] = time.perf_counter() - start
            result["ok"] = bool(result["ip"])
            if maps and result["ok"]:
                resp = session.get(build_search_url("dentist", 0),
                                   proxies=proxies, timeout=timeout)
                # valid Maps payloads always start with the )]}' XSSI prefix
                result["maps"] = (resp.status_code == 200
                                  and resp.text.startswith(")]}'"))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:90]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Simple proxy checker - exit IP, latency and optional "
                    "Google Maps reachability per proxy.")
    parser.add_argument("--proxy-file", default="config/proxies.txt",
                        help="proxy list file, one per line "
                             "(default: config/proxies.txt)")
    parser.add_argument("--proxy",
                        help="check a single proxy instead of a file")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="per-request timeout in seconds (default: 15)")
    parser.add_argument("--maps", action="store_true",
                        help="also probe Google Maps reachability per proxy")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel checks (default: 8)")
    args = parser.parse_args(argv)

    proxies = [args.proxy] if args.proxy else []
    if not proxies:
        try:
            proxies = load_proxies(args.proxy_file)
        except FileNotFoundError:
            print(f"proxy file not found: {args.proxy_file}")
            print("create it (one proxy per line) or pass --proxy <url>")
            return 2
    if not proxies:
        print(f"no usable proxies in {args.proxy_file}")
        return 2

    print(f"checking {len(proxies)} prox"
          f"{'y' if len(proxies) == 1 else 'ies'} "
          f"(timeout {args.timeout:.0f}s{', + maps probe' if args.maps else ''})")
    print("-" * 72)

    ok = 0
    ips: set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(lambda p: check_proxy(p, args.timeout, args.maps),
                          proxies):
            proxy = mask_proxy(r["proxy"])
            if not r["ok"]:
                print(f"  FAIL  {proxy:<45} {r['error'] or 'no exit IP'}")
                continue
            ok += 1
            ips.add(r["ip"])
            maps_col = "-"
            if r["maps"] is not None:
                maps_col = "ok" if r["maps"] else "BLOCKED"
            print(f"  OK    {proxy:<45} {r['ip']:<16} "
                  f"{r['latency']:5.2f}s   maps: {maps_col}")

    print("-" * 72)
    print(f"{ok}/{len(proxies)} proxies working | {len(ips)} unique exit IPs")
    if args.maps:
        print("maps: BLOCKED means the proxy works but Google refuses it - "
              "rotate or replace it")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
