"""FastAPI service behind the two Letterboxd tools on peterwdunphy.com.

Jobs run in a background thread and report progress, so the frontend can
show a live status line while a user's history is scraped and enriched.

Run locally:   ../.venv/bin/uvicorn api:app --reload --port 8000
On the droplet: systemd unit -> uvicorn api:app --port 8010
"""

import io
import os
import threading
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from lbtools import config, export, letterboxd, pipeline

config.load_env()

# Shared beta password. This gates casual visitors, nothing more: anyone
# who reads the page source can find it, so never put anything sensitive
# behind it.
BETA_PASSWORD = os.environ.get("BETA_PASSWORD", "filmwhore")

ORIGINS = ["https://peterwdunphy.com", "https://www.peterwdunphy.com"]
LOCAL_ORIGIN = r"http://(localhost|127\.0\.0\.1)(:\d+)?"   # any dev port

app = FastAPI(title="Letterboxd Tools API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS,
                   allow_origin_regex=LOCAL_ORIGIN,
                   allow_methods=["*"], allow_headers=["*"])

JOBS = {}
JOB_TTL = 3600
MAX_USERS = 8


def _check_password(pw):
    if pw != BETA_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")


def _reap():
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
        JOBS.pop(jid, None)


def _run(job_id, fn):
    job = JOBS[job_id]

    def progress(label, page=0, total=0):
        job["stage"] = label
        job["page"], job["total"] = page, total

    try:
        job["result"] = fn(progress)
        job["status"] = "done"
    except letterboxd.ProfileNotFound as e:
        job["status"], job["error"] = "error", str(e)
    except letterboxd.ChallengeError:
        job["status"] = "error"
        job["error"] = "Letterboxd is rate-limiting us right now. Try again shortly."
    except Exception as e:                       # surfaced verbatim in the UI
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"


def _start(fn):
    _reap()
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "running", "stage": "starting", "page": 0,
                    "total": 0, "created": time.time(), "result": None,
                    "error": None}
    threading.Thread(target=_run, args=(job_id, fn), daemon=True).start()
    return {"job_id": job_id}


class EnrichRequest(BaseModel):
    password: str
    username: str
    recency: float = Field(0.5, ge=0, le=1)
    source: float = Field(0.35, ge=0, le=1)
    limit: int = Field(40, ge=1, le=200)


class GroupRequest(BaseModel):
    password: str
    usernames: list[str]
    recency: float = Field(0.5, ge=0, le=1)
    source: float = Field(0.35, ge=0, le=1)
    seen_weight: float = Field(0.0, ge=0, le=1)
    languages: list[str] | None = None
    availability: str | None = None          # 'flatrate' | 'any' | None
    limit: int = Field(40, ge=1, le=200)


@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS)}


@app.post("/api/auth")
def auth(body: dict):
    _check_password(body.get("password", ""))
    return {"ok": True}


@app.post("/api/enrich")
def enrich(req: EnrichRequest):
    _check_password(req.password)
    name = req.username.strip().lstrip("@").lower()
    if not name:
        raise HTTPException(400, "Username required")
    return _start(lambda p: pipeline.recommend(
        name, recency=req.recency, source=req.source, limit=req.limit,
        progress=p))


@app.post("/api/enrich-upload")
async def enrich_upload(password: str = Form(...), username: str = Form(...),
                        recency: float = Form(0.5), source: float = Form(0.35),
                        limit: int = Form(40), zipfile: UploadFile = File(...)):
    """Private-profile path: parse the user's export ZIP in memory."""
    _check_password(password)
    raw = await zipfile.read()
    try:
        watched, watchlist = export.parse_zip(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"Could not read that ZIP: {e}")
    if not watched and not watchlist:
        raise HTTPException(400, "No watched.csv or watchlist.csv in that ZIP")
    name = (username or "you").strip().lstrip("@").lower()
    return _start(lambda p: pipeline.recommend_from_export(
        name, watched, watchlist, recency=recency, source=source,
        limit=limit, progress=p))


@app.post("/api/group")
def group(req: GroupRequest):
    _check_password(req.password)
    names = [u.strip().lstrip("@").lower() for u in req.usernames if u.strip()]
    names = list(dict.fromkeys(names))
    if not 2 <= len(names) <= MAX_USERS:
        raise HTTPException(400, f"Enter between 2 and {MAX_USERS} usernames")
    return _start(lambda p: pipeline.group(
        names, recency=req.recency, source=req.source,
        seen_weight=req.seen_weight, limit=req.limit,
        languages=req.languages, availability=req.availability, progress=p))


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired")
    return {k: job[k] for k in
            ("status", "stage", "page", "total", "result", "error")}
