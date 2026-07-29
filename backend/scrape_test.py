#!/usr/bin/env python3
"""Phase 1 viability test: can this machine scrape Letterboxd?

Hits the three public endpoints the tools depend on (RSS feed, watchlist
grid, watched-films grid) for a given username, plus a short throttled
multi-page crawl to see whether sustained polite scraping trips
Cloudflare's bot protection. Stdlib only, so it runs anywhere:

    python3 scrape_test.py dave
    python3 scrape_test.py dave --pages 5   # crawl more watchlist pages

Exit code 0 = all critical checks passed, 1 = something is blocked.
"""

import re
import sys
import time
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
THROTTLE_SECONDS = 1.5


def fetch(url):
    """GET a URL with a browser UA. Returns (status, body, seconds)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.time() - start
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - start
    except Exception as e:
        return 0, f"[{type(e).__name__}] {e}", time.time() - start


def is_challenge(body):
    """Cloudflare's bot-check page always titles itself 'Just a moment...'."""
    return "Just a moment..." in body[:2000]


def parse_grid(body):
    """Film slugs from a watchlist/films grid page."""
    return re.findall(r'data-item-slug="([^"]+)"', body)


def page_count(body):
    """Highest page number linked in the paginator (1 if no paginator)."""
    nums = re.findall(r'/page/(\d+)/', body)
    return max(int(n) for n in nums) if nums else 1


def report(label, ok, detail):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: python3 scrape_test.py <letterboxd-username> [--pages N]")
    user = args[0]
    pages_to_crawl = 3
    for a in sys.argv[1:]:
        if a.startswith("--pages"):
            pages_to_crawl = int(sys.argv[sys.argv.index(a) + 1] if a == "--pages" else a.split("=")[1])

    base = "https://letterboxd.com"
    all_ok = True
    print(f"Letterboxd scrape viability test for '{user}'\n")

    # 1. RSS: the recency + ratings + TMDB-id source. No bot challenge expected.
    status, body, secs = fetch(f"{base}/{user}/rss/")
    items = body.count("<item>")
    tmdb_ids = len(re.findall(r"<tmdb:movieId>\d+</tmdb:movieId>", body))
    ok = status == 200 and items > 0
    all_ok &= report("RSS feed", ok,
                     f"HTTP {status}, {items} diary entries, {tmdb_ids} with TMDB ids, {secs:.1f}s")

    time.sleep(THROTTLE_SECONDS)

    # 2. Watchlist page 1: slugs + total page count.
    status, body, secs = fetch(f"{base}/{user}/watchlist/")
    slugs = parse_grid(body)
    wl_pages = page_count(body)
    if is_challenge(body):
        all_ok &= report("Watchlist", False, f"HTTP {status}, Cloudflare challenge page")
    else:
        ok = status == 200 and len(slugs) > 0
        all_ok &= report("Watchlist", ok,
                         f"HTTP {status}, {len(slugs)} films on page 1 of {wl_pages}, "
                         f"first: {slugs[0] if slugs else 'n/a'}, {secs:.1f}s")

    time.sleep(THROTTLE_SECONDS)

    # 3. Watched-films page 1: same grid format.
    status, body, secs = fetch(f"{base}/{user}/films/")
    slugs = parse_grid(body)
    if is_challenge(body):
        all_ok &= report("Watched films", False, f"HTTP {status}, Cloudflare challenge page")
    else:
        ok = status == 200 and len(slugs) > 0
        all_ok &= report("Watched films", ok,
                         f"HTTP {status}, {len(slugs)} films on page 1 of {page_count(body)}, {secs:.1f}s")

    # 4. Sustained crawl: a few throttled watchlist pages in a row. This is
    #    the part most likely to differ between a home IP and a datacenter IP.
    n = min(pages_to_crawl, wl_pages)
    print(f"\n  Sustained crawl: {n} watchlist pages at 1 request / {THROTTLE_SECONDS}s ...")
    got, blocked = 0, 0
    for p in range(1, n + 1):
        time.sleep(THROTTLE_SECONDS)
        status, body, secs = fetch(f"{base}/{user}/watchlist/page/{p}/")
        count = len(parse_grid(body))
        challenged = is_challenge(body)
        blocked += challenged
        got += count
        print(f"    page {p}: HTTP {status}, {count} films"
              f"{', CHALLENGE' if challenged else ''} ({secs:.1f}s)")
    all_ok &= report("Sustained crawl", blocked == 0 and got > 0,
                     f"{got} films across {n} pages, {blocked} challenges")

    # 5. Known-blocked endpoint, for reference only (does not affect pass/fail).
    time.sleep(THROTTLE_SECONDS)
    status, body, _ = fetch(f"{base}/{user}/films/diary/")
    note = "challenge (expected)" if is_challenge(body) or status == 403 else "accessible!"
    print(f"  [info] Diary page (not required): HTTP {status}, {note}")

    print(f"\n{'ALL CRITICAL CHECKS PASSED' if all_ok else 'SOME CHECKS FAILED'} "
          f"on this machine.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
