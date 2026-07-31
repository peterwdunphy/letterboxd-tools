"""TMDB lookups: resolve Letterboxd films to TMDB ids, enrich with metadata.

One enrichment call per film via append_to_response gets details, keywords,
credits, and watch providers together. Everything is cached in SQLite, so
each film costs at most two API calls ever (one search + one enrich).
"""

import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cache
from .config import tmdb_token

# TMDB allows ~40-50 req/s; 8 workers keeps us comfortably under that
# while turning a 10-minute sequential enrichment into ~1 minute.
WORKERS = 8

API = "https://api.themoviedb.org/3"
# Bump when the shape of a cached film payload changes; entries written by
# an older version are refetched lazily rather than silently missing fields.
SCHEMA_VERSION = 2
POSTER_BASE = "https://image.tmdb.org/t/p/w342"


def _get(path, **params):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tmdb_token()}",
        "accept": "application/json",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:                       # rate limited: back off
                time.sleep(2 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise
    raise RuntimeError(f"TMDB kept rate-limiting: {path}")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _search(film):
    """Network-only: find a Film's TMDB id via title+year search."""
    results = []
    for params in ({"query": film.title, "year": film.year} if film.year else {"query": film.title},
                   {"query": film.title}):
        data = _get("/search/movie", **{k: v for k, v in params.items() if v})
        results = (data or {}).get("results", [])
        if results:
            break
    best = None
    for r in results[:5]:
        # Prefer an exact-ish title match near the right year; Letterboxd
        # years occasionally differ from TMDB's by one (premiere vs release).
        title_ok = _norm(r.get("title")) == _norm(film.title) or \
                   _norm(r.get("original_title")) == _norm(film.title)
        r_year = int(r["release_date"][:4]) if r.get("release_date") else None
        year_ok = film.year is None or (r_year and abs(r_year - film.year) <= 1)
        if title_ok and year_ok:
            best = r
            break
    if best is None and results:
        best = results[0]
    return best["id"] if best else None


_CERT_ORDER = ("G", "PG", "PG-13", "R", "NC-17")


def _us_certification(d):
    """US theatrical rating, e.g. 'PG-13'. The strongest available signal
    for who a film is *for*: it is what separates Grave of the Fireflies
    from Captain Underpants when both are tagged Animation."""
    for entry in (d.get("release_dates", {}) or {}).get("results", []):
        if entry.get("iso_3166_1") != "US":
            continue
        certs = [r.get("certification") for r in entry.get("release_dates", [])
                 if r.get("certification")]
        for c in _CERT_ORDER:              # prefer a recognised rating
            if c in certs:
                return c
        if certs:
            return certs[0]
    return None


def _fetch_enrichment(tmdb_id):
    """Network-only: one API call for a film's full slimmed metadata."""
    d = _get(f"/movie/{tmdb_id}",
             append_to_response="keywords,credits,watch/providers,release_dates")
    if d is None:
        return None
    us = d.get("watch/providers", {}).get("results", {}).get("US", {})
    slim = {
        "v": SCHEMA_VERSION,
        "certification": _us_certification(d),
        "tmdb_id": tmdb_id,
        "title": d.get("title"),
        "original_language": d.get("original_language"),
        "release_date": d.get("release_date"),
        "status": d.get("status"),
        "runtime": d.get("runtime"),
        "overview": d.get("overview"),
        "popularity": d.get("popularity"),
        "vote_average": d.get("vote_average"),
        "vote_count": d.get("vote_count"),
        "poster_path": d.get("poster_path"),
        "genres": [g["name"] for g in d.get("genres", [])],
        "keywords": [k["name"] for k in d.get("keywords", {}).get("keywords", [])],
        "directors": [c["name"] for c in d.get("credits", {}).get("crew", [])
                      if c.get("job") == "Director"],
        "cast": [c["name"] for c in d.get("credits", {}).get("cast", [])[:8]],
        "providers": {kind: [p["provider_name"] for p in plist]
                      for kind, plist in us.items() if kind != "link"},
        "justwatch_link": us.get("link"),
    }
    return slim


def enrich_films(films, progress=None, workers=WORKERS):
    """Resolve + enrich Films in parallel. Returns ({film.key: metadata}, misses).

    Cache reads/writes happen only on this thread; the worker pool does
    pure network I/O. Films already in the cache cost nothing.
    """
    conn = cache.connect()
    enriched, misses, todo = {}, [], []
    seen = set()
    for film in films:
        if film.key in seen:
            continue
        seen.add(film.key)
        tmdb_id = film.tmdb_id
        if not tmdb_id:
            found, cached_id = cache.get_slug(conn, film.key)
            if found:
                if cached_id is None:        # searched before, no match exists
                    misses.append(film.key)
                    continue
                tmdb_id = cached_id
        if tmdb_id:
            hit = cache.get_film(conn, tmdb_id)
            if hit and hit.get("v") == SCHEMA_VERSION:
                enriched[film.key] = hit
                continue
        todo.append((film, tmdb_id))

    def work(item):
        film, tmdb_id = item
        if not tmdb_id:
            tmdb_id = _search(film)
        payload = _fetch_enrichment(tmdb_id) if tmdb_id else None
        return film, tmdb_id, payload

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(work, t) for t in todo]):
            film, tmdb_id, payload = fut.result()
            cache.put_slug(conn, film.key, tmdb_id)
            if payload:
                cache.put_film(conn, tmdb_id, payload)
                enriched[film.key] = payload
            else:
                misses.append(film.key)
            done += 1
            if progress and (done % 20 == 0 or done == len(todo)):
                progress(done, len(todo))
    conn.close()
    return enriched, misses


def poster_url(meta):
    return f"{POSTER_BASE}{meta['poster_path']}" if meta.get("poster_path") else None


def _fetch_candidates(tmdb_id):
    """Candidate ids from TMDB's two suggestion engines for one seed film:
    /recommendations (collaborative, from real user co-ratings) and
    /similar (keyword/genre based)."""
    ids = []
    for endpoint in ("recommendations", "similar"):
        d = _get(f"/movie/{tmdb_id}/{endpoint}") or {}
        ids.extend(r["id"] for r in d.get("results", []))
    return ids


def candidates_for(seed_ids, workers=WORKERS):
    """{seed_id: [candidate ids]} for many seeds, cached + parallel."""
    conn = cache.connect()
    result, todo = {}, []
    for sid in seed_ids:
        hit = cache.get_recs(conn, sid)
        if hit is not None:
            result[sid] = hit
        else:
            todo.append(sid)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_candidates, sid): sid for sid in todo}
        for fut in as_completed(futures):
            sid = futures[fut]
            ids = fut.result()
            cache.put_recs(conn, sid, ids)
            result[sid] = ids
    conn.close()
    return result


def enrich_ids(tmdb_ids, workers=WORKERS, progress=None):
    """{tmdb_id: metadata} for known ids (no search step), cached + parallel."""
    conn = cache.connect()
    out, todo = {}, []
    for tid in tmdb_ids:
        hit = cache.get_film(conn, tid)
        if hit and hit.get("v") == SCHEMA_VERSION:
            out[tid] = hit
        else:
            todo.append(tid)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_enrichment, tid): tid for tid in todo}
        for fut in as_completed(futures):
            tid = futures[fut]
            payload = fut.result()
            if payload:
                cache.put_film(conn, tid, payload)
                out[tid] = payload
            done += 1
            if progress and (done % 20 == 0 or done == len(todo)):
                progress(done, len(todo))
    conn.close()
    return out
