"""Base collector: polite fetching, retry, on-disk cache, uniform interface."""
from __future__ import annotations
import os, time, hashlib, logging, pathlib
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("collectors")

CACHE_DIR = pathlib.Path(os.environ.get("WG_CACHE", "/tmp/wg_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = int(os.environ.get("WG_CACHE_TTL", 60 * 60 * 6))  # 6h

UA = os.environ.get(
    "WG_USER_AGENT",
    "WoodgoodsBot/1.0 (+vendor event sourcing; contact: justin@woodgoods.com)",
)


class FetchError(Exception):
    pass


class BaseCollector:
    """Subclass and implement collect() -> list[Event].

    slug        stable id, matches the seed config
    organizer   display name written to every row
    tier        1 fast/static, 2 needs adapter work, 3 hostile/manual
    """

    slug: str = ""
    organizer: str = ""
    sport: str = "lacrosse"
    tier: int = 3
    respects_robots: bool = True
    enabled: bool = True

    def __init__(self, session=None, use_cache=True):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA,
                                     "Accept-Language": "en-US,en;q=0.9"})
        self.use_cache = use_cache
        self.errors: list[str] = []

    # ---------- fetching ----------
    def _cache_path(self, url):
        return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def get(self, url, *, retries=3, backoff=2.0, timeout=30):
        cp = self._cache_path(url)
        if self.use_cache and cp.exists() and time.time() - cp.stat().st_mtime < CACHE_TTL:
            return cp.read_text(encoding="utf-8", errors="replace")

        last = None
        for attempt in range(retries):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200:
                    cp.write_text(r.text, encoding="utf-8")
                    time.sleep(1.0)  # be a good citizen
                    return r.text
                last = f"HTTP {r.status_code}"
                if r.status_code in (403, 404, 410):
                    break
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            time.sleep(backoff * (attempt + 1))
        raise FetchError(f"{url} -> {last}")

    def soup(self, url, **kw):
        return BeautifulSoup(self.get(url, **kw), "lxml")

    # ---------- interface ----------
    def collect(self):
        raise NotImplementedError

    def run(self):
        """Wrap collect() so one bad site can't take down the batch."""
        try:
            events = self.collect() or []
        except Exception as e:  # noqa: BLE001
            msg = f"{self.slug}: {type(e).__name__}: {e}"
            log.warning(msg)
            self.errors.append(msg)
            return []
        for ev in events:
            ev.collector = self.slug
            if not ev.organizer:
                ev.organizer = self.organizer
            if not ev.sport:
                ev.sport = self.sport
        log.info("%-14s %3d events", self.slug, len(events))
        return events

    # ---------- helpers ----------
    @staticmethod
    def txt(node, default=None):
        return node.get_text(" ", strip=True) if node else default
