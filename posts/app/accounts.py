"""Social accounts by geo and platform: login, encrypted password, target streamers and stats.

Passwords are encrypted with Fernet using a key derived from the ACCOUNTS_KEY variable; the API never returns them
except from the explicit reveal call. An account linked to a Buffer channel gets its stats from the posts table;
other accounts keep stats typed in by hand."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import config, db, notify
from .workers import manual

router = APIRouter()

PLATFORMS = ("instagram", "tiktok", "youtube")
STATUSES = ("warming", "active", "paused", "banned", "retired")
COLS = ("id, geo_id, platform, handle, login, profile_url, status, streamers, buffer_channel_id, followers, total_views, "
        "total_posts, total_likes, stats_at, notes, created_at, updated_at, upload_mode, manual_since, week_hint_at, "
        "banners, password_enc IS NOT NULL AS has_password")
MODES = ("manual", "automatic")


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
    # an account's posts: its Buffer channel's, or the uploads recorded by hand from its reminder emails
    key = lambda a: a["buffer_channel_id"] or f"manual:{a['id']}"
    stats = _pipeline_stats([key(a) for a in accounts])
    s = db.settings()
    uploads = _manual_uploads([a["id"] for a in accounts])
    for a in accounts:
        a["pipeline"] = stats.get(key(a))
        a["manual"] = None
        if a["upload_mode"] == "manual":
            a["manual"] = {"day": notify.manual_day(a), "uploads": uploads.get(a["id"], []),
                           "next_at": manual.next_slot(a, s) if a["status"] in manual.LIVE_STATUSES else None,
                           "slot_tz": manual.slot_plan(s, manual.geo_of(a))[0].key}
    streamers = sorted({s for r in db.q("SELECT channels FROM campaigns") for s in (r["channels"] or [])}
                       | {s for a in accounts for s in a["streamers"]}, key=str.lower)
    return {"geos": geos, "accounts": accounts, "buffer_channels": _buffer_channels(), "streamers": streamers,
            "encryption_ready": bool(config.ACCOUNTS_KEY), "email_ready": notify.enabled(),
            "services": s.get("services") or []}


def _manual_uploads(ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    rows = db.q("""SELECT * FROM (
                     SELECT mu.id, mu.account_id, mu.clip_id, mu.slot_at, mu.emailed_at, mu.done_at, mu.link,
                       c.title, c.thumb_url, c.video_url,
                       row_number() OVER (PARTITION BY mu.account_id ORDER BY mu.slot_at DESC) AS n
                     FROM manual_uploads mu JOIN clips c ON c.id = mu.clip_id WHERE mu.account_id = ANY(%s)) x
                   WHERE n <= 6 ORDER BY slot_at DESC""", (ids,))
    out: dict[int, list[dict]] = {}
    for r in rows:
        r.pop("n", None)
        out.setdefault(r["account_id"], []).append(r)
    return out


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
    # None = keep (new accounts start manual)
    upload_mode: str | None = None
    # takes clips of sponsor banner campaigns (BigBalls); off by default so a banner never lands on an account by accident
    banners: bool = False


def _clean(body: AccountIn) -> dict:
    if body.platform not in PLATFORMS:
        raise HTTPException(400, f"platform must be one of {', '.join(PLATFORMS)}")
    if body.upload_mode is not None and body.upload_mode not in MODES:
        raise HTTPException(400, "upload_mode must be manual or automatic")
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
            "notes": blank(body.notes), "banners": bool(body.banners)}


def _stats_changed(old: dict | None, new: dict) -> bool:
    keys = ("followers", "total_views", "total_posts", "total_likes")
    return any((old or {}).get(k) != new[k] for k in keys) and any(new[k] is not None for k in keys)


@router.post("/accounts")
def create_account(body: AccountIn):
    data = _clean(body)
    data["upload_mode"] = body.upload_mode or "manual"
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
    if body.upload_mode and body.upload_mode != old["upload_mode"]:
        sets.append("upload_mode=%s")
        params.append(body.upload_mode)
        if body.upload_mode == "manual":
            sets += ["manual_since=now()", "week_hint_at=NULL"]
    sets.append("updated_at=now()")
    db.q(f"UPDATE accounts SET {', '.join(sets)} WHERE id=%s", params + [aid])
    if body.upload_mode and body.upload_mode != old["upload_mode"]:
        _mode_changed({**old, **data}, body.upload_mode)
    return {"ok": True}


def _mode_changed(a: dict, mode: str):
    note = ""
    if mode == "automatic" and a.get("buffer_channel_id"):
        # switching an account to automatic means "post to it": make sure its platform is on in the publish settings
        services = db.settings().get("services") or []
        if a["platform"] not in services:
            db.set_setting("services", services + [a["platform"]])
            note = f"; {a['platform']} turned on for publishing"
    db.log("accounts", f"{a['platform']} @{a['handle']} switched to {mode} uploads{note}")


class ModeIn(BaseModel):
    upload_mode: str


@router.post("/accounts/{aid}/mode")
def set_mode(aid: int, body: ModeIn):
    if body.upload_mode not in MODES:
        raise HTTPException(400, "upload_mode must be manual or automatic")
    old = db.one("SELECT * FROM accounts WHERE id=%s", (aid,))
    if not old:
        raise HTTPException(404, "account not found")
    if old["upload_mode"] == body.upload_mode:
        return {"ok": True}
    if body.upload_mode == "manual":
        db.q("UPDATE accounts SET upload_mode='manual', manual_since=now(), week_hint_at=NULL, updated_at=now() WHERE id=%s", (aid,))
    else:
        db.q("UPDATE accounts SET upload_mode='automatic', updated_at=now() WHERE id=%s", (aid,))
    _mode_changed(old, body.upload_mode)
    return {"ok": True}


@router.post("/accounts/{aid}/upload-now")
def upload_now(aid: int):
    """Email this manual account its next clip right away (outside the slots)."""
    from datetime import datetime, timezone
    a = db.one("SELECT * FROM accounts WHERE id=%s", (aid,))
    if not a:
        raise HTTPException(404, "account not found")
    if not notify.enabled():
        raise HTTPException(400, "email is not configured on the server")
    try:
        row = manual.send_one(a, datetime.now(timezone.utc).replace(microsecond=0))
    except Exception as e:
        raise HTTPException(502, f"sending failed: {e}")
    if not row:
        raise HTTPException(409, "No new clip for this account: every recent clip was already sent to it")
    return row


class UploadDone(BaseModel):
    link: str | None = None


@router.post("/manual-uploads/{uid}/done")
def upload_done(uid: int, body: UploadDone):
    """Mark a reminder as uploaded. With a link, the upload is also recorded as a live post (Posts page, stats,
    Content Rewards 'Submitted' tick), and is never posted to the same account by Buffer later."""
    mu = db.one("""SELECT mu.*, a.platform, a.handle, a.buffer_channel_id, c.campaign_id FROM manual_uploads mu
                   JOIN accounts a ON a.id = mu.account_id JOIN clips c ON c.id = mu.clip_id WHERE mu.id=%s""", (uid,))
    if not mu:
        raise HTTPException(404, "reminder not found")
    link = (body.link or "").strip() or None
    if link and not link.startswith(("http://", "https://")):
        raise HTTPException(400, "the link must start with https://")
    db.q("UPDATE manual_uploads SET done_at=COALESCE(done_at, now()), link=%s WHERE id=%s", (link, uid))
    channel = mu["buffer_channel_id"] or f"manual:{mu['account_id']}"
    existing = db.one("SELECT id FROM posts WHERE clip_id=%s AND channel_id=%s AND buffer_post_id IS NULL",
                      (mu["clip_id"], channel))
    if link and existing:
        db.q("UPDATE posts SET external_link=%s, updated_at=now() WHERE id=%s", (link, existing["id"]))
    elif link:
        db.q("""INSERT INTO posts(clip_id, campaign_id, channel_id, channel_name, service, due_at, sent_at, status,
                                  external_link, notified_at)
                VALUES (%s,%s,%s,%s,%s,%s,now(),'sent',%s,now())""",
             (mu["clip_id"], mu["campaign_id"], channel, mu["handle"], mu["platform"], mu["slot_at"], link))
    db.log("accounts", f"manual upload of clip #{mu['clip_id']} on {mu['platform']} @{mu['handle']} marked done"
                       + (f": {link}" if link else ""))
    if link:
        from .workers import runner
        runner.trigger("submit")  # auto-submit picks the new link up right away
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
