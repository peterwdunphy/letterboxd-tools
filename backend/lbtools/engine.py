"""Recommendation engine: theme communities, taste profiles, scoring.

Pure stdlib. The three UI sliders map directly onto parameters here:
  recency  [0,1]  how fast older watches fade from the taste profile
  source   [0,1]  0 = profile from watched films only, 1 = watchlist only
  seen     [0,1]  group tool: 0 = exclude anything anyone watched,
                  1 = watched films welcome
"""

import math
from collections import Counter, defaultdict
from datetime import date


# ---------------------------------------------------------------- themes

def build_context(metas, min_count=3):
    """Corpus-level state needed to vectorize films consistently:
    (communities, feature_idf). Compute once over profile + candidate
    metas. Every feature (genres included) is IDF-weighted, so 'Drama'
    (in half the corpus) barely counts while a shared director or niche
    theme dominates: without this, sparse generic films win on cosine."""
    n = max(2, len(metas))
    df = Counter(k for m in metas for k in set(m.get("keywords", [])))
    communities = keyword_communities(metas, df, n, min_count)
    feat_df = Counter()
    for m in metas:
        feat_df.update(_raw_features(m, communities).keys())
    max_l = math.log10(n)
    feat_idf = {f: math.log10(n / c) / max_l for f, c in feat_df.items()}
    return communities, feat_idf


def keyword_communities(metas, df, n, min_count=3, iterations=8):
    """Collapse sparse TMDB keywords into theme communities.

    Builds a keyword co-occurrence graph over the corpus and runs
    deterministic label propagation, so near-synonyms ('heist', 'bank
    robbery') that co-occur with the same neighbors converge on one
    label. Ubiquitous keywords (mood tags, credits stingers, 'based on
    novel or book') are excluded up front: hubs that co-occur with
    everything would otherwise glue the whole graph into one mega-theme.
    Returns {keyword: community_label}.
    """
    vocab = {k for k, c in df.items() if min_count <= c <= max(5, n * 0.05)}
    raw = defaultdict(Counter)
    for m in metas:
        ks = sorted(set(m.get("keywords", [])) & vocab)
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                raw[a][b] += 1
                raw[b][a] += 1
    # Keep only edges backed by 2+ films: one film's keyword list is a
    # fully-connected clique, and those single-film edges are what glue
    # the whole graph into one giant community.
    co = {a: Counter({b: w for b, w in nbrs.items() if w >= 2})
          for a, nbrs in raw.items()}
    label = {k: k for k in vocab}
    for _ in range(iterations):
        changed = 0
        for k in sorted(vocab):
            neighbors = co.get(k)      # a keyword can co-occur with nothing
            if not neighbors:
                continue
            votes = Counter()
            for neighbor, w in neighbors.items():
                votes[label[neighbor]] += w
            votes[label[k]] += 1                     # slight self-stickiness
            new = min(votes, key=lambda c: (-votes[c], c))   # deterministic tie-break
            if new != label[k]:
                label[k] = new
                changed += 1
        if not changed:
            break
    # Dissolve anything that still grew into a blob: a 'theme' spanning
    # dozens of unrelated keywords carries no meaning.
    sizes = Counter(label.values())
    return {k: c for k, c in label.items() if 2 <= sizes[c] <= 25}


# ---------------------------------------------------------------- vectors

BLOCK_NORM = 0.25

# "Animation" lumps Grave of the Fireflies in with Captain Underpants, so
# the coarse genre tag carries less weight than what a film is actually
# about (its themes and keywords), who made it, and what language it is
# in — the things that separate arthouse animation from kids' fare.
FEATURE_WEIGHTS = {"genre": 1.3, "theme": 2.0, "kw": 1.3, "dir": 2.5,
                   "act": 1.0, "lang": 1.1, "dec": 0.75, "cert": 1.6}


def _raw_features(meta, communities):
    """Unweighted features, grouped by type."""
    v = {}
    for g in meta.get("genres", []):
        v[f"genre:{g}"] = 1.0
    for k in meta.get("keywords", []):
        v[f"kw:{k}"] = 1.0
        c = communities.get(k)
        if c:
            key = f"theme:{c}"
            v[key] = min(v.get(key, 0) + 1.0, 2.0)
    for d in meta.get("directors", []):
        v[f"dir:{d}"] = 1.0
    for a in meta.get("cast", []):
        v[f"act:{a}"] = 1.0
    if meta.get("original_language"):
        v[f"lang:{meta['original_language']}"] = 1.0
    rd = meta.get("release_date") or ""
    if len(rd) >= 4:
        v[f"dec:{rd[:3]}0s"] = 1.0
    cert = meta.get("certification")
    if cert:
        v[f"cert:{cert}"] = 1.0
    return v


def _soft_idf(x):
    """Compress IDF into [0.35, 1].

    Raw IDF scored 'Drama' at 0.08 and 'Animation' at 0.26, so a plain
    drama arrived nearly featureless while anything animated carried three
    times the weight. Rare features should count for more, not for
    everything.
    """
    return 0.35 + 0.65 * x


def film_vector(meta, ctx):
    """Feature vector with each TYPE normalized to the same total mass.

    Without per-type normalization, films that carry many genres beat
    films that carry few, regardless of taste: a kids' animation tagged
    Animation/Family/Comedy/Adventure/Fantasy/Sci-Fi hits six profile
    dimensions at once while a drama hits one. That is a property of TMDB's
    tagging conventions, not of what anyone likes to watch.
    """
    communities, feat_idf = ctx
    raw = _raw_features(meta, communities)
    blocks = {}
    for f, w in raw.items():
        kind = f.split(":", 1)[0]
        blocks.setdefault(kind, {})[f] = w * _soft_idf(feat_idf.get(f, 0.6))
    out = {}
    for kind, feats in blocks.items():
        mag = math.sqrt(sum(x * x for x in feats.values())) or 1.0
        # BLOCK_NORM interpolates between "more tags = more weight" (0.0,
        # which floods results with genre-dense kids animation) and full
        # normalization (1.0, which floods them with single-genre drama).
        scale = FEATURE_WEIGHTS.get(kind, 1.0) / (mag ** BLOCK_NORM)
        for f, x in feats.items():
            out[f] = x * scale
    return out


def _norm(v):
    n = math.sqrt(sum(x * x for x in v.values()))
    return {k: x / n for k, x in v.items()} if n else {}


def cosine(a, b):
    if len(b) < len(a):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in a.items())


# ---------------------------------------------------------------- profiles

def watch_weights(watched, recency=0.5):
    """{film.key: weight}. Dated films decay by recency rank; undated
    films (the release-date-ordered grid sample) sit at the decayed tail.
    Ratings scale the weight: a 5-star film says more than a 2-star."""
    dated = sorted((f for f in watched if f.watched_date),
                   key=lambda f: f.watched_date, reverse=True)
    undated = [f for f in watched if not f.watched_date]
    half_life = 200 - 185 * recency          # films until weight halves
    weights = {}
    for i, f in enumerate(dated):
        weights[f.key] = 0.5 ** (i / half_life)
    tail = (0.5 ** (len(dated) / half_life)) * 0.7
    for f in undated:
        weights[f.key] = tail
    for f in watched:
        if f.rating:
            weights[f.key] *= 0.6 + 0.18 * f.rating   # 0.69x .. 1.5x
        if f.liked:
            weights[f.key] *= 1.15
    return weights


def taste_profile(watched, watchlist, enriched, ctx,
                  recency=0.5, source=0.35):
    weights = watch_weights(watched, recency)
    watched_sum, wl_sum = defaultdict(float), defaultdict(float)
    for f in watched:
        meta = enriched.get(f.key)
        if meta:
            for k, x in film_vector(meta, ctx).items():
                watched_sum[k] += x * weights[f.key]
    for f in watchlist:
        meta = enriched.get(f.key)
        if meta:
            for k, x in film_vector(meta, ctx).items():
                wl_sum[k] += x
    watched_n, wl_n = _norm(watched_sum), _norm(wl_sum)
    profile = defaultdict(float)
    for k, x in watched_n.items():
        profile[k] += (1 - source) * x
    for k, x in wl_n.items():
        profile[k] += source * x
    return _norm(profile), weights


# ---------------------------------------------------------------- scoring

def released(meta, today=None):
    rd = meta.get("release_date") or ""
    return bool(rd) and rd <= (today or date.today().isoformat())


MIN_VOTES = 250        # below this, TMDB scores are noise


def quality_prior(meta):
    """0..1 from TMDB ratings, Bayesian-shrunk toward the global mean so
    a 9.0 from 30 votes doesn't beat an 8.1 from 20,000."""
    v = meta.get("vote_count") or 0
    r = meta.get("vote_average") or 0
    bayes = (v * r + 300 * 6.2) / (v + 300)
    return max(0.0, min(1.0, (bayes - 5.0) / 3.0))


def taste_level(metas):
    """The quality bar the user actually watches at, as a prior value."""
    if not metas:
        return 0.5
    vals = sorted(quality_prior(m) for m in metas)
    return vals[len(vals) // 2]


def level_fit(meta, level):
    """Penalize films well below the user's usual bar, and don't reward
    films far above it either. Recommending Minions to someone whose
    animation diet is Ghibli is a quality mismatch, not a genre one."""
    gap = quality_prior(meta) - level
    if gap >= 0:
        return 1.0
    return max(0.35, 1.0 + 2.2 * gap)          # falls off below their bar


def norm_title(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def genre_mix(metas, weights=None):
    """Share of viewing per genre. Each film splits one unit across its
    genres, so a six-genre kids film doesn't count six times."""
    mix = defaultdict(float)
    total = 0.0
    for m in metas:
        gs = m.get("genres") or []
        if not gs:
            continue
        w = 1.0 if weights is None else weights.get(m["tmdb_id"], 1.0)
        for g in gs:
            mix[g] += w / len(gs)
        total += w
    return {g: v / total for g, v in mix.items()} if total else {}


def calibrate(ranked, target, limit, lam=0.6):
    """Greedy re-rank so the selected set's genre mix tracks the user's.

    Pure cosine ranking collapses onto whatever the profile's densest
    dimension is: it returned 100% Drama for a viewer who watches 44%
    Drama, and before that 48% Animation for one who watches 20%. Both are
    the same failure — the ranking optimizes per-film similarity with no
    notion of what the *set* should look like. This is the standard
    calibrated-recommendation fix (Steck 2018): at each step, pick the
    film that best trades off its own score against how far it pushes the
    running genre mix away from the target.

    lam=0 reproduces the raw ranking; higher values track the mix harder.
    """
    if not target:
        return ranked[:limit]
    pool = list(ranked)
    picked, counts, out = [], defaultdict(float), []
    for _ in range(min(limit, len(pool))):
        best, best_val, best_i = None, None, None
        n = len(out) + 1
        # Only the strongest remaining candidates are worth considering;
        # scanning the whole tail each round is quadratic for no gain.
        for i, item in enumerate(pool[:120]):
            meta = item[1]
            gs = meta.get("genres") or []
            trial = dict(counts)
            for g in gs:
                trial[g] = trial.get(g, 0.0) + 1.0 / len(gs)
            # KL(target || selected), smoothed so unseen genres don't blow up
            kl = 0.0
            for g, p in target.items():
                q = (trial.get(g, 0.0) / n) * 0.99 + 0.01 * p
                if q > 0:
                    kl += p * math.log(p / q)
            val = item[0] - lam * kl
            if best_val is None or val > best_val:
                best, best_val, best_i = item, val, i
        if best is None:
            break
        out.append(best)
        for g in (best[1].get("genres") or []):
            counts[g] += 1.0 / len(best[1]["genres"])
        pool.pop(best_i)
    return out


def score_candidates(profile, cand_metas, ctx, seed_hits=None,
                     require_released=True, exclude_ids=frozenset(),
                     exclude_titles=frozenset(), level=None, list_idx=None):
    """Ranked [(score, meta, reasons)] for candidate films.

    exclude_titles holds normalized titles of everything the user has
    watched or watchlisted: same-title different-year entries (remakes,
    TMDB duplicates like Cleopatra 1934 vs 1963) read as 'you recommended
    what I just watched', so they are filtered even though the ids differ.
    """
    out = []
    for tid, meta in cand_metas.items():
        if tid in exclude_ids or norm_title(meta.get("title")) in exclude_titles:
            continue
        if require_released and not released(meta):
            continue
        if (meta.get("vote_count") or 0) < MIN_VOTES:
            continue
        vec = film_vector(meta, ctx)
        sim = cosine(profile, _norm(vec))
        hits = (seed_hits or {}).get(tid, 0)
        s = sim * (0.55 + 0.45 * quality_prior(meta)) \
                * (1 + 0.06 * min(hits, 8))
        if level is not None:
            s *= level_fit(meta, level)
        if list_idx:
            s *= list_affinity(list_idx, meta)
        out.append((s, meta, _reasons(profile, vec)))
    out.sort(key=lambda t: -t[0])
    return out


def score_group(profiles, cand_metas, ctx, watched_keys, watchlist_keys,
                title_index, seen_weight=0.0, seed_hits=None,
                require_released=True, languages=None, availability=None,
                level=None, list_idx=None):
    """Rank candidates for a group of users.

    profiles         {username: taste vector}
    watched_keys     {username: set of film keys they have logged}
    watchlist_keys   {username: set of film keys on their watchlist}
    title_index      {normalized title: film key} for candidate matching
    seen_weight      0 = drop anything anyone has seen, 1 = seen is fine
    languages        keep only these original_language codes (None = all)
    availability     'flatrate' | 'any' | None  (streaming filter)
    """
    users = list(profiles)
    out = []
    for tid, meta in cand_metas.items():
        if require_released and not released(meta):
            continue
        if (meta.get("vote_count") or 0) < MIN_VOTES:
            continue
        if languages and meta.get("original_language") not in languages:
            continue
        provs = meta.get("providers", {})
        if availability == "flatrate" and not provs.get("flatrate"):
            continue
        if availability == "any" and not any(provs.get(k) for k in
                                             ("flatrate", "rent", "buy")):
            continue
        key = title_index.get(norm_title(meta.get("title")))
        seen_by = [u for u in users if key and key in watched_keys[u]]
        if seen_by and seen_weight <= 0.01:
            continue
        wants = [u for u in users if key and key in watchlist_keys[u]]

        vec = _norm(film_vector(meta, ctx))
        sims = {u: cosine(profiles[u], vec) for u in users}
        mean = sum(sims.values()) / len(users)
        worst = min(sims.values())
        # Reward broad appeal but protect the least-served person: a film
        # three people love and one hates is a bad group pick.
        base = 0.6 * mean + 0.4 * worst
        base *= 1 + 0.25 * len(wants) / len(users)      # on their watchlists
        if seen_by:
            base *= seen_weight ** (len(seen_by) / len(users))
        s = base * (0.55 + 0.45 * quality_prior(meta)) \
                 * (1 + 0.05 * min((seed_hits or {}).get(tid, 0), 8))
        if level is not None:
            s *= level_fit(meta, level)
        if list_idx:
            s *= list_affinity(list_idx, meta)
        out.append({
            "score": s, "meta": meta,
            "seen_by": seen_by, "wants": wants,
            "per_user": {u: round(sims[u], 4) for u in users},
            "reasons": _reasons_multi(profiles, film_vector(meta, ctx)),
        })
    out.sort(key=lambda d: -d["score"])
    return out


def _reasons_multi(profiles, vec):
    avg = defaultdict(float)
    for p in profiles.values():
        for k, x in p.items():
            avg[k] += x / len(profiles)
    return _reasons(avg, vec)


def list_affinity(idx, meta):
    """How often this film shares Letterboxd lists with the user's films."""
    from . import lists
    return lists.boost(idx, norm_title(meta.get("title")))


def _reasons(profile, vec):
    """Top overlapping features, human-readable, for 'why this?' display."""
    scored = sorted(((profile.get(k, 0) * x, k) for k, x in vec.items()),
                    key=lambda t: -t[0])[:4]
    labels = []
    for val, k in scored:
        if val <= 0:
            continue
        kind, _, name = k.partition(":")
        labels.append({"genre": name, "theme": f"theme: {name}",
                       "kw": f"theme: {name}", "dir": f"dir. {name}",
                       "act": name, "lang": f"{name}-language",
                       "dec": name}.get(kind, name))
    return labels
