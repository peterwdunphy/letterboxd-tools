"""Scrape a Letterboxd user's public watchlist, watched films, and RSS diary.

All fetches are throttled (Letterboxd is not rate-limit-friendly territory)
and use a browser User-Agent. Grid pages carry ~28 (watchlist) or ~72
(films) posters per page with machine-readable data attributes.
"""

import html
import re
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

BASE = "https://letterboxd.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
THROTTLE_SECONDS = 1.2
RETRIES = 3

RSS_NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}


class ChallengeError(RuntimeError):
    """Cloudflare served a bot-challenge page instead of content."""


@dataclass
class Film:
    slug: str
    title: str
    year: int | None
    tmdb_id: int | None = None          # only RSS provides this directly
    watched_date: str | None = None     # ISO date, RSS/export only
    rating: float | None = None         # 0.5-5.0, RSS/export only
    liked: bool = False
    rank: int = 0                       # position in the source listing
    uri: str | None = None              # boxd.it link (export files only)

    @property
    def letterboxd_url(self):
        return f"{BASE}/film/{self.slug}/" if self.slug else (self.uri or "")

    @property
    def key(self):
        """Identity across sources that lack a slug (export CSVs)."""
        return self.slug or f"{_norm_title(self.title)}|{self.year}"


def _norm_title(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


@dataclass
class UserData:
    username: str
    watchlist: list[Film] = field(default_factory=list)
    watched: list[Film] = field(default_factory=list)
    recent: list[Film] = field(default_factory=list)   # RSS diary, newest first
    # Cloudflare blocks */films/page/N (site-wide WAF rule, verified
    # 2026-07-28), so scraped watched history is capped at grid page 1 +
    # RSS. These fields say how much of the user's history that covers.
    watched_est_total: int = 0          # ~pages * 72 from the paginator
    history_complete: bool = False      # True when export supplied or 1-page user


_last_fetch = 0.0


def _fetch(url):
    """Throttled GET with retries. Raises ChallengeError on a Cloudflare page."""
    global _last_fetch
    for attempt in range(RETRIES):
        wait = THROTTLE_SECONDS - (time.time() - _last_fetch)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _last_fetch = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < RETRIES - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        if "Just a moment..." in body[:2000]:
            raise ChallengeError(f"Cloudflare challenge at {url}")
        return body
    raise RuntimeError(f"unreachable: {url}")


# Grid items carry data-item-name="Tuner (2025)" and data-item-slug="tuner"
# in no guaranteed attribute order, so match the tag first, then its attrs.
_GRID_TAG = re.compile(r'<div[^>]*data-item-slug="[^"]+"[^>]*>')
_ITEM_ATTR = re.compile(r'data-item-(slug|name)="([^"]*)"')
_NAME_YEAR = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)$")
_PAGE_NUM = re.compile(r"/page/(\d+)/")


def _parse_grid(body, start_rank):
    films = []
    for m in _GRID_TAG.finditer(body):
        attrs = dict(_ITEM_ATTR.findall(m.group(0)))
        name = html.unescape(attrs.get("name", ""))   # &amp; &#039; etc.
        ym = _NAME_YEAR.match(name)
        title, year = (ym.group("title"), int(ym.group("year"))) if ym else (name, None)
        films.append(Film(slug=attrs["slug"], title=title, year=year,
                          rank=start_rank + len(films)))
    return films


def _crawl_grid(username, section, max_pages=None, progress=None):
    """Fetch every page of a grid section ('watchlist' or 'films')."""
    films, page, total_pages = [], 1, 1
    while page <= total_pages and (max_pages is None or page <= max_pages):
        suffix = "" if page == 1 else f"page/{page}/"
        body = _fetch(f"{BASE}/{username}/{section}/{suffix}")
        if page == 1:
            nums = _PAGE_NUM.findall(body)
            total_pages = max(int(n) for n in nums) if nums else 1
        films.extend(_parse_grid(body, start_rank=len(films)))
        if progress:
            progress(section, page, min(total_pages, max_pages or total_pages))
        page += 1
    return films


def get_watchlist(username, max_pages=None, progress=None):
    return _crawl_grid(username, "watchlist", max_pages, progress)


def get_watched_sample(username):
    """Watched films, page 1 only: ~72 films.

    Two hard limits, both verified 2026-07-28: deeper pages 403 behind a
    Cloudflare WAF rule matching */films/page/N, and the grid's default
    order is RELEASE DATE (newest first), with every /by/ sorted view
    (including by/date = watch order) also 403. So this is a sample of
    the user's newest-released watched films, useful for membership and
    taste but NOT for recency. True watch dates come only from the RSS
    diary (get_recent) and the export ZIP (export.py).

    Returns (films, est_total, is_complete).
    """
    body = _fetch(f"{BASE}/{username}/films/")
    films = _parse_grid(body, start_rank=0)
    nums = _PAGE_NUM.findall(body)
    pages = max(int(n) for n in nums) if nums else 1
    per_page = len(films)
    est_total = per_page if pages == 1 else (pages - 1) * per_page + per_page // 2
    return films, est_total, pages == 1


def get_recent(username):
    """Parse the RSS diary feed: last ~100 entries, newest first.

    Watch entries carry watchedDate, rating, and a real TMDB id. List
    entries (letterboxd:filmTitle absent) are skipped.
    """
    body = _fetch(f"{BASE}/{username}/rss/")
    root = ET.fromstring(body.encode())
    films = []
    for rank, item in enumerate(root.iter("item")):
        def txt(tag, ns="letterboxd"):
            el = item.find(f"{{{RSS_NS[ns]}}}{tag}")
            return el.text if el is not None else None
        title = txt("filmTitle")
        if title is None:
            continue
        link = item.findtext("link") or ""
        slug_m = re.search(r"/film/([^/]+)/", link)
        tmdb_el = txt("movieId", ns="tmdb")
        year = txt("filmYear")
        rating = txt("memberRating")
        films.append(Film(
            slug=slug_m.group(1) if slug_m else "",
            title=title,
            year=int(year) if year else None,
            tmdb_id=int(tmdb_el) if tmdb_el else None,
            watched_date=txt("watchedDate"),
            rating=float(rating) if rating else None,
            liked=(txt("memberLike") == "Yes"),
            rank=len(films),
        ))
    return films


def get_user(username, max_pages=None, progress=None, export_watched=None):
    """Everything we can grab for one user.

    Watched history = films grid page 1 (recently added) merged with the
    RSS diary (dated + rated), deduped by slug. If export_watched (a list
    of Films from export.py) is supplied, it replaces the scraped history
    entirely and marks the profile complete.
    """
    data = UserData(username=username)
    data.recent = get_recent(username)
    data.watchlist = get_watchlist(username, max_pages, progress)
    grid, data.watched_est_total, complete = get_watched_sample(username)
    if progress:
        progress("films (page 1 only, see WAF note)", 1, 1)

    by_slug = {f.slug: f for f in data.recent if f.slug}
    for f in grid:                       # backfill RSS detail onto grid films
        r = by_slug.get(f.slug)
        if r:
            f.watched_date, f.rating = r.watched_date, r.rating
            f.tmdb_id, f.liked = r.tmdb_id, r.liked

    if export_watched is not None:
        merged = {f.key: f for f in export_watched}
        for f in grid + data.recent:     # scraped data is fresher than the export
            merged[f.key] = f
        data.watched = list(merged.values())
        data.history_complete = True
        data.watched_est_total = len(data.watched)
    else:
        merged = {f.slug: f for f in grid}
        for f in data.recent:
            merged.setdefault(f.slug, f)
        data.watched = list(merged.values())
        data.history_complete = complete
    return data
