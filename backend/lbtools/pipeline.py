"""Shared scrape -> enrich -> recommend pipeline used by the CLI and API."""

from collections import Counter

from . import engine, letterboxd, lists, tmdb

SEEDS_FROM_WATCHED = 25
SEEDS_FROM_WATCHLIST = 15
# A two-person group can generate ~3,000 candidates, and enriching every one
# uncached takes minutes for recommendations nobody scrolls to. Cap the pool:
# films on someone's watchlist are always kept, the rest fill the budget by
# how many seed films suggested them.
MAX_POOL = 1200


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


def load_user(username, progress=None, export_watched=None, label=None):
    """Scrape one user and enrich everything they have touched."""
    who = f"{label}: " if label else ""
    data = letterboxd.get_user(username, progress=progress,
                               export_watched=export_watched)
    films = data.watched + data.watchlist

    def p(done, total):
        if progress:
            progress(f"{who}matching {total} films", done, total)

    if progress:
        progress(f"{who}matching films", 0, len(films))
    enriched, _ = tmdb.enrich_films(films, progress=p)
    return data, enriched


def candidate_pool(data, enriched, weights, source, progress=None, label=None):
    """TMDB suggestion pool from the user's strongest films."""
    who = f"{label}: " if label else ""
    seeds = sorted((f for f in data.watched if f.key in enriched),
                   key=lambda f: -weights[f.key])[:SEEDS_FROM_WATCHED]
    seed_ids = [enriched[f.key]["tmdb_id"] for f in seeds]
    if source > 0.15:
        seed_ids += [enriched[f.key]["tmdb_id"]
                     for f in data.watchlist[:SEEDS_FROM_WATCHLIST]
                     if f.key in enriched]
    if progress:
        progress(f"{who}finding candidates", 0, len(seed_ids))
    suggestions = tmdb.candidates_for(seed_ids)
    hits = Counter(tid for ids in suggestions.values() for tid in ids)
    own = {m["tmdb_id"] for m in enriched.values()}
    return {tid: n for tid, n in hits.items() if tid not in own}


def _trim_pool(pool, protected=frozenset(), cap=MAX_POOL):
    """Keep protected ids plus the best-supported candidates, up to cap."""
    if len(pool) <= cap:
        return pool
    kept = {tid: n for tid, n in pool.items() if tid in protected}
    room = max(0, cap - len(kept))
    for tid, n in Counter({t: c for t, c in pool.items()
                           if t not in kept}).most_common(room):
        kept[tid] = n
    return kept


def recommend_from_export(username, watched, watchlist, recency=0.5,
                          source=0.35, limit=40, progress=None):
    """Tool 1 for private profiles: everything comes from the export ZIP."""
    data = letterboxd.UserData(username=username, watched=watched,
                               watchlist=watchlist,
                               watched_est_total=len(watched))

    def p(done, total):
        if progress:
            progress(f"matching {total} films", done, total)

    if progress:
        progress("matching films", 0, len(watched) + len(watchlist))
    enriched, _ = tmdb.enrich_films(data.watched + data.watchlist, progress=p)
    return _rank(data, enriched, recency, source, limit, progress)


def recommend(username, recency=0.5, source=0.35, limit=40,
              progress=None, export_watched=None):
    """Tool 1: enrich one user's watchlist with films they should add."""
    data, enriched = load_user(username, progress, export_watched)
    return _rank(data, enriched, recency, source, limit, progress)


def _rank(data, enriched, recency, source, limit, progress, use_lists=True):
    weights = engine.watch_weights(data.watched, recency)
    hits = _trim_pool(candidate_pool(data, enriched, weights, source, progress))

    def p(done, total):
        if progress:
            progress(f"looking up {total} candidate films", done, total)

    if progress:
        progress("looking up candidate films", 0, len(hits))
    cand_metas = tmdb.enrich_ids(list(hits), progress=p)

    list_idx = {}
    if use_lists:
        top = sorted((f for f in data.watched if f.slug and f.key in enriched),
                     key=lambda f: -weights[f.key])[:lists.MAX_SEEDS]
        counts, used = lists.cooccurrence([f.slug for f in top], progress=progress)
        list_idx = lists.index(counts, used)

    if progress:
        progress("scoring", 0, 0)
    ctx = engine.build_context(list(enriched.values()) + list(cand_metas.values()))
    profile, _ = engine.taste_profile(data.watched, data.watchlist, enriched,
                                      ctx, recency=recency, source=source)
    own_titles = {engine.norm_title(f.title) for f in data.watched + data.watchlist}
    watched_meta = [enriched[f.key] for f in data.watched if f.key in enriched]
    ranked = engine.score_candidates(profile, cand_metas, ctx, seed_hits=hits,
                                     exclude_titles=own_titles,
                                     level=engine.taste_level(watched_meta),
                                     list_idx=list_idx)
    # Gentle: at higher lambda this fills genre quotas, dragging in weak
    # films (a 25%-animation target pulled in Minions-tier titles for a
    # viewer whose animation is Ghibli). Nudge the mix, don't enforce it.
    ranked = engine.calibrate(ranked, engine.genre_mix(watched_meta),
                              max(limit, 40), lam=0.25)
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
    pool, wanted = Counter(), set()

    for i, name in enumerate(usernames, 1):
        tag = f"{name} ({i}/{len(usernames)})"

        def p(section, page, total, _tag=tag):
            if progress:
                progress(f"{_tag}: {section}", page, total)

        data, enriched = load_user(name, p, (exports or {}).get(name), label=tag)
        users[name] = (data, enriched)
        all_enriched.update(enriched)
        watched_keys[name] = {f.key for f in data.watched}
        watchlist_keys[name] = {f.key for f in data.watchlist}
        weights = engine.watch_weights(data.watched, recency)
        pool.update(candidate_pool(data, enriched, weights, source, p, label=tag))
        # Everyone's watchlist films are prime candidates for the group.
        for f in data.watchlist:
            if f.key in enriched:
                tid = enriched[f.key]["tmdb_id"]
                pool[tid] += 2
                wanted.add(tid)

    # Same quality/register signals the single-user tool uses.
    all_watched_meta = [m for _, (d, e) in users.items()
                        for f in d.watched if (m := e.get(f.key))]
    list_idx = {}
    seeds = []
    for name, (d, e) in users.items():
        w = engine.watch_weights(d.watched, recency)
        top = sorted((f for f in d.watched if f.slug and f.key in e),
                     key=lambda f: -w[f.key])[:max(2, lists.MAX_SEEDS // len(users))]
        seeds += [f.slug for f in top]
    if seeds:
        counts, used = lists.cooccurrence(seeds, progress=(
            lambda s_, a, b: progress(s_, a, b)) if progress else None)
        list_idx = lists.index(counts, used)

    trimmed = _trim_pool(pool, protected=wanted)

    def pc(done, total):
        if progress:
            progress(f"looking up {total} candidate films", done, total)

    if progress:
        progress("looking up candidate films", 0, len(trimmed))
    cand_metas = tmdb.enrich_ids(list(trimmed), progress=pc)

    if progress:
        progress("scoring", 0, 0)
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
        seen_weight=seen_weight, seed_hits=trimmed, languages=languages,
        availability=availability, level=engine.taste_level(all_watched_meta),
        list_idx=list_idx)

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
