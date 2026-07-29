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
            if not co[k]:
                continue
            votes = Counter()
            for neighbor, w in co[k].items():
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

FEATURE_WEIGHTS = {"genre": 2.0, "theme": 1.5, "kw": 1.0, "dir": 2.5,
                   "act": 1.0, "lang": 0.5, "dec": 0.75}


def _raw_features(meta, communities):
    """Type-weighted features before corpus IDF is applied."""
    v = {}
    for g in meta.get("genres", []):
        v[f"genre:{g}"] = FEATURE_WEIGHTS["genre"]
    for k in meta.get("keywords", []):
        v[f"kw:{k}"] = FEATURE_WEIGHTS["kw"]
        c = communities.get(k)
        if c:
            key = f"theme:{c}"
            v[key] = min(v.get(key, 0) + FEATURE_WEIGHTS["theme"],
                         2 * FEATURE_WEIGHTS["theme"])
    for d in meta.get("directors", []):
        v[f"dir:{d}"] = FEATURE_WEIGHTS["dir"]
    for a in meta.get("cast", []):
        v[f"act:{a}"] = FEATURE_WEIGHTS["act"]
    if meta.get("original_language"):
        v[f"lang:{meta['original_language']}"] = FEATURE_WEIGHTS["lang"]
    rd = meta.get("release_date") or ""
    if len(rd) >= 4:
        v[f"dec:{rd[:3]}0s"] = FEATURE_WEIGHTS["dec"]
    return v


def film_vector(meta, ctx):
    communities, feat_idf = ctx
    return {f: w * feat_idf.get(f, 0.6)
            for f, w in _raw_features(meta, communities).items()}


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


def quality_prior(meta):
    """0..1 from TMDB ratings, Bayesian-shrunk toward the global mean so
    a 9.0 from 30 votes doesn't beat an 8.1 from 20,000."""
    v = meta.get("vote_count") or 0
    r = meta.get("vote_average") or 0
    bayes = (v * r + 300 * 6.2) / (v + 300)
    return max(0.0, min(1.0, (bayes - 5.0) / 3.0))


def score_candidates(profile, cand_metas, ctx, seed_hits=None,
                     require_released=True, exclude_ids=frozenset()):
    """Ranked [(score, meta, reasons)] for candidate films."""
    out = []
    for tid, meta in cand_metas.items():
        if tid in exclude_ids:
            continue
        if require_released and not released(meta):
            continue
        if (meta.get("vote_count") or 0) < 20:
            continue
        vec = film_vector(meta, ctx)
        sim = cosine(profile, _norm(vec))
        hits = (seed_hits or {}).get(tid, 0)
        s = sim * (0.6 + 0.4 * quality_prior(meta)) \
                * (1 + 0.06 * min(hits, 8))
        out.append((s, meta, _reasons(profile, vec)))
    out.sort(key=lambda t: -t[0])
    return out


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
