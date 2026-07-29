"""Parse a Letterboxd data-export ZIP (Settings -> Data -> Export).

This is the only way to get a user's complete watched history: scraping
is capped at ~172 recent films by a Cloudflare WAF rule on deep grid
pages. The ZIP needs no scraping at all and includes exact watch dates.

Files used:
  watched.csv   Date,Name,Year,Letterboxd URI          (every film ever)
  ratings.csv   Date,Name,Year,Letterboxd URI,Rating
  diary.csv     ...,Watched Date,Rewatch,...           (dated log entries)
  watchlist.csv Date,Name,Year,Letterboxd URI
"""

import csv
import io
import zipfile

from .letterboxd import Film


def _read_csv(zf, name):
    try:
        with zf.open(name) as fh:
            return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))
    except KeyError:
        return []


def _film(row, rank):
    year = row.get("Year") or None
    return Film(
        slug="",                                  # export uses boxd.it short links
        title=row.get("Name", ""),
        year=int(year) if year else None,
        uri=row.get("Letterboxd URI") or None,
        watched_date=row.get("Watched Date") or row.get("Date") or None,
        rank=rank,
    )


def parse_zip(path):
    """Returns (watched, watchlist) as Film lists, newest watched first."""
    with zipfile.ZipFile(path) as zf:
        watched_rows = _read_csv(zf, "watched.csv")
        ratings = {(r.get("Name"), r.get("Year")): float(r["Rating"])
                   for r in _read_csv(zf, "ratings.csv") if r.get("Rating")}
        diary_dates = {(r.get("Name"), r.get("Year")): r.get("Watched Date")
                       for r in _read_csv(zf, "diary.csv") if r.get("Watched Date")}
        watchlist_rows = _read_csv(zf, "watchlist.csv")

    watched = []
    for i, row in enumerate(reversed(watched_rows)):   # export is oldest-first
        f = _film(row, i)
        key = (row.get("Name"), row.get("Year"))
        f.rating = ratings.get(key)
        f.watched_date = diary_dates.get(key, f.watched_date)
        watched.append(f)
    watchlist = [_film(r, i) for i, r in enumerate(watchlist_rows)]
    return watched, watchlist
