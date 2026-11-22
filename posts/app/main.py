"""posts.lane.ge API: clips, approvals, posts, analytics, campaigns, settings and worker control."""
import threading
import time
from collections import defaultdict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import accounts, assistant, assistant_live, auth, calendar, captions, config, db, manage, seed, submit_api
from .workers import render, runner

app = FastAPI(title="posts.lane.ge API")
app.add_middleware(CORSMiddleware, allow_origins=config.ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])

app.include_router(accounts.router, prefix="/api", dependencies=[Depends(auth.require)])
app.include_router(calendar.router, prefix="/api", dependencies=[Depends(auth.require)])
app.include_router(assistant.router, prefix="/api", dependencies=[Depends(auth.require)])
app.include_router(manage.router, prefix="/api", dependencies=[Depends(auth.require)])
app.include_router(assistant_live.router)  # the voice WebSocket checks its own one-use ticket
app.include_router(submit_api.router)  # each route sets its own auth (the Whop callback and helper queue differ)

_login_fail: dict[str, list[float]] = defaultdict(list)


@app.on_event("startup")
def startup():
    db.migrate()
    seed.run()
    seed.switch_campaign()
    seed.posting_plan()
    seed.drop_kreekcraft()
    seed.whole_frame_layout()
    seed.diego_campaign()
    seed.boeshi_campaign()
    seed.retry_oom_renders()
    seed.russian_campaigns()
    seed.bigballs_campaign()
    seed.bigballs_trends()
    seed.retry_trend_downloads()
    seed.retry_after_storage_move()
    # a changed BUFFER_API_KEY redeploys the service: test the (maybe new) key for stats on the first monitor run
    if db.settings().get("stats_scope_missing"):
        db.set_setting("stats_scope_missing", False)
        db.set_setting("stats_at", 0)
    # a restart (deploy, crash) mid-render leaves moments marked 'rendering': put them back in the queue
    stuck = db.q("UPDATE moments SET status='new', updated_at=now() WHERE status='rendering' RETURNING id")
    if stuck:
        db.log("api", f"{len(stuck)} interrupted render(s) put back in the queue", "warn")
    if config.RUN_WORKERS:
        runner.start()
        db.log("api", "workers started")


@app.get("/api/health")
def health():
    beats = db.q("SELECT worker, last_ok, last_error_at FROM heartbeats")
    return {"ok": True, "workers": beats}


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login):
    now = time.time()
    fails = [t for t in _login_fail["admin"] if now - t < 600]
    if len(fails) >= 10:
        raise HTTPException(429, "too many attempts, wait 10 minutes")
    if not auth.check_password(body.password):
        _login_fail["admin"] = fails + [now]
        raise HTTPException(401, "wrong password")
    return {"token": auth.issue()}


A = Depends(auth.require)

# Each post is its own submission: paid views/1000 * cpm, but $0 below the campaign's minimum and capped at its maximum.
PAY = """CASE WHEN GREATEST(p.views,0) / 1000.0 * ca.cpm < COALESCE(ca.min_payout, 0) THEN 0
              ELSE LEAST(GREATEST(p.views,0) / 1000.0 * ca.cpm, COALESCE(ca.max_payout, 1e12)) END"""
EST_SQL = f"""SELECT COALESCE(SUM({PAY}), 0) AS est, count(*) FILTER (WHERE ({PAY}) > 0) AS paying
              FROM posts p JOIN campaigns ca ON ca.id = p.campaign_id WHERE p.status='sent'"""


@app.get("/api/overview", dependencies=[A])
def overview():
    clips = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM clips GROUP BY status")}
    posts = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM posts GROUP BY status")}
    tot = db.one("""SELECT COALESCE(SUM(views),0) views, COALESCE(SUM(likes),0) likes, COALESCE(SUM(comments),0) comments,
                    COALESCE(SUM(shares),0) shares FROM posts WHERE status='sent'""")
    by_service = db.q("""SELECT service, count(*) FILTER (WHERE status='sent') sent, COALESCE(SUM(views),0) views
                         FROM posts GROUP BY service ORDER BY service""")
    upcoming = db.q("""SELECT p.id, p.service, p.channel_name, p.due_at, c.title, c.thumb_url FROM posts p
                       JOIN clips c ON c.id=p.clip_id WHERE p.status='scheduled' ORDER BY p.due_at LIMIT 8""")
    # live posts still waiting on the user (auto-submit's own queue is not counted)
    to_submit = db.one("""SELECT count(*) n FROM posts WHERE status='sent' AND external_link IS NOT NULL AND NOT submitted
                          AND COALESCE(submit_status, '') NOT IN ('pending', 'agent')""")["n"]
    submit_failed = db.one("""SELECT count(*) n FROM posts WHERE NOT submitted AND submit_status = 'failed'
                              AND sent_at > now() - interval '3 days'""")["n"]
    moments = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM moments GROUP BY status")}
    est = db.one(EST_SQL)
    return {"clips": clips, "posts": posts, "moments": moments, "totals": tot, "est_earnings": float(est["est"]),
            "paying_posts": est["paying"],
            "by_service": by_service, "upcoming": upcoming, "to_submit": to_submit, "submit_failed": submit_failed,
            "workers": db.q("SELECT * FROM heartbeats ORDER BY worker"), "settings": db.settings(),
            "campaigns": db.q("""SELECT id, name, url, cpm, min_payout, max_payout, active, ended_at, end_reason
                                 FROM campaigns ORDER BY active DESC, ended_at IS NOT NULL, id DESC""")}


@app.get("/api/clips", dependencies=[A])
def clips(status: str | None = None, limit: int = 60):
    where, params = ("WHERE cl.status = %s", [status]) if status else ("", [])
    rows = db.q(f"""SELECT cl.*, m.views AS twitch_views, m.url AS source_url, m.title AS source_title, ca.name AS campaign
                    FROM clips cl LEFT JOIN moments m ON m.id = cl.moment_id LEFT JOIN campaigns ca ON ca.id = cl.campaign_id
                    {where} ORDER BY cl.created_at DESC LIMIT %s""", params + [limit])
    ids = [r["id"] for r in rows]
    posts = db.q("SELECT * FROM posts WHERE clip_id = ANY(%s) ORDER BY service", (ids,)) if ids else []
    for r in rows:
        r["posts"] = [p for p in posts if p["clip_id"] == r["id"]]
    return rows


class ClipEdit(BaseModel):
    caption: str | None = None
    title: str | None = None


@app.patch("/api/clips/{clip_id}", dependencies=[A])
def edit_clip(clip_id: int, body: ClipEdit):
    if body.caption is not None:
        db.q("UPDATE clips SET caption=%s WHERE id=%s", (captions.clean(body.caption), clip_id))
    if body.title is not None:
        db.q("UPDATE clips SET title=%s WHERE id=%s", (body.title, clip_id))
    return {"ok": True}


@app.post("/api/clips/{clip_id}/approve", dependencies=[A])
def approve(clip_id: int):
    r = db.one("UPDATE clips SET status='approved', approved_at=now() WHERE id=%s AND status IN ('review','rejected') RETURNING id",
               (clip_id,))
    if not r:
        raise HTTPException(409, "clip is not waiting for review")
    runner.trigger("publish")
    return {"ok": True}


@app.post("/api/clips/{clip_id}/rerender", dependencies=[A])
def rerender_clip(clip_id: int):
    """Render the clip again with the current editing and swap its video in place (about 2-4 minutes)."""
    if not db.one("SELECT 1 AS x FROM clips WHERE id=%s", (clip_id,)):
        raise HTTPException(404, "no such clip")
    if clip_id in render.rerendering():
        raise HTTPException(409, "this clip is already being re-rendered")
    threading.Thread(target=lambda: _quiet(render.rerender, clip_id), daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/clips/rerendering", dependencies=[A])
def rerendering():
    return {"ids": render.rerendering()}


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:
        pass  # already logged to the event log


@app.post("/api/clips/{clip_id}/reject", dependencies=[A])
def reject(clip_id: int):
    db.q("UPDATE clips SET status='rejected' WHERE id=%s AND status IN ('review','approved')", (clip_id,))
    return {"ok": True}


@app.get("/api/posts", dependencies=[A])
def posts(limit: int = 100):
    return db.q(f"""SELECT p.*, c.title, c.thumb_url, ca.name AS campaign, ca.url AS campaign_url,
                    ca.min_payout, ca.cpm, ({PAY}) AS est_pay,
                    CEIL(COALESCE(ca.min_payout,0) / NULLIF(ca.cpm,0) * 1000) AS views_needed FROM posts p
                   JOIN clips c ON c.id = p.clip_id LEFT JOIN campaigns ca ON ca.id = p.campaign_id
                   ORDER BY p.due_at DESC LIMIT %s""", (limit,))


class Submitted(BaseModel):
    submitted: bool


@app.post("/api/posts/{post_id}/submitted", dependencies=[A])
def mark_submitted(post_id: int, body: Submitted):
    # ticked by hand: auto-submit leaves it alone; unticked: back to "not handled" (Submit now can retry it)
    db.q("""UPDATE posts SET submitted=%s, submit_status=%s, submitted_at=CASE WHEN %s THEN COALESCE(submitted_at, now()) END,
              submit_next_at=NULL WHERE id=%s""",
         (body.submitted, "manual" if body.submitted else None, body.submitted, post_id))
    return {"ok": True}


@app.get("/api/analytics", dependencies=[A])
def analytics(days: int = 30):
    # daily views per service: last snapshot of each post per day, summed
    daily = db.q("""WITH snap AS (
                      SELECT DISTINCT ON (m.post_id, date_trunc('day', m.ts)) m.post_id, date_trunc('day', m.ts) AS day, m.views
                      FROM metrics m WHERE m.ts > now() - make_interval(days => %s)
                      ORDER BY m.post_id, date_trunc('day', m.ts), m.ts DESC)
                    SELECT s.day, p.service, SUM(COALESCE(s.views,0)) AS views FROM snap s JOIN posts p ON p.id = s.post_id
                    GROUP BY s.day, p.service ORDER BY s.day""", (days,))
    top = db.q("""SELECT p.id, p.service, p.views, p.likes, p.comments, p.shares, p.external_link, p.sent_at, c.title, c.thumb_url
                  FROM posts p JOIN clips c ON c.id = p.clip_id WHERE p.status='sent' ORDER BY p.views DESC LIMIT 15""")
    per_campaign = db.q("""SELECT ca.name, ca.cpm, count(p.id) FILTER (WHERE p.status='sent') AS posts,
                           COALESCE(SUM(p.views),0) AS views FROM campaigns ca LEFT JOIN posts p ON p.campaign_id = ca.id
                           GROUP BY ca.id ORDER BY ca.id""")
    funnel = db.one("""SELECT (SELECT count(*) FROM moments) moments, (SELECT count(*) FROM clips) clips,
                       (SELECT count(*) FROM clips WHERE status IN ('approved','scheduled','posted')) approved,
                       (SELECT count(*) FROM posts WHERE status='sent') posted""")
    return {"daily": daily, "top": top, "per_campaign": per_campaign, "funnel": funnel}


@app.get("/api/moments", dependencies=[A])
def moments(limit: int = 80):
    return db.q("SELECT * FROM moments ORDER BY discovered_at DESC LIMIT %s", (limit,))


@app.post("/api/moments/{moment_id}/retry", dependencies=[A])
def retry_moment(moment_id: int):
    db.q("UPDATE moments SET status='new', reason=NULL WHERE id=%s", (moment_id,))
    runner.trigger("render")
    return {"ok": True}


CAMPAIGN_FIELDS = ["name", "url", "cpm", "min_payout", "max_payout", "channels", "game", "caption_template", "overlay_url",
                   "overlay_width", "overlay_y", "captions", "cam_box", "title_secs", "daily_quota", "min_clip_views",
                   "lookback_hours", "rules", "active", "whop_bounty_id", "submit_url", "submit_window_minutes",
                   "streamer_name", "ended_at", "end_reason", "target_secs", "teaser", "punch_zoom", "layout",
                   "language", "geo", "title_template", "banner_url", "banner_width", "banner_key", "bottom_urls",
                   "bottom_kind", "trend_tags", "trend_min_views"]


@app.get("/api/campaigns", dependencies=[A])
def campaigns():
    return db.q("SELECT * FROM campaigns ORDER BY active DESC, ended_at IS NOT NULL, id DESC")


@app.post("/api/campaigns", dependencies=[A])
def create_campaign(body: dict):
    data = {k: body[k] for k in CAMPAIGN_FIELDS if k in body}
    if not data.get("name"):
        raise HTTPException(400, "name is required")
    cols = ", ".join(data)
    row = db.one(f"INSERT INTO campaigns({cols}) VALUES ({', '.join(['%s'] * len(data))}) RETURNING id", list(data.values()))
    return row


@app.put("/api/campaigns/{cid}", dependencies=[A])
def update_campaign(cid: int, body: dict):
    data = {k: body[k] for k in CAMPAIGN_FIELDS if k in body}
    if data:
        db.q(f"UPDATE campaigns SET {', '.join(f'{k}=%s' for k in data)} WHERE id=%s", list(data.values()) + [cid])
    return {"ok": True}


@app.get("/api/campaigns/{cid}/trends", dependencies=[A])
def campaign_trends(cid: int):
    """The Shorts/Reels the trends worker checked for this campaign, newest first, with its verdict."""
    return db.q("""SELECT t.*, m.status AS moment_status, (SELECT id FROM clips WHERE moment_id=t.moment_id LIMIT 1) AS clip_id
                   FROM trend_videos t LEFT JOIN moments m ON m.id = t.moment_id
                   WHERE t.campaign_id=%s ORDER BY t.checked_at DESC LIMIT 60""", (cid,))


VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


@app.put("/api/campaigns/{cid}/upload", dependencies=[A])
async def upload_campaign_video(cid: int, request: Request, kind: str = "banner", name: str = "video.mp4"):
    """The dashboard sends a banner (kind=banner, replaces it) or a bottom video for the split layout (kind=bottom,
    added to the list) as the raw request body. It goes to Blob, and its public URL onto the campaign."""
    import re
    import tempfile
    from pathlib import Path
    from .integrations import blob
    if kind not in ("banner", "bottom"):
        raise HTTPException(400, "kind must be banner or bottom")
    if not db.one("SELECT 1 AS x FROM campaigns WHERE id=%s", (cid,)):
        raise HTTPException(404, "campaign not found")
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_TYPES:
        raise HTTPException(400, "send an .mp4, .mov or .webm file")
    data = await request.body()
    if not data or len(data) > 200 * 1024 * 1024:
        raise HTTPException(400, "the file is empty or over 200 MB")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(data)
    try:
        safe = re.sub(r"[^a-z0-9.-]+", "-", Path(name).stem.lower())[:40] or kind
        url = blob.upload(Path(f.name), f"{kind}s/c{cid}-{safe}{ext}", VIDEO_TYPES[ext])
    finally:
        Path(f.name).unlink(missing_ok=True)
    if kind == "banner":
        db.q("UPDATE campaigns SET banner_url=%s WHERE id=%s", (url, cid))
    else:
        db.q("UPDATE campaigns SET bottom_urls = array_append(bottom_urls, %s) WHERE id=%s", (url, cid))
    db.log("campaigns", f"campaign #{cid}: {kind} video uploaded ({len(data) // 1024 // 1024} MB)")
    return {"url": url}


@app.get("/api/settings", dependencies=[A])
def get_settings():
    return db.settings()


@app.put("/api/settings", dependencies=[A])
def put_settings(body: dict):
    for k, v in body.items():
        if k in db.DEFAULT_SETTINGS and k not in ("auto_submit", "submit_methods"):  # those: PUT /api/submit/settings
            db.set_setting(k, v)
    return db.settings()


@app.get("/api/events", dependencies=[A])
def events(limit: int = 150, level: str | None = None):
    if level:
        return db.q("SELECT * FROM events WHERE level=%s ORDER BY id DESC LIMIT %s", (level, limit))
    return db.q("SELECT * FROM events ORDER BY id DESC LIMIT %s", (limit,))


@app.post("/api/notify/test", dependencies=[A])
def notify_test():
    from . import notify
    if not notify.enabled():
        raise HTTPException(400, "email is not configured (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL)")
    try:
        notify.send("posts.lane.ge: email alerts are on",
                    "You'll get an email whenever a clip goes live, with the links to submit on Content Rewards, "
                    "and whenever a post fails.")
    except Exception as e:
        raise HTTPException(502, f"sending failed: {e}")
    return {"ok": True}


@app.get("/api/debug/ytdlp", dependencies=[A])
def debug_ytdlp(url: str, clients: str = ""):
    """yt-dlp -v -F for a Short/Reel URL, as the render worker would fetch it: why a download fails."""
    import re as _re
    import subprocess
    import tempfile
    from pathlib import Path
    if not _re.fullmatch(r"https://(www\.)?(youtube\.com/(shorts/|watch\?v=)|youtu\.be/|instagram\.com/)[\w\-/?=&.]+", url):
        raise HTTPException(400, "not a YouTube or Instagram URL")
    if clients and not _re.fullmatch(r"[a-z_,]+", clients):
        raise HTTPException(400, "clients: comma-separated yt-dlp player client names")
    with tempfile.TemporaryDirectory() as d:
        cmd = render.ytdlp_trend_cmd(Path(d), "-v", "-F", url, clients=clients)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
            code, out = r.returncode, (r.stderr or "") + "\n" + (r.stdout or "")
        except subprocess.TimeoutExpired:
            code, out = -1, "timed out"
    lines = [ln for ln in out.splitlines() if "cookie" not in ln.lower() or "[debug]" not in ln]
    return {"code": code, "out": "\n".join(lines)[-8000:]}


@app.post("/api/workers/{name}/run", dependencies=[A])
def run_worker(name: str):
    if not runner.trigger(name):
        raise HTTPException(404, "unknown worker")
    return {"ok": True}


@app.api_route("/media/{key:path}", methods=["GET", "HEAD"])
def media(key: str):
    """Our clips, thumbnails, banners and overlays in the Railway bucket (private): a redirect to a signed link.
    Everything stored there was public on Vercel Blob before, so this needs no login (Buffer and the dashboard fetch it)."""
    from fastapi.responses import RedirectResponse
    from .integrations import blob
    if not blob.bucket_on() or not key or ".." in key.split("/"):
        raise HTTPException(404, "not found")
    return RedirectResponse(blob.signed(key), status_code=302, headers={"Cache-Control": "public, max-age=3600"})
