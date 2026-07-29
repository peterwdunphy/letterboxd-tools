# Letterboxd Tools for peterwdunphy.com — Roadmap

Two tools, both living as pages on the existing static site with a small backend API doing the work:

1. **Watchlist Enricher**: given one Letterboxd username, recommend films not on their watchlist, with sliders for (a) how heavily to weight recently watched films and (b) how much signal to draw from watched films vs. the existing watchlist.
2. **What Should We Watch**: 2 to 8 usernames, find the films with the most cross-user appeal, with the same weighting sliders plus a "seen by some of us is OK / not OK" control, a language filter, a released-only filter, and a "currently streamable/rentable" filter with provider logos.

Both display poster, title, year, hyperlinked to the film's Letterboxd page.

---

## What I verified (July 2026)

### Letterboxd data access

| Source | Status | Notes |
|---|---|---|
| Official API (api.letterboxd.com) | Application-only | Email api@letterboxd.com with intended use. Free, OAuth2, covers watchlists, ratings, diary, lists. No guaranteed reply. **Apply on day 1 anyway**; this is exactly the hobbyist use case they exist for, and it is the only fully sanctioned path. |
| `letterboxd.com/{user}/watchlist/` (HTML) | **Works** with a browser User-Agent | ~28 films/page, paginated (`/page/N/`). Each film exposes `data-target-link="/film/{slug}/"` and an `<img alt="Title">`. robots.txt does NOT disallow user watchlist/films pages for generic crawlers (it blocks only sort/genre/stats views and named AI crawlers). |
| `letterboxd.com/{user}/films/` (HTML) | **Page 1 only, release-date order** | Grid of ~72 watched films sorted by RELEASE date (newest first), not watch date. Pages 2+ return 403: a site-wide Cloudflare WAF rule blocks any `*/films/page/N` URL, and every `/by/` sorted view (including `by/date`, the watch-order sort) is 403 even on page 1 (verified 2026-07-28 from two IPs; referer, cookies, XHR headers, and `?page=` all fail). Consequences: full watched history is NOT scrapable, and scraped watch **recency** comes only from RSS; the grid sample contributes membership/taste, not order. |
| `letterboxd.com/{user}/rss/` | **Works, no bot challenge** | Last ~50 diary entries with `letterboxd:watchedDate`, member rating, and crucially `tmdb:movieId`. This is the recency signal, and it hands us TMDB IDs for free. |
| `letterboxd.com/{user}/films/diary/` (HTML) | **403** (Cloudflare challenge) | Blocked even with browser UA. Do not build on this. |
| `/film/{slug}/json/` and `/csi/film/{slug}/stats/` | **403** (Cloudflare challenge) | The "appears in N lists" count lives behind these. Treat list-overlap counts as a stretch goal (via official API if granted, or headless browser + aggressive caching). |
| User data export ZIP | Manual but complete | Settings → Data → Export gives `watched.csv` (with dates), `watchlist.csv`, `ratings.csv`, `diary.csv`. Zero scraping risk. **Promoted from fallback to first-class**: it is the only source of complete watch history (see WAF row above), so the UI offers an optional "upload your export ZIP" step; without it, tools run on the ~100-170 scrapable recent films and say so. Implemented in `backend/lbtools/export.py`. |

**Honesty note on scraping**: Letterboxd's TOS discourages automated access. The mitigations are: low volume (one user's pages per request), 1 req/sec throttle, 24h caching per username, RSS-first, the export-upload alternative, and a pending official API application. If they grant API access, the scraper gets deleted.

### Metadata, themes, streaming: TMDB covers all of it

One free API key ([developer.themoviedb.org](https://developer.themoviedb.org)) gets:

- **Search** by title+year → TMDB ID for films found via scraped watchlist slugs (RSS entries already carry the ID).
- **`/movie/{id}`**: synopsis, genres, original language, release date (solves "no Dune 3": require `release_date <= today` and `status == Released`), runtime, popularity.
- **`/movie/{id}/keywords`**: plot keywords/tags, the raw material for the theme network. Letterboxd's own film metadata is built on TMDB, so these tags line up with what Letterboxd shows on film pages. They ride along in the same cached enrichment call, so there is no speed cost. (IMDb keywords would add little here: IMDb has no free API, and its free non-commercial datasets omit keywords, so IMDb is skipped for v1.)
- **`/movie/{id}/credits`**: full cast and crew (directors, top-billed actors go into the feature vectors).
- **`/movie/{id}/recommendations`** and **`/similar`**: candidate-pool generators (recommendations is collaborative-filtering based, similar is keyword/genre based).
- **`/movie/{id}/watch/providers`**: per-country streaming/rent/buy availability, **data licensed from JustWatch**. This is the sanctioned free JustWatch route (their direct API is B2B/partners only). Requires visible "Streaming data from JustWatch" attribution and links back to TMDB's watch page. Filter: keep a film if it has any `flatrate`, `rent`, or `buy` entry for US.
- **Posters**: `image.tmdb.org/t/p/w342/{poster_path}`, free to hotlink with TMDB attribution.

Rate limit is ~40-50 req/sec, effectively unlimited for this use. Cache every film's enrichment forever (metadata barely changes; refresh watch providers weekly).

### Hosting: reuse the AMC seat watcher pattern

The site (`~/Dropbox/website`) is static HTML deployed to Cloudflare Workers static assets via `wrangler.jsonc`, and the existing Movie Seat Watcher already proves the architecture: `tools.html` / `movie-watcher.html` frontend pages with plain JS (`js/movie-watcher.js`) calling a Python backend at `amc-api.peterwdunphy.com` on the DigitalOcean droplet. These tools copy that exactly:

- Frontend: `letterboxd-enrich.html` and `what-should-we-watch.html` added to the site, entries in the Tools nav dropdown, styled with the existing `.box` / `.tool-form` / `.tool-chip` component classes, plain JS files in `js/`. No build step, works within the site's design system.
- Backend: Python FastAPI service on the **same DigitalOcean droplet** as the AMC API, served at a new subdomain (e.g. `boxd-api.peterwdunphy.com`, DNS + reverse proxy config mirroring whatever amc-api uses), CORS locked to peterwdunphy.com.
- Cache: SQLite file on the droplet (one file, no database server).

One risk to test early: Letterboxd's Cloudflare protection may challenge the droplet's datacenter IP harder than a residential one. Mitigations: `curl-cffi` browser impersonation, 24h per-user caching, RSS-first, and the export-upload fallback. Phase 1 tests scraping from the droplet before anything is built on top.

---

## Recommendation engine design

### Per-film feature vector (built from TMDB, cached in SQLite)

- Genres (multi-hot)
- Keywords/themes (multi-hot over a pruned vocabulary)
- Director(s), top ~8 billed cast (multi-hot over people seen in the corpus)
- Synopsis text: TF-IDF vector initially; upgrade path to sentence embeddings (a small local model like `all-MiniLM-L6-v2`) if TF-IDF feels shallow
- Decade, original language, runtime bucket

### Theme network (the network-analysis piece)

Keywords are noisy and sparse ("heist" vs "bank robbery" never co-occur exactly). Fix: build a keyword co-occurrence graph across the film corpus (nodes = keywords, edge weight = # films sharing both), run community detection (Louvain via `networkx`/`python-louvain`), and collapse keywords into theme communities. Films then get theme-community features, so a heist film and a bank-robbery film correctly look similar. Same trick optionally applies to a film-film graph (edges = shared cast/director/theme) for graph-based similarity scores.

### Taste profile per user

Weighted average of watched-film vectors, where the recency slider controls an exponential decay on watch order (RSS gives true dates for the last ~50; older films fall back to rank order in the films grid, or exact dates if they uploaded their export). The watched-vs-watchlist slider mixes in the centroid of watchlist vectors. Ratings, when visible in RSS/export, multiply the weight (a 5-star film says more than a 2-star).

### Candidate pool (tool 1)

Union of: TMDB recommendations + similar for the user's top-weighted ~30 seed films, minus everything watched or watchlisted. Score = cosine similarity to the taste profile, with a small popularity prior so the list isn't all obscurities. Output top ~50 with poster/title/year/Letterboxd link (link via TMDB→Letterboxd: `letterboxd.com/tmdb/{tmdb_id}` redirects to the film page, verified pattern).

### Group scoring (tool 2)

For each candidate (union of everyone's watchlists + recommendation pools):
`score = Σ_users [ sim(user_profile, film) + watchlist_bonus if on their watchlist ] − seen_penalty × (#users who watched it)`
where the seen_penalty slider runs from "hard exclude if anyone's seen it" to "seen films welcome." Watchlist-intersection films (on 2+ watchlists) get surfaced in their own "you both already want this" section, which is usually the real answer. Then apply filters: original language, released-only, and has-a-watch-provider (streaming/rent/buy toggles), with provider logos + JustWatch attribution in the card.

---

## Phased roadmap

### Phase 0: Access (half a day)
- Email api@letterboxd.com describing both tools. [ ]
- Register for a TMDB API key. [ ]
- Repo scaffolding: `backend/` (FastAPI app, scraper, engine), `frontend/` (two HTML pages + JS + CSS matching the site), `data/` (SQLite cache, gitignored).

### Design direction (applies to Phases 3-4)

Per Peter (2026-07-28): the tool pages should be **immersive and beautiful**, a clear step up from the utilitarian seat-watcher forms, while keeping his name/attribution at the top. **Mobile-first is required** (group movie-picking happens on phones on couches). Ideas to explore when we get there: poster-driven layouts, a dark cinematic theme distinct from the retro site skin, smooth reveal of results, cards designed for thumb scrolling. Load the design/dataviz skills before building these pages.

### Phase 1: Data layer (the foundation, ~a weekend)
**DONE 2026-07-28.** Python package `backend/lbtools/` (stdlib only, no pip installs needed on the droplet):
- `letterboxd.py`: watchlist (all pages), watched grid (page 1, WAF-capped), RSS diary, merged into a `UserData` with coverage flags.
- `export.py`: full watch history + ratings + dates from the export ZIP.
- `tmdb.py` + `cache.py`: slug→TMDB resolution, one-call enrichment (details, keywords, credits, US watch providers), permanent SQLite cache.
- CLI: `python3 -m lbtools profile <username> [--export zip] [--max-pages N] [--enrich N]` prints an enriched taste summary. Verified end-to-end on a real user; TV specials correctly unresolved (movies only).

### Phase 2: Engine (~a weekend)
- Feature vectors, keyword→theme community detection, taste profiles, candidate generation, scoring with the three weights as function parameters.
- Deliverable: CLI spits out top-25 recommendations for you, and for you+a friend. Eyeball tuning happens here, it is the fun part.

### Phase 3: Tool 1 live (~a weekend)
- FastAPI: `POST /api/enrich {username, recency_weight, watched_vs_watchlist}` returning JSON film cards. Long-running scrapes stream progress via SSE or simple polling of a job ID (the AMC watcher's `api()` helper pattern in `js/movie-watcher.js` carries over).
- `letterboxd-enrich.html` on the site: username field, two sliders, results grid (poster, title, year, Letterboxd link), built from the existing `.tool-form` components. Cache by username so slider changes re-rank instantly without re-scraping.
- Deploy backend to the DigitalOcean droplet at `boxd-api.peterwdunphy.com`, CORS to peterwdunphy.com, per-IP rate limit.

### Phase 4: Tool 2 live (~a weekend)
- `POST /api/group {usernames[], weights, seen_penalty, language, availability}` reusing everything from Phase 3.
- `what-should-we-watch.html`: add-user chips (2 to 8), extra controls, watchlist-intersection section, provider logos, JustWatch + TMDB attribution footer. Both pages added to the Tools nav dropdown in every HTML file (the site has no templating) and to `sitemap.xml`.

### Phase 5: Polish and stretch
- "Upload your Letterboxd export" path (better dates, zero scraping).
- List-overlap signal if API access is granted (or Playwright + long cache as plan B).
- Synopsis embeddings upgrade; "why this?" explanations (shared themes/actors/directors listed on each card).
- Watch-provider refresh job (weekly) and a "my region" selector beyond US.

## Risks, in order of likelihood

1. ~~**Letterboxd blocks the server's datacenter IP** even for watchlist pages.~~ **Tested and retired (2026-07-28)**: `backend/scrape_test.py` run on the DigitalOcean droplet passed every check (RSS, watchlist, films grid, 4-page sustained crawl at 1 req/1.5s, zero Cloudflare challenges). Residual risk is Letterboxd tightening things later; the mitigations (24h cache, throttle, export-upload fallback, API application) stay in the design as insurance.
2. **Big accounts are slow**: 2,000 watched films = ~72 pages ≈ 90s at throttle speed. Mitigate with progress UI, caching, and capping profile depth (top ~1,000 most recent).
3. **Slug→TMDB matching misses** on obscure/retitled films. RSS-sourced films come with IDs; for the rest, title+year search resolves ~95%+, log and skip the tail.
4. **API application never answered**: the scraper path is the plan of record, so nothing blocks on it.

## Costs

TMDB free, JustWatch-via-TMDB free (attribution required), backend rides on the existing DigitalOcean droplet, everything else already exists. Net new cost: $0.
