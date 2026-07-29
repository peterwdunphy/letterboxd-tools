"""CLI for the Phase 1 data layer.

    python3 -m lbtools profile <username> [--max-pages N] [--enrich N]

Scrapes the user, enriches films via TMDB (cached), and prints a taste
summary: top genres, themes, directors, actors, plus data-quality stats.
"""

import argparse
import sys
from collections import Counter

from . import engine, export, letterboxd, tmdb


def _load_user(args):
    """Shared scrape+enrich pipeline for the CLI commands."""
    export_watched = None
    if getattr(args, "export", None):
        export_watched, _ = export.parse_zip(args.export)
        print(f"Export ZIP: {len(export_watched)} watched films (complete history)")
    print(f"Fetching Letterboxd data for '{args.username}' ...")
    data = letterboxd.get_user(
        args.username, max_pages=getattr(args, "max_pages", None),
        progress=lambda s, p, t: print(f"\r  scraping {s}: page {p}/{t}    ",
                                       end="", flush=True),
        export_watched=export_watched)
    print()
    enriched, misses = tmdb.enrich_films(data.watched + data.watchlist)
    dated = sum(1 for f in data.watched if f.watched_date)
    print(f"  watchlist {len(data.watchlist)} | watched {len(data.watched)}"
          f" ({dated} dated) | enriched {len(enriched)}, unresolved {len(misses)}")
    return data, enriched


def cmd_recommend(args):
    data, enriched = _load_user(args)

    weights = engine.watch_weights(data.watched, args.recency)
    seeds = sorted((f for f in data.watched if f.key in enriched),
                   key=lambda f: -weights[f.key])[:25]
    seed_ids = [enriched[f.key]["tmdb_id"] for f in seeds]
    if args.source > 0.15:      # watchlist also seeds the candidate pool
        seed_ids += [enriched[f.key]["tmdb_id"]
                     for f in data.watchlist[:15] if f.key in enriched]

    print(f"Building candidate pool from {len(seed_ids)} seed films ...")
    cand_lists = tmdb.candidates_for(seed_ids)
    seed_hits = Counter(tid for ids in cand_lists.values() for tid in ids)
    own_ids = {m["tmdb_id"] for m in enriched.values()}
    cand_ids = [tid for tid in seed_hits if tid not in own_ids]
    print(f"  {len(cand_ids)} unseen candidates; enriching ...")
    cand_metas = tmdb.enrich_ids(cand_ids)

    ctx = engine.build_context(
        list(enriched.values()) + list(cand_metas.values()))
    profile, _ = engine.taste_profile(
        data.watched, data.watchlist, enriched, ctx,
        recency=args.recency, source=args.source)
    own_titles = {engine.norm_title(f.title)
                  for f in data.watched + data.watchlist}
    ranked = engine.score_candidates(profile, cand_metas, ctx,
                                     seed_hits=seed_hits,
                                     exclude_titles=own_titles)

    print(f"\nTop {args.n} recommendations for {args.username} "
          f"(recency={args.recency}, source={args.source}):\n")
    for i, (score, meta, reasons) in enumerate(ranked[:args.n], 1):
        year = (meta.get("release_date") or "????")[:4]
        stream = ", ".join(meta.get("providers", {}).get("flatrate", [])[:3])
        print(f"{i:3}. {meta['title']} ({year})  [{score:.3f}]"
              f"  https://letterboxd.com/tmdb/{meta['tmdb_id']}")
        if reasons:
            print(f"       because: {'; '.join(reasons)}"
                  f"{('  |  streaming: ' + stream) if stream else ''}")


def cmd_profile(args):
    def grid_progress(section, page, total):
        print(f"\r  scraping {section}: page {page}/{total}    ", end="", flush=True)

    export_watched = None
    if args.export:
        export_watched, _ = export.parse_zip(args.export)
        print(f"Export ZIP: {len(export_watched)} watched films (complete history)")

    print(f"Fetching Letterboxd data for '{args.username}' ...")
    data = letterboxd.get_user(args.username, max_pages=args.max_pages,
                               progress=grid_progress,
                               export_watched=export_watched)
    dated = sum(1 for f in data.watched if f.watched_date)
    print(f"\n  watchlist: {len(data.watchlist)} films"
          f" | watched: {len(data.watched)} films ({dated} with watch dates)"
          f" | diary entries: {len(data.diary)}")

    to_enrich = (data.watched + data.watchlist)[:args.enrich] if args.enrich \
        else data.watched + data.watchlist
    print(f"Enriching {len(to_enrich)} films via TMDB (cache-aware) ...")
    enriched, misses = tmdb.enrich_films(
        to_enrich, progress=lambda i, n: print(f"\r  {i}/{n}", end="", flush=True))
    print(f"\n  enriched: {len(enriched)} | unresolved: {len(misses)}"
          f"{' (' + ', '.join(misses[:5]) + ')' if misses else ''}")

    watched_meta = [enriched[f.key] for f in data.watched if f.key in enriched]
    if not watched_meta:
        sys.exit("No enriched watched films; nothing to summarize.")

    def top(key, n=10):
        c = Counter()
        for m in watched_meta:
            c.update(m.get(key, []))
        return ", ".join(f"{name} ({cnt})" for name, cnt in c.most_common(n))

    print(f"\nTaste summary from {len(watched_meta)} watched films:")
    print(f"  genres:    {top('genres', 8)}")
    print(f"  themes:    {top('keywords')}")
    print(f"  directors: {top('directors', 6)}")
    print(f"  actors:    {top('cast', 8)}")
    langs = Counter(m["original_language"] for m in watched_meta)
    print(f"  languages: {dict(langs.most_common(6))}")
    streamable = sum(1 for m in watched_meta if m["providers"].get("flatrate"))
    print(f"  currently on a streaming service (US): {streamable}/{len(watched_meta)}")


def main():
    ap = argparse.ArgumentParser(prog="lbtools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile", help="scrape + enrich + summarize one user")
    p.add_argument("username")
    p.add_argument("--max-pages", type=int, default=None,
                   help="cap grid pages per section (for quick tests)")
    p.add_argument("--enrich", type=int, default=None,
                   help="cap number of films sent to TMDB (for quick tests)")
    p.add_argument("--export", default=None,
                   help="path to a Letterboxd export ZIP for complete watch history")
    p.set_defaults(func=cmd_profile)

    r = sub.add_parser("recommend", help="watchlist-enricher recommendations")
    r.add_argument("username")
    r.add_argument("-n", type=int, default=25, help="how many recommendations")
    r.add_argument("--recency", type=float, default=0.5,
                   help="0..1: how heavily recent watches dominate taste")
    r.add_argument("--source", type=float, default=0.35,
                   help="0..1: taste from watched films (0) vs watchlist (1)")
    r.add_argument("--max-pages", type=int, default=None)
    r.add_argument("--export", default=None,
                   help="path to a Letterboxd export ZIP for complete watch history")
    r.set_defaults(func=cmd_recommend)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
