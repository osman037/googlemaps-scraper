"""Proxy inventory management: rotation, cooldowns and health tracking.

Google blocks per-IP, so the proxy is the scraper's most precious resource.
The pool implements the standard defensive behaviours:

* **Round-robin rotation** - consecutive requests use different proxies.
* **Escalating cooldowns** - a blocked proxy sits out for
  ``base_cooldown * min(fails, max_multiplier)`` seconds (10 min, 20 min,
  ... capped), so persistently burning proxies rest longer than flaky ones.
* **Health resets** - a successful request clears the failure count.
* **Credential masking** - proxies are always logged/stored as
  ``scheme://user:***@host:port`` so secrets never reach logs or the DB.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

_ALLOWED_SCHEMES = ("http://", "https://", "socks5://", "socks5h://")
_MASK_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)")


def mask_proxy(proxy_url: str | None) -> str:
    """Mask the password of a proxy URL for safe logging.

    >>> mask_proxy("http://user:secretpw@1.2.3.4:8080")
    'http://user:***@1.2.3.4:8080'
    """
    if not proxy_url:
        return "direct"
    return _MASK_RE.sub(r"\1***\2", proxy_url)


def load_proxies(path: str | Path) -> list[str]:
    """Read the proxy list from ``path``.

    Blank lines and ``#`` comments are ignored. Lines with an unsupported
    scheme are skipped so a typo cannot take down a run. Duplicates are
    dropped (they would receive double traffic under round-robin).

    Returns:
        A list of validated proxy URLs (order-preserving).

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"proxy file not found: {p}")
    out: list[str] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith(_ALLOWED_SCHEMES):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


class ProxyPool:
    """Thread-safe rotating pool of proxy URLs.

    Args:
        urls: Proxy URLs (may be empty for direct connections).
        base_cooldown: First-level cooldown in seconds after a failure.
        max_multiplier: Cap for the escalating cooldown multiplier.
    """

    def __init__(
        self,
        urls: list[str],
        base_cooldown: float = 600.0,
        max_multiplier: int = 6,
    ) -> None:
        self._lock = threading.Lock()
        self._base_cooldown = base_cooldown
        self._max_multiplier = max(1, max_multiplier)
        self._items = [{"url": u, "blocked_until": 0.0, "fails": 0} for u in urls]
        self._rr = -1

    def __len__(self) -> int:
        return len(self._items)

    def next(self, avoid_url: str | None = None) -> str | None:
        """Pick the next usable proxy, skipping ``avoid_url`` if possible.

        Returns:
            A proxy URL, or ``None`` when every proxy is cooling down (the
            caller should wait and retry) or the pool is empty.
        """
        with self._lock:
            now = time.time()
            usable = [
                item for item in self._items
                if item["blocked_until"] < now and item["url"] != avoid_url
            ]
            if not usable:
                return None
            self._rr = (self._rr + 1) % len(usable)
            return usable[self._rr]["url"]

    def report_ok(self, proxy_url: str) -> None:
        """Record a successful request: reset the failure counter."""
        with self._lock:
            for item in self._items:
                if item["url"] == proxy_url:
                    item["fails"] = 0
                    item["blocked_until"] = 0.0

    def report_failure(self, proxy_url: str) -> None:
        """Put a proxy into an escalating cooldown."""
        with self._lock:
            for item in self._items:
                if item["url"] == proxy_url:
                    item["fails"] += 1
                    mult = min(item["fails"], self._max_multiplier)
                    item["blocked_until"] = time.time() + self._base_cooldown * mult

    def snapshot(self) -> list[dict]:
        """Health summary (masked URLs) for the ``report`` command."""
        with self._lock:
            now = time.time()
            return [
                {
                    "proxy": mask_proxy(item["url"]),
                    "fails": item["fails"],
                    "cooling_for_s": round(max(0.0, item["blocked_until"] - now), 1),
                }
                for item in self._items
            ]
