"""Shared scrape -> enrich -> recommend pipeline used by the CLI and API."""

from collections import Counter

from . import engine, letterboxd, tmdb

SEEDS_FROM_WATCHED = 25
SEEDS_FROM_WATCHLIST = 15


def card(meta, extra=None):
    """The JSON shape the frontend renders."""
    d = {
        "tmdb_id": meta["tmdb_id"],
        "title": meta.get("title"),
        "year": (meta.get("release_date") or "")[:4] or None,
        "poster": tmdb.poster_url(meta),
        "overview": meta.get("overview"),
        "runtime": meta.get("runtime"),
        "language": meta.get("original_language"),
        "genres": meta.get("genres", [])[:3],
        "letterboxd_url": f"https://letterboxd.com/tmdb/{meta['tmdb_id']}",
        "streaming": meta.get("providers", {}).get("flatrate", [])[:4],
        "rent": meta.get("providers", {}).get("rent", [])[:3],
        "justwatch": meta.get("justwatch_link"),
    }
    if extra:
        d.update(extra)
    return d


def load_user(username, progress=None, export_watched=None):
    """Scrape one user and enrich everything they have touched."""
    data = letterboxd.get_user(username, progress=progress,
                               export_watched=export_watched)
    if progress:
        progress("matching films", 0, 1)
    enriched, _ = tmdb.enrich_films(data.watched + data.watchlist)
    return data, enriched


def candidate_pool(data, enriched, weights, source, progress=None):
    """TMDB suggestion pool from the user's strongest films."""
    seeds = sorted((f for f in data.watched if f.key in enriched),
                   key=lambda f: -weights[f.key])[:SEEDS_FROM_WATCHED]
    seed_ids = [enriched[f.key]["tmdb_id"] for f in seeds]
    if source > 0.15:
        seed_ids += [enriched[f.key]["tmdb_id"]
                     for f in data.watchlist[:SEEDS_FROM_WATCHLIST]
                     if f.key in enriched]
    if progress:
        progress("finding candidates", 0, 1)
    lists = tmdb.candidates_for(seed_ids)
    hits = Counter(tid for ids in lists.values() for tid in ids)
    own = {m["tmdb_id"] for m in enriched.values()}
    return {tid: n for tid, n in hits.items() if tid not in own}


def recommend_from_export(username, watched, watchlist, recency=0.5,
                          source=0.35, limit=40, progress=None):
    """Tool 1 for private profiles: everything comes from the export ZIP."""
    data = letterboxd.UserData(username=username, watched=watched,
                               watchlist=watchlist,
                               watched_est_total=len(watched))
    if progress:
        progress("matching films", 0, 1)
    enriched, _ = tmdb.enrich_films(data.watched + data.watchlist)
    return _rank(data, enriched, recency, source, limit, progress)


def recommend(username, recency=0.5, source=0.35, limit=40,
              progress=None, export_watched=None):
    """Tool 1: enrich one user's watchlist with films they should add."""
    data, enriched = load_user(username, progress, export_watched)
    return _rank(data, enriched, recency, source, limit, progress)


def _rank(data, enriched, recency, source, limit, progress):
    weights = engine.watch_weights(data.watched, recency)
    hits = candidate_pool(data, enriched, weights, source, progress)
    if progress:
        progress("scoring", 0, 1)
    cand_metas = tmdb.enrich_ids(list(hits))
    ctx = engine.build_context(list(enriched.values()) + list(cand_metas.values()))
    profile, _ = engine.taste_profile(data.watched, data.watchlist, enriched,
                                      ctx, recency=recency, source=source)
    own_titles = {engine.norm_title(f.title) for f in data.watched + data.watchlist}
    ranked = engine.score_candidates(profile, cand_metas, ctx, seed_hits=hits,
                                     exclude_titles=own_titles)
    return {
        "username": data.username,
        "stats": {
            "watched": len(data.watched),
            "dated": sum(1 for f in data.watched if f.watched_date),
            "watchlist": len(data.watchlist),
            "candidates": len(cand_metas),
        },
        "results": [card(meta, {"why": reasons, "score": round(s, 4)})
                    for s, meta, reasons in ranked[:limit]],
    }


def group(usernames, recency=0.5, source=0.35, seen_weight=0.0, limit=40,
          languages=None, availability=None, progress=None, exports=None):
    """Tool 2: what should this group watch together."""
    users, profiles = {}, {}
    watched_keys, watchlist_keys, all_enriched = {}, {}, {}
    pool = Counter()

    for name in usernames:
        def p(section, page, total, _n=name):
            if progress:
                progress(f"{_n}: {section}", page, total)
        data, enriched = load_user(name, p, (exports or {}).get(name))
        users[name] = (data, enriched)
        all_enriched.update(enriched)
        watched_keys[name] = {f.key for f in data.watched}
        watchlist_keys[name] = {f.key for f in data.watchlist}
        weights = engine.watch_weights(data.watched, recency)
        pool.update(candidate_pool(data, enriched, weights, source, progress))
        # Everyone's watchlist films are candidates for the group too.
        for f in data.watchlist:
            if f.key in enriched:
                pool[enriched[f.key]["tmdb_id"]] += 2

    if progress:
        progress("scoring", 0, 1)
    cand_metas = tmdb.enrich_ids(list(pool))
    ctx = engine.build_context(list(all_enriched.values()) + list(cand_metas.values()))
    for name, (data, enriched) in users.items():
        profiles[name], _ = engine.taste_profile(
            data.watched, data.watchlist, enriched, ctx,
            recency=recency, source=source)

    title_index = {}
    for name, (data, enriched) in users.items():
        for f in data.watched + data.watchlist:
            title_index.setdefault(engine.norm_title(f.title), f.key)

    ranked = engine.score_group(
        profiles, cand_metas, ctx, watched_keys, watchlist_keys, title_index,
        seen_weight=seen_weight, seed_hits=pool, languages=languages,
        availability=availability)

    return {
        "usernames": list(usernames),
        "stats": {name: {"watched": len(d.watched), "watchlist": len(d.watchlist)}
                  for name, (d, _) in users.items()},
        "results": [card(r["meta"], {
            "why": r["reasons"], "score": round(r["score"], 4),
            "seen_by": r["seen_by"], "wants": r["wants"],
            "per_user": r["per_user"],
        }) for r in ranked[:limit]],
    }
