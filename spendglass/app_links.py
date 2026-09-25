"""The header's row of links to the owner's other apps.

Each sibling app reports where a browser can open it, as `browser_origin` on
the health route it answers on loopback without a session. This module asks
each configured sibling, keeps the answers for a minute, and builds the row
for the page that asked: tailnet links when that page was opened on a
tailnet name, loopback links when it was opened on this machine. A sibling
that doesn't answer inside the timeout is left out, so the row never offers
a link that won't open.

The list of siblings is config (`sibling_apps`), never discovered, and only
loopback addresses in it are ever asked.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit

APP = "spendglass"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROBE_TIMEOUT_S = 1.0
CACHE_TTL_S = 60.0
# A health answer is a few hundred bytes. Anything far bigger isn't one.
MAX_BODY_BYTES = 65536


def is_loopback(host: str | None) -> bool:
    h = (host or "").strip("[]").lower()
    return h in LOOPBACK_HOSTS or h.startswith("127.")


def clean_origin(value) -> str:
    """`scheme://host[:port]` for an http or https origin, else "". A
    sibling's answer ends up in an href, so nothing else gets through."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        u = urlsplit(value.strip())
        u.port  # raises on a malformed port
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    if u.username or u.password or u.query or u.fragment \
            or u.path not in ("", "/"):
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def local_origin(url) -> str:
    """The origin of a URL on this machine, or "" for anywhere else."""
    try:
        u = urlsplit(str(url or "").strip())
        u.port
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK_HOSTS:
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def read_health(url: str, timeout: float = PROBE_TIMEOUT_S) -> dict | None:
    """One sibling's health answer, or None unless it is a 200 JSON object.
    Proxies are bypassed: the address is on this machine."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            body = r.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            return None
        data = json.loads(body)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


class SiblingProbe:
    """The siblings that answered, asked at most once per `ttl` seconds.
    Each entry is {name, origin, local}: where the sibling says a browser
    opens it ("" when it didn't say), and its loopback address."""

    def __init__(self, siblings: dict, *, timeout: float = PROBE_TIMEOUT_S,
                 ttl: float = CACHE_TTL_S, fetch=read_health,
                 clock=time.monotonic):
        self.siblings = {str(n): str(u) for n, u in (siblings or {}).items()
                         if local_origin(u)}
        self.timeout = timeout
        self.ttl = ttl
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._found: list[dict] = []

    def found(self) -> list[dict]:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self.ttl:
                self._found = self._probe()
                self._at = now
            return [dict(s) for s in self._found]

    def _probe(self) -> list[dict]:
        names = list(self.siblings)
        if not names:
            return []
        pool = ThreadPoolExecutor(max_workers=len(names))
        futures = {n: pool.submit(self._fetch, self.siblings[n], self.timeout)
                   for n in names}
        # The socket timeout is per read, so a sibling that trickles its
        # answer could hold the page longer. This caps the whole wait, and a
        # straggler finishes on its own thread without being waited for.
        wait(futures.values(), timeout=self.timeout)
        pool.shutdown(wait=False, cancel_futures=True)
        found = []
        for n, f in futures.items():
            answered = f.done() and not f.cancelled() and f.exception() is None
            data = f.result() if answered else None
            if data is not None:
                found.append({"name": n,
                              "origin": clean_origin(data.get("browser_origin")),
                              "local": local_origin(self.siblings[n])})
        return found


def sibling_href(page_host: str | None, sibling: dict) -> str:
    """Where a page opened at `page_host` links this sibling, or "" when the
    sibling can't open from there. On this machine every sibling opens at
    its loopback address, and a page on `localhost` keeps that name so a
    passkey made there still offers itself. Anywhere else only a sibling
    that reports a non-loopback address can open."""
    if is_loopback(page_host):
        local = local_origin(sibling.get("local"))
        if not local:
            return ""
        if (page_host or "").lower() == "localhost":
            u = urlsplit(local)
            local = f"{u.scheme}://localhost" + (f":{u.port}" if u.port else "")
        return local + "/"
    origin = clean_origin(sibling.get("origin"))
    if not origin or is_loopback(urlsplit(origin).hostname):
        return ""
    return origin + "/"


def link_row(page_host: str | None, current: str, siblings: list[dict]) -> list[dict]:
    """The row for a page at `page_host`: [{name, href, current}] sorted by
    name, the current app unlinked, or [] when no sibling can open from
    there (a row of one says nothing)."""
    links = []
    for s in siblings or []:
        name = s.get("name")
        if not name or name == current:
            continue
        href = sibling_href(page_host, s)
        if href:
            links.append({"name": name, "href": href, "current": False})
    if not links:
        return []
    links.append({"name": current, "href": "", "current": True})
    return sorted(links, key=lambda link: link["name"])
