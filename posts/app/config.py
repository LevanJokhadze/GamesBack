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
# Optional: a YouTube Data API v3 key (Google Cloud console). Reads public view/like/comment counts of our Shorts,
# since Buffer's post metrics may not cover YouTube. Free: 1 quota unit per 50 videos, 10,000 units a day.
YOUTUBE_API_KEY = env("YOUTUBE_API_KEY")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
TITLE_MODEL = env("TITLE_MODEL", "claude-haiku-5-5")
# Look at each source clip for nudity before cutting it (integrations/safety.py). NUDITY_CHECK=0 turns it off.
NUDITY_CHECK = env("NUDITY_CHECK", "1") != "0"

# Encrypts social account passwords stored in the database (any long random string; never change it once set,
# or saved passwords can no longer be read)
ACCOUNTS_KEY = env("ACCOUNTS_KEY")

# Workers run by default only in Railway's production environment (or outside Railway): a forked or preview
# environment has a copy of the database and the same keys, and must never post, email or submit twice.
_RAILWAY_ENV = env("RAILWAY_ENVIRONMENT_NAME")
RUN_WORKERS = env("RUN_WORKERS", "1" if _RAILWAY_ENV in (None, "production") else "0") == "1"
# Optional allowlist, e.g. "render" in a test environment: only these workers start
ONLY_WORKERS = [w.strip() for w in (env("WORKERS") or "").split(",") if w.strip()]
WORK_DIR = Path(env("WORK_DIR", "/tmp/posts-work"))
VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "gtavi"

# Email alerts (Gmail SMTP with an app password works: smtp.gmail.com, 465)
SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = env("SMTP_PORT", "465")
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
MAIL_FROM = env("MAIL_FROM")
MAIL_FROM_NAME = env("MAIL_FROM_NAME", "posts.lane.ge")
ALERT_EMAIL = env("ALERT_EMAIL")
DASHBOARD_URL = (env("DASHBOARD_URL", "https://posts.lane.ge") or "").rstrip("/")

# Auto-submit to Content Rewards (Whop). OAuth app from whop.com/dashboard/developer (Sign in with Whop):
# its client id and secret; the redirect URI to register there is {PUBLIC_API_URL}/api/whop/callback
WHOP_CLIENT_ID = env("WHOP_CLIENT_ID")
WHOP_CLIENT_SECRET = env("WHOP_CLIENT_SECRET")
# Public URL of this API (for the Whop sign-in redirect), e.g. https://posts-api-src-production.up.railway.app
PUBLIC_API_URL = (env("PUBLIC_API_URL") or env("RAILWAY_PUBLIC_DOMAIN") or "").rstrip("/")
if PUBLIC_API_URL and not PUBLIC_API_URL.startswith("http"):
    PUBLIC_API_URL = "https://" + PUBLIC_API_URL
# Shared secret for an outside submit helper (laptop / server running Claude with a browser): sent as
# "Authorization: Bearer <SUBMIT_AGENT_TOKEN>" to /api/agent/submissions. Any long random string.
SUBMIT_AGENT_TOKEN = env("SUBMIT_AGENT_TOKEN")

# Jarvis, the dashboard's voice assistant (Gemini Live, relayed by this API so credentials never reach the browser).
# The same variables as the Personal Dashboard's work mode, so its values can be copied over as they are:
# Vertex AI when GOOGLE_CLOUD_PROJECT is set (with GOOGLE_CREDENTIALS_JSON, the service account key as JSON or base64),
# otherwise a Gemini API key from https://aistudio.google.com/apikey in GEMINI_API_KEY.
GOOGLE_CLOUD_PROJECT = env("GOOGLE_CLOUD_PROJECT")
GOOGLE_CLOUD_LOCATION = env("GOOGLE_CLOUD_LOCATION", "eu")  # gemini-3.8-live is served on "eu" and "us"
GOOGLE_CREDENTIALS_JSON = env("GOOGLE_CREDENTIALS_JSON")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_LIVE_MODEL = env("GEMINI_LIVE_MODEL", "gemini-3.8-live")
GEMINI_VOICE = env("GEMINI_VOICE")
