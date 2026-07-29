"""SQLite cache: film enrichments live forever, slug->tmdb_id mappings too.

One file at backend/data/cache.sqlite. JSON blobs, no ORM.
"""

import json
import sqlite3
import time

from .config import DATA_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    tmdb_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slug_map (
    slug TEXT PRIMARY KEY,
    tmdb_id INTEGER            -- NULL means "searched and found nothing"
);
CREATE TABLE IF NOT EXISTS recs (
    tmdb_id INTEGER PRIMARY KEY,   -- seed film
    payload TEXT NOT NULL,         -- JSON list of recommended/similar tmdb ids
    fetched_at REAL NOT NULL
);
"""


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "cache.sqlite")
    conn.executescript(_SCHEMA)
    return conn


def get_film(conn, tmdb_id):
    row = conn.execute("SELECT payload FROM films WHERE tmdb_id=?", (tmdb_id,)).fetchone()
    return json.loads(row[0]) if row else None


def put_film(conn, tmdb_id, payload):
    conn.execute("INSERT OR REPLACE INTO films VALUES (?,?,?)",
                 (tmdb_id, json.dumps(payload), time.time()))
    conn.commit()


def get_recs(conn, tmdb_id):
    row = conn.execute("SELECT payload FROM recs WHERE tmdb_id=?", (tmdb_id,)).fetchone()
    return json.loads(row[0]) if row else None


def put_recs(conn, tmdb_id, ids):
    conn.execute("INSERT OR REPLACE INTO recs VALUES (?,?,?)",
                 (tmdb_id, json.dumps(ids), time.time()))
    conn.commit()


def get_slug(conn, slug):
    """Returns (found_in_cache, tmdb_id_or_None)."""
    row = conn.execute("SELECT tmdb_id FROM slug_map WHERE slug=?", (slug,)).fetchone()
    return (row is not None, row[0] if row else None)


def put_slug(conn, slug, tmdb_id):
    conn.execute("INSERT OR REPLACE INTO slug_map VALUES (?,?)", (slug, tmdb_id))
    conn.commit()
