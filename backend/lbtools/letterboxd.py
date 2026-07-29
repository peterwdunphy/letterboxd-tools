"""Scrape a Letterboxd user's public watchlist, watched films, and diary.

Requests go through curl_cffi with a Chrome TLS fingerprint. Letterboxd
sits behind Cloudflare, which fingerprints the TLS handshake rather than
checking JavaScript: a stdlib urllib/curl handshake gets 403 on deep
pagination and the diary, while a browser-shaped handshake gets the same
public pages a logged-out browser sees. Same pages, same throttling, no
challenge solving and no authentication.

Sources per user:
  /watchlist/          every page  -> films they want to see
  /films/              every page  -> every film they have logged
  /diary/films/        every page  -> watch DATES, ratings, likes, rewatches
  /rss/                one request -> TMDB ids for the last ~50 (saves lookups)
"""

import html
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from curl_cffi import requests

BASE = "https://letterboxd.com"
IMPERSONATE = "chrome"
THROTTLE_SECONDS = 0.6
RETRIES = 3
MAX_PAGES_HARD = 200          # ~14k films; stops runaway crawls

RSS_NS = {"letterboxd": "https://letterboxd.com", "tmdb": "https://themoviedb.org"}


class ChallengeError(RuntimeError):
    """Cloudflare served a bot-challenge page instead of content."""


class ProfileNotFound(RuntimeError):
    """No such Letterboxd user, or their profile is private."""


@dataclass
class Film:
    slug: str
    title: str
    year: int | None
    tmdb_id: int | None = None
    watched_date: str | None = None     # ISO date, diary/export only
    rating: float | None = None         # 0.5-5.0
    liked: bool = False
    rewatch: bool = False
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
    watched: list[Film] = field(default_factory=list)   # complete, dated where known
    diary: list[Film] = field(default_factory=list)     # dated entries, newest first
    history_complete: bool = True
    watched_est_total: int = 0


_session = None
_last_fetch = 0.0


def _fetch(url):
    """Throttled browser-fingerprinted GET with retries."""
    global _session, _last_fetch
    if _session is None:
        _session = requests.Session(impersonate=IMPERSONATE)
    for attempt in range(RETRIES):
        wait = THROTTLE_SECONDS - (time.time() - _last_fetch)
        if wait > 0:
            time.sleep(wait)
        _last_fetch = time.time()
        try:
            resp = _session.get(url, timeout=30)
        except Exception:
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 404:
            user = url.split("/")[3] if len(url.split("/")) > 3 else url
            raise ProfileNotFound(
                f"No public Letterboxd profile for '{user}'. "
                f"Check the spelling, or the profile may be private.")
        if resp.status_code in (429, 502, 503) and attempt < RETRIES - 1:
            time.sleep(5 * (attempt + 1))
            continue
        body = resp.text
        if "Just a moment..." in body[:2000]:
            raise ChallengeError(f"Cloudflare challenge at {url}")
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} at {url}")
        return body
    raise RuntimeError(f"unreachable: {url}")


# Grid items carry data-item-name="Tuner (2025)" and data-item-slug="tuner"
# in no guaranteed attribute order, so match the tag first, then its attrs.
_GRID_TAG = re.compile(r'<div[^>]*data-item-slug="[^"]+"[^>]*>')
_ITEM_ATTR = re.compile(r'data-item-(slug|name)="([^"]*)"')
_NAME_YEAR = re.compile(r"^(?P<title>.*?)\s*\((?P<year>\d{4})\)$")
_PAGE_NUM = re.compile(r"/page/(\d+)/")
_DIARY_ROW = re.compile(r'<tr class="diary-entry-row.*?</tr>', re.S)
_DIARY_DATE = re.compile(r"/for/(\d{4})/(\d{2})/(\d{2})/")
_RATED = re.compile(r"rated-(\d+)")


def _split_name(name):
    name = html.unescape(name)
    m = _NAME_YEAR.match(name)
    return (m.group("title"), int(m.group("year"))) if m else (name, None)


def _parse_grid(body, start_rank):
    films = []
    for m in _GRID_TAG.finditer(body):
        attrs = dict(_ITEM_ATTR.findall(m.group(0)))
        title, year = _split_name(attrs.get("name", ""))
        films.append(Film(slug=attrs["slug"], title=title, year=year,
                          rank=start_rank + len(films)))
    return films


def _crawl(path, parser, max_pages=None, progress=None, label=""):
    """Fetch every page of a paginated section."""
    items, page, total = [], 1, 1
    cap = min(max_pages or MAX_PAGES_HARD, MAX_PAGES_HARD)
    while page <= total and page <= cap:
        suffix = "" if page == 1 else f"page/{page}/"
        body = _fetch(f"{BASE}{path}{suffix}")
        if page == 1:
            nums = _PAGE_NUM.findall(body)
            total = max(int(n) for n in nums) if nums else 1
        items.extend(parser(body, len(items)))
        if progress:
            progress(label, page, min(total, cap))
        page += 1
    return items


def get_watchlist(username, max_pages=None, progress=None):
    return _crawl(f"/{username}/watchlist/", _parse_grid, max_pages,
                  progress, "watchlist")


def get_watched(username, max_pages=None, progress=None):
    """Every film the user has logged, all pages (release-date order)."""
    return _crawl(f"/{username}/films/", _parse_grid, max_pages,
                  progress, "watched films")


def _parse_diary(body, start_rank):
    entries = []
    for m in _DIARY_ROW.finditer(body):
        row = m.group(0)
        attrs = dict(_ITEM_ATTR.findall(row))
        if "slug" not in attrs:
            continue
        title, year = _split_name(attrs.get("name", ""))
        d = _DIARY_DATE.search(row)
        rating = _RATED.search(row)
        entries.append(Film(
            slug=attrs["slug"], title=title, year=year,
            watched_date=f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else None,
            rating=int(rating.group(1)) / 2 if rating else None,
            liked="icon-liked" in row,
            rewatch="icon-rewatch" in row and "icon-rewatch-off" not in row,
            rank=start_rank + len(entries),
        ))
    return entries


def get_diary(username, max_pages=None, progress=None):
    """Dated watch log, newest first, all pages."""
    return _crawl(f"/{username}/diary/films/", _parse_diary, max_pages,
                  progress, "diary")


def get_recent_rss(username):
    """Last ~50 diary entries with TMDB ids attached (one cheap request)."""
    try:
        body = _fetch(f"{BASE}/{username}/rss/")
        root = ET.fromstring(body.encode())
    except Exception:
        return []
    films = []
    for item in root.iter("item"):
        def txt(tag, ns="letterboxd"):
            el = item.find(f"{{{RSS_NS[ns]}}}{tag}")
            return el.text if el is not None else None
        title = txt("filmTitle")
        if title is None:
            continue
        link = item.findtext("link") or ""
        slug_m = re.search(r"/film/([^/]+)/", link)
        tmdb_el, year, rating = txt("movieId", ns="tmdb"), txt("filmYear"), txt("memberRating")
        films.append(Film(
            slug=slug_m.group(1) if slug_m else "",
            title=title, year=int(year) if year else None,
            tmdb_id=int(tmdb_el) if tmdb_el else None,
            watched_date=txt("watchedDate"),
            rating=float(rating) if rating else None,
            liked=(txt("memberLike") == "Yes"),
            rank=len(films),
        ))
    return films


def get_user(username, max_pages=None, progress=None, export_watched=None):
    """Complete public picture of one user.

    Watched history = the full films grid, enriched with diary dates,
    ratings, and likes wherever the diary covers a film. Films logged
    without a diary entry (bulk-imported history) stay undated and are
    treated as background taste by the engine.
    """
    data = UserData(username=username)
    data.watchlist = get_watchlist(username, max_pages, progress)
    grid = get_watched(username, max_pages, progress)
    data.diary = get_diary(username, max_pages, progress)
    rss = get_recent_rss(username)

    latest = {}                     # slug -> newest diary entry
    for f in data.diary:
        if f.slug and f.slug not in latest:
            latest[f.slug] = f
    tmdb_by_slug = {f.slug: f.tmdb_id for f in rss if f.slug and f.tmdb_id}

    merged = {}
    for f in grid:
        d = latest.get(f.slug)
        if d:
            f.watched_date, f.rating = d.watched_date, d.rating
            f.liked, f.rewatch = d.liked, d.rewatch
        f.tmdb_id = tmdb_by_slug.get(f.slug)
        merged[f.key] = f
    for f in data.diary:            # diary-only films (rare, but keep them)
        merged.setdefault(f.key, f)

    if export_watched is not None:
        for f in export_watched:
            merged.setdefault(f.key, f)

    data.watched = list(merged.values())
    data.watched_est_total = len(data.watched)
    data.history_complete = True
    return data
