"""Settings read from environment variables (Railway service variables)."""
import os
from pathlib import Path


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


DATABASE_URL = env("DATABASE_URL")
ADMIN_PASSWORD = env("ADMIN_PASSWORD")
SECRET_KEY = env("SECRET_KEY") or (ADMIN_PASSWORD or "dev") + "-posts-secret"
ALLOWED_ORIGINS = [o.strip() for o in (env("ALLOWED_ORIGINS", "*") or "*").split(",") if o.strip()]

BUFFER_API_KEY = env("BUFFER_API_KEY")
BLOB_READ_WRITE_TOKEN = env("BLOB_READ_WRITE_TOKEN")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
TITLE_MODEL = env("TITLE_MODEL", "claude-haiku-5-5")

RUN_WORKERS = env("RUN_WORKERS", "1") == "1"
WORK_DIR = Path(env("WORK_DIR", "/tmp/posts-work"))
VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "gtavi"
