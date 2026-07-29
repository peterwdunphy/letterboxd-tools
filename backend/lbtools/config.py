"""Load configuration from the repo-root .env file (no python-dotenv needed)."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "backend" / "data"


def load_env():
    """Parse KEY=VALUE lines from .env into os.environ (existing env wins)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def tmdb_token():
    load_env()
    token = os.environ.get("TMDB_READ_ACCESS_TOKEN", "")
    if not token:
        raise SystemExit("TMDB_READ_ACCESS_TOKEN missing; copy .env.example to .env and fill it in.")
    return token
