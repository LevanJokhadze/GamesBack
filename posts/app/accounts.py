"""Social accounts by geo and platform: login, encrypted password, target streamers and stats.

Passwords are encrypted with Fernet using a key derived from the ACCOUNTS_KEY variable; the API never returns them
except from the explicit reveal call. An account linked to a Buffer channel gets its stats from the posts table;
other accounts keep stats typed in by hand."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import config, db

router = APIRouter()

PLATFORMS = ("instagram", "tiktok", "youtube")
STATUSES = ("warming", "active", "paused", "banned", "retired")
COLS = ("id, geo_id, platform, handle, login, profile_url, status, streamers, buffer_channel_id, followers, total_views, "
        "total_posts, total_likes, stats_at, notes, created_at, updated_at, password_enc IS NOT NULL AS has_password")


def _fernet() -> Fernet | None:
    if not config.ACCOUNTS_KEY:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.ACCOUNTS_KEY.encode()).digest()))


def encrypt(pw: str) -> str:
    f = _fernet()
    if not f:
        raise HTTPException(400, "Passwords can't be saved yet: add the ACCOUNTS_KEY variable on the server first")
    return f.encrypt(pw.encode()).decode()


def decrypt(token: str) -> str:
    f = _fernet()
    if not f:
        raise HTTPException(400, "ACCOUNTS_KEY is not set on the server")
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken:
        raise HTTPException(409, "This password was saved with a different ACCOUNTS_KEY and can't be read")


def _buffer_channels() -> list[dict]:
    """Channels Buffer listed (cached, no API call) plus any channel that has posts, so old ones stay linkable."""
    seen: dict[str, dict] = {}
    cached = (db.settings().get("buffer_channels") or {}).get("value") or []
    for c in cached:
        seen[c["id"]] = {"id": c["id"], "name": c.get("displayName") or c.get("name"), "service": c.get("service"),
                         "connected": True}
    for r in db.q("""SELECT channel_id, max(channel_name) AS name, max(service) AS service FROM posts
                     WHERE channel_id IS NOT NULL GROUP BY channel_id"""):
        seen.setdefault(r["channel_id"], {"id": r["channel_id"], "name": r["name"], "service": r["service"], "connected": False})
    return sorted(seen.values(), key=lambda c: (c["service"] or "", c["name"] or ""))


def _pipeline_stats(channel_ids: list[str]) -> dict[str, dict]:
    if not channel_ids:
        return {}
    rows = db.q("""WITH p AS (
                     SELECT p.*, ca.cpm, ca.min_payout,
                       (SELECT m.views FROM metrics m WHERE m.post_id = p.id AND m.ts <= now() - interval '7 days'
                        ORDER BY m.ts DESC LIMIT 1) AS views_week_ago
                     FROM posts p LEFT JOIN campaigns ca ON ca.id = p.campaign_id WHERE p.channel_id = ANY(%s))
                   SELECT channel_id,
                     count(*) FILTER (WHERE status = 'sent') AS posts,
                     count(*) FILTER (WHERE status = 'scheduled') AS scheduled,
                     count(*) FILTER (WHERE status = 'error') AS failed,
                     COALESCE(SUM(views) FILTER (WHERE status = 'sent'), 0) AS views,
                     COALESCE(SUM(likes) FILTER (WHERE status = 'sent'), 0) AS likes,
                     COALESCE(SUM(comments) FILTER (WHERE status = 'sent'), 0) AS comments,
                     COALESCE(MAX(views) FILTER (WHERE status = 'sent'), 0) AS best_views,
                     COALESCE(SUM(GREATEST(views - COALESCE(views_week_ago, 0), 0)) FILTER (WHERE status = 'sent'), 0) AS views_7d,
                     count(*) FILTER (WHERE status = 'sent' AND views / 1000.0 * COALESCE(cpm, 0) >= COALESCE(min_payout, 0)
                                      AND views > 0) AS paying,
                     MAX(sent_at) AS last_post_at, MAX(metrics_at) AS metrics_at
                   FROM p GROUP BY channel_id""", (channel_ids,))
    return {r["channel_id"]: r for r in rows}


@router.get("/accounts")
def list_accounts():
    geos = db.q("SELECT * FROM geos ORDER BY sort, id")
    accounts = db.q(f"SELECT {COLS} FROM accounts ORDER BY platform, lower(handle)")
    stats = _pipeline_stats([a["buffer_channel_id"] for a in accounts if a["buffer_channel_id"]])
    for a in accounts:
        a["pipeline"] = stats.get(a["buffer_channel_id"]) if a["buffer_channel_id"] else None
    streamers = sorted({s for r in db.q("SELECT channels FROM campaigns") for s in (r["channels"] or [])}
                       | {s for a in accounts for s in a["streamers"]}, key=str.lower)
    return {"geos": geos, "accounts": accounts, "buffer_channels": _buffer_channels(), "streamers": streamers,
            "encryption_ready": bool(config.ACCOUNTS_KEY)}


class GeoIn(BaseModel):
    code: str = Field(min_length=2, max_length=8)
    name: str = Field(min_length=1, max_length=60)
    sort: int | None = None


@router.post("/geos")
def create_geo(body: GeoIn):
    code = body.code.strip().upper()
    if db.one("SELECT 1 AS x FROM geos WHERE code=%s", (code,)):
        raise HTTPException(409, f"Geo {code} already exists")
    sort = body.sort if body.sort is not None else (db.one("SELECT COALESCE(MAX(sort),0)+1 AS n FROM geos")["n"])
    return db.one("INSERT INTO geos(code, name, sort) VALUES (%s,%s,%s) RETURNING *", (code, body.name.strip(), sort))


@router.put("/geos/{gid}")
def update_geo(gid: int, body: GeoIn):
    code = body.code.strip().upper()
    if db.one("SELECT 1 AS x FROM geos WHERE code=%s AND id<>%s", (code, gid)):
        raise HTTPException(409, f"Geo {code} already exists")
    r = db.one("UPDATE geos SET code=%s, name=%s, sort=COALESCE(%s, sort) WHERE id=%s RETURNING *",
               (code, body.name.strip(), body.sort, gid))
    if not r:
        raise HTTPException(404, "geo not found")
    return r


@router.delete("/geos/{gid}")
def delete_geo(gid: int):
    n = db.one("SELECT count(*) AS n FROM accounts WHERE geo_id=%s", (gid,))["n"]
    if n:
        raise HTTPException(409, f"Move or delete its {n} account(s) first")
    db.q("DELETE FROM geos WHERE id=%s", (gid,))
    return {"ok": True}


class AccountIn(BaseModel):
    geo_id: int
    platform: str
    handle: str = Field(min_length=1, max_length=120)
    login: str | None = None
    # None = keep the saved password, "" = remove it
    password: str | None = None
    profile_url: str | None = None
    status: str = "warming"
    streamers: list[str] = []
    buffer_channel_id: str | None = None
    followers: int | None = None
    total_views: int | None = None
    total_posts: int | None = None
    total_likes: int | None = None
    notes: str | None = None


def _clean(body: AccountIn) -> dict:
    if body.platform not in PLATFORMS:
        raise HTTPException(400, f"platform must be one of {', '.join(PLATFORMS)}")
    if body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    if not db.one("SELECT 1 AS x FROM geos WHERE id=%s", (body.geo_id,)):
        raise HTTPException(400, "unknown geo")
    blank = lambda v: (v.strip() or None) if isinstance(v, str) else v
    streamers = []
    for s in body.streamers:
        s = s.strip()
        if s and s.lower() not in {x.lower() for x in streamers}:
            streamers.append(s)
    return {"geo_id": body.geo_id, "platform": body.platform, "handle": body.handle.strip().lstrip("@"),
            "login": blank(body.login), "profile_url": blank(body.profile_url), "status": body.status,
            "streamers": streamers, "buffer_channel_id": blank(body.buffer_channel_id), "followers": body.followers,
            "total_views": body.total_views, "total_posts": body.total_posts, "total_likes": body.total_likes,
            "notes": blank(body.notes)}


def _stats_changed(old: dict | None, new: dict) -> bool:
    keys = ("followers", "total_views", "total_posts", "total_likes")
    return any((old or {}).get(k) != new[k] for k in keys) and any(new[k] is not None for k in keys)


@router.post("/accounts")
def create_account(body: AccountIn):
    data = _clean(body)
    if body.password:
        data["password_enc"] = encrypt(body.password)
    cols, vals = list(data), ["%s"] * len(data)
    if _stats_changed(None, data):
        cols.append("stats_at")
        vals.append("now()")
    r = db.one(f"INSERT INTO accounts({', '.join(cols)}) VALUES ({', '.join(vals)}) RETURNING id", list(data.values()))
    db.log("accounts", f"account added: {data['platform']} @{data['handle']}")
    return r


@router.put("/accounts/{aid}")
def update_account(aid: int, body: AccountIn):
    old = db.one("SELECT * FROM accounts WHERE id=%s", (aid,))
    if not old:
        raise HTTPException(404, "account not found")
    data = _clean(body)
    sets = [f"{k}=%s" for k in data]
    params = list(data.values())
    if body.password is not None:
        sets.append("password_enc=%s")
        params.append(encrypt(body.password) if body.password else None)
    if _stats_changed(old, data):
        sets.append("stats_at=now()")
    sets.append("updated_at=now()")
    db.q(f"UPDATE accounts SET {', '.join(sets)} WHERE id=%s", params + [aid])
    return {"ok": True}


@router.delete("/accounts/{aid}")
def delete_account(aid: int):
    r = db.one("DELETE FROM accounts WHERE id=%s RETURNING platform, handle", (aid,))
    if r:
        db.log("accounts", f"account deleted: {r['platform']} @{r['handle']}")
    return {"ok": True}


@router.post("/accounts/{aid}/reveal")
def reveal_password(aid: int):
    r = db.one("SELECT platform, handle, password_enc FROM accounts WHERE id=%s", (aid,))
    if not r:
        raise HTTPException(404, "account not found")
    if not r["password_enc"]:
        raise HTTPException(404, "no password saved for this account")
    pw = decrypt(r["password_enc"])
    db.log("accounts", f"password revealed: {r['platform']} @{r['handle']}")
    return {"password": pw}
