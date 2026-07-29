# Letterboxd Tools

Two film recommenders built on public Letterboxd data plus TMDB metadata,
running as password-gated beta pages on peterwdunphy.com.

1. **Watchlist Enricher** — one username in, films they'd like that aren't on
   their watchlist out. Sliders for recency weighting and whether taste comes
   from watch history or the existing watchlist.
2. **What Should We Watch?** — two to eight usernames, ranked by overlap in
   everyone's taste, with a "already seen it" slider, language filter, and a
   streaming-availability filter.

Both show poster, title, year, why it was picked, where it's streaming, and
link to the film on Letterboxd.

## How it works

```
Letterboxd (scrape)          TMDB (enrich)              engine
  watchlist, all pages   ->   metadata, keywords,   ->   theme communities
  watched films, all          credits, providers         IDF-weighted vectors
  diary: dates, ratings       posters                    taste profiles
                              recommendations            scoring + filters
```

- `backend/lbtools/letterboxd.py` — scraping. Uses `curl_cffi` with a Chrome
  TLS fingerprint: Cloudflare here fingerprints the TLS handshake, so a plain
  urllib/curl client gets 403 on deep pagination and the diary while a
  browser-shaped one gets the same public pages a logged-out browser sees.
- `backend/lbtools/tmdb.py` + `cache.py` — enrichment, 8 parallel workers,
  permanent SQLite cache shared across all users.
- `backend/lbtools/engine.py` — keyword co-occurrence graph collapsed into
  theme communities by label propagation, IDF-weighted feature vectors
  (genres, themes, directors, cast, language, decade), recency-decayed taste
  profiles, single-user and group scoring.
- `backend/lbtools/export.py` — Letterboxd export ZIP parser, for private
  profiles only.
- `backend/api.py` — FastAPI service with background jobs and progress.
- Frontend lives in `~/Dropbox/website`: `watchlist-enricher.html`,
  `what-should-we-watch.html`, `css/letterboxd-tools.css`,
  `js/letterboxd-tools.js`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env      # then paste your TMDB credentials in
```

## CLI

```bash
cd backend
../.venv/bin/python -m lbtools profile <username>
../.venv/bin/python -m lbtools recommend <username> -n 25 --recency 0.7 --source 0.3
../.venv/bin/python -m lbtools recommend <username> --export ~/Downloads/letterboxd-export.zip
```

## Local server

```bash
cd backend && ../.venv/bin/uvicorn api:app --reload --port 8011
cd ~/Dropbox/website && python3 -m http.server 8000
# http://localhost:8000/watchlist-enricher.html  (password: filmwhore)
```

The frontend auto-targets `localhost:8011` when served from localhost and
`boxd-api.peterwdunphy.com` otherwise. See `DEPLOY.md` for the droplet setup.

## Notes

- The beta password is checked server-side but is a shared secret; it keeps the
  tools quiet, it is not real security.
- Scraping is throttled to ~0.6s/page and results are cached. A 900-film
  profile takes about 40 seconds cold.
- Film data from TMDB (this product uses the TMDB API but is not endorsed or
  certified by TMDB); streaming availability from JustWatch via TMDB. Not
  affiliated with Letterboxd.
- Never commit `.env` (gitignored). `backend/data/cache.sqlite` is also
  gitignored and rebuilds itself.
