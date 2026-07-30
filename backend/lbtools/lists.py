"""Letterboxd list co-occurrence: cultural similarity TMDB genres can't see.

The genre tag "Animation" covers both Grave of the Fireflies and Captain
Underpants, so genre-based similarity keeps proposing kids' fare to people
who watch arthouse animation. Letterboxd's user-made lists encode the
distinction directly: the same curators who list Spirited Away also list
Perfect Blue, and nobody puts Minions on that list. Two films appearing
together across many independent lists is a strong signal that they belong
to the same conversation.

Cost is the catch: each seed film costs one request for its lists page,
then one request per list. Everything is cached forever in SQLite (lists
change slowly), and the crawl is bounded by MAX_SEEDS / LISTS_PER_SEED so
a cold run adds a predictable amount of time rather than an open-ended one.
"""

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cache
from .letterboxd import BASE, _fetch

MAX_SEEDS = 12          # user films whose lists we crawl
LISTS_PER_SEED = 8      # lists taken from each seed's lists page
MAX_LISTS = 60          # hard ceiling on list pages fetched per run
MIN_LIST_FILMS = 8      # ignore near-empty lists
MAX_LIST_FILMS = 400    # ignore "every film I own" mega-lists: no signal

_LIST_HREF = re.compile(r'href="(/[^/"]+/list/[^"/]+/)"')
_ITEM_SLUG = re.compile(r'data-item-slug="([^"]+)"')


def film_lists(slug, limit=LISTS_PER_SEED):
    """Public lists that contain this film (cached)."""
    conn = cache.connect()
    hit = cache.get_json(conn, "film_lists", "slug", slug)
    if hit is None:
        try:
            body = _fetch(f"{BASE}/film/{slug}/lists/")
            hit = list(dict.fromkeys(
                u for u in _LIST_HREF.findall(body) if not u.endswith("/likes/")))
        except Exception:
            hit = []
        cache.put_json(conn, "film_lists", slug, hit)
    conn.close()
    return hit[:limit]


def list_films(url):
    """Film slugs in one list (first page only, cached)."""
    conn = cache.connect()
    hit = cache.get_json(conn, "list_films", "url", url)
    if hit is None:
        try:
            body = _fetch(f"{BASE}{url}")
            hit = list(dict.fromkeys(_ITEM_SLUG.findall(body)))
        except Exception:
            hit = []
        cache.put_json(conn, "list_films", url, hit)
    conn.close()
    return hit


def cooccurrence(seed_slugs, progress=None):
    """{film slug: how many of the user's films' lists also contain it}.

    Fetching is serialised through letterboxd._fetch's throttle, so the
    thread pool here only overlaps parsing, not requests.
    """
    seeds = [s for s in seed_slugs if s][:MAX_SEEDS]
    urls, seen = [], set()
    for i, slug in enumerate(seeds):
        for u in film_lists(slug):
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if progress:
            progress("reading Letterboxd lists", i + 1, len(seeds))
        if len(urls) >= MAX_LISTS:
            break
    urls = urls[:MAX_LISTS]

    counts, lists_used = Counter(), 0
    for i, url in enumerate(urls):
        films = list_films(url)
        if MIN_LIST_FILMS <= len(films) <= MAX_LIST_FILMS:
            counts.update(films)
            lists_used += 1
        if progress and (i + 1) % 5 == 0:
            progress("cross-referencing lists", i + 1, len(urls))
    for s in seeds:                       # a film co-occurring with itself is noise
        counts.pop(s, None)
    return counts, lists_used


_YEAR_SUFFIX = re.compile(r"-\d{4}$")


def _key(slug):
    """Slug -> comparable key ('grave-of-the-fireflies' -> 'graveofthefireflies')."""
    return _YEAR_SUFFIX.sub("", slug).replace("-", "")


def index(counts, lists_used):
    """{title key: co-occurrence share}, keyed so TMDB candidates (which
    carry titles, not Letterboxd slugs) can be looked up by title."""
    if not lists_used:
        return {}
    out = {}
    for slug, n in counts.items():
        k = _key(slug)
        out[k] = max(out.get(k, 0.0), n / lists_used)
    return out


def boost(idx, title_key, floor=0.78, ceiling=1.35):
    """Multiplier for a candidate, from how often it shares lists.

    Two-sided on purpose. A bonus-only version left Minions in the results
    for someone whose animation is Ghibli and Spider-Verse: it simply
    failed to gain, which wasn't enough to sink it. Sharing no lists at all
    with anything the user watches is itself evidence, so that earns a
    penalty. Bounded either way, since list membership partly tracks plain
    popularity, which the score already handles elsewhere.
    """
    if not idx or not title_key:
        return 1.0
    share = min(1.0, idx.get(title_key, 0.0) * 4)
    return floor + (ceiling - floor) * share
