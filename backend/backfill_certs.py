"""One-off: refresh cached films to the current payload schema.

Run after bumping tmdb.SCHEMA_VERSION so the whole cache gains the new
fields at once, instead of every user's first run paying for it.
"""
import sys, time
from lbtools import cache, tmdb

conn = cache.connect()
rows = conn.execute("SELECT tmdb_id, payload FROM films").fetchall()
import json
stale = [tid for tid, p in rows if json.loads(p).get("v") != tmdb.SCHEMA_VERSION]
print(f"{len(rows)} cached films, {len(stale)} need refresh")
conn.close()
if not stale:
    sys.exit(0)
t0 = time.time()
out = tmdb.enrich_ids(stale, progress=lambda d, t: print(
    f"  {d}/{t}  ({time.time()-t0:.0f}s)", flush=True))
conn = cache.connect()
certs = {}
for tid, p in conn.execute("SELECT tmdb_id, payload FROM films"):
    c = json.loads(p).get("certification")
    certs[c] = certs.get(c, 0) + 1
print(f"\nrefreshed {len(out)} in {time.time()-t0:.0f}s")
print("certification coverage:", dict(sorted(certs.items(), key=lambda x: -x[1])[:8]))
